"""V4.2 snapshot-based descriptive market sentiment, Python 3.9+.

Requires db.connect(), market_monitor_snapshots, market_official_daily.
Historical 5/20 comparisons are trading-observation offsets, not calendar days.
The score is a heuristic, NOT a calibrated forecast or probability.
"""
from datetime import datetime, timezone, date
from zoneinfo import ZoneInfo
from db import connect

WEIGHTS = {'usdtwd': 15, 'foreign_cash': 15, 'foreign_futures': 15,
           'twii': 15, 'margin': 10, 'tw_vix': 10, 'nasdaq': 5,
           'sox': 5, 'breadth': 5, 'trust_cash': 5}
TOTAL_WEIGHT = sum(WEIGHTS.values())
OFFICIAL_COLUMNS = {'foreign_futures': 'foreign_futures_net',
                    'margin': 'margin_balance', 'tw_vix': 'tw_vix'}


def _number(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _date(x):
    try:
        return date.fromisoformat(str(x)[:10])
    except (ValueError, TypeError):
        return None


def _fresh(item, now):
    d = _date(item.get('date'))
    return item.get('status') == 'ok' and d is not None and 0 <= (now.date() - d).days <= 4


def _direction(key, item):
    value = item.get('value')
    if not _number(value):
        return None
    if key in ('usdtwd', 'nasdaq', 'sox', 'twii'):
        change = item.get('change_pct')
        if not _number(change):
            return None
        if abs(change) < 0.15:
            return 0.5
        return float((change < 0) if key == 'usdtwd' else (change > 0))
    if key in ('foreign_cash', 'trust_cash', 'breadth'):
        return 1.0 if value > 0 else 0.0 if value < 0 else 0.5
    return None


def _historical(rows, key, item):
    """Return (fraction, detail) only if latest official row matches snapshot date/value.

    Require 21 valid observations for each metric; never skip missing observations
    and silently interpret an older point as the 20-session comparison.
    """
    column = OFFICIAL_COLUMNS[key]
    observed = _date(item.get('date'))
    if observed is None or not _number(item.get('value')):
        return None, {'reason': '快照缺少有效日期或數值'}
    if not rows or rows[0]['trade_date'] != observed:
        return None, {'reason': '官方歷史最新日期與快照日期不一致'}
    if len(rows) < 21:
        return None, {'reason': '歷史資料不足21個交易日'}
    vals = [r[column] for r in rows[:21]]
    if any(v is None for v in vals):
        return None, {'reason': '20日視窗內存在缺值'}
    vals = [float(v) for v in vals]
    current, prior5, prior20 = vals[0], vals[5], vals[20]
    if abs(current - float(item['value'])) > (0.011 if key != 'foreign_futures' else 0.5):
        return None, {'reason': '快照數值與歷史資料不一致'}
    delta5, delta20 = current - prior5, current - prior20
    info = {'observed_date': observed.isoformat(), 'value': current,
            'change_5d': round(delta5, 4), 'change_20d': round(delta20, 4),
            'reference_5d_date': rows[5]['trade_date'].isoformat(),
            'reference_20d_date': rows[20]['trade_date'].isoformat(),
            'method': 'rule_based_heuristic_not_probability'}
    if key == 'foreign_futures':
        # Net OI moving toward long = positive; both windows must agree.
        # Smaller disagreement is neutral rather than claiming a trend.
        direction = 1.0 if delta5 > 0 and delta20 > 0 else 0.0 if delta5 < 0 and delta20 < 0 else 0.5
        info['rule'] = '5日及20日淨部位同向變化；不依空單絕對口數評分'
    elif key == 'tw_vix':
        # Rising volatility is a risk-off proxy, not a guaranteed decline.
        direction = 0.0 if delta5 > 0 and delta20 > 0 else 1.0 if delta5 < 0 and delta20 < 0 else 0.5
        info['rule'] = '5日及20日VIX同升偏風險、同降偏穩定；不預測報酬'
    else:
        if prior5 <= 0 or prior20 <= 0:
            return None, {'reason': '融資比較基期無效'}
        pct5 = 100 * delta5 / prior5
        pct20 = 100 * delta20 / prior20
        info['change_5d_pct'] = round(pct5, 2)
        info['change_20d_pct'] = round(pct20, 2)
        # Financing contraction alone is NOT bullish. Keep it neutral until
        # a price-conditioned and historically validated rule is implemented.
        direction = 0.5
        info['rule'] = '融資變動僅揭露、暫採中性；尚未建立價格條件及回測'
    return direction, info


def read_market_monitor():
    with connect() as conn:
        snapshots = conn.execute('''
            SELECT collected_at, payload FROM public.market_monitor_snapshots
            ORDER BY collected_at DESC LIMIT 50
        ''').fetchall()
        if not snapshots:
            return {'status': 'no_snapshot', 'metrics': {}, 'score': None,
                    'warning': '尚未完成第一次排程更新'}
        # All 21 recent trading sessions are fetched once, not per indicator.
        official_error = None
        try:
            official = conn.execute('''
                SELECT trade_date, foreign_futures_net, margin_balance, tw_vix
                FROM public.market_official_daily
                ORDER BY trade_date DESC LIMIT 21
            ''').fetchall()
        except Exception as exc:
            official = []
            official_error = type(exc).__name__
    latest = snapshots[0]
    payload = dict(latest['payload'])
    metrics = dict(payload.get('metrics') or {})
    for row in snapshots[1:]:
        for key, old in (row['payload'].get('metrics') or {}).items():
            current = metrics.get(key) or {}
            if current.get('status') != 'ok' and old.get('status') == 'ok':
                metrics[key] = {**old, 'status': 'stale',
                                'note': '最近一次抓取失敗；僅供參考，不納入評分'}
    now = datetime.now(ZoneInfo('Asia/Taipei'))
    collected = latest['collected_at']
    if collected.tzinfo is None:
        collected = collected.replace(tzinfo=timezone.utc)
    age_minutes = max(0, (datetime.now(timezone.utc) - collected).total_seconds() / 60)
    payload['metrics'] = metrics
    payload['snapshot_saved_at'] = collected.isoformat()
    payload['snapshot_age_minutes'] = round(age_minutes, 1)
    payload['data_mode'] = 'database_snapshot_not_live'
    details = {}
    covered, earned = 0, 0.0
    if age_minutes <= 24 * 60:
        for key, weight in WEIGHTS.items():
            item = metrics.get(key) or {}
            extra = {}
            if not _fresh(item, now):
                direction = None
                extra['reason'] = '快照指標缺漏、失敗或過期'
            elif key in OFFICIAL_COLUMNS:
                direction, extra = _historical(official, key, item)
                if official_error:
                    extra['database_error'] = official_error
            else:
                direction = _direction(key, item)
                if direction is None:
                    extra['reason'] = '缺少可驗證方向資料'
            if direction is None:
                details[key] = {'weight': weight, 'included': False, **extra}
                continue
            covered += weight
            earned += weight * direction
            details[key] = {'weight': weight, 'included': True,
                            'bullish_fraction': direction,
                            'observed_date': item.get('date'), **extra}
    else:
        details = {key: {'weight': weight, 'included': False,
                         'reason': '快照超過24小時'} for key, weight in WEIGHTS.items()}
    payload['score'] = round(100 * earned / covered, 1) if covered >= 60 else None
    payload['coverage_weight'] = covered
    payload['total_weight'] = TOTAL_WEIGHT
    payload['score_label'] = '已覆蓋指標方向分數（規則式、非上漲機率）'
    payload['score_details'] = details
    payload['sentiment'] = ('偏多' if payload['score'] >= 60 else '偏空'
                            if payload['score'] <= 40 else '中性') if payload['score'] is not None else '資料不足'
    payload['sentiment_color'] = ('red' if payload['sentiment'] == '偏多' else
                                  'green' if payload['sentiment'] == '偏空' else 'gray')
    payload['forecast_next_day'] = None
    payload['forecast_20d'] = None
    payload['model_version'] = 'V4.2-heuristic'
    payload['warning'] = ('本分數僅為規則式市場方向描述，非預測勝率。'
                          '融資暫採中性，尚未建立價格條件與歷史回測。'
                          '官方歷史資料與快照日期或數值不一致時不計分；快照非即時。')
    return payload
