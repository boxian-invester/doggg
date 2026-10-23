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

app = FastAPI(title='台股智研 API', version='4.0.0')
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
