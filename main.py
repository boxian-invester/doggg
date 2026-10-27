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

def volume_surge_score(df):
    '''Compare latest volume against preceding N completed sessions (exclude latest).'''
    v = df['Volume'].astype(float)
    ratios = {}
    points = []
    for days in (5, 10, 20, 60):
        baseline = v.shift(1).tail(days).mean() if len(v) >= days + 1 else float('nan')
        ratio = float(v.iloc[-1] / baseline) if pd.notna(baseline) and baseline > 0 else None
        ratios[str(days)] = num(ratio)
        points.append(0 if ratio is None or ratio < 2 else 2 if ratio < 3 else 3 if ratio < 5 else 4)
    raw = min(15, sum(points))
    last = df.iloc[-1]
    prev_close = float(df.Close.iloc[-2])
    # Volume with a bearish candle / sharp selloff is not automatically bullish.
    bearish = bool(last.Close < last.Open or (prev_close > 0 and last.Close / prev_close - 1 < -0.02))
    upper_shadow = float(last.High - max(last.Close, last.Open))
    candle_range = float(last.High - last.Low)
    long_upper = bool(candle_range > 0 and upper_shadow / candle_range >= 0.45)
    penalty = min(raw, 7) if (bearish or long_upper) and raw > 0 else 0
    return {'score': raw - penalty, 'ratios': ratios, 'raw_score': raw,
            'risk_penalty': penalty, 'risk_flag': '爆量賣壓／長上影線' if penalty else None}


def _score_institutions(rows, price_dates):
    """Score only 10 verified institutional sessions, aligned to latest price bar.

    Holiday bars from a price vendor must not be treated as trading days.
    A missing institutional day is NOT silently filled with zero.
    """
    by_date = {str(r['trade_date']): r for r in rows}
    price_day_set = {str(x.date()) for x in price_dates}
    latest_price_date = str(price_dates[-1].date())
    if latest_price_date not in by_date:
        return None  # No current institutional observation; never score stale data.
    wanted = sorted((d for d in by_date if d <= latest_price_date), reverse=True)[:10]
    if len(wanted) != 10 or any(d not in price_day_set for d in wanted):
        return None
    wanted.reverse()
    records = [by_date[d] for d in wanted]
    if any(r.get('volume') is None or int(r['volume']) <= 0 or
           any(r.get(k) is None for k in ('foreign_net_shares', 'trust_net_shares', 'dealer_net_shares'))
           for r in records):
        return None
    volumes = [int(r['volume']) for r in records]
    weights = {'foreign_net_shares': 0.4, 'trust_net_shares': 0.4, 'dealer_net_shares': 0.2}
    combined = [sum(float(r[k]) * w for k, w in weights.items()) / volumes[i]
                for i, r in enumerate(records)]
    overall = sum(combined) / 10
    recent = sum(combined[-5:]) / 5
    positive_days = sum(x > 0 for x in combined)
    magnitude = max(-10, min(10, overall / 0.015 * 10))
    momentum = max(-3, min(3, (recent - overall) / 0.015 * 3))
    persistence = (positive_days - 5) / 5 * 2
    score = int(round(max(-15, min(15, magnitude + momentum + persistence))))
    return {'score': score, 'net_ratio': num(overall * 100, 3),
            'positive_days': positive_days, 'latest_date': wanted[-1],
            'matched_trading_days': 10, 'date_alignment': 'institutional_sessions'}


def _score_chip(row):
    '''CMoney-sourced values only; never derive a fake CMoney concentration.'''
    if row is None or any(row.get(k) is None for k in ('main_net_shares', 'branch_count_diff', 'concentration_5d', 'concentration_20d')):
        return None
    net = float(row['main_net_shares'])
    count = int(row['branch_count_diff'])
    c5 = float(row['concentration_5d'])
    c20 = float(row['concentration_20d'])
    # Concentration units are percentage points (e.g. 3.5 means 3.5%).
    s_net = 5 if net > 0 else -5 if net < 0 else 0
    s_branch = (3 if count < 0 else -3 if count > 0 else 0) if net > 0 else (-3 if count < 0 else 0)
    def tier(value, bands):
        if value >= bands[2]: return 6
        if value >= bands[1]: return 4
        if value >= bands[0]: return 2
        if value <= -bands[2]: return -6
        if value <= -bands[1]: return -4
        if value <= -bands[0]: return -2
        return 0
    s5 = tier(c5, (2, 5, 7))
    s20 = tier(c20, (1, 3, 5))
    return {'score': max(-20, min(20, s_net + s_branch + s5 + s20)),
            'main_net_shares': int(net), 'branch_count_diff': count,
            'concentration_5d': c5, 'concentration_20d': c20,
            'latest_date': str(row['trade_date'])}


