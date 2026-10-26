"""Five-year daily market index backfill; standalone, idempotent, no model probabilities."""
import argparse
import logging
import os
import time
from datetime import datetime, timedelta, timezone
import pandas as pd
import psycopg
import yfinance as yf

TICKERS = {
    "^TWII": "Taiwan Weighted",
    "^GSPC": "S&P 500",
    "^IXIC": "NASDAQ",
    "^SOX": "Philadelphia Semiconductor",
    "^DJI": "Dow Jones",
    "TSM": "TSMC ADR",
    "^VIX": "VIX",
}
LOG = logging.getLogger("market_backfill")

def fetch(ticker, years):
    # Extra calendar days account for weekends; avoid look-ahead or synthetic rows.
    start = (datetime.now(timezone.utc).date() - timedelta(days=round(365.25 * years + 10))).isoformat()
    df = yf.Ticker(ticker).history(start=start, interval="1d", auto_adjust=True)
    if df.empty:
        raise RuntimeError(f"No rows from source: {ticker}")
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Open", "High", "Low", "Close"])
    rows = []
    for timestamp, row in df.iterrows():
        o, h, l, c = [float(row[k]) for k in ["Open", "High", "Low", "Close"]]
        if min(o, h, l, c) <= 0 or h < max(o, c, l) or l > min(o, c, h):
            continue
        volume = row["Volume"]
        v = int(volume) if pd.notna(volume) and float(volume) >= 0 else 0
        rows.append((ticker, timestamp.date(), o, h, l, c, v))
    if len(rows) < 500:
        raise RuntimeError(f"Only {len(rows)} valid rows for {ticker}; refusing incomplete backfill")
    return rows

def run(years):
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL secret is missing")
    # Fail early before downloads when SQL schema has not been applied.
    with psycopg.connect(url, connect_timeout=20) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.market_daily_bars')")
            if cur.fetchone()[0] is None:
                raise RuntimeError("Run sql/003_market_daily_bars.sql in Supabase first")
    failed = []
    for ticker in TICKERS:
        try:
            rows = fetch(ticker, years)
            with psycopg.connect(url, connect_timeout=20) as conn:
                with conn.cursor() as cur:
                    # Bounded chunks and transactional per-ticker commits.
                    for i in range(0, len(rows), 250):
                        cur.executemany("""
                            INSERT INTO public.market_daily_bars
                              (ticker,trade_date,open,high,low,close,volume,source,adjusted)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,'yfinance',true)
                            ON CONFLICT (ticker,trade_date) DO UPDATE SET
                              open=excluded.open,high=excluded.high,low=excluded.low,
                              close=excluded.close,volume=excluded.volume,
                              loaded_at=now()
                        """, rows[i:i+250])
            LOG.info("%s: saved %d rows", ticker, len(rows))
        except Exception as exc:
            failed.append(ticker)
            LOG.exception("%s failed: %s", ticker, exc)
        time.sleep(1)
    if failed:
        raise RuntimeError("Failed tickers: " + ", ".join(failed))
    LOG.info("All market tickers backfilled successfully")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--years", type=int, default=5, choices=[3, 5])
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run(args.years)
