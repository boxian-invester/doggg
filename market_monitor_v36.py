"""Conservative official daily adapters. Python 3.9+. Never substitute stale/mismatched dates."""
import json
import os
import re
from datetime import datetime, timedelta
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def _norm(s):
    return re.sub(r'\s+', '', str(s)).replace(',', '').replace('，', '')


def _fetch_json(url):
    req = Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'})
    with urlopen(req, timeout=15) as response:
        return json.load(response)


def _tables(payload):
    if isinstance(payload, dict):
        if isinstance(payload.get('tables'), list):
            return payload['tables']
        return [payload]
    return []


def parse_margin(payload):
    """MI_MARGN: listed financing balance, amount (千元) -> 億元.

    Fail closed unless an explicit amount unit and balance column are identifiable.
    """
    for table in _tables(payload):
        title = _norm(table.get('title', ''))
        fields = [_norm(x) for x in table.get('fields', [])]
        if '融資' not in title or '融券' not in title:
            continue
        if not ('千元' in title or '仟元' in title):
            continue
        balance_col = next((i for i, f in enumerate(fields) if ('今日餘額' in f or '當日餘額' in f or '本日餘額' in f)), None)
        if balance_col is None:
            continue
        for row in table.get('data') or []:
            if not isinstance(row, (list, tuple)) or len(row) <= balance_col:
                continue
            name = _norm(row[0])
            if name in ('融資金額', '融資(交易單位)', '融資') or name.startswith('融資金額'):
                raw = _norm(row[balance_col])
                if not re.fullmatch(r'\d+(?:\.\d+)?', raw):
                    raise ValueError('margin_balance_invalid')
                value = float(raw) / 100000.0  # 千元 / 100,000 = 億元
                if value <= 0:
                    raise ValueError('margin_balance_nonpositive')
                return round(value, 3)
    raise ValueError('margin_official_table_unrecognized')


def parse_breadth(payload):
    """MI_INDEX stocks-only counts; avoid aggregate including warrants/ETFs."""
    for table in _tables(payload):
        title = _norm(table.get('title', ''))
        fields = [_norm(x) for x in table.get('fields', [])]
        if '漲跌' not in title or not fields:
            continue
        stock_idx = next((i for i, f in enumerate(fields) if f in ('股票', '股票(不含權證)', '股票(不含ETF及權證)')), None)
        if stock_idx is None:
            continue
        found = {}
        for row in table.get('data') or []:
            if not isinstance(row, (list, tuple)) or len(row) <= stock_idx:
                continue
            label = _norm(row[0])
            raw = _norm(row[stock_idx])
            if not re.fullmatch(r'\d+', raw):
                continue
            val = int(raw)
            if label in ('上漲', '上漲家數'):
                found['up'] = val
            elif label in ('下跌', '下跌家數'):
                found['down'] = val
            elif label in ('持平', '平盤', '持平家數'):
                found['flat'] = val
            elif label in ('漲停', '漲停家數'):
                found['limit_up'] = val
            elif label in ('跌停', '跌停家數'):
                found['limit_down'] = val
        if all(k in found for k in ('up', 'down', 'flat')) and 100 <= sum(found[k] for k in ('up', 'down', 'flat')) <= 5000:
            return found
    raise ValueError('breadth_stocks_only_table_unrecognized')


def parse_vix_txt(content, expected_date=None):
    """Parse TAIFEX intraday TXT. Require official Last 1 min AVG row."""
    records = []
    avg = None
    for line in content.splitlines():
        cells = line.strip().split()
        if len(cells) < 3 or not re.fullmatch(r'20\d{6}', cells[0]):
            continue
        date = datetime.strptime(cells[0], '%Y%m%d').date()
        if expected_date and date != expected_date:
            raise ValueError('vix_date_mismatch')
        if len(cells) >= 5 and cells[1:5] == ['Last', '1', 'min', 'AVG']:
            avg = (date, float(cells[-1]))
        elif re.fullmatch(r'\d{7,8}', cells[1]):
            records.append((date, cells[1], float(cells[-1])))
    if not records or not avg or avg[0] != records[-1][0]:
        raise ValueError('vix_last_minute_average_missing')
    if not 0 < avg[1] < 200 or not all(0 < r[2] < 200 for r in records):
        raise ValueError('vix_out_of_range')
    return {'date': avg[0].isoformat(), 'value': round(avg[1], 3), 'first': records[0][2], 'last': records[-1][2], 'count': len(records)}


