# 台股智研 AI v5 — 有限股票掃描與美股盤前觀測

本版本在 v4 上新增：

- `/api/v5/scan?limit=5`：固定 20 檔觀察股票的日線規則評分，並顯示量比、KD、MACD 等理由。**不是全市場掃描，也不是勝率。**
- `/api/v5/premarket`：讀取美股指數、費城半導體、台積電 ADR 的最近日線，提供描述性加權漲跌觀測；**沒有台股隔日漲跌機率模型。**
- 首頁第 1、2、3 區加入新資料視圖。其他 v4 功能保留。

## 部署 Render

1. 解壓縮後，把檔案和 `static` 資料夾的**內容**上傳至原本 GitHub repository 的根目錄（不要上傳 ZIP 本身），覆蓋同名檔案。
2. 確認 GitHub 根目錄有 `Dockerfile`、`main.py`、`backtest.py`、`requirements.txt`、`render.yaml`，且 `static/index.html` 存在。
3. Render → 原 Web Service → Manual Deploy → Deploy latest commit。
4. 測試 `/api/health`、`/api/v5/premarket`、`/api/v5/scan?limit=5`。
5. 網站首頁第 1、2、3 個功能區點擊 v5 更新按鈕。

## 本機執行

Python 3.11+：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

瀏覽 `http://localhost:8000`。

## 重要限制

- Yahoo Finance / yfinance 為非官方資料來源，可能限流、延遲、停用或回傳錯誤；上櫃股票由 `.TWO` 備援。
- `auto_adjust=True` 使用調整後價格，**不能直接當作實際下單價位**。
- v5 掃描固定 20 檔，採 4 執行緒、15 分鐘快取；Render 免費方案可能休眠，第一次請求較慢或逾時。
- v5 **沒有**排程自動產生 08:45 報告、盤中報價、全市場掃描、勝率預測或可執行的當沖進出場價。
- v4 歷史回測是研究性實作，須進一步驗證企業行動、滑價、漲跌停與樣本外可靠性。
- API 端點公開，勿在公開網站輸入或保存真實個人帳戶、交易憑證或敏感資料。

## 後續工程

合法全市場資料源、持久資料庫、排程任務、時間對齊的跨市場特徵、走勢預測的 walk-forward 樣本外驗證、流動性與風險控制，以及實際盤中行情授權。
