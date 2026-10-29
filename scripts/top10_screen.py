#!/usr/bin/env python3
"""Independent research screener. Read-only PostgreSQL; no external AI claims."""
import os, json, math, datetime as dt, csv, html
from pathlib import Path
from collections import defaultdict
import psycopg2
import psycopg2.extras

OUT=Path(os.getenv('TOP10_OUTPUT','top10_output')); OUT.mkdir(parents=True,exist_ok=True)
WEIGHTS={'breakout':25,'up_volume':20,'relative_strength':20,'higher_lows':15,'ownership':20}
def finite(v):
    try: return float(v) if math.isfinite(float(v)) else None
    except (ValueError,TypeError): return None
def avg(xs): return sum(xs)/len(xs) if xs else None
def clamp(x,a,b): return max(a,min(b,x))
# Exchange industry classification. TPEx exposes numeric codes; TWSE exposes Chinese labels.
# 23 = 電子工業; 24..31 = its eight subindustries.
ELECTRONIC_CODES={f'{i:02d}' for i in range(23,32)}
EXCLUDED_INDUSTRY_CODES={'91'}  # Depositary receipts; not ordinary shares
ELECTRONIC_LABELS=('半導體','電腦及週邊','電腦週邊','光電','通信網路','通訊網路',
                   '電子零組件','電子通路','資訊服務','其他電子','電子工業','電子類')
# Industry labels are classifications, not company names. An unknown label is not guessed.
NON_ELECTRONIC_LABELS=('水泥','食品','塑膠','紡織','電機機械','電器電纜','化學',
 '生技醫療','玻璃陶瓷','造紙','鋼鐵','橡膠','汽車','建材營造','航運','觀光',
 '金融保險','貿易百貨','油電燃氣','文化創意','農業科技','電子商務',
 '綠能環保','數位雲端','運動休閒','居家生活','其他業','其他類',
 '化學工業','化學生技醫療','觀光餐旅','數位雲端業')

def classify_industry(code, label):
    """Exchange industry grouping, not a guess based on a company's name.

    TWSE sometimes stores numeric industry codes in industry_name.
    36 (digital/cloud) remains non-electronic under the official 23-31 grouping.
    """
    code = str(code).strip() if code is not None else ''
    label = str(label).strip() if label is not None else ''
    if code.lower() in ('none', 'nan', 'null'): code = ''
    if label.lower() in ('none', 'nan', 'null'): label = ''
    if not code and label.isdigit():
        code = label
    if code.isdigit():
        code = code.zfill(2)
        if code in EXCLUDED_INDUSTRY_CODES:
            return None
        # Unknown / invalid numeric codes must not silently become non-electronic.
        if not 1 <= int(code) <= 38:
            return None
        return 'electronic' if code in ELECTRONIC_CODES else 'non_electronic'
    if any(x in label for x in NON_ELECTRONIC_LABELS):
        return 'non_electronic'
    if any(x in label for x in ELECTRONIC_LABELS):
        return 'electronic'
    return None

def read_industry_overrides():
    """Optional verified classification CSV: symbol,industry_code,industry_name.
    Industry classification must be sourced by operator, not inferred from stock name.
    """
    path=os.getenv('TOP10_INDUSTRY_CSV','').strip()
    if not path:return {}
    with open(path,encoding='utf-8-sig',newline='') as f:
        rows=csv.DictReader(f)
        if not {'symbol','industry_code','industry_name'}.issubset(rows.fieldnames or []):
            raise RuntimeError('TOP10_INDUSTRY_CSV needs symbol,industry_code,industry_name')
        return {str(r['symbol']).strip():(r['industry_code'],r['industry_name']) for r in rows}

