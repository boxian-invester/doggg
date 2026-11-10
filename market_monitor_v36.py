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
    """TWSE MI_MARGN: listed financing amount, 仟元 -> 億元."""
    for table in _tables(payload):
        title = _norm(table.get('title', ''))
        fields = [_norm(x) for x in table.get('fields', [])]
        if '信用交易統計' not in title or '項目' not in fields:
            continue
        item_col = fields.index('項目')
        balance_col = next((i for i, f in enumerate(fields) if f in ('今日餘額', '當日餘額', '本日餘額')), None)
        previous_col = next((i for i, f in enumerate(fields) if f == '前日餘額'), None)
        if balance_col is None:
            continue
        for row in table.get('data') or []:
            if not isinstance(row, (list, tuple)) or len(row) <= balance_col:
                continue
            name = _norm(row[item_col])
            # Explicit unit on the ROW, not the table title. Never use 交易單位.
            if not re.fullmatch(r'融資金額\((?:仟元|千元)\)', name):
                continue
            raw = _norm(row[balance_col])
            if not re.fullmatch(r'\d+(?:\.\d+)?', raw):
                raise ValueError('margin_balance_invalid')
            value = float(raw) / 100000.0
            if value <= 0:
                raise ValueError('margin_balance_nonpositive')
            # Preserve old return type (float) for compatibility with market_monitor.py.
            return round(value, 3)
    raise ValueError('margin_official_table_unrecognized')


def parse_breadth(payload):
    """TWSE MI_INDEX: stocks-only up/down with embedded limit counts."""
    for table in _tables(payload):
        title = _norm(table.get('title', ''))
        fields = [_norm(x) for x in table.get('fields', [])]
        if title != '漲跌證券數合計' or '股票' not in fields or '類型' not in fields:
            continue
        stock_idx = fields.index('股票')
        label_idx = fields.index('類型')
        found = {}
        for row in table.get('data') or []:
            if not isinstance(row, (list, tuple)) or len(row) <= max(stock_idx, label_idx):
                continue
            label = _norm(row[label_idx])
            raw = _norm(row[stock_idx])
            if label.startswith('上漲'):
                match = re.fullmatch(r'(\d+)\((\d+)\)', raw) if '漲停' in label else re.fullmatch(r'(\d+)', raw)
                if match:
                    found['up'] = int(match.group(1))
                    if '漲停' in label:
                        found['limit_up'] = int(match.group(2))
            elif label.startswith('下跌'):
                match = re.fullmatch(r'(\d+)\((\d+)\)', raw) if '跌停' in label else re.fullmatch(r'(\d+)', raw)
                if match:
                    found['down'] = int(match.group(1))
                    if '跌停' in label:
                        found['limit_down'] = int(match.group(2))
            elif label in ('持平', '平盤') and re.fullmatch(r'\d+', raw):
                found['flat'] = int(raw)
        if all(k in found for k in ('up', 'down', 'flat')) and 100 <= sum(found[k] for k in ('up', 'down', 'flat')) <= 5000:
            if found.get('limit_up', 0) > found['up'] or found.get('limit_down', 0) > found['down']:
                raise ValueError('breadth_limit_counts_invalid')
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
        'response': 'json', 'date': day.strftime('%Y%m%d'), 'selectType': 'MS' if endpoint == 'MI_MARGN' else 'ALL', 'type': 'ALLBUT0999' if endpoint == 'MI_INDEX' else 'ALL'
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
                # Do not label a previous session as the requested day.
                if payload.get('date') and str(payload['date']) != day.strftime('%Y%m%d'):
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
    # TAIFEX publishes a monthly closing VIX TXT (YYYYMMnew.txt).
    # Keep the official VIX and the 1-minute average as separate fields.
    try:
        from taifex_vix_parser import fetch_latest_vix
        vix = fetch_latest_vix(now=now)
        metrics['tw_vix'] = {
            'label': '台灣VIX（臺指選擇權波動率指數）',
            'value': vix['value'],
            'close_1min_avg': vix['close_1min_avg'],
            'date': vix['date'],
            'status': 'ok',
            'source': 'TAIFEX monthly closing VIX TXT',
            'source_url': vix['source_url'],
            'frequency': 'official_after_close',
            'note': 'value 為官方臺指選擇權波動率指數；close_1min_avg 為收盤前1分鐘平均指數。非即時數據。',
        }
    except Exception as exc:
        metrics['tw_vix'] = missing(
            'tw_vix', 'unavailable',
            'TAIFEX 月份 VIX TXT 下載或解析失敗：{}: {}'.format(type(exc).__name__, str(exc)[:120])
        )
