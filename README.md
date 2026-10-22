# 台股智研 AI v2 — 歷史行情後端與部署
本版保留原先八個 DEMO 頁籤，另在頁面底部加入真實**歷史日線**行情資料中心。非即時報價，未產生可驗證的勝率或當沖訊號。

## 本機執行（Python 3.11+）
```bash
python -m venv .venv
source .venv/bin/activate
# Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```
開啟 http://127.0.0.1:8000 ，API 文件在 http://127.0.0.1:8000/docs 。

## 部署到 Render
1. 解壓縮，將所有檔案（含 Dockerfile、render.yaml、static 資料夾）推送到 GitHub Repository 根目錄。
2. 登入 Render，選 New → Blueprint，連接 GitHub Repository，依 render.yaml 部署；也可建立 Docker Web Service。
3. 部署成功後開啟 Render 提供的網址；檢查 /api/health 回傳 status: ok。

## API
- /api/health：服務狀態
- /api/us-market：美股指數與 ADR 歷史日線
- /api/watchlist：固定 10 檔台股觀察清單（非全市場掃描）
- /api/stock/2330：台股歷史日線、KD、MACD、RSI、均線、量比

資料來自 yfinance 非官方 Yahoo Finance 介面，可能延遲、缺漏或受限流；商業使用須確認授權。上市 .TW，上櫃 .TWO。八大原型頁籤仍是模擬資料。

尚未實作：經驗證的隔日漲跌機率、當沖勝率、盤中即時報價、全市場掃描、正式回測、新聞串流、08:45 自動排程、帳戶與雲端同步。不得將示範值用於真實下單。
