"""v6.7 diagnostic: strict as-of features, rolling validation and leakage controls.
Read-only Supabase query. Does not publish predictions.
"""
import os
import logging
import numpy as np
import pandas as pd
import psycopg
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, brier_score_loss

logging.basicConfig(level=logging.INFO,format="%(levelname)s %(message)s")
US=["^GSPC","^IXIC","^SOX","^DJI","^VIX","TSM"]

def build(rows):
    d=pd.DataFrame(rows)
    d["trade_date"]=pd.to_datetime(d.trade_date)
    d["close"]=pd.to_numeric(d.close,errors="coerce")
    def get(t):
        x=d[d.ticker.eq(t)].sort_values("trade_date").drop_duplicates("trade_date")
        if len(x)<700:raise ValueError(f"{t}: only {len(x)} rows")
        return x.set_index("trade_date").close
    tw=get("^TWII")
    t_ret=tw.pct_change(fill_method=None)
    out=pd.DataFrame(index=tw.index)
    out["tw_last_ret1"]=t_ret.shift(1)
    out["tw_last_ret3"]=tw.pct_change(3,fill_method=None).shift(1)
    out["tw_last_ret10"]=tw.pct_change(10,fill_method=None).shift(1)
    out["tw_last_vol10"]=t_ret.rolling(10).std().shift(1)
    out["tw_last_ma20_gap"]=(tw/tw.rolling(20).mean()-1).shift(1)
    left=pd.DataFrame({"tw_date":out.index})
    for ticker in US:
        u=get(ticker)
        right=pd.DataFrame({"us_date":u.index,
          "ret1":u.pct_change(fill_method=None).values,
          "ret3":u.pct_change(3,fill_method=None).values})
        merged=pd.merge_asof(left,right,left_on="tw_date",right_on="us_date",
                  direction="backward",allow_exact_matches=False)
        # Assertion: latest US data used for each TW session is earlier in calendar time.
        assert (merged.us_date.dropna().to_numpy() <
                merged.loc[merged.us_date.notna(),"tw_date"].to_numpy()).all()
        out[ticker+"_ret1"]=merged.ret1.to_numpy()
        out[ticker+"_ret3"]=merged.ret3.to_numpy()
        out[ticker+"_source_date"]=merged.us_date.to_numpy()
    out["target"]=(t_ret>0).astype(float)
    out.loc[t_ret.isna(),"target"]=np.nan
    return out.replace([np.inf,-np.inf],np.nan)

def score(y,p):
    return (float(accuracy_score(y,np.asarray(p)>=.5)),
            float(brier_score_loss(y,p)))

def main():
    url=os.getenv("DATABASE_URL")
    if not url:raise RuntimeError("DATABASE_URL GitHub secret missing")
    with psycopg.connect(url,row_factory=psycopg.rows.dict_row,connect_timeout=20) as c:
        rows=c.execute("""select ticker,trade_date,close
        from public.market_daily_bars
        where ticker in ('^TWII','^GSPC','^IXIC','^SOX','^DJI','^VIX','TSM')
        order by ticker,trade_date""").fetchall()
    f=build(rows)
    date_cols=[c for c in f if c.endswith("_source_date")]
    feats=[c for c in f if c not in date_cols+["target"]]
    valid=f.dropna(subset=feats+["target"]).copy()
    n=len(valid); start=max(450,int(n*.7))
    if n-start<150:raise RuntimeError("Too few OOS rows")
    print("=== DATA CHECK ===")
    print(f"raw TWII latest={f.index.max().date()}, usable={n}, OOS={n-start}")
    print(f"first OOS={valid.index[start].date()}, last={valid.index[-1].date()}")
    print("NOTE: market data source timestamps are calendar dates, not release timestamps.")
    print("NOTE: test period overlaps earlier v6.6 model development; NOT fresh holdout.")
    # Two runs: production-like one-session-ahead forecast, and negative
    # control with intentionally shuffled training labels.
    result=[]
    rng=np.random.default_rng(20261027)
    for i in range(start,n):
        tr=valid.iloc[:i]
        te=valid.iloc[i:i+1]
        y=tr.target.astype(int).to_numpy()
        x=tr[feats].to_numpy()
        xt=te[feats].to_numpy()
        model=make_pipeline(StandardScaler(),LogisticRegression(C=.1,max_iter=800))
        model.fit(x,y)
        p=float(model.predict_proba(xt)[0,1])
        # Permutation control: randomized training targets must not
        # systematically beat baseline. Expensive but deterministic.
        yp=rng.permutation(y)
        null_model=make_pipeline(StandardScaler(),LogisticRegression(C=.1,max_iter=800))
        null_model.fit(x,yp)
        pnull=float(null_model.predict_proba(xt)[0,1])
        result.append((te.index[0],int(te.target.iloc[0]),p,float(y.mean()),pnull))
    out=pd.DataFrame(result,columns=["date","actual","model","baseline","shuffled"])
    print("=== OVERALL: daily refit, no future labels ===")
    for key in ("model","baseline","shuffled"):
        a,b=score(out.actual,out[key])
        print(f"{key}: accuracy={a:.4%}, brier={b:.5f}")
    print("=== BY YEAR ===")
    for year,g in out.groupby(out.date.dt.year):
        a,b=score(g.actual,g.model)
        ba,bb=score(g.actual,g.baseline)
        print(f"{year}: n={len(g)} model_acc={a:.4%} base_acc={ba:.4%} "
              f"model_brier={b:.5f} base_brier={bb:.5f}")
    print("=== BASELINE SANITY ===")
    print(f"test up-rate={out.actual.mean():.4%}")
    print(f"always-up accuracy={out.actual.mean():.4%}")
    print("=== RECENT TWII ===")
    print(f.index[-5:].date.tolist())
    print("READ-ONLY. No rows written. Do not claim production accuracy.")
if __name__=="__main__":
    main()