def load_v51_chips():
    # Read institutional and licensed CMoney data independently.

    # A failure or missing permission in one source must not hide the other.
    if not database_configured():
        return {}, {}, '未設定 DATABASE_URL；法人與 CMoney 資料暫不可用'

    inst_by_symbol, chip_by_symbol, warnings = {}, {}, []
    try:
        with database_connect() as conn:
            rows = conn.execute('''SELECT symbol, trade_date, foreign_net_shares,
                trust_net_shares, dealer_net_shares, volume
                FROM public.v5_institution_daily
                WHERE trade_date >= CURRENT_DATE - INTERVAL '90 days'
                ORDER BY symbol, trade_date''').fetchall()
        for row in rows:
            record = dict(row)
            inst_by_symbol.setdefault(str(record['symbol']).strip(), []).append(record)
        if not rows:
            warnings.append('法人資料表目前沒有最近90日紀錄')
    except Exception as exc:
        warnings.append('法人資料讀取失敗：' + type(exc).__name__ + '（請檢查 Render DATABASE_URL、資料表與權限；詳細錯誤見伺服器日誌）')
        import logging
        logging.getLogger(__name__).exception('v5.3 institutional query failed')

    try:
        with database_connect() as conn:
            rows = conn.execute('''SELECT symbol, trade_date, main_net_shares,
                branch_count_diff, concentration_5d, concentration_20d
                FROM public.v5_cmoney_chip_daily
                WHERE trade_date >= CURRENT_DATE - INTERVAL '10 days'
                ORDER BY symbol, trade_date DESC''').fetchall()
        for row in rows:
            record = dict(row)
            chip_by_symbol.setdefault(str(record['symbol']).strip(), []).append(record)
        if not rows:
            warnings.append('CMoney 主力尚無授權匯入資料')
    except Exception as exc:
        warnings.append('CMoney 主力資料讀取失敗：' + type(exc).__name__ + '（不影響法人評分）')
        import logging
        logging.getLogger(__name__).exception('v5.3 CMoney query failed')

    return inst_by_symbol, chip_by_symbol, '；'.join(warnings) if warnings else None


def _official_session_dates(inst_by_symbol):
    """Derive exchange-wide recent sessions only from broadly covered T86 dates.

    A missing import day is not a market holiday: if coverage is sparse, do not
    filter price bars. This is a guard, not a substitute for an official calendar.
    """
    from collections import Counter
    counts = Counter(str(r['trade_date']) for rows in (inst_by_symbol or {}).values() for r in rows)
    return {d for d, n in counts.items() if n >= 500}


def scan_one(pair, inst_by_symbol=None, chip_by_symbol=None, official_dates=None):
    symbol, name = pair
    df, ticker = fetch_tw(symbol, '6mo')
    # Remove vendor holiday bars only inside the reliable T86 coverage window.
    # Do not drop earlier historical bars or any bar newer than T86's latest day.
    if official_dates and len(official_dates) >= 10:
        start, end = min(official_dates), max(official_dates)
        keep = [(str(dt.date()) < start or str(dt.date()) > end or
                 str(dt.date()) in official_dates) for dt in df.index]
        df = df.loc[keep].copy()
    if len(df) < 65: raise ValueError('有效日線不足 65 根')
    m = summary(df)
    technical = 0; reasons = []
    for condition, weight, label in [
        (m['breakout_20d_volume_confirmed'], 35, '收盤突破前20日高點且量比≥1.5'),
        (m['ma20'] is not None and m['close'] > m['ma20'], 20, '收盤高於MA20'),
        (m['ma60'] is not None and m['close'] > m['ma60'], 15, '收盤高於MA60'),
        (m['kd_k'] is not None and m['kd_d'] is not None and m['kd_k'] > m['kd_d'], 10, 'K高於D'),
        (m['macd_hist'] is not None and m['macd_hist'] > 0, 10, 'MACD柱體為正'),
        (m['volume_ratio'] is not None and m['volume_ratio'] >= 1.5, 10, '量比≥1.5')]:
        if condition: technical += weight; reasons.append(label)
    surge = volume_surge_score(df)
    if surge['raw_score'] > 0: reasons.append('爆量評分+' + str(surge['score']) + ('（含風險扣分）' if surge['risk_penalty'] else ''))
    inst = _score_institutions((inst_by_symbol or {}).get(symbol, []), df.index)
    last_date = str(df.index[-1].date())
    chip_rows = (chip_by_symbol or {}).get(symbol, [])
    chip_row = next((r for r in chip_rows if str(r['trade_date']) == last_date), None)
    chip = _score_chip(chip_row)
    available_max = 115 + (15 if inst is not None else 0) + (20 if chip is not None else 0)
    total = technical + surge['score'] + (inst['score'] if inst else 0) + (chip['score'] if chip else 0)
    return {'symbol': symbol, 'name': name, 'ticker': ticker,
            'score': total, 'technical_score': technical, 'volume_surge': surge,
            'institutional': inst, 'cmoney_chip': chip,
            'available_max_score': available_max,
            'normalized_score': round(100 * total / available_max, 2),
            'data_coverage': {'institutional': inst is not None, 'cmoney_chip': chip is not None},
            'reasons': reasons, **m, 'entry_price': None, 'stop_price': None,
            'target_price': None, 'win_probability': None}


