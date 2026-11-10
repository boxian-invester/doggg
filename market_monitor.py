"""Market monitoring with delayed vendor bars and conservative TWSE cash-flow adapter.
Missing official data remains missing. Scores are descriptive, not probabilities.
Python 3.9+ compatible.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import json
import math
import threading
import time
import yfinance as yf

TZ = ZoneInfo('Asia/Taipei')
SYMBOLS = {
    'twii': ('台灣加權指數', '^TWII'),
    'usdtwd': ('美元兌新台幣', 'TWD=X'),
    'nasdaq': ('NASDAQ', '^IXIC'),
    'sox': ('費城半導體', '^SOX'),
    'sp500': ('S&P 500', '^GSPC'),
    'us_vix': ('美國VIX', '^VIX'),
    'dxy_proxy': ('美元指數 DXY（指數代理行情）', 'DX-Y.NYB'),
    'us10y': ('美國10年期殖利率指標', '^TNX'),
}
OFFICIAL = {
    'foreign_cash': ('外資現貨買賣超（上市）', 'TWSE'),
    'trust_cash': ('投信買賣超（上市）', 'TWSE'),
    'foreign_futures': ('外資台指期淨空單', 'TAIFEX'),
    'margin': ('上市櫃融資餘額', 'TWSE/TPEx'),
    'tw_vix': ('台灣VIX', 'TAIFEX'),
    'breadth': ('台股市場廣度', 'TWSE/TPEx'),
}
_LOCK = threading.Lock()
_CACHE = {'at': 0.0, 'result': None}
_TTL = 300


def _vendor(label, symbol):
    try:
        df = yf.Ticker(symbol).history(period='3mo', interval='1d', auto_adjust=False)
        closes = df['Close'].dropna()
        if len(closes) < 2:
            raise ValueError('not_enough_daily_bars')
        last, prev = float(closes.iloc[-1]), float(closes.iloc[-2])
        if not all(map(math.isfinite, (last, prev))) or prev == 0:
            raise ValueError('invalid_quote')
        return {'label': label, 'value': round(last, 4),
                'date': closes.index[-1].strftime('%Y-%m-%d'),
                'change_pct': round((last / prev - 1) * 100, 3),
                'status': 'ok', 'source': 'Yahoo Finance via yfinance',
                'frequency': 'delayed_daily', 'symbol': symbol}
    except Exception as exc:
        return {'label': label, 'value': None, 'date': None,
                'status': 'unavailable', 'source': 'Yahoo Finance via yfinance',
                'frequency': 'delayed_daily', 'symbol': symbol,
                'error': type(exc).__name__}


def _missing(key, status='not_connected', note=None):
    label, source = OFFICIAL[key]
    out = {'label': label, 'value': None, 'date': None, 'status': status,
           'source': source, 'frequency': 'official_after_close'}
    if note:
        out['note'] = note
    return out


def _number(raw):
    s = str(raw).strip().replace(',', '').replace('＋', '+').replace('－', '-')
    if not s or s in ('--', '—', 'N/A'):
        raise ValueError('empty_numeric_field')
    return float(s)


def _normalize_name(value):
    """Normalize exchange row labels without conflating foreign subcategories."""
    import unicodedata
    return ''.join(unicodedata.normalize('NFKC', str(value)).split()).replace('：', ':')


def _parse_twse_cash(payload):
    """Return only explicitly identified TWSE institutional net cash values in 億元.

    The official feed may contain a foreign aggregate and component rows;
    prefer an explicit aggregate, never sum overlapping rows.
    """
    if str(payload.get('stat', '')).strip() != 'OK':
        return None
    fields = [_normalize_name(f) for f in payload.get('fields', [])]
    name_i = next((i for i, f in enumerate(fields) if '單位名稱' in f or '類別' in f), None)
    net_i = next((i for i, f in enumerate(fields) if '買賣差額' in f or '買賣超' in f), None)
    if name_i is None or net_i is None:
        raise ValueError('twse_schema_unrecognized')
    foreign_candidates = []
    found = {}
    for row in payload.get('data') or []:
        if not isinstance(row, (list, tuple)) or len(row) <= max(name_i, net_i):
            continue
        name = _normalize_name(row[name_i])
        if name.startswith('投信'):
            found['trust_cash'] = _number(row[net_i]) / 1e8
        elif name.startswith(('外資及陸資', '外資')):
            if '自營商' in name and '不含' not in name and '不包括' not in name:
                continue
            # Prefer explicit total if present; otherwise use the ex-proprietary
            # institutional line and retain its scope in the display label.
            priority = 2 if ('合計' in name or '總計' in name) else 1 if ('不含' in name or '不包括' in name) else 0
            foreign_candidates.append((priority, name, _number(row[net_i]) / 1e8))
    if foreign_candidates:
        foreign_candidates.sort(key=lambda x: x[0], reverse=True)
        found['foreign_cash'] = foreign_candidates[0][2]
        found['_foreign_label'] = foreign_candidates[0][1]
    if not any(k in found for k in ('foreign_cash', 'trust_cash')):
        raise ValueError('twse_rows_unrecognized')
    return found


def _twse_institution_day(day):
    """Parse TWSE BFI82U only when field labels can be identified unambiguously.

    Amounts are TWD; display values are converted to 億元. Never sum the
    total foreign row together with its subcategories.
    """
    query = urlencode({'response': 'json', 'dayDate': day.strftime('%Y%m%d'), 'type': 'day'})
    url = 'https://www.twse.com.tw/fund/BFI82U?' + query
    req = Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'})
    with urlopen(req, timeout=12) as resp:
        payload = json.load(resp)
    if str(payload.get('stat', '')).strip() != 'OK':
        return None
    return _parse_twse_cash(payload)


def _official_cash(metrics):
    now = datetime.now(TZ)
    # Official daily data can be delayed; look back up to 7 calendar days.
    # This is not a live feed and the result retains its original trading date.
    last_error = None
    for offset in range(8):
        day = (now - timedelta(days=offset)).date()
        if day.weekday() >= 5:
            continue
        try:
            result = _twse_institution_day(day)
            if not result:
                continue
            for key in ('foreign_cash', 'trust_cash'):
                if key in result:
                    metrics[key] = {'label': OFFICIAL[key][0],
                                    'value': round(result[key], 2), 'unit': '億元',
                                    'date': day.isoformat(), 'status': 'ok',
                                    'source': 'TWSE BFI82U (上市)',
                                    'frequency': 'official_after_close',
                                    'note': ('僅上市市場；正數為買超，負數為賣超。' + (' 外資口徑：' + result['_foreign_label'] if key == 'foreign_cash' and result.get('_foreign_label') else ''))}
            return
        except Exception as exc:
            last_error = type(exc).__name__
    for key in ('foreign_cash', 'trust_cash'):
        metrics[key] = _missing(key, 'unavailable',
                                '官方資料尚未取得或欄位格式不符' + (' (' + last_error + ')' if last_error else ''))


def get_market_monitor(force=False):
    with _LOCK:
        if not force and _CACHE['result'] and time.monotonic() - _CACHE['at'] < _TTL:
            return _CACHE['result']
        metrics = {key: _vendor(*pair) for key, pair in SYMBOLS.items()}
        for key in OFFICIAL:
            metrics[key] = _missing(key)
        _official_cash(metrics)
        # Only explicitly defined comparable directions are scored.  The
        # full-weight scoring model is NOT ready until the official data exist.
        weighted = [('usdtwd', 15, False), ('nasdaq', 5, True),
                    ('sox', 5, True), ('us_vix', 10, False)]
        known, earned = 0, 0.0
        for key, weight, positive_bull in weighted:
            item = metrics[key]
            if item['status'] != 'ok':
                continue
            known += weight
            change = item['change_pct']
            if abs(change) < .15:
                earned += weight / 2
            elif (change > 0) == positive_bull:
                earned += weight
        score = round(earned / known * 100, 1) if known else None
        result = {
            'generated_at_taipei': datetime.now(TZ).isoformat(),
            'refresh_seconds': _TTL, 'metrics': metrics,
            'score': score, 'coverage_weight': known, 'total_weight': 100,
            'score_label': '局部方向分數（非預測機率）',
            'official_cash_connected': all(metrics[k]['status'] == 'ok' for k in ('foreign_cash', 'trust_cash')),
            'warning': '正式多空評分尚未啟用：期貨淨部位、融資、台灣VIX及市場廣度仍待可靠官方解析；外資現貨資料不等同資金跨境流量。DXY 行情為第三方延遲指數報價。',
            'forecast_next_day': None, 'forecast_20d': None,
        }
        _CACHE.update(at=time.monotonic(), result=result)
        return result
