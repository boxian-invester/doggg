"""Walk-forward, leakage-conscious EOD model research. Not an 08:45 live signal."""
import logging
import os
from datetime import date
import numpy as np
import pandas as pd
import psycopg
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import accuracy_score, brier_score_loss

LOG = logging.getLogger("market_model")
VERSION = "v6.4-walkforward-1"
US = ["^GSPC", "^IXIC", "^SOX", "^DJI", "^VIX", "TSM"]
FEATURES = [f"{t}_ret1" for t in US] + ["twii_ret1", "twii_ret5", "twii_vol10"]

def build_frame(raw):
    df = pd.DataFrame(raw)
    if df.empty: raise RuntimeError("market_daily_bars empty")
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["close"])
    tw = (df[df.ticker == "^TWII"].sort_values("trade_date")
          .drop_duplicates("trade_date").set_index("trade_date")["close"])
    if len(tw) < 400: raise RuntimeError("Insufficient ^TWII history (<400 rows)")
    t = pd.DataFrame(index=tw.index)
    t["twii_ret1"] = tw.pct_change(fill_method=None)
    t["twii_ret5"] = tw.pct_change(5, fill_method=None)
    t["twii_vol10"] = t["twii_ret1"].rolling(10).std()
    # At end of Taiwan session T, the most recent *completed* US session has
    # US date strictly less than T. Avoid same-date US closes (future).
    for ticker in US:
        s = (df[df.ticker == ticker].sort_values("trade_date")
             .drop_duplicates("trade_date").set_index("trade_date")["close"])
        if len(s) < 400: raise RuntimeError("Insufficient history: " + ticker)
        usret = s.pct_change(fill_method=None).rename(f"{ticker}_ret1")
        left = pd.DataFrame({"taiwan_date": t.index})
        right = pd.DataFrame({"us_date": usret.index, f"{ticker}_ret1": usret.values})
        aligned = pd.merge_asof(left.sort_values("taiwan_date"),
                                right.sort_values("us_date"),
                                left_on="taiwan_date", right_on="us_date",
                                direction="backward", allow_exact_matches=False)
        t[f"{ticker}_ret1"] = aligned[f"{ticker}_ret1"].to_numpy()
    t["next_return"] = tw.shift(-1) / tw - 1
    t["target"] = (t["next_return"] > 0).astype(float)
    # Last row target is unknown; don't convert to false!
    t.loc[t["next_return"].isna(), "target"] = np.nan
    return t.replace([np.inf, -np.inf], np.nan)

def evaluate(frame):
    labeled = frame.dropna(subset=FEATURES + ["target"]).copy()
    if len(labeled) < 500: raise RuntimeError("Too few labeled samples for evaluation")
    # Expanding window; at each test point only prior labels are used.
    # Training label at day T uses close T+1, so exclude the immediately
    # preceding row when predicting test day T (one-row embargo).
    start = max(350, int(len(labeled) * .65))
    actual, pred, base = [], [], []
    for i in range(start, len(labeled)):
        train = labeled.iloc[:i-1]
        test = labeled.iloc[i:i+1]
        if train["target"].nunique() < 2: continue
        model = make_pipeline(StandardScaler(), LogisticRegression(max_iter=500))
        model.fit(train[FEATURES], train["target"].astype(int))
        pred.append(float(model.predict_proba(test[FEATURES])[0,1]))
        base.append(float(train["target"].mean()))
        actual.append(int(test["target"].iloc[0]))
    if len(actual) < 100: raise RuntimeError("Insufficient out-of-sample test days")
    a=np.asarray(actual)
    p=np.asarray(pred)
    b=np.asarray(base)
    return {
        "training_samples":start-1, "test_samples":len(a),
        "model_accuracy":float(accuracy_score(a,p>=.5)),
        "baseline_accuracy":float(accuracy_score(a,b>=.5)),
        "model_brier":float(brier_score_loss(a,p)),
        "baseline_brier":float(brier_score_loss(a,b)),
    }

def main():
    url=os.environ.get("DATABASE_URL")
    if not url: raise RuntimeError("Missing DATABASE_URL")
    with psycopg.connect(url,connect_timeout=20,row_factory=psycopg.rows.dict_row) as conn:
        raw=conn.execute("""SELECT ticker,trade_date,close FROM public.market_daily_bars
                           WHERE ticker IN ('^TWII','^GSPC','^IXIC','^SOX','^DJI','^VIX','TSM')
                           ORDER BY ticker,trade_date""").fetchall()
    frame=build_frame(raw)
    metrics=evaluate(frame)
    latest=frame.index.max().date()
    us_latest=max(x["trade_date"] for x in raw if x["ticker"]=="^GSPC")
    # Deliberately withhold operational probability until index freshness and
    # US-market session cutoff can be independently verified.
    status="research_only"
    notes=("Walk-forward OOS research; baseline is expanding historical up-frequency. "
           "No live prediction: TWII freshness and US session timing not validated. "
           "Model uses end-of-day TWII features; not suitable for 08:45 as-is.")
    with psycopg.connect(url,connect_timeout=20) as conn:
        conn.execute("""INSERT INTO public.market_model_reports
          (model_version,target_date,latest_twii_date,last_us_date,
           training_samples,test_samples,baseline_accuracy,model_accuracy,
           baseline_brier,model_brier,predicted_up_probability,status,notes)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL,%s,%s)""",
          (VERSION,None,latest,us_latest,metrics["training_samples"],
           metrics["test_samples"],metrics["baseline_accuracy"],
           metrics["model_accuracy"],metrics["baseline_brier"],
           metrics["model_brier"],status,notes))
    LOG.info("TWII latest=%s; US latest=%s; metrics=%s; status=%s",
             latest,us_latest,metrics,status)

if __name__=="__main__":
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
    main()
