#!/usr/bin/env python3
"""Local server for NDX100 scanner — serves HTML, proxies Yahoo/Nasdaq, saves chartpacks."""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
PACKS_DIR = ROOT / "chartpacks"
PORT = 8765
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# 道瓊 30 成分股（2025-11 調整後 · NVDA 取代 INTC、AMZN 取代 WBA）
DJI30_SYMBOLS = [
    "AAPL", "AMGN", "AMZN", "AXP", "BA", "CAT", "CRM", "CSCO", "CVX", "DIS",
    "DOW", "GS", "HD", "HON", "IBM", "JNJ", "JPM", "KO", "MCD", "MMM",
    "MRK", "MSFT", "NKE", "NVDA", "PG", "TRV", "UNH", "V", "VZ", "WMT",
]


def http_get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json,*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def ensure_packs_dir() -> None:
    PACKS_DIR.mkdir(parents=True, exist_ok=True)


def pack_path(date: str) -> Path:
    return PACKS_DIR / f"{date}.json"


def list_pack_dates() -> list[str]:
    ensure_packs_dir()
    dates = []
    for p in PACKS_DIR.glob("*.json"):
        if DATE_RE.match(p.stem):
            dates.append(p.stem)
    dates.sort(reverse=True)
    return dates


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        print(f"[{self.log_date_time_string()}] {args[0]}")

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, *")

    def _json(self, code: int, obj) -> None:
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self._cors()
        self.end_headers()
        self.wfile.write(raw)

    def _bytes(self, code: int, data: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return None
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html", "/ndx100-scanner.html"):
            fp = ROOT / "ndx100-scanner.html"
            self._bytes(200, fp.read_bytes(), "text/html; charset=utf-8")
            return

        if path == "/api/ndx100":
            try:
                raw = http_get("https://api.nasdaq.com/api/quote/list-type/nasdaq100")
                self._bytes(200, raw, "application/json")
            except Exception as e:
                self._json(502, {"error": str(e)})
            return

        if path == "/api/dji30":
            self._json(200, {"symbols": DJI30_SYMBOLS, "count": len(DJI30_SYMBOLS)})
            return

        if path == "/api/chart":
            qs = parse_qs(parsed.query)
            symbol = (qs.get("symbol") or [""])[0].strip().upper()
            range_ = (qs.get("range") or ["10y"])[0]
            # Yahoo range=max often downsamples to week/month/quarter — prefer 10y/5y for 1d
            if range_ == "max":
                range_ = "10y"
            if not symbol:
                self._json(400, {"error": "symbol required"})
                return
            url = (
                f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
                f"?interval=1d&range={range_}&includePrePost=false"
            )
            try:
                raw = http_get(url)
                self._bytes(200, raw, "application/json")
            except urllib.error.HTTPError as e:
                self._json(e.code, {"error": f"yahoo HTTP {e.code}"})
            except Exception as e:
                self._json(502, {"error": str(e)})
            return

        if path == "/api/packs":
            self._json(200, {"dates": list_pack_dates()})
            return

        if path.startswith("/api/packs/"):
            date = path[len("/api/packs/") :].strip("/")
            if not DATE_RE.match(date):
                self._json(400, {"error": "invalid date"})
                return
            fp = pack_path(date)
            if not fp.exists():
                self._json(404, {"error": "pack not found", "date": date})
                return
            try:
                self._bytes(200, fp.read_bytes(), "application/json; charset=utf-8")
            except Exception as e:
                self._json(500, {"error": str(e)})
            return

        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        if path.startswith("/api/packs/"):
            date = path[len("/api/packs/") :].strip("/")
            if not DATE_RE.match(date):
                self._json(400, {"error": "invalid date"})
                return
            try:
                body = self._read_json()
            except Exception as e:
                self._json(400, {"error": f"invalid json: {e}"})
                return
            if not isinstance(body, dict):
                self._json(400, {"error": "body must be object"})
                return
            body["date"] = date
            ensure_packs_dir()
            fp = pack_path(date)
            try:
                raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
                fp.write_bytes(raw)
                self._json(200, {"ok": True, "date": date, "bytes": len(raw)})
            except Exception as e:
                self._json(500, {"error": str(e)})
            return

        self._json(404, {"error": "not found"})


def main() -> None:
    ensure_packs_dir()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"NDX100 Scanner → http://127.0.0.1:{PORT}/")
    print(f"Chartpacks → {PACKS_DIR}")
    print("Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
