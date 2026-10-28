#!/usr/bin/env python3
"""Independent research screener. Read-only PostgreSQL; no external AI claims."""
import os, json, math, datetime as dt
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
    # compare market on matching dates only, avoiding assumed calendar continuity
    old=rows[-21]; m0=index_map.get(old['trade_date']); m1=index_map.get(today)
    if m0 and m1 and m0>0:
        rel20=(close/old['close']-1)-(m1/m0-1)
        relative=clamp(10+rel20*200,0,20)
    else:relative=None;rel20=None
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
    flags=[]
    if burst:flags.append('爆量突破60日前高（不計分）')
    if inst_total is None:flags.append('近10日法人資料不足（不計入持股集中評分）')
    flags.append('大戶持股集中度未接入，20分暫不計算')
    if rel20 is None:flags.append('大盤對齊資料不足，抗跌分數缺失')
    return {'symbol':symbol,'name':name,'trade_date':str(today),'close':close,'score_observed':round(raw,2),'available_max':available,'normalized_score':normalized,'coverage_pct':coverage,'scores':components,'breakout_volume_flag':burst,'volume_ratio_20':round(vol_ratio,2),'prior_high_60':round(prior_high,2),'return_5d_pct':round(ret5*100,2),'relative_20d_pct':round(rel20*100,2) if rel20 is not None else None,'institution_net_10d_shares':inst_total,'industry':'待接入官方公司資料','industry_demand_outlook':'資料不足，未作判斷','reasons':flags,'research_note':f'整理位置分數 {components["breakout"]}/25；量價 {components["up_volume"]}/20；相對大盤 {components["relative_strength"]}/20；低點結構 {components["higher_lows"]}/15。大戶持股資料缺少，不能聲稱主力正在吸籌。國際需求與未來展望須有公司公告、財報及產業來源才可產生。'}

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
            names={}
            if {'symbol','name'}.issubset(uc):
                c.execute('SELECT symbol,name FROM public.stock_universe');names={str(x['symbol']):x['name'] for x in c.fetchall()}
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
    results=[]
    for sym,rows in by.items():
        if rows[-1]['trade_date']!=target:continue
        x=score_stock(rows,index_map,inst,sym,names.get(sym,sym))
        if x:results.append(x)
    results.sort(key=lambda x:(x['normalized_score'] or -1,x['coverage_pct']),reverse=True)
    result={'model_version':'top10-research-v0.1','as_of':str(target),'generated_at_utc':dt.datetime.now(dt.timezone.utc).isoformat(),'universe_evaluated':len(by),'eligible':len(results),'limitations':['大戶持股與集中度缺少時不給分；不以法人買超代替','產業需求與前景未接入來源，故不生成假AI結論','模型尚未歷史回測，排名不是上漲機率','若日線成交量單位非股數，成交金額門檻須先修正','目前為研究原型，休市日需再與官方交易日曆驗證'],'top10':results[:10]}
    (OUT/'top10.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    import html
    trs=[]
    for i,x in enumerate(results[:10],1):
        safe=lambda v:html.escape(str(v))
        trs.append(f'<tr><td>{i}</td><td>{safe(x["symbol"])} {safe(x["name"])}</td><td>{x["score_observed"]}/{x["available_max"]}</td><td>{x["normalized_score"]}</td><td>{"是" if x["breakout_volume_flag"] else "否"}</td><td>{safe(x["research_note"])}</td></tr>')
    page='''<!doctype html><html lang="zh-Hant"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>每日潛力起漲 TOP 10｜研究原型</title><style>body{font-family:system-ui;background:#0c192d;color:#eef5ff;padding:24px}table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #345;padding:12px;text-align:left}h1{color:#5eead4}.note{color:#ffdc93;line-height:1.7}</style><h1>每日潛力起漲 TOP 10</h1><p>資料日期：DATE</p><p class="note">研究原型：大戶持股集中度、AI 產業研究尚未接入；已得分/可得分不代表完整100分，無法證明未來漲幅。爆量突破僅標示不加分。</p><table><thead><tr><th>名次</th><th>股票</th><th>已得/可得</th><th>可得分標準化</th><th>爆量突破</th><th>評分說明</th></tr></thead><tbody>ROWS</tbody></table></html>'''.replace('DATE',str(target)).replace('ROWS',''.join(trs))
    (OUT/'index.html').write_text(page,encoding='utf-8')
    print(f'DONE target={target} scanned={len(by)} eligible={len(results)} top={len(results[:10])} output={OUT}')
if __name__=='__main__':main()
