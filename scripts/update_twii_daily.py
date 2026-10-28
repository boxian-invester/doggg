#!/usr/bin/env python3
"""Refresh actual ^TWII daily bars and require alignment with latest broad stock session."""
import logging
import os
from datetime import date
import psycopg
import yfinance as yf

logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
LOG = logging.getLogger(__name__)


def main():
    url = os.environ.get('DATABASE_URL')
    if not url:
        raise RuntimeError('DATABASE_URL is missing')
    with psycopg.connect(url, connect_timeout=20) as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT trade_date, count(*) FROM public.daily_bars
                WHERE trade_date >= CURRENT_DATE - INTERVAL '20 days'
                GROUP BY trade_date HAVING count(*) >= 500
                ORDER BY trade_date DESC LIMIT 1""")
            target_row = cur.fetchone()
            if not target_row:
                raise RuntimeError('No recent broad stock session')
            target = target_row[0]
            cur.execute("SELECT close FROM public.market_daily_bars WHERE ticker='^TWII' AND trade_date=%s", (target,))
            existing = cur.fetchone()
            if existing and existing[0] is not None and float(existing[0]) > 0:
                LOG.info('^TWII already available for target=%s; skipping fetch', target)
                return

    df = yf.Ticker('^TWII').history(period='20d', interval='1d', auto_adjust=True)
    if df.empty:
        raise RuntimeError('Yahoo Finance returned no ^TWII data')
    records = []
    for stamp, bar in df.iterrows():
        d = stamp.date()
        if d > date.today():
            continue
        values = [float(bar[k]) for k in ('Open', 'High', 'Low', 'Close')]
        o, h, l, c = values
        if min(values) <= 0 or h < max(o, c, l) or l > min(o, c, h):
            continue
        volume = bar['Volume']
        v = int(volume) if volume == volume and float(volume) >= 0 else 0
        records.append(('^TWII', d, o, h, l, c, v))
    if not records:
        raise RuntimeError('No valid ^TWII bars from Yahoo Finance')
    with psycopg.connect(url, connect_timeout=20) as conn:
        with conn.cursor() as cur:
            cur.executemany("""INSERT INTO public.market_daily_bars
                (ticker,trade_date,open,high,low,close,volume,source,adjusted)
                VALUES (%s,%s,%s,%s,%s,%s,%s,'yfinance',true)
                ON CONFLICT (ticker,trade_date) DO UPDATE SET
                open=excluded.open,high=excluded.high,low=excluded.low,
                close=excluded.close,volume=excluded.volume,
                source=excluded.source,adjusted=excluded.adjusted,loaded_at=now()""", records)
            cur.execute("SELECT close FROM public.market_daily_bars WHERE ticker='^TWII' AND trade_date=%s", (target,))
            aligned = cur.fetchone()
            if not aligned or aligned[0] is None or float(aligned[0]) <= 0:
                raise RuntimeError(f'^TWII missing for stock session {target}; latest Yahoo bar {max(r[1] for r in records)}. Refusing TOP20 to avoid incomplete scoring.')
    LOG.info('^TWII refreshed %d bars; aligned with stock target=%s', len(records), target)


if __name__ == '__main__':
    main()
