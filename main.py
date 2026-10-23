"""Taiwan Stock Intelligence: research API, not a broker or real-time feed."""
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Optional
import math
import re
import time
import numpy as np
import pandas as pd
import yfinance as yf
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from backtest import STRATEGIES, simulate

app = FastAPI(title='台股智研 API', version='5.0.0')
BASE = Path(__file__).resolve().parent
app.mount('/static', StaticFiles(directory=BASE/'static'), name='static')
CACHE = {}
TTL = 900

US = {'S&P 500':'^GSPC', 'NASDAQ':'^IXIC', '費城半導體':'^SOX', '道瓊':'^DJI', '台積電 ADR':'TSM', 'VIX':'^VIX'}
WATCH = {'2330':'台積電','2317':'鴻海','2454':'聯發科','8299':'群聯','8046':'南電','2344':'華邦電','2408':'南亞科','2484':'希華','3037':'欣興','2303':'聯電'}

def stock_ticker(symbol):
    if not re.fullmatch(r'\d{4,6}',symbol):
        raise HTTPException(400, '請輸入 4–6 位台股代號')
    return symbol + '.TW'

def history(ticker, period='1y'):
    key = (ticker,period)
    if key in CACHE and time.time()-CACHE[key][0]<TTL:
        return CACHE[key][1].copy()
    try:
        df = yf.Ticker(ticker).history(period=period, interval='1d', auto_adjust=True)
    except Exception as exc:
        raise HTTPException(503, f'行情供應商暫時無法取得資料：{str(exc)[:120]}')
    if df.empty:
        raise HTTPException(404, f'{ticker} 無可用日線資料；上櫃股票可能需改用 .TWO')
    df = df[['Open','High','Low','Close','Volume']].dropna().copy()
    df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
    CACHE[key]=(time.time(),df.copy())
    return df

def fetch_tw(symbol,period='1y'):
    try:
        return history(symbol+'.TW',period),symbol+'.TW'
    except HTTPException as err:
        try:
            return history(symbol+'.TWO',period),symbol+'.TWO'
        except HTTPException:
            raise err

def num(x, digits=2):
    return round(float(x),digits) if pd.notna(x) and math.isfinite(float(x)) else None

def enrich(df):
    d=df.copy()
    c=d.Close
    for n in (5,10,20,60,120): d[f'MA{n}']=c.rolling(n).mean()
    ema12=c.ewm(span=12,adjust=False).mean(); ema26=c.ewm(span=26,adjust=False).mean()
    d['DIF']=ema12-ema26
    d['DEA']=d.DIF.ewm(span=9,adjust=False).mean()
    d['HIST']=d.DIF-d.DEA
    delta=c.diff(); gain=delta.clip(lower=0).ewm(alpha=1/14,adjust=False).mean(); loss=(-delta.clip(upper=0)).ewm(alpha=1/14,adjust=False).mean()
    d['RSI']=100-100/(1+gain/loss.replace(0,np.nan))
    low=d.Low.rolling(9).min(); high=d.High.rolling(9).max()
    rsv=(100*(c-low)/(high-low).replace(0,np.nan)).fillna(50)
    d['K']=rsv.ewm(alpha=1/3,adjust=False).mean();d['D']=d.K.ewm(alpha=1/3,adjust=False).mean()
    d['VMA20']=d.Volume.rolling(20).mean()
    d['PREV20HIGH']=d.High.shift(1).rolling(20).max()
    return d

def summary(df):
    d=enrich(df); last=d.iloc[-1]; prev=d.iloc[-2] if len(d)>1 else last
    change=(last.Close/prev.Close-1)*100 if prev.Close else 0
    volume_ratio=last.Volume/last.VMA20 if pd.notna(last.VMA20) and last.VMA20 else None
    breakout=bool(pd.notna(last.PREV20HIGH) and last.Close>last.PREV20HIGH and volume_ratio is not None and volume_ratio>=1.5)
    return {'date':str(d.index[-1].date()),'close':num(last.Close),'change_pct':num(change),
      'volume':int(last.Volume),'volume_ratio':num(volume_ratio),'ma5':num(last.MA5),'ma20':num(last.MA20),'ma60':num(last.MA60),
      'kd_k':num(last.K),'kd_d':num(last.D),'rsi14':num(last.RSI),'macd_dif':num(last.DIF,4),'macd_signal':num(last.DEA,4),
      'macd_hist':num(last.HIST,4),'breakout_20d_volume_confirmed':breakout,
      'support_20d':num(d.Low.tail(20).min()),'resistance_20d':num(d.High.tail(20).max()),
      'signal':'量價突破（歷史日線）' if breakout else '未達量價突破條件'}

@app.get('/')
def index():return FileResponse(BASE/'static'/'index.html')

