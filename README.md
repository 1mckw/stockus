# stockus

NDX100 / DJI30 日線掃描器：四點成線趨勢線、Creek / Ice、平行通道，可存 Chartpack。

## 本機執行

```bash
python server.py
```

瀏覽器開啟 [http://127.0.0.1:8765/](http://127.0.0.1:8765/)

- `server.py`：靜態頁面、Yahoo 圖表代理、Nasdaq 成分股、Chartpack 存檔
- `ndx100-scanner.html`：掃描與圖表 UI（含手機版面）

## GitHub Actions

倉庫：<https://github.com/1mckw/stockus>

無介面掃描（與網頁同一套規則：四點成線、間隔 ≤120K、最新 60K 須有觸點）：

```bash
python scan.py --index both
```

在 GitHub：**Actions → Scan → Run workflow**。可選指數與日期。平日 22:30 UTC 也會自動跑。結果在該次 run 的 **Artifacts → chartpacks**。
