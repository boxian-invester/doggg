"""v6.8 research audit: probability calibration, confidence bins, permutation controls.
READ ONLY: no live forecast, no writes to database.
"""
import os
import numpy as np
import pandas as pd
import psycopg
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score,brier_score_loss,log_loss

US=["^GSPC","^IXIC","^SOX","^DJI","^VIX","TSM"]
def dataset(rows):
    d=pd.DataFrame(rows)
    d["trade_date"]=pd.to_datetime(d["trade_date"])
    d["close"]=pd.to_numeric(d["close"],errors="coerce")
    def series(t):
        a=d[d.ticker.eq(t)].sort_values("trade_date").drop_duplicates("trade_date")
        if len(a)<700:raise RuntimeError(f"{t}: only {len(a)} prices")
        return a.set_index("trade_date").close
    tw=series("^TWII")
    ret=tw.pct_change(fill_method=None)
    x=pd.DataFrame(index=tw.index)
    x["tw_prev_ret1"]=ret.shift(1)
    x["tw_prev_ret3"]=tw.pct_change(3,fill_method=None).shift(1)
    x["tw_prev_ret10"]=tw.pct_change(10,fill_method=None).shift(1)
    x["tw_prev_vol10"]=ret.rolling(10).std().shift(1)
    x["tw_prev_ma20gap"]=(tw/tw.rolling(20).mean()-1).shift(1)
    for ticker in US:
        s=series(ticker)
        us=pd.DataFrame({"us_date":s.index,
            "ret1":s.pct_change(fill_method=None).values,
            "ret3":s.pct_change(3,fill_method=None).values})
        aligned=pd.merge_asof(pd.DataFrame({"tw_date":x.index}),us,
                left_on="tw_date",right_on="us_date",
                direction="backward",allow_exact_matches=False)
        assert (aligned.us_date.dropna() <
                aligned.loc[aligned.us_date.notna(),"tw_date"]).all()
        x[ticker+"_ret1"]=aligned.ret1.to_numpy()
        x[ticker+"_ret3"]=aligned.ret3.to_numpy()
    x["target"]=(ret>0).astype(float)
    x.loc[ret.isna(),"target"]=np.nan
    return x.replace([np.inf,-np.inf],np.nan).dropna()

def fit(x,y):
    m=make_pipeline(StandardScaler(),LogisticRegression(C=.1,max_iter=800))
    m.fit(x,y)
    return m

def main():
    url=os.getenv("DATABASE_URL")
    if not url:raise RuntimeError("DATABASE_URL secret is missing")
    with psycopg.connect(url,row_factory=psycopg.rows.dict_row,connect_timeout=20) as c:
        rows=c.execute("""select ticker,trade_date,close from public.market_daily_bars
            where ticker in ('^TWII','^GSPC','^IXIC','^SOX','^DJI','^VIX','TSM')
            order by ticker,trade_date""").fetchall()
    d=dataset(rows)
    feats=[c for c in d if c!="target"]
    n=len(d); start=max(450,int(n*.7))
    if n-start<150:raise RuntimeError("Too few OOS days")
    # Same test span as earlier versions: exploratory, NOT untouched holdout.
    out=[]
    rng=np.random.default_rng(20261027)
    # Refit every 20 sessions; training ends before each forecast block.
    # 20 shuffled-label controls with independent random permutations.
    for i in range(start,n,20):
        tr=d.iloc[:i]; te=d.iloc[i:min(i+20,n)]
        xt=te[feats].to_numpy(dtype=float)
        X=tr[feats].to_numpy(dtype=float)
        y=tr.target.to_numpy(dtype=int)
        pred=fit(X,y).predict_proba(xt)[:,1]
        nulls=[]
        for _ in range(20):
            nulls.append(fit(X,rng.permutation(y)).predict_proba(xt)[:,1])
        nulls=np.array(nulls)
        for j,(day,row) in enumerate(te.iterrows()):
            out.append([day.date(),int(row.target),float(pred[j]),
                float(y.mean()),*nulls[:,j].tolist()])
    columns=["date","actual","model","baseline"]+[f"null_{j}" for j in range(20)]
    o=pd.DataFrame(out,columns=columns)
    def metrics(p):
        return (accuracy_score(o.actual,np.asarray(p)>=.5),
                brier_score_loss(o.actual,p))
    print("=== V6.8 RESEARCH AUDIT ===")
    print(f"n={n} OOS={len(o)} dates={o.date.min()} to {o.date.max()}")
    for name in ("model","baseline"):
        a,b=metrics(o[name])
        print(f"{name}: accuracy={a:.4%} brier={b:.5f}")
    print("=== CONFIDENCE BINS ===")
    edges=[0,.4,.5,.6,.7,.8,1.000001]
    o["bin"]=pd.cut(o.model,edges,right=False)
    for bucket,g in o.groupby("bin",observed=True):
        print(f"{bucket}: n={len(g)} avg_prediction={g.model.mean():.3f}"
              f" realized_up={g.actual.mean():.3f}"
              f" accuracy={accuracy_score(g.actual,g.model>=.5):.3f}")
    print("=== PERMUTED LABEL NEGATIVE CONTROLS ===")
    scores=[metrics(o[f"null_{j}"]) for j in range(20)]
    print(f"20 shuffled controls: accuracy min={min(v[0] for v in scores):.4%}"
          f" median={np.median([v[0] for v in scores]):.4%}"
          f" max={max(v[0] for v in scores):.4%}")
    print(f"20 shuffled controls: brier min={min(v[1] for v in scores):.5f}"
          f" median={np.median([v[1] for v in scores]):.5f}"
          f" max={max(v[1] for v in scores):.5f}")
    print("=== BY YEAR ===")
    for year,g in o.groupby(pd.to_datetime(o.date).dt.year):
        print(f"{year}: n={len(g)} accuracy={accuracy_score(g.actual,g.model>=.5):.4%}"
              f" brier={brier_score_loss(g.actual,g.model):.5f}")
    print("LIMITATIONS: same historical OOS reused for model design; no future live")
    print("validation; yfinance release timestamps and Taiwan holiday schedule not verified.")
    print("READ ONLY: no rows saved and no production prediction issued.")
if __name__=="__main__":main()
