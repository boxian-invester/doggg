"""Run from repository root: python -m scripts.update_market_monitor"""
import json
from datetime import datetime, timezone
from db import connect
from market_monitor import get_market_monitor


def main():
    snapshot = get_market_monitor(force=True)
    good = [key for key, val in snapshot['metrics'].items() if val.get('status') == 'ok']
    if not good:
        raise RuntimeError('No verified metrics: refusing to save empty snapshot')
    with connect() as conn:
        conn.execute(
            'INSERT INTO public.market_monitor_snapshots (payload, source) VALUES (%s::jsonb, %s)',
            (json.dumps(snapshot, ensure_ascii=False, allow_nan=False), 'github_actions'),
        )
    print(json.dumps({'saved_at_utc': datetime.now(timezone.utc).isoformat(),
                      'ok_metrics': good, 'count': len(good)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