@app.get('/api/health')
def health():return {'status':'ok','utc':datetime.now(timezone.utc).isoformat(),'data_type':'delayed_daily_external'}

@app.get('/api/stock/{symbol}')
def stock(symbol:str,period:str=Query('1y',pattern='^(6mo|1y|2y|5y)$')):
    stock_ticker(symbol)
    df,ticker=fetch_tw(symbol,period)
    d=enrich(df)
    rows=[]
    for dt,r in d.tail(130).iterrows():
        rows.append({'date':str(dt.date()),'open':num(r.Open),'high':num(r.High),'low':num(r.Low),'close':num(r.Close),
                     'volume':int(r.Volume),'ma20':num(r.MA20),'ma60':num(r.MA60),'k':num(r.K),'d':num(r.D),'rsi':num(r.RSI),'macd':num(r.HIST,4)})
    return {'symbol':symbol,'name':WATCH.get(symbol,symbol),'provider_ticker':ticker,'adjusted_prices':True,
            'quote_status':'歷史日線／非即時；供應商可能延遲','as_of':str(df.index[-1].date()),'metrics':summary(df),'candles':rows}

@app.get('/api/us-market')
def us_market():
    out=[]
    for name,ticker in US.items():
        try:
            df=history(ticker,'1mo');out.append({'name':name,'ticker':ticker,**summary(df)})
        except Exception as e:
            out.append({'name':name,'ticker':ticker,'error':str(e)[:150]})
    return {'market':'US','quote_status':'歷史日線／非即時','items':out,'taiwan_probability':None,
            'note':'尚未建立經樣本外驗證的台股隔日方向模型，不顯示虛構機率。'}

@app.get('/api/watchlist')
def watchlist():
    out=[]
    for sym,name in WATCH.items():
        try:
            df,ticker=fetch_tw(sym,'6mo');out.append({'symbol':sym,'name':name,'ticker':ticker,**summary(df)})
        except Exception as e:out.append({'symbol':sym,'name':name,'error':str(e)[:120]})
    ranked=sorted([x for x in out if 'close' in x],key=lambda x:(x['breakout_20d_volume_confirmed'],x['volume_ratio'] or 0),reverse=True)
    return {'items':ranked,'errors':[x for x in out if 'error' in x],
            'universe':'固定觀察清單（10 檔），不是全市場掃描',
            'note':'僅歷史日線訊號；非盤前勝率、非當沖進場建議。'}


@app.get('/api/backtest/strategies')
def strategies():
    return {'strategies':STRATEGIES,'note':'收盤產生訊號、下一交易日開盤成交；不保證未來績效。'}

@app.get('/api/backtest/{symbol}')
def backtest(symbol:str, strategy:str=Query('ma_cross'), period:str=Query('5y',pattern='^(2y|5y)$'),
             fee_bps:float=Query(14.25,ge=0,le=100),tax_bps:float=Query(30,ge=0,le=100),
             slippage_bps:float=Query(5,ge=0,le=100)):
    stock_ticker(symbol)
    if strategy not in STRATEGIES: raise HTTPException(400,'策略不存在')
    df,ticker=fetch_tw(symbol,period)
    if len(df)<220: raise HTTPException(422,'有效歷史資料不足 220 根，無法回測')
    enriched=enrich(df)
    # Split chronologically, no randomized split. Indicators use earlier history as warmup.
    cut=int(len(enriched)*0.7)
    train=enriched.iloc[:cut].copy()
    test=enriched.iloc[cut:].copy()
    # Provide earlier history to compute prior-bar crossover on test's first day;
    # simulation starts after split and never executes on split boundary based on training signals.
    try:
        train_result=simulate(train,strategy,fee_bps,tax_bps,slippage_bps)
        test_result=simulate(test,strategy,fee_bps,tax_bps,slippage_bps)
    except ValueError as e: raise HTTPException(422,str(e))
    return {'symbol':symbol,'ticker':ticker,'strategy':strategy,'strategy_label':STRATEGIES[strategy],
      'quote_status':'歷史日線，非即時；auto_adjust=True 調整後價格',
      'execution':'當日收盤確認訊號，下一交易日開盤執行；多頭全倉、整股、不加槓桿',
      'costs':{'fee_bps_each_side':fee_bps,'tax_bps_sell':tax_bps,'slippage_bps_each_side':slippage_bps},
      'train':train_result,'out_of_sample':test_result,
      'limitations':['訓練/測試為時間先後 70/30；沒有跨股票參數搜尋或最佳化','測試區間單獨初始化資金，未承接訓練區間持倉','未模擬漲跌停無法成交、流動性限制、除權息稅務細節','調整後價格不適合直接作為委託價格','勝率為歷史已平倉交易勝率，非隔日上漲機率']}

