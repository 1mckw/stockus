#!/usr/bin/env python3
"""Headless NDX100 / DJI30 daily scanner for local use and GitHub Actions.

Trend lines follow the four-point expanding-window rules (same concept as ts):
  ≥4 merged wick touches, support slope ≥ 0, resistance slope ≤ 0.
  Draw from the first touch through the latest bar of the search window.
  Creek / Ice naming stays Wyckoff for the existing UI (Creek = support, Ice = resistance).
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from server import DJI30_SYMBOLS, PACKS_DIR, ensure_packs_dir, http_get

NDX100_FALLBACK = [
    "AAPL", "ABNB", "ADBE", "ADI", "ADP", "ADSK", "AEP", "ALAB", "ALNY", "AMAT", "AMD", "AMGN", "AMZN", "APP",
    "ARM", "ASML", "AVGO", "AXON", "BKR", "BKNG", "CCEP", "CDNS", "CEG", "CMCSA", "COST", "CPRT", "CRWD", "CRWV",
    "CSCO", "CSX", "CTAS", "DASH", "DDOG", "DXCM", "EXC", "FANG", "FAST", "FER", "FTNT", "GEHC", "GILD", "GOOG",
    "GOOGL", "HON", "HONA", "IDXX", "INTC", "INTU", "ISRG", "KDP", "KLAC", "LIN", "LITE", "LRCX", "MAR", "MCHP",
    "MDLZ", "MELI", "META", "MNST", "MPWR", "MRNA", "MRVL", "MSFT", "MSTR", "MU", "NBIS", "NFLX", "NVDA", "NXPI",
    "ODFL", "ORLY", "PANW", "PAYX", "PCAR", "PDD", "PEP", "PLTR", "PYPL", "QCOM", "REGN", "RKLB", "ROP", "ROST",
    "SBUX", "SHOP", "SNDK", "SNPS", "STX", "TER", "TMUS", "TRI", "TSLA", "TTWO", "TXN", "VRTX", "WDAY", "WDC",
    "WMT", "XEL",
]

PIVOT_LEFT = 3
PIVOT_RIGHT = 3
MIN_ANCHOR_GAP = 5
TOUCH_TOL_PCT = 0.0045
TOUCH_MERGE_BARS = 5
MAX_TOUCH_GAP_BARS = 120
RECENT_TOUCH_BARS = 60
MAX_BODY_BREAKS = 3
MAX_WICK_BREAKS = 5
HORIZ_SLOPE_PCT = 0.015
MIN_TOUCHES = 4
WINDOWS = [200, 300, 400, 500, 600, 800, 1000]
CHART_WINDOW = 600
TREND_SIGNAL_BARS = 5


def today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def bars_as_of(bars: list[dict], date_str: str) -> list[dict]:
    if not date_str or not bars:
        return bars or []
    end = datetime.fromisoformat(date_str + "T23:59:59").timestamp() * 1000
    return [b for b in bars if b["t"] <= end]


def median_gap_days(bars: list[dict]) -> float:
    if not bars or len(bars) < 3:
        return float("inf")
    gaps = sorted((bars[i]["t"] - bars[i - 1]["t"]) / 86400000 for i in range(1, len(bars)))
    return gaps[len(gaps) // 2]


def parse_yahoo_bars(payload: dict) -> list[dict]:
    r = (payload.get("chart") or {}).get("result") or [None]
    r = r[0]
    if not r:
        raise ValueError("no result")
    gran = (r.get("meta") or {}).get("dataGranularity")
    ts = r.get("timestamp") or []
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0]
    bars = []
    for i, t in enumerate(ts):
        try:
            o = q["open"][i]
            h = q["high"][i]
            l = q["low"][i]
            c = q["close"][i]
        except (KeyError, IndexError, TypeError):
            continue
        if any(x is None for x in (o, h, l, c)):
            continue
        v = 0
        try:
            vv = q["volume"][i]
            v = 0 if vv is None else float(vv)
        except (KeyError, IndexError, TypeError):
            pass
        bars.append({"t": int(t) * 1000, "o": float(o), "h": float(h), "l": float(l), "c": float(c), "v": v})
    if len(bars) < 50:
        raise ValueError("too few bars")
    if gran and gran != "1d":
        raise ValueError(f"非日線資料（gran={gran}）")
    if median_gap_days(bars) > 5:
        raise ValueError(f"非日線資料（gap≈{median_gap_days(bars):.1f}d）")
    return bars


def fetch_ndx_symbols() -> list[str]:
    try:
        raw = http_get("https://api.nasdaq.com/api/quote/list-type/nasdaq100")
        data = json.loads(raw.decode("utf-8"))
        rows = (((data.get("data") or {}).get("data") or {}).get("rows")) or []
        syms = sorted({str(r.get("symbol") or "").upper() for r in rows if r.get("symbol")})
        if len(syms) >= 50:
            return syms
    except Exception as e:
        print(f"nasdaq list failed: {e}")
    return list(NDX100_FALLBACK)


def fetch_daily_bars(symbol: str) -> list[dict]:
    last = None
    for rng in ("10y", "5y"):
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
            f"?interval=1d&range={rng}&includePrePost=false"
        )
        try:
            raw = http_get(url, timeout=40)
            return parse_yahoo_bars(json.loads(raw.decode("utf-8")))
        except Exception as e:
            last = e
            time.sleep(0.4)
    raise last or RuntimeError("fetch failed")


def find_pivots(bars, left, right):
    highs, lows = [], []
    n = len(bars)
    for i in range(left, n - right):
        is_h = is_l = True
        for j in range(i - left, i + right + 1):
            if j == i:
                continue
            if bars[j]["h"] >= bars[i]["h"]:
                is_h = False
            if bars[j]["l"] <= bars[i]["l"]:
                is_l = False
        if is_h:
            highs.append(i)
        if is_l:
            lows.append(i)
    return highs, lows


def line_through(i1, p1, i2, p2):
    m = (p2 - p1) / (i2 - i1)
    return {"m": m, "b": p1 - m * i1}


def y_at(line, i):
    return line["m"] * i + line["b"]


def body_top(bar):
    return max(bar["o"], bar["c"])


def body_bot(bar):
    return min(bar["o"], bar["c"])


def _tol(lp):
    return max(abs(lp) * TOUCH_TOL_PCT, 1e-9)


def is_touch_bar(bar, lp, resistance):
    wick = bar["h"] if resistance else bar["l"]
    return abs(wick - lp) <= _tol(lp)


def body_full_cross(bar, lp, resistance):
    if resistance:
        return body_bot(bar) > lp
    return body_top(bar) < lp


def wick_only_cross(bar, lp, resistance):
    if body_full_cross(bar, lp, resistance):
        return False
    if resistance:
        return bar["h"] > lp and body_top(bar) <= lp
    return bar["l"] < lp and body_bot(bar) >= lp


def body_fully_held(bar, lp, resistance):
    return body_top(bar) < lp if resistance else body_bot(bar) > lp


def merge_touch_indices(indices):
    if not indices:
        return []
    clusters = [[indices[0]]]
    for i in indices[1:]:
        if i - clusters[-1][-1] <= TOUCH_MERGE_BARS:
            clusters[-1].append(i)
        else:
            clusters.append([i])
    return [cl[0] for cl in clusters]


def longest_touch_segment(indices):
    merged = merge_touch_indices(indices)
    if not merged:
        return []
    segs = [[merged[0]]]
    for i in merged[1:]:
        if i - segs[-1][-1] > MAX_TOUCH_GAP_BARS:
            segs.append([i])
        else:
            segs[-1].append(i)
    return max(segs, key=lambda s: (len(s), s[-1] - s[0]))


def collect_raw_touches(bars, line, resistance, start_i, end_i):
    hits = []
    for i in range(start_i, end_i + 1):
        if is_touch_bar(bars[i], y_at(line, i), resistance):
            hits.append(i)
    return hits


def pierce_counts(bars, line, resistance, start_i, end_i):
    body_n = wick_n = 0
    for i in range(start_i, end_i + 1):
        lp = y_at(line, i)
        bar = bars[i]
        if body_full_cross(bar, lp, resistance):
            body_n += 1
        elif wick_only_cross(bar, lp, resistance):
            wick_n += 1
    return body_n, wick_n


def _line_score(L):
    return (len(L["touches"]), L.get("_span", 0), -L["touches"][0])


def pack_line(bars, line, kind, touches, body_n, wick_n, lookback, *, horizontal=False):
    n = len(bars)
    first_i, last_touch = touches[0], touches[-1]
    span = last_touch - first_i
    if horizontal:
        typ, label = ("creek", "Creek") if kind == "support" else ("ice", "Ice")
    else:
        typ = "uptrend" if kind == "support" else "downtrend"
        label = "向上趨勢線" if kind == "support" else "向下趨勢線"
    return {
        "kind": kind,
        "line": line,
        "touches": touches,
        "bodyBreaks": body_n,
        "wickBreaks": wick_n,
        "start": max(0, n - lookback),
        "drawFrom": first_i,
        "drawTo": n - 1,
        "windowSize": lookback,
        "score": len(touches) * 10 + span * 0.01,
        "_span": span,
        "label": label,
        "type": typ,
    }


def evaluate_candidate(bars, line, kind, win_lo, lookback):
    n = len(bars)
    last_i = n - 1
    resistance = kind == "resistance"
    raw = collect_raw_touches(bars, line, resistance, win_lo, last_i)
    seg = longest_touch_segment(raw)
    if len(seg) < MIN_TOUCHES:
        return None
    recent_lo = last_i - (RECENT_TOUCH_BARS - 1)
    if not any(i >= recent_lo for i in seg):
        return None
    first_i = seg[0]
    body_n, wick_n = pierce_counts(bars, line, resistance, first_i + 1, last_i)
    if body_n > MAX_BODY_BREAKS or wick_n > MAX_WICK_BREAKS:
        return None
    out_line = dict(line)
    horizontal = False
    span = seg[-1] - first_i
    p0 = y_at(line, first_i)
    if lookback >= WINDOWS[-1] and p0:
        drift = abs(line["m"]) * span / abs(p0)
        if drift < HORIZ_SLOPE_PCT:
            level = sum(y_at(line, i) for i in seg) / len(seg)
            out_line = {"m": 0.0, "b": level}
            horizontal = True
    packed = pack_line(
        bars, out_line, kind, seg, body_n, wick_n, lookback, horizontal=horizontal
    )
    return packed


def _pivots_in_lookback(bars, lookback):
    start_idx = max(0, len(bars) - lookback)
    slice_b = bars[start_idx:]
    highs, lows = find_pivots(slice_b, PIVOT_LEFT, PIVOT_RIGHT)
    return [i + start_idx for i in highs], [i + start_idx for i in lows]


def collect_side_line(bars, lookback, kind):
    n = len(bars)
    actual = min(lookback, n)
    win_lo = n - actual
    highs, lows = _pivots_in_lookback(bars, actual)
    pts = lows if kind == "support" else highs
    best = None
    for a in range(len(pts)):
        for c in range(a + 1, len(pts)):
            i1, i2 = pts[a], pts[c]
            if i2 - i1 < MIN_ANCHOR_GAP:
                continue
            p1 = bars[i1]["l"] if kind == "support" else bars[i1]["h"]
            p2 = bars[i2]["l"] if kind == "support" else bars[i2]["h"]
            if kind == "support" and p2 < p1:
                continue
            if kind == "resistance" and p2 > p1:
                continue
            line = line_through(i1, p1, i2, p2)
            cand = evaluate_candidate(bars, line, kind, win_lo, actual)
            if cand is None:
                continue
            if best is None or _line_score(cand) > _line_score(best):
                best = cand
    return best


def make_parallel_channel(L, bars):
    if not L or not L.get("touches"):
        return None
    n = len(bars)
    lookback = int(L.get("windowSize") or n)
    win_lo = max(0, n - lookback)
    last_i = n - 1
    i0 = L.get("drawFrom", L["touches"][0])
    if i0 >= last_i:
        return None
    opp = "resistance" if L["kind"] == "support" else "support"
    highs, lows = _pivots_in_lookback(bars, lookback)
    pts = lows if opp == "support" else highs
    m = L["line"]["m"]
    best = None
    for i in pts:
        src_y = y_at(L["line"], i)
        price = bars[i]["l"] if opp == "support" else bars[i]["h"]
        if opp == "resistance" and price < src_y:
            continue
        if opp == "support" and price > src_y:
            continue
        cand = evaluate_candidate(bars, {"m": m, "b": price - m * i}, opp, win_lo, lookback)
        if cand is None:
            continue
        if best is None or _line_score(cand) > _line_score(best):
            best = cand
    if best is None:
        return None
    best["type"] = "channel"
    best["label"] = "平行通道"
    best["isParallel"] = True
    best["pairOf"] = L["type"]
    best["drawFrom"] = min(i0, best["touches"][0])
    best["drawTo"] = n - 1
    return best


def analyze_symbol(bars, windows=None):
    windows = list(windows or WINDOWS)
    n = len(bars)
    found = {}
    for kind in ("support", "resistance"):
        for lookback in windows:
            actual = min(lookback, n)
            if actual < 80 and lookback >= 80:
                continue
            line = collect_side_line(bars, actual, kind)
            if line:
                found[kind] = line
                break
            if actual >= n:
                break
    up = found.get("support")
    down = found.get("resistance")
    used = None
    for L in (up, down):
        if L and L.get("windowSize"):
            used = L["windowSize"] if used is None else max(used, L["windowSize"])
    channel = None
    primary = up if up and not down else (down if down and not up else None)
    if primary:
        channel = make_parallel_channel(primary, bars)
    return {"up": up, "down": down, "channel": channel, "window": used}


def shift_line(L, offset: int):
    """Map line indices from a longer bar series onto a suffix of `offset` fewer bars."""
    if not L or offset <= 0:
        return L
    line = L.get("line")
    if line:
        line = {"m": line["m"], "b": line["b"] + line["m"] * offset}
    touches = [i - offset for i in (L.get("touches") or []) if i >= offset]
    out = dict(L)
    out["line"] = line
    out["touches"] = touches
    for key in ("start", "drawFrom", "drawTo"):
        if out.get(key) is not None:
            out[key] = out[key] - offset
    return out


def shift_analysis(analysis: dict, offset: int) -> dict:
    if offset <= 0:
        return analysis
    return {
        "window": analysis.get("window"),
        "up": shift_line(analysis.get("up"), offset),
        "down": shift_line(analysis.get("down"), offset),
        "channel": shift_line(analysis.get("channel"), offset),
    }


def slim_line(L):
    if not L:
        return None
    return {
        "kind": L.get("kind"),
        "line": L.get("line"),
        "touches": L.get("touches"),
        "bodyBreaks": L.get("bodyBreaks"),
        "wickBreaks": L.get("wickBreaks"),
        "start": L.get("start"),
        "drawFrom": L.get("drawFrom"),
        "drawTo": L.get("drawTo"),
        "windowSize": L.get("windowSize"),
        "score": L.get("score"),
        "label": L.get("label"),
        "type": L.get("type"),
        "isParallel": L.get("isParallel"),
        "pairOf": L.get("pairOf"),
    }


def scan_index(index: str, date_str: str, sleep_s: float) -> dict:
    if index == "dji30":
        symbols = list(DJI30_SYMBOLS)
        label = "DJI30"
    else:
        symbols = fetch_ndx_symbols()
        label = "NDX100"
        index = "ndx100"
    results = []
    ok = 0
    for i, sym in enumerate(symbols, 1):
        print(f"[{i}/{len(symbols)}] {sym}", flush=True)
        try:
            full = fetch_daily_bars(sym)
            bars = bars_as_of(full, date_str)
            if len(bars) < 50:
                raise ValueError("該日期前資料不足")
            analysis = analyze_symbol(bars)
            if len(bars) > CHART_WINDOW:
                analysis = shift_analysis(analysis, len(bars) - CHART_WINDOW)
                bars = bars[-CHART_WINDOW:]
            results.append({
                "symbol": sym,
                "bars": bars,
                "analysis": {
                    "window": analysis["window"],
                    "up": slim_line(analysis["up"]),
                    "down": slim_line(analysis["down"]),
                    "channel": slim_line(analysis["channel"]),
                },
            })
            ok += 1
        except Exception as e:
            results.append({"symbol": sym, "error": str(e)})
            print(f"  fail: {e}", flush=True)
        time.sleep(sleep_s)
    return {
        "date": date_str,
        "index": index,
        "indexLabel": label,
        "savedAt": datetime.now(timezone.utc).isoformat(),
        "count": ok,
        "results": results,
    }


def recent_signal(row: dict, lookback: int = TREND_SIGNAL_BARS) -> str:
    bars = row.get("bars") or []
    analysis = row.get("analysis") or {}
    if not bars:
        return row.get("error") or "無資料"
    n = len(bars)
    frm = max(0, n - lookback)
    breakout = touch = reclaim = False
    for key in ("up", "down", "channel"):
        L = analysis.get(key)
        if not L or not L.get("line"):
            continue
        resistance = L.get("kind") == "resistance"
        body_broke_at = -1
        for i in range(frm, n):
            y = y_at(L["line"], i)
            bar = bars[i]
            body_brk = body_full_cross(bar, y, resistance)
            wick_brk = wick_only_cross(bar, y, resistance)
            if body_brk or wick_brk:
                breakout = True
            if body_brk and body_broke_at < 0:
                body_broke_at = i
            elif not body_brk and not wick_brk and is_touch_bar(bar, y, resistance):
                touch = True
            if body_broke_at >= 0 and i > body_broke_at and body_fully_held(bar, y, resistance):
                reclaim = True
    if reclaim:
        return "收回"
    if breakout:
        return "突破"
    if touch:
        return "觸碰"
    return ""


def write_latest_md(out: Path, packs: list[dict]) -> None:
    lines = ["# 最新掃描", ""]
    for pack in packs:
        lines.append(f"## {pack.get('indexLabel') or pack.get('index')} · {pack.get('date')}")
        lines.append("")
        lines.append(f"可用圖表 **{pack.get('count', 0)}** 檔。在 GitHub Pages 開啟掃描器檢視圖表。")
        lines.append("")
        lines.append("| 代號 | 近 5 根 | 線 |")
        lines.append("| --- | --- | --- |")
        for r in pack.get("results") or []:
            sig = recent_signal(r) if r.get("bars") else (r.get("error") or "無資料")
            a = r.get("analysis") or {}
            labels = []
            for key in ("up", "down", "channel"):
                L = a.get(key)
                if L and L.get("label"):
                    labels.append(L["label"])
            lines.append(f"| {r.get('symbol')} | {sig or '—'} | {' / '.join(labels) or '—'} |")
        lines.append("")
    (out.parent / "LATEST.md").write_text("\n".join(lines), encoding="utf-8")


def write_manifest(out: Path) -> dict:
    packs = []
    for fp in sorted(out.glob("*.json"), reverse=True):
        name = fp.stem
        if name == "manifest":
            continue
        # index-YYYY-MM-DD
        if len(name) < 12 or name[-11] != "-" or name[-6] != "-" or name[-3] != "-":
            continue
        date = name[-10:]
        index = name[:-11]
        if not index:
            continue
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        packs.append({
            "index": data.get("index") or index,
            "date": data.get("date") or date,
            "file": fp.name,
            "label": data.get("indexLabel") or index.upper(),
            "count": data.get("count", 0),
        })
    packs.sort(key=lambda x: (x["date"], x["index"]), reverse=True)
    manifest = {"packs": packs}
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main() -> None:
    p = argparse.ArgumentParser(description="Scan NDX100 / DJI30 daily trend lines")
    p.add_argument("--index", choices=["ndx100", "dji30", "both"], default="both")
    p.add_argument("--date", default="", help="YYYY-MM-DD (UTC today if empty)")
    p.add_argument("--sleep", type=float, default=0.25, help="delay between Yahoo requests")
    p.add_argument("--out", default="", help="output directory (default chartpacks/)")
    args = p.parse_args()
    date_str = args.date.strip() or today_iso()
    out = Path(args.out) if args.out else PACKS_DIR
    out.mkdir(parents=True, exist_ok=True)
    ensure_packs_dir()
    indexes = ["ndx100", "dji30"] if args.index == "both" else [args.index]
    written = []
    for idx in indexes:
        pack = scan_index(idx, date_str, args.sleep)
        fp = out / f"{idx}-{date_str}.json"
        fp.write_text(json.dumps(pack, ensure_ascii=False), encoding="utf-8")
        print(f"wrote {fp} · {pack['count']} ok", flush=True)
        written.append(pack)
    write_manifest(out)
    write_latest_md(out, written)
    print("wrote chartpacks/manifest.json and LATEST.md", flush=True)


if __name__ == "__main__":
    main()
