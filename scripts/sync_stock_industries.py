#!/usr/bin/env python3
"""Sync TWSE/TPEx official industry classification into existing stock_universe."""
import json, os, urllib.request, time
import psycopg2
from psycopg2.extras import execute_values

SOURCES = {
    'TWSE': 'https://openapi.twse.com.tw/v1/opendata/t187ap03_L',
    'TPEx': 'https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O',
}
SYMBOL_KEYS = ['公司代號','公司代碼','股票代號','證券代號','SecuritiesCompanyCode','Code']
NAME_KEYS = ['產業別','產業類別','產業名稱','產業','Industry','IndustryName']
CODE_KEYS = ['產業代號','產業代碼','產業別代碼','產業類別代碼','IndustryCode']

def pick(row, keys):
    for key in keys:
        val = row.get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
    return None

def fetch(market, url):
    req = urllib.request.Request(url, headers={'User-Agent':'Mozilla/5.0 TaiwanIndustryResearch/0.1','Accept':'application/json'})
    with urllib.request.urlopen(req, timeout=35) as resp:
        payload=json.load(resp)
    if isinstance(payload,dict):
        for key in ('data','result','results'):
            if isinstance(payload.get(key),list):payload=payload[key];break
    if not isinstance(payload,list) or not payload or not isinstance(payload[0],dict):
        raise RuntimeError(f'{market}: Unexpected response shape')
    print(f'{market}: received {len(payload)} rows; sample keys={list(payload[0])}',flush=True)
    result=[]
    for row in payload:
        sym=pick(row,SYMBOL_KEYS)
        industry=pick(row,NAME_KEYS)
        code=pick(row,CODE_KEYS)
        if sym and len(sym)==4 and sym.isdigit() and industry:
            # Do not infer numeric codes from industry names.
            result.append((sym,code,industry,market))
    if len(result)<100:
        raise RuntimeError(f'{market}: Only {len(result)} classified rows; check API field names and availability')
    return result

def main():
    url=os.environ.get('DATABASE_URL')
    if not url:raise RuntimeError('DATABASE_URL secret is missing')
    all_rows=[]
    for market,endpoint in SOURCES.items():
        rows=fetch(market,endpoint)
        print(f'{market}: usable classified rows={len(rows)}',flush=True)
        all_rows.extend(rows)
        time.sleep(0.5)
    conn=psycopg2.connect(url,sslmode='require',connect_timeout=20)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name='stock_universe'")
                cols={r[0] for r in cur.fetchall()}
                required={'symbol','market','industry_code','industry_name'}
                if not required.issubset(cols):raise RuntimeError(f'Missing stock_universe columns: {required-cols}')
                cur.execute('CREATE TEMP TABLE industry_stage(symbol text, industry_code text, industry_name text, market text) ON COMMIT DROP')
                execute_values(cur,'INSERT INTO industry_stage(symbol,industry_code,industry_name,market) VALUES %s',all_rows,page_size=500)
                # Match both stock code and exchange to prevent accidental cross-market updates.
                cur.execute('''UPDATE public.stock_universe u
                  SET industry_code=s.industry_code,industry_name=s.industry_name,updated_at=NOW()
                  FROM industry_stage s
                  WHERE u.symbol=s.symbol AND UPPER(u.market)=UPPER(s.market)
                    AND (u.industry_code IS DISTINCT FROM s.industry_code OR u.industry_name IS DISTINCT FROM s.industry_name)''')
                changed=cur.rowcount
                cur.execute('''SELECT market,COUNT(*) total,COUNT(*) FILTER (WHERE industry_name IS NOT NULL AND btrim(industry_name)<>'') classified
                               FROM public.stock_universe GROUP BY market ORDER BY market''')
                coverage=cur.fetchall()
                print(f'UPDATED rows={changed}; coverage={coverage}',flush=True)
                # Ensure both exchanges have meaningful coverage before committing.
                for market,total,classified in coverage:
                    if market.upper() in ('TWSE','TPEX','TPEx'.upper()) and total>=100 and classified/total<0.60:
                        raise RuntimeError(f'{market} classified only {classified}/{total}; rolling back')
    finally:conn.close()
if __name__=='__main__':main()