def score_stock(rows, index_map, inst_rows, symbol, name):
    # rows chronological; volume expected shares (see volume-unit check)
    if len(rows)<85:return None
    r=rows[-1]; prev=rows[:-1]; p20=prev[-20:]; p60=prev[-60:]
    close=r['close']; volume=r['volume']; today=r['trade_date']
    if close<=0 or volume<=0:return None
    ma20=avg([x['close'] for x in p20]); vol20=avg([x['volume'] for x in p20]);
    if not ma20 or not vol20:return None
    turnover=avg([x['close']*x['volume'] for x in p20])
    if turnover<30_000_000:return None
    ret5=close/rows[-6]['close']-1
    deviation=close/ma20-1
    # 'not yet surged' eligibility. Thresholds are research defaults, not optimized.
    if ret5>0.15 or deviation>0.12:return None
    prior_high=max(x['high'] for x in p60)
    prior_20_high=max(x['high'] for x in p20)
    proximity=close/prior_high
    above20=close>ma20
    breakout=clamp(10+10*(proximity-0.92)/0.08+5*int(above20),0,25)
    if close>prior_20_high:breakout=max(breakout,20)
    up=[x['volume'] for x in rows[-21:] if x['close']>x['open']]
    down=[x['volume'] for x in rows[-21:] if x['close']<x['open']]
    ud=avg(up)/avg(down) if up and down and avg(down)>0 else None
    up_volume=clamp(10+10*(ud-1),0,20) if ud is not None else None
    # Relative strength (20 points): 12 for 20-session outperformance,
    # 8 for resilience specifically on market down sessions.
    # The previous 10 + rel20*200 formula saturated at +5pp outperformance.
    old=rows[-21]; m0=index_map.get(old['trade_date']); m1=index_map.get(today)
    rel20=None; relative=None; downside_rel=None; down_sessions=0
    if m0 is not None and m1 is not None and m0>0 and m1>0 and old['close']>0:
        rel20=(close/old['close']-1)-(m1/m0-1)
        # 0pp outperformance -> 6/12; +/-15pp -> endpoints.
        trend_points=clamp(6+40*rel20,0,12)
        downside_diffs=[]
        for previous,current in zip(rows[-21:-1],rows[-20:]):
            p_idx=index_map.get(previous['trade_date'])
            c_idx=index_map.get(current['trade_date'])
            if p_idx is None or c_idx is None or p_idx<=0 or c_idx<=0 or previous['close']<=0:
                continue
            index_return=c_idx/p_idx-1
            if index_return<0:
                stock_return=current['close']/previous['close']-1
                downside_diffs.append(stock_return-index_return)
        down_sessions=len(downside_diffs)
        if down_sessions>=3:
            downside_rel=avg(downside_diffs)
            # Equal performance on down days -> 4/8; +/-2pp daily -> endpoints.
            defense_points=clamp(4+200*downside_rel,0,8)
            relative=round(trend_points+defense_points,2)
        # Do not fabricate the missing downside-resilience component.
    lows=[min(x['low'] for x in rows[-30:-20]),min(x['low'] for x in rows[-20:-10]),min(x['low'] for x in rows[-10:])]
    higher=int(lows[0]<lows[1])+int(lows[1]<lows[2])
    higher_lows=5+5*higher if close>=min(x['low'] for x in rows[-20:]) else 0
    # Never substitute institutional flow for ownership concentration.
    components={'breakout':round(breakout,2),'up_volume':round(up_volume,2) if up_volume is not None else None,'relative_strength':round(relative,2) if relative is not None else None,'higher_lows':round(higher_lows,2),'ownership':None}
    available=sum(WEIGHTS[k] for k,v in components.items() if v is not None)
    raw=sum(v for v in components.values() if v is not None)
    coverage=round(100*available/100,1)
    # Rank only on observed data; missing ownership is not assumed neutral or zero.
    normalized=round(raw/available*100,2) if available else None
    inst=inst_rows.get(symbol,[])
    inst10=inst[-10:]
    inst_total=sum(x['foreign_net_shares']+x['trust_net_shares']+x['dealer_net_shares'] for x in inst10) if len(inst10)==10 else None
    vol_ratio=volume/vol20
    burst=bool(close>prior_high and vol_ratio>=2)
    # Descriptive labels only: no score changes or additional ranking bonuses.
    if burst:
        signal='爆量突破60日前高'
    elif close>prior_high:
        signal='突破60日前高（未爆量）'
    elif proximity>=0.97 and close>ma20:
        signal='接近60日前高'
    elif above20 and vol_ratio<0.8:
        signal='量縮整理（站上20日均線）'
    else:
        signal='整理觀察'
    flags=[]
    if burst:flags.append('爆量突破60日前高（不計分）')
    if inst_total is None:flags.append('近10日法人資料不足（不計入持股集中評分）')
    flags.append('大戶持股集中度未接入，20分暫不計算')
    if rel20 is None:flags.append('大盤對齊資料不足，相對強弱分數缺失')
    elif down_sessions<3:flags.append(f'大盤下跌有效樣本僅{down_sessions}日（至少3日），相對強弱分數暫缺')
    return {'symbol':symbol,'name':name,'trade_date':str(today),'close':close,'score_observed':round(raw,2),'available_max':available,'normalized_score':normalized,'coverage_pct':coverage,'scores':components,'breakout_volume_flag':burst,'volume_ratio_20':round(vol_ratio,2),'up_day_avg_volume_21':round(avg(up),2) if up else None,'down_day_avg_volume_21':round(avg(down),2) if down else None,'up_down_volume_ratio_21':round(ud,3) if ud is not None else None,'up_day_count_21':len(up),'down_day_count_21':len(down),'signal_label':signal,'prior_high_60':round(prior_high,2),'return_5d_pct':round(ret5*100,2),'relative_20d_pct':round(rel20*100,2) if rel20 is not None else None,'market_down_days_20':down_sessions,'downside_relative_avg_pct':round(downside_rel*100,3) if downside_rel is not None else None,'institution_net_10d_shares':inst_total,'industry':'待接入官方公司資料','industry_demand_outlook':'資料不足，未作判斷','reasons':flags,'research_note':f'整理位置分數 {components["breakout"]}/25；量價 {components["up_volume"]}/20；相對大盤 {components["relative_strength"]}/20；低點結構 {components["higher_lows"]}/15。大戶持股資料缺少，不能聲稱主力正在吸籌。國際需求與未來展望須有公司公告、財報及產業來源才可產生。'}