@app.get('/api/opportunities')
def opportunities():
    items=[]; errors=[]
    for sym,name in WATCH.items():
        try:
            df,ticker=fetch_tw(sym,'1y'); m=summary(df)
            score=0; reasons=[]
            if m['breakout_20d_volume_confirmed']:score+=40;reasons.append('20 日高點放量突破')
            if m['ma20'] is not None and m['close']>m['ma20']:score+=20;reasons.append('收盤站上 MA20')
            if m['ma60'] is not None and m['close']>m['ma60']:score+=15;reasons.append('收盤站上 MA60')
            if m['kd_k'] is not None and m['kd_d'] is not None and m['kd_k']>m['kd_d']:score+=10;reasons.append('KD K 高於 D')
            if m['macd_hist'] is not None and m['macd_hist']>0:score+=10;reasons.append('MACD 柱體為正')
            if m['volume_ratio'] is not None and m['volume_ratio']>=1.5:score+=5;reasons.append('量比達 1.5')
            items.append({'symbol':sym,'name':name,'ticker':ticker,'score':score,'reasons':reasons,**m})
        except Exception as e:errors.append({'symbol':sym,'error':str(e)[:100]})
    return {'items':sorted(items,key=lambda x:x['score'],reverse=True)[:5], 'errors':errors,
      'universe':'固定 10 檔觀察清單；非全台股掃描',
      'score_definition':'規則符合度 0–100 分，不是勝率或上漲機率',
      'note':'日線資料，非當沖訊號；不提供無依據的進場/停損/目標價。'}


# v5: bounded watch-universe research. Daily closes are not intraday quotes.
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta

SCAN_CACHE = {'time':0, 'data':None}
SCAN_UNIVERSE = {**WATCH, '2603':'長榮', '2881':'富邦金', '2882':'國泰金', '3711':'日月光投控', '2308':'台達電', '2382':'廣達', '2357':'華碩', '2379':'瑞昱', '3231':'緯創', '6669':'緯穎'}

def scan_one(pair):
    symbol, name = pair
    df, ticker = fetch_tw(symbol, '6mo')
    if len(df)<65: raise ValueError('有效日線不足 65 根')
    m=summary(df)
    score=0; reasons=[]
    for condition, weight, label in [
        (m['breakout_20d_volume_confirmed'], 35, '收盤突破前20日高點且量比≥1.5'),
        (m['ma20'] is not None and m['close']>m['ma20'], 20, '收盤高於MA20'),
        (m['ma60'] is not None and m['close']>m['ma60'], 15, '收盤高於MA60'),
        (m['kd_k'] is not None and m['kd_d'] is not None and m['kd_k']>m['kd_d'], 10, 'K高於D'),
        (m['macd_hist'] is not None and m['macd_hist']>0, 10, 'MACD柱體為正'),
        (m['volume_ratio'] is not None and m['volume_ratio']>=1.5, 10, '量比≥1.5')]:
        if condition: score+=weight; reasons.append(label)
    # Adjusted OHLC data must not be presented as executable order prices.
    return {'symbol':symbol,'name':name,'ticker':ticker,'score':score,'reasons':reasons,**m,
            'entry_price':None,'stop_price':None,'target_price':None,'win_probability':None}

@app.get('/api/v5/scan')
def v5_scan(limit:int=Query(5,ge=1,le=20)):
    if SCAN_CACHE['data'] is not None and time.time()-SCAN_CACHE['time']<900:
        cached=SCAN_CACHE['data'].copy()
        cached['items']=cached['items'][:limit]
        return cached
    items=[]; errors=[]
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs={pool.submit(scan_one,pair):pair[0] for pair in SCAN_UNIVERSE.items()}
        for future in as_completed(jobs):
            try: items.append(future.result())
            except Exception as e: errors.append({'symbol':jobs[future],'error':str(e)[:160]})
    items.sort(key=lambda x:(-x['score'],-(x['volume_ratio'] or 0),x['symbol']))
    result={'items':items, 'scanned':len(SCAN_UNIVERSE), 'succeeded':len(items),
            'errors':errors,'universe':'固定20檔研究清單，並非台灣全市場掃描',
            'score_definition':'規則符合度（0–100分），不是預測勝率',
            'quote_status':'調整後歷史日線；非盤中即時；不能直接當委託價格',
            'generated_at_taipei':datetime.now(ZoneInfo('Asia/Taipei')).isoformat(),
            'limitations':['僅20檔固定清單','非即時當沖篩選','尚無樣本外隔日漲跌機率模型','部分行情可能延遲或缺漏']}
    SCAN_CACHE.update(time=time.time(),data=result)
    return {**result,'items':items[:limit]}

