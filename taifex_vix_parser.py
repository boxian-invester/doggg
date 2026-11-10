"""Parse TAIFEX monthly Taiwan VIX closing TXT (Big5/CP950).
Python 3.9 compatible; standard library only.
"""
from datetime import datetime
from pathlib import Path
import re
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

URL_TEMPLATE = 'https://www.taifex.com.tw/file/taifex/Dailydownload/vix/log2data/{yearmonth}new.txt'
LINE = re.compile(r'^\s*(\d{8})\s+(\d{8})\s+([0-9]+(?:\.[0-9]+)?)\s+([0-9]+(?:\.[0-9]+)?)\s*$')


def parse_vix_txt(raw):
    """Return latest valid row with official VIX and 1-min average separately."""
    if isinstance(raw, bytes):
        raw = raw.decode('cp950')
    records = []
    for line in raw.splitlines():
        match = LINE.match(line)
        if not match:
            continue
        day, hhmmsscc, vix, avg = match.groups()
        datetime.strptime(day, '%Y%m%d')
        records.append({
            'date': '{}-{}-{}'.format(day[:4], day[4:6], day[6:]),
            'time': hhmmsscc,
            'value': float(vix),
            'close_1min_avg': float(avg),
        })
    if not records:
        raise ValueError('No valid TAIFEX VIX rows found')
    return max(records, key=lambda item: (item['date'], item['time']))


def fetch_latest_vix(now=None):
    """Download current month's file; fallback to prior month at month rollover."""
    now = now or datetime.now(ZoneInfo('Asia/Taipei'))
    months = [now.strftime('%Y%m')]
    previous_month_last_day = now.replace(day=1).date().toordinal() - 1
    from datetime import date
    months.append(date.fromordinal(previous_month_last_day).strftime('%Y%m'))
    errors = []
    for ym in months:
        url = URL_TEMPLATE.format(yearmonth=ym)
        try:
            request = Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urlopen(request, timeout=20) as response:
                raw = response.read()
            result = parse_vix_txt(raw)
            if result['date'].replace('-', '') > now.strftime('%Y%m%d'):
                raise ValueError('Future-dated row')
            result.update({'source': 'TAIFEX monthly closing VIX TXT', 'source_url': url, 'status': 'ok', 'frequency': 'official_after_close'})
            return result
        except Exception as exc:
            errors.append('{}: {}'.format(ym, exc))
    raise RuntimeError('TAIFEX VIX fetch failed: ' + '; '.join(errors))


if __name__ == '__main__':
    import json
    import sys
    result = parse_vix_txt(Path(sys.argv[1]).read_bytes()) if len(sys.argv) > 1 else fetch_latest_vix()
    print(json.dumps(result, ensure_ascii=False, indent=2))
