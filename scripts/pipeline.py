"""Taiwan Stock Intelligence: bounded PostgreSQL OHLCV ingestion and scanning.

Run: python -m scripts.pipeline --mode all --limit 120 --workers 2 --period 1y

Ingestion prioritizes missing symbols, then the least recently refreshed symbols.
One run processes at most --limit symbols; it does NOT refresh the entire market.
Signals are heuristic rankings, NOT probabilities or validated day-trading returns.
"""
import argparse
import datetime as dt
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd
import requests
import yfinance as yf
from psycopg.types.json import Jsonb

from db import connect
from main import summary

LOG = logging.getLogger('pipeline')
SOURCES = {
    'TWSE': 'https://openapi.twse.com.tw/v1/opendata/t187ap03_L',
    'TPEx': 'https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O',
}


def field(item, *keys):
    for key in keys:
        value = item.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ''


def fetch_universe():
    found, errors = {}, []
    for market, url in SOURCES.items():
        try:
            response = requests.get(url, timeout=35, headers={'User-Agent': 'TaiwanStockResearch/1.0'})
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, list):
                raise ValueError('Expected JSON array')
            for row in data:
                symbol = field(row, '公司代號', '股票代號', 'SecuritiesCompanyCode', 'Code')
                name = field(row, '公司簡稱', '公司名稱', '名稱', 'CompanyName', 'Name')
                if symbol.isdigit() and 4 <= len(symbol) <= 6 and name:
                    found[symbol] = (symbol, name, market)
            LOG.info('%s: received %d entries', market, len(data))
        except Exception as exc:
            errors.append(f'{market}: {exc}')
            LOG.warning('Universe source failed: %s: %s', market, exc)
    if not found:
        raise RuntimeError('Unable to fetch stock universe; existing records preserved. ' + '; '.join(errors))
    with connect() as conn:
        with conn.cursor() as cur:
            cur.executemany('''
                INSERT INTO stock_universe(symbol, name, market)
                VALUES (%s, %s, %s)
                ON CONFLICT(symbol) DO UPDATE SET
                    name=excluded.name, market=excluded.market,
                    active=true, updated_at=now()
            ''', list(found.values()))
    LOG.info('Universe upserted: %d; source errors: %s', len(found), errors)
    return len(found)


def load_candidates(limit, offset=0):
    """Missing symbols first, then symbols whose bars were refreshed longest ago.

    loaded_at is the ingestion timestamp, unlike trade_date (market session date).
    This makes the selection fair after initial backfill, including weekends.
    """
    with connect() as conn:
        rows = conn.execute('''
            SELECT u.symbol, u.market,
                   MAX(b.trade_date) AS last_trade_date,
                   MAX(b.loaded_at) AS last_loaded_at
            FROM stock_universe u
            LEFT JOIN daily_bars b ON b.symbol = u.symbol
            WHERE u.active = true
            GROUP BY u.symbol, u.market
            ORDER BY (MAX(b.loaded_at) IS NOT NULL) ASC,
                     MAX(b.loaded_at) ASC NULLS FIRST,
                     u.symbol ASC
            LIMIT %s OFFSET %s
        ''', (limit, offset)).fetchall()
    return rows


def download_one(item, period):
    symbol = item['symbol']
    suffix = '.TW' if item['market'] == 'TWSE' else '.TWO'
    df = yf.Ticker(symbol + suffix).history(period=period, interval='1d', auto_adjust=True)
    if df.empty:
        raise ValueError('no bars')
    df = df[['Open', 'High', 'Low', 'Close', 'Volume']].dropna()
    records = []
    for day, bar in df.iterrows():
        prices = [float(bar[key]) for key in ('Open', 'High', 'Low', 'Close')]
        volume = float(bar['Volume'])
        if not all(np.isfinite(x) and x > 0 for x in prices):
            continue
        if not np.isfinite(volume) or volume < 0:
            continue
        if prices[1] < max(prices[0], prices[3]) or prices[2] > min(prices[0], prices[3]):
            continue
        if prices[1] < prices[2]:
            continue
        records.append((symbol, str(day.date()), *prices, int(volume), 'yfinance_adjusted', True))
    if not records:
        raise ValueError('no valid OHLCV')
    return symbol, records


def ingest(limit, offset, period, workers):
    candidates = load_candidates(limit, offset)
    if not candidates:
        LOG.warning('No candidates; run --mode universe first')
        return 0
    failures, succeeded = [], 0
    LOG.info('Ingestion candidates=%d, period=%s, workers=%d', len(candidates), period, workers)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        jobs = {executor.submit(download_one, item, period): item['symbol'] for item in candidates}
        for job in as_completed(jobs):
            symbol = jobs[job]
            try:
                _, rows = job.result()
                with connect() as conn:
                    with conn.cursor() as cur:
                        cur.executemany('''
                            INSERT INTO daily_bars
                                (symbol, trade_date, open, high, low, close, volume, source, adjusted)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                            ON CONFLICT(symbol, trade_date) DO UPDATE SET
                                open=excluded.open, high=excluded.high,
                                low=excluded.low, close=excluded.close,
                                volume=excluded.volume, source=excluded.source,
                                adjusted=excluded.adjusted, loaded_at=now()
                        ''', rows)
                succeeded += 1
                LOG.info('Ingested %s (%d bars); progress %d/%d', symbol, len(rows), succeeded, len(candidates))
            except Exception as exc:
                failures.append(f'{symbol}: {str(exc)[:120]}')
                LOG.warning('Ingestion failed for %s: %s', symbol, exc)
    LOG.info('Ingestion completed: success=%d/%d; failed=%d', succeeded, len(candidates), len(failures))
    if failures:
        LOG.warning('First failures: %s', failures[:15])
    if succeeded == 0:
        raise RuntimeError('No symbols successfully ingested; check yfinance/network/DB')
    return succeeded


