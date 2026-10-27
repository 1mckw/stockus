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

在 GitHub 只跑 **Actions → Scan → Run workflow**：

- 預設會掃描並更新網站
- 把「掃描最新日線」關掉 = 只佈署現有 Chartpack 到 Pages

平日自動兩次：**10:30 UTC**、**22:30 UTC**（掃描 + 更新網站）。

掃描完成後可直接在 GitHub 上看：

- 表格：[LATEST.md](https://github.com/1mckw/stockus/blob/main/LATEST.md)
- 圖表站（GitHub Pages）：<https://1mckw.github.io/stockus/>

第一次請到倉庫 **Settings → Pages → Source: GitHub Actions**。環境 `github-pages` 若出現等待審核，按 Approve。
