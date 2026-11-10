"""Read saved market data without querying Yahoo on every page view."""
from datetime import datetime, timezone
from db import connect


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
    # Preserve latest known-good observations without presenting them as current.
    metrics = dict(payload.get('metrics') or {})
    for row in rows[1:]:
        for key, old in (row['payload'].get('metrics') or {}).items():
            current = metrics.get(key) or {}
            if current.get('status') != 'ok' and old.get('status') == 'ok':
                metrics[key] = {**old, 'status': 'stale',
                                'note': '最近一次抓取失敗；顯示較早成功資料，請核對日期'}
    payload['metrics'] = metrics
    payload['snapshot_saved_at'] = latest['collected_at'].isoformat()
    payload['snapshot_age_minutes'] = round(
        (datetime.now(timezone.utc) - latest['collected_at']).total_seconds() / 60, 1)
    payload['data_mode'] = 'database_snapshot_not_live'
    # Scores based on mixed-age observations are not meaningful.
    payload['score'] = None
    payload['coverage_weight'] = None
    payload['forecast_next_day'] = None
    payload['forecast_20d'] = None
    payload['warning'] = '資料庫快照，非即時；部分指標可能過期。正式多空分數待資料覆蓋與回測驗證。'
    return payload