def _float(value):
    if value is None:
        return None
    try:
        result = float(value)
        return result if np.isfinite(result) else None
    except (ValueError, TypeError, OverflowError):
        return None


def scan():
    with connect() as conn:
        universe = conn.execute('SELECT COUNT(*) AS n FROM stock_universe WHERE active=true').fetchone()['n']
        bars = conn.execute('''
            SELECT b.symbol, b.trade_date, b.open, b.high, b.low, b.close, b.volume
            FROM daily_bars b
            JOIN stock_universe u ON u.symbol=b.symbol AND u.active=true
            WHERE b.trade_date >= current_date - interval '500 days'
            ORDER BY b.symbol, b.trade_date
        ''').fetchall()
    grouped = {}
    for row in bars:
        grouped.setdefault(row['symbol'], []).append(row)
    latest = max((row['trade_date'] for row in bars), default=None)
    results, stale, insufficient, errors = [], 0, 0, 0
    for symbol, records in grouped.items():
        if len(records) < 70:
            insufficient += 1
            continue
        if latest and (latest - records[-1]['trade_date']).days > 4:
            stale += 1
            continue
        try:
            frame = pd.DataFrame(records).set_index('trade_date').rename(columns={
                'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close', 'volume': 'Volume'
            })
            frame.index = pd.to_datetime(frame.index)
            # PostgreSQL NUMERIC is returned as Decimal; convert BEFORE indicator math.
            for column in ('Open', 'High', 'Low', 'Close', 'Volume'):
                frame[column] = pd.to_numeric(frame[column].astype(float), errors='coerce')
            frame = frame.dropna(subset=['Open', 'High', 'Low', 'Close', 'Volume'])
            if len(frame) < 70:
                insufficient += 1
                continue
            metrics = summary(frame)
            close = _float(metrics.get('close'))
            if close is None:
                insufficient += 1
                continue
            ma20 = _float(metrics.get('ma20'))
            ma60 = _float(metrics.get('ma60'))
            k = _float(metrics.get('kd_k'))
            d = _float(metrics.get('kd_d'))
            macd = _float(metrics.get('macd_hist'))
            volume_ratio = _float(metrics.get('volume_ratio'))
            score, reasons = 0, []
            signals = [
                (bool(metrics.get('breakout_20d_volume_confirmed')), 35, '20日放量突破'),
                (ma20 is not None and close > ma20, 20, '高於MA20'),
                (ma60 is not None and close > ma60, 15, '高於MA60'),
                (k is not None and d is not None and k > d, 10, 'KD偏強'),
                (macd is not None and macd > 0, 10, 'MACD柱正'),
                (volume_ratio is not None and volume_ratio >= 1.5, 10, '量比≥1.5'),
            ]
            for valid, points, label in signals:
                if valid:
                    score += points
                    reasons.append(label)
            results.append((symbol, metrics['date'], score, close, volume_ratio, Jsonb(reasons)))
        except Exception as exc:
            errors += 1
            LOG.warning('Scan failed for %s: %s', symbol, exc)
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute('''
                INSERT INTO scan_runs
                    (universe_count, eligible_count, failed_count, latest_trade_date, status, notes)
                VALUES (%s,%s,%s,%s,%s,%s) RETURNING id
            ''', (
                universe, len(results), max(0, universe-len(results)), latest,
                'complete' if results else 'insufficient_data',
                f'歷史資料不足或過期；過期 {stale}，不足70根 {insufficient}，指標錯誤 {errors}。'
                '僅技術訊號排序，非當沖策略或歷史勝率。',
            ))
            run_id = cur.fetchone()['id']
            if results:
                cur.executemany('''
                    INSERT INTO scan_results
                        (run_id, symbol, trade_date, score, close, volume_ratio, reasons)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)
                ''', [(run_id, *record) for record in results])
            cur.execute('UPDATE scan_runs SET finished_at=now() WHERE id=%s', (run_id,))
    LOG.info('Scan run=%s eligible=%d/%d stale=%d insufficient=%d errors=%d',
             run_id, len(results), universe, stale, insufficient, errors)
    return len(results)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=['all', 'universe', 'ingest', 'scan'], default='all')
    parser.add_argument('--limit', type=int, default=int(os.getenv('BATCH_SIZE', '60')))
    parser.add_argument('--offset', type=int, default=0)
    parser.add_argument('--period', choices=['3mo', '6mo', '1y', '2y'], default='1y')
    parser.add_argument('--workers', type=int, default=2)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    if args.limit < 1 or args.offset < 0:
        parser.error('--limit must be positive and --offset nonnegative')
    if args.mode in ('all', 'universe'):
        fetch_universe()
    if args.mode in ('all', 'ingest'):
        ingest(args.limit, args.offset, args.period, max(1, min(args.workers, 4)))
    if args.mode in ('all', 'scan'):
        scan()


if __name__ == '__main__':
    main()