@app.get('/api/v5/scan')
def v5_scan(limit: int = Query(5, ge=1, le=20)):
    if SCAN_CACHE['data'] is not None and time.time() - SCAN_CACHE['time'] < 900:
        cached = SCAN_CACHE['data'].copy()
        cached['items'] = cached['items'][:limit]
        return cached
    inst, chips, data_warning = load_v51_chips()
    items = []; errors = []
    official_dates = _official_session_dates(inst)
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = {pool.submit(scan_one, pair, inst, chips, official_dates): pair[0] for pair in SCAN_UNIVERSE.items()}
        for future in as_completed(jobs):
            try: items.append(future.result())
            except Exception as e: errors.append({'symbol': jobs[future], 'error': str(e)[:160]})
    items.sort(key=lambda x: (-x['normalized_score'], -x['score'], x['symbol']))
    result = {'items': items, 'scanned': len(SCAN_UNIVERSE), 'succeeded': len(items),
              'errors': errors, 'universe': '固定20檔研究清單，並非台灣全市場掃描',
              'score_definition': '技術100 + 爆量15 + 法人15 + CMoney主力20；依實際資料覆蓋率呈現可評分滿分與標準化分數，不是勝率',
              'data_warning': data_warning,
               'data_sources': {'institutional_symbols_loaded': len(inst),
                                'cmoney_symbols_loaded': len(chips),
                                 'official_session_dates': len(official_dates)},
              'quote_status': '調整後歷史日線；非盤中即時；不能直接當委託價格',
              'generated_at_taipei': datetime.now(ZoneInfo('Asia/Taipei')).isoformat(),
              'limitations': ['僅20檔固定清單', '非即時當沖篩選', '法人與CMoney資料須另外匯入',
                              '不同資料覆蓋率下的標準化排名僅供參考', '部分行情可能延遲或缺漏']}
    SCAN_CACHE.update(time=time.time(), data=result)
    return {**result, 'items': items[:limit]}

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

# v6.3 additive endpoint; all v5/v6.1 endpoints remain unchanged.
@app.get('/api/v6/premarket')
def v63_premarket():
    if not database_configured():
        raise HTTPException(503,'尚未設定 DATABASE_URL')
    try:
        with database_connect() as conn:
            row=conn.execute('SELECT report FROM premarket_reports ORDER BY report_date DESC LIMIT 1').fetchone()
        if not row:
            raise HTTPException(404,'尚未產生盤前報告，請執行 Taiwan Stock Premarket Report')
        return row['report']
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503,'盤前報告暫時無法讀取；請確認已執行 sql/002_premarket.sql')


# v6.9: read-only historical model research endpoint.
@app.get('/api/v6/market-model')
def v69_market_model():
    if not database_configured():
        raise HTTPException(503, '尚未設定 DATABASE_URL')
    try:
        with database_connect() as conn:
            rows = conn.execute("""
                SELECT model_version, created_at, latest_twii_date, last_us_date,
                       training_samples, test_samples, model_accuracy,
                       baseline_accuracy, model_brier, baseline_brier, status
                FROM public.market_model_reports
                WHERE model_version IN ('v6.8-premarket-logistic',
                                        'v6.7-premarket-logistic',
                                        'v6.6-premarket-logistic')
                ORDER BY CASE model_version
                  WHEN 'v6.8-premarket-logistic' THEN 0
                  WHEN 'v6.7-premarket-logistic' THEN 1
                  ELSE 2 END, created_at DESC
                LIMIT 1
            """).fetchall()
        if not rows:
            return {'status':'no_research_record', 'note':'目前只有 GitHub Actions 的 v6.7/v6.8 稽核日誌；資料庫尚無對應研究紀錄。請勿把日誌成績當成即時預測。',
                    'daily_prediction':None}
        r = rows[0]
        def val(k):
            v=r[k]
            return v.isoformat() if hasattr(v,'isoformat') else float(v) if v is not None and k in ('model_accuracy','baseline_accuracy','model_brier','baseline_brier') else v
        return {'status':'research_only', 'report':{k:val(k) for k in (
            'model_version','created_at','latest_twii_date','last_us_date',
            'training_samples','test_samples','model_accuracy',
            'baseline_accuracy','model_brier','baseline_brier','status')},
            'daily_prediction':None,
            'note':'僅為歷史研究回測；不是當日盤前上漲機率。v6.8 稽核結果未自動存入資料庫。'}
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, 'AI 大盤研究資料暫時無法讀取，請確認 market_model_reports 資料表')
