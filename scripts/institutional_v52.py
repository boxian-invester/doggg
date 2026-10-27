"""v5.2 TWSE listed-stock institutional backfill. Official T86, safe date/field validation."""
import argparse
import json
import os
import re
import time
from datetime import date, timedelta
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import psycopg

URL = 'https://www.twse.com.tw/rwd/zh/fund/T86'

def number(s):
    return int(str(s).replace(',', '').replace(' ', '').strip())

def get_day(day):
    url = URL + '?' + urlencode({'response':'json','date':day.strftime('%Y%m%d'),'selectType':'ALLBUT0999'})
    req = Request(url, headers={'User-Agent':'Mozilla/5.0 (compatible; TaiwanStockResearch/1.0)', 'Accept':'application/json'})
    with urlopen(req, timeout=30) as resp:
        payload=json.load(resp)
    if payload.get('stat') != 'OK':
        return [], payload.get('stat','no data')
    fields=payload.get('fields') or []
    def col_exact(label):
        # TWSE provides a total dealer column as well as separate proprietary/hedging columns.
        # Substring matching would match all three and fail; use normalized exact labels.
        def normalize(value):
            return re.sub(r"\s+", "", str(value)).replace("（", "(").replace("）", ")")
        wanted=normalize(label)
        matches=[i for i,field in enumerate(fields) if normalize(field)==wanted]
        if len(matches)!=1:
            raise ValueError(f'Expected exactly one official column {label!r}; matches={matches}; fields={fields}')
        return matches[0]
    indices={
        'symbol':col_exact('證券代號'),
        'foreign':col_exact('外陸資買賣超股數(不含外資自營商)'),
        'trust':col_exact('投信買賣超股數'),
        'dealer':col_exact('自營商買賣超股數'),
    }
    results=[]
    for row in payload.get('data') or []:
        symbol=str(row[indices['symbol']]).strip()
        if not re.fullmatch(r'\d{4}',symbol): continue
        try:
            results.append((symbol,day,number(row[indices['foreign']]),number(row[indices['trust']]),number(row[indices['dealer']])))
        except (ValueError,IndexError): continue
    return results, 'OK'

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--days',type=int,default=35)
    p.add_argument('--dry-run',action='store_true')
    args=p.parse_args()
    if not 1<=args.days<=90: raise ValueError('days must be 1..90')
    if not os.getenv('DATABASE_URL'): raise RuntimeError('DATABASE_URL secret missing')
    with psycopg.connect(os.environ['DATABASE_URL']) as conn:
        cols={r[0] for r in conn.execute("select column_name from information_schema.columns where table_schema='public' and table_name='daily_bars'")}
        required={'symbol','trade_date','volume'}
        if not required.issubset(cols): raise RuntimeError(f'daily_bars missing columns: {required-cols}')
        existing={r[0] for r in conn.execute("select table_name from information_schema.tables where table_schema='public' and table_name='v5_institution_daily'")}
        if not existing: raise RuntimeError('Run sql/006_v51_scoring.sql first')
        start=date.today()-timedelta(days=args.days)
        total=0; valid_days=0
        for offset in range(args.days+1):
            day=start+timedelta(days=offset)
            if day.weekday()>=5: continue
            try: rows,status=get_day(day)
            except Exception as exc:
                print(f'{day}: fetch error {type(exc).__name__}: {exc}',flush=True)
                continue
            if not rows:
                print(f'{day}: no official records ({status})',flush=True)
                continue
            valid_days+=1
            # Daily volume is from the existing stock bar DB, not guessed from net buys.
            bars=conn.execute('SELECT symbol, volume FROM public.daily_bars WHERE trade_date=%s AND volume>0',(day,)).fetchall()
            vol={str(symbol):int(volume) for symbol,volume in bars}
            data=[(*r,vol[r[0]],'TWSE T86 official') for r in rows if r[0] in vol]
            print(f'{day}: T86 {len(rows)} rows, matching daily_bars {len(data)}; dry_run={args.dry_run}',flush=True)
            if not args.dry_run and data:
                with conn.transaction():
                    with conn.cursor() as cur:
                        cur.executemany('''INSERT INTO public.v5_institution_daily
                          (symbol,trade_date,foreign_net_shares,trust_net_shares,dealer_net_shares,volume,source)
                          VALUES (%s,%s,%s,%s,%s,%s,%s)
                          ON CONFLICT (symbol,trade_date) DO UPDATE SET
                            foreign_net_shares=EXCLUDED.foreign_net_shares,
                            trust_net_shares=EXCLUDED.trust_net_shares,
                            dealer_net_shares=EXCLUDED.dealer_net_shares,
                            volume=EXCLUDED.volume,source=EXCLUDED.source,imported_at=now()''',data)
            total+=len(data)
            time.sleep(0.3)
        print(f'Finished: official trading days={valid_days}, matched records={total}, dry_run={args.dry_run}')
        if not valid_days or not total: raise RuntimeError('No matching records. Check API and daily_bars freshness.')

if __name__=='__main__': main()
