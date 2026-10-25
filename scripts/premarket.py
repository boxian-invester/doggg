"""08:45 TW research snapshot from most recent completed DB scan.
No fabricated probabilities, intraday triggers or verified win rates.
"""
import argparse
import datetime as dt
import json
import logging
from decimal import Decimal
from zoneinfo import ZoneInfo
from db import connect

LOG = logging.getLogger('premarket')
TZ = ZoneInfo('Asia/Taipei')

def make_report():
    now = dt.datetime.now(TZ)
    with connect() as conn:
        run = conn.execute('''SELECT id,finished_at,latest_trade_date,eligible_count,status
                              FROM scan_runs WHERE finished_at IS NOT NULL
                              ORDER BY id DESC LIMIT 1''').fetchone()
        if not run:
            raise RuntimeError('No completed scan run. Execute the daily refresh/scan first.')
        rows = conn.execute('''SELECT r.symbol,u.name,u.market,r.trade_date,r.score,r.close,
                                     r.volume_ratio,r.reasons
                              FROM scan_results r JOIN stock_universe u ON u.symbol=r.symbol
                              WHERE r.run_id=%s AND r.trade_date=%s
                              ORDER BY r.score DESC,r.volume_ratio DESC NULLS LAST,r.symbol
                              LIMIT 5''',(run['id'],run['latest_trade_date'])).fetchall()
        items=[]
        for r in rows:
            # Adjusted OHLCV is unsuitable for placing real-world orders. Only
            # return relative reference levels, clearly labeled non-executable.
            close=float(r['close']) if r['close'] is not None else None
            items.append({'symbol':r['symbol'],'name':r['name'],'market':r['market'],
                          'score':r['score'],'reference_adjusted_close':close,
                          'volume_ratio':float(r['volume_ratio']) if r['volume_ratio'] is not None else None,
                          'reasons':r['reasons'],'trade_date':r['trade_date'].isoformat(),
                          'entry_rule':'開盤後確認5分鐘區間突破且量能同步；盤前無法確定觸發',
                          'stop_rule':'依開盤後5分鐘區間低點設條件停損；尚無可執行價格',
                          'entry_price':None,'stop_price':None,'win_rate':None})
        report={'generated_at':now.isoformat(),'as_of_trade_date':run['latest_trade_date'].isoformat() if run['latest_trade_date'] else None,
                'scan_run_id':run['id'],'eligible_count':run['eligible_count'],
                'items':items,'next_day_up_probability':None,
                'probability_status':'尚未完成時間序列樣本外校驗，故不提供機率',
                'win_rate_status':'無分時回測及交易成本驗證，不提供當沖勝率',
                'data_warning':'Yahoo Finance 調整後日線、非即時；參考收盤價不可直接作為委託價格。'}
        with conn.cursor() as cur:
            cur.execute('''INSERT INTO premarket_reports(report_date,generated_at,scan_run_id,report)
                           VALUES (%s,%s,%s,%s::jsonb)
                           ON CONFLICT(report_date) DO UPDATE SET
                           generated_at=excluded.generated_at,scan_run_id=excluded.scan_run_id,
                           report=excluded.report''', (now.date(),now,run['id'],json.dumps(report,ensure_ascii=False)))
    LOG.info('Premarket report %s: %d candidates, scan=%s',now.date(),len(items),run['id'])
    return report

if __name__=='__main__':
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(make_report(),ensure_ascii=False,indent=2))
