"""v6.6 premarket point-in-time walk-forward research, never a live signal."""
import os, logging
import numpy as np
import pandas as pd
import psycopg
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score,brier_score_loss

logging.basicConfig(level=logging.INFO,format="%(levelname)s %(message)s")
US=["^GSPC","^IXIC","^SOX","^DJI","^VIX","TSM"]

def build(rows):
    df=pd.DataFrame(rows)
    df["trade_date"]=pd.to_datetime(df["trade_date"])
    df["close"]=pd.to_numeric(df["close"],errors="coerce")
    def s(t):
        z=df[df.ticker.eq(t)].sort_values("trade_date").drop_duplicates("trade_date")
        if len(z)<700: raise RuntimeError(f"Insufficient history for {t}: {len(z)}")
        return z.set_index("trade_date")["close"]
    tw=s("^TWII")
    # Each row is the Taiwan session to be predicted BEFORE its open.
    # Taiwan features are shifted one session: latest known Taiwan close.
    r=tw.pct_change(fill_method=None)
    X=pd.DataFrame(index=tw.index)
    X["tw_last_ret1"]=r.shift(1)
    X["tw_last_ret3"]=tw.pct_change(3,fill_method=None).shift(1)
    X["tw_last_ret10"]=tw.pct_change(10,fill_method=None).shift(1)
    X["tw_last_vol10"]=r.rolling(10).std().shift(1)
    X["tw_last_ma20_gap"]=(tw/tw.rolling(20).mean()-1).shift(1)
    left=pd.DataFrame({"tw_date":X.index})
    for ticker in US:
        u=s(ticker)
        right=pd.DataFrame({
            "us_date":u.index,
            "ret1":u.pct_change(fill_method=None).values,
            "ret3":u.pct_change(3,fill_method=None).values,
        })
        # US close dated strictly earlier than Taiwan target session.
        # Calendar-only conservative convention; not intraday release proof.
        m=pd.merge_asof(left,right,left_on="tw_date",right_on="us_date",
                        direction="backward",allow_exact_matches=False)
        X[ticker+"_ret1"]=m["ret1"].to_numpy()
        X[ticker+"_ret3"]=m["ret3"].to_numpy()
    # Target: Taiwan session T close > preceding Taiwan session close.
    X["target"]=(r>0).astype(float)
    X.loc[r.isna(),"target"]=np.nan
    return X.replace([np.inf,-np.inf],np.nan).dropna()

def evaluate(data):
    cols=[c for c in data.columns if c!="target"]
    n=len(data)
    # A fixed final 30% chronological OOS, no random shuffle.
    start=max(450,int(n*.7))
    if n-start<150:raise RuntimeError("Not enough out-of-sample sessions")
    result=[]
    # Refit once per 20-session block. The last training session is
    # immediately before the test block: its close was known at test open.
    for block in range(start,n,20):
        train=data.iloc[:block]
        test=data.iloc[block:min(n,block+20)]
        y=train.target.astype(int).to_numpy()
        model=make_pipeline(StandardScaler(),
            LogisticRegression(C=.1,max_iter=800))
        model.fit(train[cols],y)
        pred=model.predict_proba(test[cols])[:,1]
        for date,actual,prob in zip(test.index,test.target.astype(int),pred):
            result.append((date.date(),int(actual),float(prob),float(y.mean())))
    out=pd.DataFrame(result,columns=["date","actual","model_p","baseline_p"])
    def score(p):
        return (accuracy_score(out.actual,(out[p]>=.5).astype(int)),
                brier_score_loss(out.actual,out[p]))
    return out,score("model_p"),score("baseline_p"),start

def main():
    url=os.environ.get("DATABASE_URL")
    if not url:raise RuntimeError("DATABASE_URL GitHub secret missing")
    with psycopg.connect(url,row_factory=psycopg.rows.dict_row,connect_timeout=20) as c:
        rows=c.execute("""SELECT ticker,trade_date,close
            FROM public.market_daily_bars
            WHERE ticker IN ('^TWII','^GSPC','^IXIC','^SOX','^DJI','^VIX','TSM')
            ORDER BY ticker,trade_date""").fetchall()
    data=build(rows)
    out,(acc,brier),(bacc,bbrier),start=evaluate(data)
    print(f"v6.6 PREMARKET RESEARCH: n={len(data)} OOS={len(out)}")
    print(f"model accuracy={acc:.4f} brier={brier:.5f}")
    print(f"baseline accuracy={bacc:.4f} brier={bbrier:.5f}")
    print("Yearly breakdown:")
    for yr,g in out.groupby(pd.to_datetime(out.date).dt.year):
        print(f" {yr}: n={len(g)}, model_acc={accuracy_score(g.actual,g.model_p>=.5):.3f},"
              f" base_acc={accuracy_score(g.actual,g.baseline_p>=.5):.3f},"
              f" model_brier={brier_score_loss(g.actual,g.model_p):.4f},"
              f" base_brier={brier_score_loss(g.actual,g.baseline_p):.4f}")
    # The report table's latest_twii_date is the last available close;
    # unlike v6.5 this evaluation includes the last TWII day as a target.
    latest=data.index.max().date()
    uslast=max(x["trade_date"] for x in rows if x["ticker"]=="^GSPC")
    with psycopg.connect(url,connect_timeout=20) as c:
        c.execute("""INSERT INTO public.market_model_reports
            (model_version,target_date,latest_twii_date,last_us_date,
             training_samples,test_samples,baseline_accuracy,model_accuracy,
             baseline_brier,model_brier,predicted_up_probability,status,notes)
             VALUES (%s,NULL,%s,%s,%s,%s,%s,%s,%s,%s,NULL,'research_only',%s)""",
            ("v6.6-premarket-logistic",latest,uslast,start,len(out),
             float(bacc),float(acc),float(bbrier),float(brier),
             "Taiwan features shifted one trading session; US closes strictly earlier "
             "calendar date; expanding-window 20-session refits; last 30% OOS. "
             "Research only. Same OOS period may have been used for prior model selection. "
             "No release-time, holiday, freshness or production readiness guarantee."))
    print("Research report saved. No live forecast published.")

if __name__=="__main__":
    main()
