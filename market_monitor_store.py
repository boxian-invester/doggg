"""V4.1 read-only market snapshot and transparent directional sentiment scoring.
Compatible with Python 3.9+. Replace market_monitor_store.py with this file.
"""
from datetime import datetime, timezone, date
from zoneinfo import ZoneInfo
from db import connect

WEIGHTS = {'usdtwd': 15, 'foreign_cash': 15, 'foreign_futures': 15,
           'twii': 15, 'margin': 10, 'tw_vix': 10, 'nasdaq': 5,
           'sox': 5, 'breadth': 5, 'trust_cash': 5}
TOTAL_WEIGHT = sum(WEIGHTS.values())


def _direction(key, item):
    """Return 0 bearish, 0.5 neutral, 1 bullish; None means no valid rule."""
    value = item.get('value')
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    if key in ('usdtwd', 'nasdaq', 'sox', 'twii'):
        change = item.get('change_pct')
        if not isinstance(change, (int, float)):
            return None
        if abs(change) < 0.15:
            return 0.5
        bullish = change < 0 if key == 'usdtwd' else change > 0
        return 1.0 if bullish else 0.0
    if key in ('foreign_cash', 'trust_cash', 'breadth'):
        return 1.0 if value > 0 else 0.0 if value < 0 else 0.5
    if key == 'foreign_futures':
        # A negative net position is not itself a sell signal without changes
        # versus earlier days. Do not invent a directional score.
        return None
    if key == 'tw_vix':
        # Absolute VIX levels require validated historical thresholds.
        return None
    if key == 'margin':
        # Absolute margin balance has no meaningful bullish/bearish sign.
        return None
    return None


def _fresh(item, now):
    if item.get('status') != 'ok':
        return False
    try:
        observed = date.fromisoformat(str(item['date'])[:10])
    except (ValueError, TypeError, KeyError):
        return False
    # Weekend/holiday tolerance, but never treat future dates as valid.
    return 0 <= (now.date() - observed).days <= 4


def read_market_monitor():
    with connect() as conn:
        rows = conn.execute('''
            SELECT collected_at, payload FROM public.market_monitor_snapshots
            ORDER BY collected_at DESC LIMIT 50
        ''').fetchall()
    if not rows:
        return {'status': 'no_snapshot', 'metrics': {}, 'score': None,
                'warning': '尚未完成第一次排程更新'}
    latest = rows[0]
    payload = dict(latest['payload'])
    metrics = dict(payload.get('metrics') or {})
    for row in rows[1:]:
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
            direction = _direction(key, item) if _fresh(item, now) else None
            if direction is None:
                details[key] = {'weight': weight, 'included': False, 'reason': '缺少可驗證的近期資料或方向比較基準'}
                continue
            covered += weight
            earned += weight * direction
            details[key] = {'weight': weight, 'included': True, 'bullish_fraction': direction,
                            'observed_date': item.get('date')}
    else:
        details = {key: {'weight': weight, 'included': False, 'reason': '快照超過24小時'}
                   for key, weight in WEIGHTS.items()}
    # Never extrapolate partial coverage to a purported full 100-point score.
    payload['score'] = round(100 * earned / covered, 1) if covered >= 60 else None
    payload['coverage_weight'] = covered
    payload['total_weight'] = TOTAL_WEIGHT
    payload['score_label'] = '已覆蓋指標方向分數（非上漲機率）'
    payload['score_details'] = details
    payload['sentiment'] = ('偏多' if payload['score'] >= 60 else '偏空' if payload['score'] <= 40 else '中性') if payload['score'] is not None else '資料不足'
    payload['sentiment_color'] = ('red' if payload['sentiment'] == '偏多' else 'green' if payload['sentiment'] == '偏空' else 'gray')
    payload['forecast_next_day'] = None
    payload['forecast_20d'] = None
    payload['warning'] = ('僅為歷史日線及官方盤後數據的描述性方向分數，非勝率或預測。'
                          '融資、台灣VIX及期貨持倉尚缺歷史比較基準，不強行計分。'
                          '快照非即時；缺漏或過期數據不計分。')
    return payload