def _day_json(endpoint, day):
    url = 'https://www.twse.com.tw/exchangeReport/' + endpoint + '?' + urlencode({
        'response': 'json', 'date': day.strftime('%Y%m%d'), 'selectType': 'ALL'
    })
    return _fetch_json(url)


def update_official_v36(metrics, now, missing):
    """Independent fetches; an unavailable metric cannot erase the other two."""
    tasks = [('margin', 'MI_MARGN', parse_margin), ('breadth', 'MI_INDEX', parse_breadth)]
    for key, endpoint, parser in tasks:
        last_error = 'no_official_data'
        for offset in range(8):
            day = (now - timedelta(days=offset)).date()
            if day.weekday() >= 5:
                continue
            try:
                payload = _day_json(endpoint, day)
                # TWSE returns its actual date in some responses; reject explicit mismatches.
                if str(payload.get('stat', '')).upper() != 'OK':
                    continue
                value = parser(payload)
                if key == 'margin':
                    metrics[key] = {'label': '上市融資餘額', 'value': value, 'unit': '億元', 'date': day.isoformat(), 'status': 'ok', 'source': 'TWSE MI_MARGN', 'frequency': 'official_after_close', 'note': '僅上市；依官方融資金額欄位（千元）換算。'}
                else:
                    metrics[key] = {'label': '上市股票市場廣度', 'value': value['up'] - value['down'], 'unit': '家', 'date': day.isoformat(), 'status': 'ok', 'source': 'TWSE MI_INDEX', 'frequency': 'official_after_close', **value, 'note': '僅上市股票；value 為上漲減下跌家數。'}
                break
            except Exception as exc:
                last_error = type(exc).__name__ + ': ' + str(exc)[:70]
        else:
            metrics[key] = missing(key, 'unavailable', '官方表格未能安全識別：' + last_error)
    # This URL must point to an OFFICIAL TAIFEX daily TXT; never guess its download route.
    # Example template may contain {date} (YYYYMMDD). Set as GitHub Actions secret/env.
    template = os.getenv('TAIFEX_VIX_TXT_URL_TEMPLATE', '').strip()
    if not template or not template.startswith('https://'):
        metrics['tw_vix'] = missing('tw_vix', 'not_connected', '尚未設定經驗證的期交所 TXT 自動下載網址 TAIFEX_VIX_TXT_URL_TEMPLATE')
        return
    last_error = 'no_data'
    for offset in range(8):
        day = (now - timedelta(days=offset)).date()
        if day.weekday() >= 5:
            continue
        try:
            url = template.replace('{date}', day.strftime('%Y%m%d'))
            if not url.startswith('https://www.taifex.com.tw/'):
                raise ValueError('vix_url_not_official')
            req = Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urlopen(req, timeout=15) as response:
                raw = response.read(400000)
                try:
                    body = raw.decode('utf-8-sig')
                except UnicodeDecodeError:
                    body = raw.decode('cp950')
            vix = parse_vix_txt(body, expected_date=day)
            metrics['tw_vix'] = {'label': '台灣VIX（最後一分鐘平均）', 'value': vix['value'], 'date': vix['date'], 'status': 'ok', 'source': 'TAIFEX official intraday TXT', 'frequency': 'official_after_close', 'note': '採官方 Last 1 min AVG；不等同盤中即時數值。'}
            break
        except Exception as exc:
            last_error = type(exc).__name__ + ': ' + str(exc)[:70]
    else:
        metrics['tw_vix'] = missing('tw_vix', 'unavailable', '官方 VIX TXT 抓取或日期驗證失敗：' + last_error)
