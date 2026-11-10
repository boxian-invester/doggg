"""Market monitor: delayed vendor daily bars, explicit unavailable official fields.
No forecast probability, no invented institutional data.
"""
from datetime import datetime
from zoneinfo import ZoneInfo
import math
import time
import threading
import yfinance as yf

_SYMBOLS = {
    'twii': ('台灣加權指數', '^TWII'),
    'usdtwd': ('美元兌新台幣', 'TWD=X'),
    'nasdaq': ('NASDAQ', '^IXIC'),
    'sox': ('費城半導體', '^SOX'),
    'us_vix': ('美國VIX', '^VIX'),
    'dxy_proxy': ('美元指數期貨', 'DX=F'),
    'us10y': ('美國10年期殖利率', '^TNX'),
}
_LOCK = threading.Lock()
_CACHE = {'at': 0, 'result': None}
_TTL = 300

def _get_one(name, symbol):
    try:
        bars = yf.Ticker(symbol).history(period='3mo', interval='1d', auto_adjust=False)
        closes = bars['Close'].dropna()
        if len(closes) < 2:
            raise ValueError('有效日線不足')
        latest = float(closes.iloc[-1]); previous = float(closes.iloc[-2])
        if not math.isfinite(latest) or not math.isfinite(previous) or previous == 0:
            raise ValueError('行情無效')
        idx = closes.index[-1]
        return {'label':name, 'value':round(latest,4), 'date':idx.strftime('%Y-%m-%d'),
                'change_pct':round((latest/previous-1)*100,3),
                'source':'Yahoo Finance via yfinance', 'frequency':'delayed_daily',
                'status':'ok', 'symbol':symbol}
    except Exception as exc:
        return {'label':name, 'value':None, 'date':None, 'status':'unavailable',
                'source':'Yahoo Finance via yfinance', 'frequency':'delayed_daily',
                'symbol':symbol, 'error':type(exc).__name__}

def get_market_monitor(force=False):
    with _LOCK:
        if not force and _CACHE['result'] and time.monotonic()-_CACHE['at'] < _TTL:
            return _CACHE['result']
        metrics = {key:_get_one(*pair) for key,pair in _SYMBOLS.items()}
        # Not provided by Yahoo. These must remain missing until validated official adapters are installed.
        for key,label,source in [
            ('foreign_cash','外資現貨買賣超','TWSE'),
            ('foreign_futures','外資台指期淨空單','TAIFEX'),
            ('margin','上市櫃融資餘額','TWSE/TPEx'),
            ('tw_vix','台灣VIX','TAIFEX'),
            ('trust_cash','投信買賣超','TWSE'),
            ('breadth','台股市場廣度','TWSE/TPEx')]:
            metrics[key] = {'label':label,'value':None,'date':None,'status':'not_connected',
                            'source':source,'frequency':'official_after_close'}
        # Deliberately exclude missing indicators from scoring. These are transparent rules,
        # not calibrated probabilities. Current day vs previous daily close is insufficient for trend.
        weighted = [('usdtwd',15,False),('nasdaq',5,True),('sox',5,True),('us_vix',10,False)]
        known=0; earned=0
        for key,weight,positive_is_bull in weighted:
            m=metrics[key]
            if m['status']!='ok':continue
            known+=weight
            pct=m['change_pct']
            if abs(pct)<0.15: earned+=weight/2
            elif (pct>0)==positive_is_bull: earned+=weight
        result={'generated_at_taipei':datetime.now(ZoneInfo('Asia/Taipei')).isoformat(),
                'refresh_seconds':_TTL,'metrics':metrics,
                'score':round(earned/known*100,1) if known else None,
                'coverage_weight':known,'total_weight':100,
                'score_label':'可用資料方向性評分（非上漲機率）',
                'warning':'關鍵外資、融資、台灣VIX尚未串接；分數覆蓋率不足，不能用來判定台股多空。美元指數為期貨代理標的，不是DXY現貨。',
                'forecast_next_day':None,'forecast_20d':None}
        _CACHE.update(at=time.monotonic(),result=result)
        return result
