# 台股智研 AI v4

在 v3 基礎上新增可重現的歷史日線回測引擎，以及固定 10 檔股票的規則評分 API。沿用 FastAPI / Docker / Render 部署方式。

## 部署

1. 解壓縮 ZIP，將**資料夾內**所有檔案與 `static` 資料夾上傳原 GitHub Repository 根目錄，覆蓋同名檔案。
2. Render → 原 Web Service → Manual Deploy → Deploy latest commit。
3. 打開 `https://doggg.onrender.com`，切到「交易策略回測」。
4. 測試 `/api/health`、`/api/backtest/2330?strategy=ma_cross&period=5y`、`/api/opportunities`。

## 回測定義

- 支援 `ma_cross`、`kd_cross`、`rsi_reversion`、`macd_cross`、`volume_breakout`。
- 每日收盤後計算訊號，下一個交易日**開盤**成交，避免同日收盤價前視偏差。
- 僅做多；資金全倉、整股、不加槓桿。交易成本預設：手續費 14.25 bps/邊、賣出交易稅 30 bps、滑價 5 bps/邊，**僅為可調示例假設**，非個人實際費率；ETF 等商品稅制不同。
- 70/30 時間切分；測試區間單獨初始化資金。沒有用測試資料選參數；但策略規則本身並非經過全面樣本外驗證。
- 使用 yfinance 調整後歷史日線；不模擬漲跌停無法成交、停牌、真實逐筆撮合、實際稅務細節；分紅調整與真實交易可有差異。
- 歷史勝率只計**已平倉**交易，未平倉部位計入淨值。交易筆數太少時無法推論未來勝率。
- `/api/opportunities` 是固定觀察清單 10 檔的規則分數，**不是**全市場掃描，也**不是**預測勝率或當沖訊號。

## 本機測試

```bash
pip install -r requirements.txt
uvicorn main:app --reload
```

打開 http://localhost:8000

## 尚未實作

台股全市場掃描、隔日漲跌機率模型、盤前 08:45 自動排程、授權盤中分鐘資料與當沖回測、新聞模型、個人持股匯入。不要將本版本的歷史回測數字當成投資保證。