@app.get('/api/v5/premarket')
def v5_premarket():
    now=datetime.now(ZoneInfo('Asia/Taipei'))
    us=[]; failures=[]
    for name,ticker in US.items():
        try:
            df=history(ticker,'1mo')
            if len(df)<2: raise ValueError('歷史資料不足')
            m=summary(df)
            us.append({'name':name,'ticker':ticker,'date':m['date'],'close':m['close'],'change_pct':m['change_pct']})
        except Exception as e:failures.append({'name':name,'error':str(e)[:100]})
    lookup={x['ticker']:x for x in us}
    # This is a descriptive score only, not a probability forecast.
    inputs=[('^GSPC',1),('^IXIC',2),('^SOX',3),('TSM',2)]
    available=[(lookup[t]['change_pct'],weight) for t,weight in inputs if t in lookup and lookup[t]['change_pct'] is not None]
    weighted=sum(v*w for v,w in available)/sum(w for _,w in available) if available else None
    if weighted is None: sentiment='資料不足'
    elif weighted>0.7: sentiment='美股偏強（描述性）'
    elif weighted< -0.7: sentiment='美股偏弱（描述性）'
    else: sentiment='美股訊號混合／中性'
    dates=[x['date'] for x in us]
    return {'generated_at_taipei':now.isoformat(),'us_last_dates':sorted(set(dates)),
            'us':us,'errors':failures,'weighted_us_daily_change_pct':num(weighted),
            'market_read':sentiment,'taiwan_up_probability':None,'taiwan_down_probability':None,
            'intraday_win_probability':None,
            'note':'美股加權漲跌僅是方向性描述，未經台股隔日樣本外驗證；美股交易日與台股交易日不一定一致。',
            'schedule':'此端點在使用者查詢時即時計算；尚未設定08:45定時任務。'}

# v6.1: database-backed daily batch scanner. v5 endpoints are unchanged.
from db import configured as database_configured, connect as database_connect

@app.get('/api/v6/status')
def v6_status():
    if not database_configured():
        return {'configured':False,'status':'database_not_configured','message':'尚未設定 DATABASE_URL；v5 功能仍可使用'}
    try:
        with database_connect() as conn:
            universe=conn.execute('select count(*) as n from stock_universe where active=true').fetchone()['n']
            covered=conn.execute('select count(distinct symbol) as n from daily_bars').fetchone()['n']
            run=conn.execute('select id,started_at,finished_at,universe_count,eligible_count,failed_count,latest_trade_date,status,notes from scan_runs order by id desc limit 1').fetchone()
        def serialize(x):
            return {k:(v.isoformat() if hasattr(v,'isoformat') else v) for k,v in x.items()} if x else None
        return {'configured':True,'status':'ok','universe_count':universe,'symbols_with_history':covered,
                'coverage_pct':round(100*covered/universe,2) if universe else 0,'latest_run':serialize(run),
                'note':'資料覆蓋率不代表當日資料新鮮度；請同時確認 latest_trade_date 與 eligible_count。'}
    except Exception:
        raise HTTPException(503,'資料庫連線或資料表初始化失敗，請檢查 DATABASE_URL 和 SQL schema；詳細原因見伺服器 Logs')

@app.get('/api/v6/scan')
def v6_scan(limit:int=Query(5,ge=1,le=100)):
    if not database_configured():raise HTTPException(503,'尚未設定 DATABASE_URL')
    try:
        with database_connect() as conn:
            run=conn.execute('select * from scan_runs order by id desc limit 1').fetchone()
            if not run:raise HTTPException(404,'尚無掃描紀錄，請先執行 GitHub Actions')
            rows=conn.execute('''select r.symbol,u.name,u.market,r.trade_date,r.score,r.close,r.volume_ratio,r.reasons
                 from scan_results r join stock_universe u on u.symbol=r.symbol
                 where r.run_id=%s order by r.score desc,r.volume_ratio desc nulls last,r.symbol limit %s''',
                 (run['id'],limit)).fetchall()
        def ser(row):
            return {k:(v.isoformat() if hasattr(v,'isoformat') else float(v) if isinstance(v, __import__('decimal').Decimal) else v) for k,v in row.items()}
        return {'run':ser(run),'items':[ser(x) for x in rows],
                'universe':'官方上市櫃名單；實際評分僅涵蓋已下載且通過品質檢查的股票',
                'score_definition':'日線技術規則符合度，不是勝率或隔日漲跌機率',
                'entry_price':None,'stop_price':None,'intraday_win_probability':None,
                'warning':'調整後歷史價格不得直接作為委託價格；非盤中即時行情。'}
    except HTTPException:raise
    except Exception:
        raise HTTPException(503,'無法讀取掃描結果；請檢查資料庫連線與掃描紀錄')