def main():
    url=os.getenv('DATABASE_URL')
    if not url:raise RuntimeError('Missing DATABASE_URL')
    conn=psycopg2.connect(url,connect_timeout=20,sslmode='require')
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as c:
            c.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name='daily_bars'")
            cols={x['column_name'] for x in c.fetchall()}
            required={'symbol','trade_date','open','high','low','close','volume'}
            if not required.issubset(cols):raise RuntimeError(f'daily_bars columns missing: {sorted(required-cols)}; actual={sorted(cols)}')
            c.execute("SELECT trade_date, count(*) AS n FROM public.daily_bars WHERE trade_date >= CURRENT_DATE - INTERVAL '20 days' GROUP BY trade_date HAVING count(*) >= 500 ORDER BY trade_date DESC LIMIT 1")
            last=c.fetchone()
            if not last:raise RuntimeError('No recent broad market session; check daily_bars freshness')
            target=last['trade_date']
            # Use index trading sessions as validation when available, but do not assume ^TWII is fresh.
            c.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name='market_daily_bars'")
            mc={x['column_name'] for x in c.fetchall()}
            index_map={}
            if {'ticker','trade_date','close'}.issubset(mc):
                c.execute("SELECT trade_date,close FROM public.market_daily_bars WHERE ticker='^TWII' AND trade_date >= %s - INTERVAL '180 days'",(target,))
                index_map={x['trade_date']:finite(x['close']) for x in c.fetchall()}
            if target not in index_map:print('WARNING: ^TWII index not available on target session; relative strength will be missing.')
            c.execute('SELECT symbol,trade_date,open,high,low,close,volume FROM public.daily_bars WHERE trade_date BETWEEN %s - INTERVAL \'220 days\' AND %s ORDER BY symbol,trade_date',(target,target))
            raw=c.fetchall()
            c.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name='stock_universe'")
            uc={x['column_name'] for x in c.fetchall()}
            names={}; industries={}
            code_cols=('industry_code','industry_id','sector_code','industry_type_code')
            label_cols=('industry_name','industry','sector','sector_name','industry_type')
            code_col=next((k for k in code_cols if k in uc),None)
            label_col=next((k for k in label_cols if k in uc),None)
            if {'symbol','name'}.issubset(uc):
                select_cols=['symbol','name']+([code_col] if code_col else [])+([label_col] if label_col and label_col!=code_col else [])
                c.execute('SELECT '+','.join('"'+x+'"' for x in select_cols)+' FROM public.stock_universe')
                for item in c.fetchall():
                    sym=str(item['symbol']).strip(); names[sym]=item['name']
                    industries[sym]=(item[code_col] if code_col else None,item[label_col] if label_col else None)
            print(f'INDUSTRY SOURCE: stock_universe code={code_col or "missing"} label={label_col or "missing"}')
            inst=defaultdict(list)
            c.execute("SELECT to_regclass('public.v5_institution_daily') AS t")
            if c.fetchone()['t']:
                c.execute('SELECT symbol,trade_date,foreign_net_shares,trust_net_shares,dealer_net_shares FROM public.v5_institution_daily WHERE trade_date >= %s - INTERVAL \'40 days\' ORDER BY symbol,trade_date',(target,))
                for x in c.fetchall():inst[str(x['symbol'])].append(x)
    finally:conn.close()
    by=defaultdict(list)
    for x in raw:
        sym=str(x['symbol'])
        if len(sym)!=4 or not sym.isdigit():continue
        vals={k:finite(x[k]) for k in ('open','high','low','close','volume')}
        if any(v is None or v<=0 for v in vals.values()):continue
        by[sym].append({'trade_date':x['trade_date'],**vals})
    overrides=read_industry_overrides()
    results={'electronic':[],'non_electronic':[]}
    unclassified=[]
    eligible_count=0
    for sym,rows in by.items():
        if rows[-1]['trade_date']!=target:continue
        x=score_stock(rows,index_map,inst,sym,names.get(sym,sym))
        if not x:continue
        eligible_count+=1
        code,label=overrides.get(sym,industries.get(sym,(None,None)))
        group=classify_industry(code,label)
        if not group:
            unclassified.append(sym)
            continue
        resolved_code = str(code).strip() if code is not None else ''
        resolved_label = str(label).strip() if label is not None else ''
        if not resolved_code and resolved_label.isdigit():
            resolved_code = resolved_label
        x['industry_code']=resolved_code.zfill(2) if resolved_code.isdigit() else (resolved_code or None)
        x['industry']=resolved_label if resolved_label and not resolved_label.isdigit() and not resolved_label.startswith('官方產業代碼') else ('產業代碼 '+str(x['industry_code']) if x['industry_code'] else '產業名稱待補')
        x['group']=group
        results[group].append(x)
    for arr in results.values():
        arr.sort(key=lambda x:(x['normalized_score'] if x['normalized_score'] is not None else -1,x['coverage_pct']),reverse=True)
    if not results['electronic'] or not results['non_electronic']:
        raise RuntimeError('電子/非電子分類結果有一組為空。請確認 stock_universe 產業欄位，或提供 TOP10_INDUSTRY_CSV（symbol,industry_code,industry_name）。')
    top_e=results['electronic'][:10];top_n=results['non_electronic'][:10]
    limitations=['大戶持股與集中度缺少時不給分；不以法人買超代替',
      '產業需求與前景未接入來源，故不生成假AI結論','模型尚未歷史回測，排名不是上漲機率',
      '若日線成交量單位非股數，成交金額門檻須先修正','目前為研究原型，休市日需再與官方交易日曆驗證',
      '電子/非電子分類依官方產業代碼23～31或已辨識官方產業名稱；數位雲端36依官方分類屬非電子；代碼91及未知類別不進榜',
       '量價分數衡量近21日紅K與黑K平均量比，非當日量比；新增起漲標籤僅供觀察不加分',
       '相對強弱20分拆為20日超額報酬12分及大盤下跌日抗跌8分；不足3個有效下跌日則缺分；參數尚待回測']
    result={'model_version':'top20-sector-research-v0.5','as_of':str(target),
      'generated_at_utc':dt.datetime.now(dt.timezone.utc).isoformat(),
      'universe_evaluated':len(by),'eligible':eligible_count,
      'classified_eligible':sum(map(len,results.values())),
      'unclassified_eligible':len(unclassified),'unclassified_symbols':unclassified[:100],
      'group_counts':{k:len(v) for k,v in results.items()},'limitations':limitations,
      'electronic_top10':top_e,'non_electronic_top10':top_n,
      'top20':top_e+top_n}
    (OUT/'top20.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    # Keep a compatibility output for workflows expecting top10.json.
    (OUT/'top10.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    def table(items):
        lines=[]
        for i,x in enumerate(items,1):
            h=lambda v:html.escape(str(v))
            lines.append(f'<tr><td>{i}</td><td>{h(x["symbol"])} {h(x["name"])}</td><td>{h(x["industry"])}</td><td>{x["score_observed"]}/{x["available_max"]}</td><td>{x["normalized_score"]}</td><td>{h(x["signal_label"])}</td><td>{h(x["volume_ratio_20"])}</td><td>{h(x["up_down_volume_ratio_21"])}</td><td>{h(x["research_note"])}</td></tr>')
        return '<table><thead><tr><th>名次</th><th>股票</th><th>產業</th><th>已得/可得</th><th>標準化</th><th>訊號</th><th>當日量比</th><th>漲跌日均量比</th><th>評分說明</th></tr></thead><tbody>'+''.join(lines)+'</tbody></table>'
    page="""<!doctype html><html lang="zh-Hant"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>每日潛力起漲 TOP 20｜電子與非電子</title><style>body{font-family:system-ui;background:#0c192d;color:#eef5ff;padding:24px;line-height:1.6}table{border-collapse:collapse;width:100%;margin-bottom:40px}td,th{border-bottom:1px solid #345;padding:12px;text-align:left}h1,h2{color:#5eead4}.note{color:#ffdc93;line-height:1.7}th{white-space:nowrap}@media(max-width:800px){table{display:block;overflow-x:auto}}</style><h1>每日潛力起漲 TOP 20</h1><p>資料日期：DATE</p><p class="note">研究原型：大戶持股集中度、AI 產業研究尚未接入；分數不是上漲機率。起漲訊號僅標示不加分；漲跌日均量比不同於當日量比。未分類符合資格股票 UNCLASSIFIED 檔，未納入排行。</p><h2>電子股 TOP 10</h2>ETABLE<h2>非電子股 TOP 10</h2>NTABLE</html>""".replace('DATE',str(target)).replace('UNCLASSIFIED',str(len(unclassified))).replace('ETABLE',table(top_e)).replace('NTABLE',table(top_n))
    (OUT/'index.html').write_text(page,encoding='utf-8')
    print(f'DONE target={target} scanned={len(by)} eligible={eligible_count} electronic={len(results["electronic"])} non_electronic={len(results["non_electronic"])} unclassified={len(unclassified)} top_e={len(top_e)} top_n={len(top_n)} output={OUT}')
if __name__=='__main__':main()
