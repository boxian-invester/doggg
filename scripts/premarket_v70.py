"""Manual research-only premarket prediction. No schedule until calendar/freshness verified."""
import os
from datetime import date,datetime,time,timedelta
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd
import psycopg
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import LogisticRegression

US=['^GSPC','^IXIC','^SOX','^DJI','^VIX','TSM']
MODEL='v7.0-logistic-frozen-spec'

def main():
    now=datetime.now(ZoneInfo('Asia/Taipei'))
    target=date.fromisoformat(os.environ['TARGET_DATE'])
    # Manual execution requires target date to be confirmed by the operator.
    # Weekday check is not a Taiwan exchange holiday calendar.
    if target!=now.date() or target.weekday()>=5 or not(time(8,0)<=now.time()<time(8,50)):
        raise RuntimeError('Run only on TARGET_DATE, Taiwan weekday 08:00-08:49. Confirm TWSE open day manually.')
    url=os.environ['DATABASE_URL']
    with psycopg.connect(url,row_factory=psycopg.rows.dict_row,connect_timeout=20) as conn:
        rows=conn.execute("""SELECT ticker,trade_date,close FROM public.market_daily_bars
           WHERE ticker IN ('^TWII','^GSPC','^IXIC','^SOX','^DJI','^VIX','TSM')
           ORDER BY ticker,trade_date""").fetchall()
    df=pd.DataFrame(rows)
    df['trade_date']=pd.to_datetime(df.trade_date)
    df['close']=pd.to_numeric(df.close,errors='coerce')
    def series(t):
        z=df[df.ticker.eq(t)].dropna(subset=['close']).sort_values('trade_date').drop_duplicates('trade_date')
        if len(z)<700:raise RuntimeError('Not enough history: '+t)
        return z.set_index('trade_date').close
    tw=series('^TWII')
    last_tw=tw.index.max().date()
    if not (last_tw<target and target-last_tw<=timedelta(days=7)):
        raise RuntimeError(f'TWII stale/future: {last_tw}')
    last_us={t:series(t).index.max().date() for t in US}
    if any(not (last_us[t]<target and target-last_us[t]<=timedelta(days=4)) for t in US):
        raise RuntimeError(f'US data stale/future: {last_us}')
    # Conservative availability alignment for historical Taiwan sessions.
    ret=tw.pct_change(fill_method=None)
    X=pd.DataFrame(index=tw.index)
    X['tw_last_ret1']=ret.shift(1)
    X['tw_last_ret3']=tw.pct_change(3,fill_method=None).shift(1)
    X['tw_last_ret10']=tw.pct_change(10,fill_method=None).shift(1)
    X['tw_last_vol10']=ret.rolling(10).std().shift(1)
    X['tw_last_ma20_gap']=(tw/tw.rolling(20).mean()-1).shift(1)
    latest={
      'tw_last_ret1':float(ret.iloc[-1]),
      'tw_last_ret3':float(tw.pct_change(3,fill_method=None).iloc[-1]),
      'tw_last_ret10':float(tw.pct_change(10,fill_method=None).iloc[-1]),
      'tw_last_vol10':float(ret.rolling(10).std().iloc[-1]),
      'tw_last_ma20_gap':float((tw/tw.rolling(20).mean()-1).iloc[-1])}
    for t in US:
        u=series(t)
        uret=u.pct_change(fill_method=None)
        m=pd.merge_asof(pd.DataFrame({'tw_date':X.index}),
             pd.DataFrame({'us_date':u.index,'r1':uret.values,
                           'r3':u.pct_change(3,fill_method=None).values}),
             left_on='tw_date',right_on='us_date',direction='backward',allow_exact_matches=False)
        X[t+'_ret1']=m.r1.to_numpy()
        X[t+'_ret3']=m.r3.to_numpy()
        latest[t+'_ret1']=float(uret.iloc[-1])
        latest[t+'_ret3']=float(u.pct_change(3,fill_method=None).iloc[-1])
    X['target']=(ret>0).astype(float)
    X.loc[ret.isna(),'target']=np.nan
    X=X.replace([np.inf,-np.inf],np.nan).dropna()
    cols=[c for c in X if c!='target']
    # Train only using historical sessions that already have known closes.
    train=X[X.index.date<target]
    if len(train)<700:raise RuntimeError('Insufficient training history')
    new=pd.DataFrame([latest],columns=cols)
    if not np.isfinite(new.to_numpy(dtype=float)).all():raise RuntimeError('Non-finite latest features')
    model=make_pipeline(StandardScaler(),LogisticRegression(C=.1,max_iter=800))
    model.fit(train[cols],train.target.astype(int))
    p=float(model.predict_proba(new)[0,1])
    print(f'Research-only forecast {target}: up_probability={p:.4f}; TWII={last_tw}; US={last_us}; n={len(train)}')
    if os.environ.get('SAVE_PREDICTION','false').lower()!='true':
        print('DRY RUN: no database write. Set SAVE_PREDICTION=true only after checking dates.')
        return
    with psycopg.connect(url,connect_timeout=20) as conn:
        conn.execute("""INSERT INTO public.premarket_predictions
          (target_date,model_version,probability_up,twii_source_date,us_source_date,training_samples)
          VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (target_date,model_version) DO NOTHING""",
          (target,MODEL,p,last_tw,max(last_us.values()),len(train)))
    print('Insert attempted; existing predictions are never overwritten.')
if __name__=='__main__':main()
