"""Backfill official daily TXF foreign net OI, TWSE margin and TAIFEX VIX.
Requires existing project market_monitor.py, market_monitor_v36.py, taifex_vix_parser.py.
Never fills missing observations with synthetic values.
"""
import argparse
import os
import time
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from urllib.request import Request, urlopen

import psycopg
from market_monitor import _taifex_tx_day
from market_monitor_v36 import _day_json, parse_margin
from taifex_vix_parser import URL_TEMPLATE, LINE


def get_vix_month(ym):
    req = Request(URL_TEMPLATE.format(yearmonth=ym), headers={'User-Agent': 'Mozilla/5.0'})
    with urlopen(req, timeout=25) as resp:
        raw = resp.read()
    decoded = raw.decode('cp950')
    result = {}
    for line in decoded.splitlines():
        m = LINE.match(line)
        if not m:
            continue
        ymd, clock, vix, avg = m.groups()
        day = datetime.strptime(ymd, '%Y%m%d').date()
        if day.strftime('%Y%m') != ym:
            continue
        val = float(vix)
        if not 0 < val < 200:
            continue
        if day not in result or clock > result[day][0]:
            result[day] = (clock, val)
    return {d: value for d, (_, value) in result.items()}


def retry(func, label, attempts=2):
    for attempt in range(attempts):
        try:
            return func()
        except Exception as exc:
            print(f'{label}: {type(exc).__name__}: {str(exc)[:110]}', flush=True)
            if attempt + 1 < attempts:
                time.sleep(2)
    return None


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--days', type=int, default=90)
    args = p.parse_args()
    if not 1 <= args.days <= 365:
        raise SystemExit('--days must be 1..365')
    if not os.getenv('DATABASE_URL'):
        raise SystemExit('DATABASE_URL missing')
    today = datetime.now(ZoneInfo('Asia/Taipei')).date()
    start = today - timedelta(days=args.days - 1)
    days = [start + timedelta(days=i) for i in range(args.days)]
    days = [d for d in days if d.weekday() < 5]
    vix = {}
    for ym in sorted({d.strftime('%Y%m') for d in days}):
        month = retry(lambda ym=ym: get_vix_month(ym), 'VIX ' + ym)
        if month:
            vix.update(month)
    stats = {'futures': 0, 'margin': 0, 'vix': 0}
    with psycopg.connect(os.environ['DATABASE_URL'], connect_timeout=20) as conn:
        for i, day in enumerate(days, 1):
            # Dates are independently verified; never assign a previous trading day's values.
            def futures():
                r = _taifex_tx_day(day)
                return r['net_oi']
            net = retry(futures, f'TXF {day}')
            def margin():
                payload = _day_json('MI_MARGN', day)
                if str(payload.get('stat', '')).upper() != 'OK':
                    return None
                if payload.get('date') and str(payload['date']) != day.strftime('%Y%m%d'):
                    raise ValueError('TWSE date mismatch')
                return parse_margin(payload)
            bal = retry(margin, f'MARGIN {day}')
            vv = vix.get(day)
            if all(x is None for x in (net, bal, vv)):
                continue
            # Partial records merge without erasing previously valid values.
            conn.execute('''
                INSERT INTO public.market_official_daily
                    (trade_date, foreign_futures_net, margin_balance, tw_vix, updated_at)
                VALUES (%s, %s, %s, %s, NOW())
                ON CONFLICT (trade_date) DO UPDATE SET
                    foreign_futures_net = COALESCE(EXCLUDED.foreign_futures_net, market_official_daily.foreign_futures_net),
                    margin_balance = COALESCE(EXCLUDED.margin_balance, market_official_daily.margin_balance),
                    tw_vix = COALESCE(EXCLUDED.tw_vix, market_official_daily.tw_vix),
                    updated_at = NOW()
            ''', (day, net, bal, vv))
            conn.commit()
            for k, x in (('futures', net), ('margin', bal), ('vix', vv)):
                stats[k] += x is not None
            print(f'{i}/{len(days)} {day} TXF={net} margin={bal} VIX={vv}', flush=True)
            time.sleep(0.3)
    print('SUCCESS', stats, flush=True)
    if not any(stats.values()):
        raise SystemExit('No official observations obtained; check source formats and logs')


if __name__ == '__main__':
    main()
