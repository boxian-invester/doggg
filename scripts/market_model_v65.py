"""v6.5: walk-forward benchmark comparison, research only; no live probability."""
import logging
import os
import numpy as np
import pandas as pd
import psycopg
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import LogisticRegression
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import accuracy_score, brier_score_loss

LOG=logging.getLogger("v65")
US=["^GSPC","^IXIC","^SOX","^DJI","^VIX","TSM"]

def frame_from_rows(rows):
    d=pd.DataFrame(rows)
    d["trade_date"]=pd.to_datetime(d["trade_date"])
    d["close"]=pd.to_numeric(d["close"],errors="coerce")
    d=d.dropna(subset=["close"])
    def series(ticker):
        return (d[d.ticker==ticker].sort_values("trade_date")
                .drop_duplicates("trade_date").set_index("trade_date")["close"])
    tw=series("^TWII")
    if len(tw)<700:raise ValueError("TWII history too short")
    out=pd.DataFrame(index=tw.index)
    twret=tw.pct_change(fill_method=None)
    out["tw_ret1"]=twret
    out["tw_ret3"]=tw.pct_change(3,fill_method=None)
    out["tw_ret10"]=tw.pct_change(10,fill_method=None)
    out["tw_vol10"]=twret.rolling(10).std()
    out["tw_ma20_gap"]=tw/tw.rolling(20).mean()-1
    # US calendar date strictly before Taiwan trading date; conservative
    # and avoids same-day US future close leakage.
    for ticker in US:
        s=series(ticker)
        if len(s)<700:raise ValueError("US history too short: "+ticker)
        f=pd.DataFrame({"us_date":s.index,
                        "ret1":s.pct_change(fill_method=None).values,
                        "ret3":s.pct_change(3,fill_method=None).values})
        left=pd.DataFrame({"tw_date":out.index})
        merged=pd.merge_asof(left,f,left_on="tw_date",right_on="us_date",
                             direction="backward",allow_exact_matches=False)
        out[ticker+"_ret1"]=merged["ret1"].to_numpy()
        out[ticker+"_ret3"]=merged["ret3"].to_numpy()
    out["next_ret"]=tw.shift(-1)/tw-1
    out["target"]=(out["next_ret"]>0).astype(float)
    out.loc[out.next_ret.isna(),"target"]=np.nan
    return out.replace([np.inf,-np.inf],np.nan)

def metrics(y,p):
    y=np.array(y,dtype=int);p=np.array(p,dtype=float)
    return round(float(accuracy_score(y,p>=.5)),4),round(float(brier_score_loss(y,p)),5)

def run():
    url=os.getenv("DATABASE_URL")
    if not url:raise RuntimeError("Missing DATABASE_URL")
    with psycopg.connect(url,row_factory=psycopg.rows.dict_row,connect_timeout=20) as c:
        rows=c.execute("""select ticker,trade_date,close from public.market_daily_bars
                       where ticker in ('^TWII','^GSPC','^IXIC','^SOX','^DJI','^VIX','TSM')
                       order by ticker,trade_date""").fetchall()
    f=frame_from_rows(rows)
    features=[x for x in f.columns if x not in ("next_ret","target")]
    valid=f.dropna(subset=features+["target"])
    # Preserve chronological split; test only on last 30% of labeled days.
    start=max(450,int(len(valid)*.7))
    if len(valid)-start<150:raise RuntimeError("Insufficient OOS data")
    predictions={k:[] for k in ["baseline","logistic","histgb"]}
    ys=[]
    # 20-day block refits reduce runtime; each prediction only sees earlier
    # labeled training rows. One-row embargo prevents target T+1 leakage.
    for block in range(start,len(valid),20):
        train=valid.iloc[:block-1]
        test=valid.iloc[block:min(block+20,len(valid))]
        X=train[features].to_numpy(dtype=float)
        Xt=test[features].to_numpy(dtype=float)
        y=train.target.to_numpy(dtype=int)
        if len(set(y))<2:raise RuntimeError("Training has one class")
        models={
          "logistic":make_pipeline(StandardScaler(),LogisticRegression(max_iter=600,C=.1)),
          "histgb":HistGradientBoostingClassifier(max_iter=80,max_leaf_nodes=7,
                    learning_rate=.04,l2_regularization=5,random_state=42)
        }
        predictions["baseline"].extend([float(y.mean())]*len(test))
        for key,model in models.items():
            model.fit(X,y)
            predictions[key].extend(model.predict_proba(Xt)[:,1].tolist())
        ys.extend(test.target.astype(int).tolist())
    scores={k:metrics(ys,v) for k,v in predictions.items()}
    LOG.info("n=%d train_initial=%d OOS=%d scores(accuracy,brier)=%s",
             len(valid),start-1,len(ys),scores)
    # Write comparable results into existing v6.4 reports table. Do NOT
    # pick the best OOS model and claim unbiased production performance.
    latest=valid.index.max().date()
    uslast=max(x["trade_date"] for x in rows if x["ticker"]=="^GSPC")
    with psycopg.connect(url,connect_timeout=20) as c:
        for key in ("logistic","histgb"):
            a,b=scores[key]
            ba,bb=scores["baseline"]
            c.execute("""insert into public.market_model_reports
              (model_version,target_date,latest_twii_date,last_us_date,
               training_samples,test_samples,baseline_accuracy,model_accuracy,
               baseline_brier,model_brier,predicted_up_probability,status,notes)
               values (%s,NULL,%s,%s,%s,%s,%s,%s,%s,%s,NULL,'research_only',%s)""",
              ("v6.5-"+key,latest,uslast,start-1,len(ys),ba,a,bb,b,
               "30% final OOS, expanding training with 20-session refits; "
               "one-row embargo; US strictly earlier calendar date. "
               "Comparative exploratory evaluation, not final unbiased "
               "model selection. TWII freshness unresolved; no live signal."))
    LOG.info("Saved two research reports. No production probability published.")

if __name__=="__main__":
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
    run()
