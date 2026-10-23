"""Incremental daily ingestion and DB-based ranking. Run with: python -m scripts.pipeline --mode all
No artificial win probabilities. Default backfill is deliberately bounded for free tiers.
"""
import argparse
import datetime as dt
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd
import requests
import yfinance as yf
from psycopg.types.json import Jsonb
from db import connect
from main import enrich, summary

LOG=logging.getLogger('pipeline')
SOURCES={
 'TWSE':'https://openapi.twse.com.tw/v1/opendata/t187ap03_L',
 'TPEx':'https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O',
}

def field(item,*keys):
    for k in keys:
        v=item.get(k)
        if v is not None and str(v).strip():return str(v).strip()
    return ''

def fetch_universe():
    result={}; errors=[]
    for market,url in SOURCES.items():
        try:
            response=requests.get(url,timeout=35,headers={'User-Agent':'TaiwanStockResearch/1.0'})
            response.raise_for_status(); data=response.json()
            if not isinstance(data,list):raise ValueError('Expected JSON array')
            for row in data:
                sym=field(row,'公司代號','股票代號','SecuritiesCompanyCode','Code')
                name=field(row,'公司簡稱','公司名稱','名稱','CompanyName','Name')
                if sym.isdigit() and 4<=len(sym)<=6 and name:
                    result[sym]=(sym,name,market)
            LOG.info('%s fetched %d entries',market,len(data))
        except Exception as exc:
            errors.append(f'{market}: {exc}')
    if not result:
        raise RuntimeError('無法取得任何官方上市櫃名單；不覆蓋現有資料。'+'; '.join(errors))
    with connect() as conn:
        with conn.cursor() as cur:
            cur.executemany('''insert into stock_universe(symbol,name,market) values(%s,%s,%s)
            on conflict(symbol) do update set name=excluded.name,market=excluded.market,active=true,updated_at=now()''',list(result.values()))
    LOG.info('Universe upserted %d; source errors: %s',len(result),errors)
    return len(result)

def load_candidates(limit,offset):
    with connect() as conn:
        return conn.execute('''select u.symbol,u.market,coalesce(max(b.trade_date),date '1900-01-01') last_date
          from stock_universe u left join daily_bars b on b.symbol=u.symbol
          where u.active=true group by u.symbol,u.market
          order by last_date asc,u.symbol asc limit %s offset %s''',(limit,offset)).fetchall()

def download_one(item,period):
    sym=item['symbol']; suffix='.TW' if item['market']=='TWSE' else '.TWO'
    data=yf.Ticker(sym+suffix).history(period=period,interval='1d',auto_adjust=True)
    if data.empty:raise ValueError('no bars')
    data=data[['Open','High','Low','Close','Volume']].dropna()
    rows=[]
    for day,row in data.iterrows():
        vals=[float(row[k]) for k in ['Open','High','Low','Close']]
        if min(vals)<=0 or max(vals)!=vals[1] or min(vals)!=vals[2]:continue
        rows.append((sym,str(day.date()),*vals,int(row['Volume']),'yfinance_adjusted',True))
    return sym,rows

def ingest(limit,offset,period,workers):
    candidates=load_candidates(limit,offset)
    if not candidates:LOG.warning('No candidates; run --mode universe first');return
    failures=[]; count=0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        jobs={pool.submit(download_one,item,period):item['symbol'] for item in candidates}
        for future in as_completed(jobs):
            sym=jobs[future]
            try:
                _,rows=future.result()
                if not rows:raise ValueError('no valid OHLCV')
                with connect() as conn:
                    with conn.cursor() as cur:
                        cur.executemany('''insert into daily_bars(symbol,trade_date,open,high,low,close,volume,source,adjusted)
                        values(%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        on conflict(symbol,trade_date) do update set open=excluded.open,high=excluded.high,
                        low=excluded.low,close=excluded.close,volume=excluded.volume,source=excluded.source,
                        adjusted=excluded.adjusted,loaded_at=now()''',rows)
                count+=1
            except Exception as exc:
                failures.append(f'{sym}: {str(exc)[:100]}')
    LOG.info('Ingest success=%d / %d; errors=%s',count,len(candidates),failures[:25])
    if count==0:raise RuntimeError('No symbols successfully ingested')

def scan():
    with connect() as conn:
        universe=conn.execute('select count(*) as n from stock_universe where active=true').fetchone()['n']
        bars=conn.execute('''select b.symbol,b.trade_date,b.open,b.high,b.low,b.close,b.volume
            from daily_bars b join stock_universe u on u.symbol=b.symbol and u.active=true
            where b.trade_date >= current_date - interval '500 days' order by b.symbol,b.trade_date''').fetchall()
    by={}
    for r in bars:by.setdefault(r['symbol'],[]).append(r)
    latest=max((r['trade_date'] for r in bars),default=None)
    results=[]; stale=0; insufficient=0
    for sym,records in by.items():
        if len(records)<70:insufficient+=1;continue
        if latest and (latest-records[-1]['trade_date']).days>4:stale+=1;continue
        df=pd.DataFrame(records).set_index('trade_date').rename(columns={
            'open':'Open','high':'High','low':'Low','close':'Close','volume':'Volume'})
        df.index=pd.to_datetime(df.index)
        m=summary(df)
        score=0;reasons=[]
        for ok,pts,label in [
            (m['breakout_20d_volume_confirmed'],35,'20日放量突破'),
            (m['ma20'] is not None and m['close']>m['ma20'],20,'高於MA20'),
            (m['ma60'] is not None and m['close']>m['ma60'],15,'高於MA60'),
            (m['kd_k'] is not None and m['kd_d'] is not None and m['kd_k']>m['kd_d'],10,'KD偏強'),
            (m['macd_hist'] is not None and m['macd_hist']>0,10,'MACD柱正'),
            (m['volume_ratio'] is not None and m['volume_ratio']>=1.5,10,'量比≥1.5')]:
            if ok:score+=pts;reasons.append(label)
        results.append((sym,m['date'],score,m['close'],m['volume_ratio'],Jsonb(reasons)))
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute('''insert into scan_runs(universe_count,eligible_count,failed_count,latest_trade_date,status,notes)
            values(%s,%s,%s,%s,%s,%s) returning id''',(
                universe,len(results),max(0,universe-len(results)),latest,
                'complete' if results else 'insufficient_data',
                f'缺歷史資料或不足70根；過期股票 {stale}，不足70根 {insufficient}。非當沖、非勝率。'))
            run_id=cur.fetchone()['id']
            if results:
                cur.executemany('''insert into scan_results(run_id,symbol,trade_date,score,close,volume_ratio,reasons)
                values(%s,%s,%s,%s,%s,%s,%s)''',[(run_id,*r) for r in results])
            cur.execute('update scan_runs set finished_at=now() where id=%s',(run_id,))
    LOG.info('Scan %s: %d/%d eligible; latest %s',run_id,len(results),universe,latest)

def main():
    p=argparse.ArgumentParser();p.add_argument('--mode',choices=['all','universe','ingest','scan'],default='all')
    p.add_argument('--limit',type=int,default=int(os.getenv('BATCH_SIZE','60')))
    p.add_argument('--offset',type=int,default=0)
    p.add_argument('--period',choices=['3mo','6mo','1y','2y'],default='1y')
    p.add_argument('--workers',type=int,default=2)
    args=p.parse_args();logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    if args.mode in ('all','universe'):fetch_universe()
    if args.mode in ('all','ingest'):ingest(args.limit,args.offset,args.period,max(1,min(args.workers,4)))
    if args.mode in ('all','scan'):scan()
if __name__=='__main__':main()
