# 台股智研 AI v3

v3 改用圖表及表格呈現既有 FastAPI 歷史行情資料，包含美股行情卡、台股固定 10 檔觀察清單、個股 K 線 / 成交量 / KD / MACD。其餘五項研究模組仍為清楚標記的待開發功能。

## Mac 本機執行

```bash
cd tsi-v3
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

瀏覽 http://localhost:8000 。

## 更新現有 Render 網站

1. 解壓縮 `taiwan-stock-intelligence-v3.zip`。
2. 打開解壓縮資料夾，把 `main.py`、`requirements.txt`、`Dockerfile`、`render.yaml`、`static` 資料夾及其他專案檔案上傳至原 GitHub repository，覆蓋同名檔案。
3. 確保 GitHub 上存在 `static/index.html`，且 `Dockerfile` 在 repository 根目錄。
4. Commit changes，然後於 Render 選擇 Manual Deploy → Deploy latest commit（如未自動部署）。
5. 開啟既有網址 https://doggg.onrender.com ，按 Cmd+Shift+R 強制更新快取。

## API

- `/api/health`
- `/api/us-market`
- `/api/watchlist`
- `/api/stock/2330?period=1y`
- `/docs`

## 注意

行情來自 yfinance 非官方介面，可能延遲、缺漏、調整或有誤。圖表使用 auto_adjust=True 的調整後歷史日線，並非即時委託價格。美股隔日預測機率、當沖勝率、新聞與投資組合分析尚未完成，不提供假數字。若要公開營運或商業使用，須另行評估資料來源授權、穩定性及隱私安全。
