#!/usr/bin/env python3
"""Headless NDX100 / DJI30 daily scanner for local use and GitHub Actions."""
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


def compact_touches_by_gap(touches, recent_from):
    if not touches:
        return []
    segments = []
    cur = [touches[0]]
    for i in range(1, len(touches)):
        if touches[i] - touches[i - 1] <= MAX_TOUCH_GAP_BARS:
            cur.append(touches[i])
        else:
            segments.append(cur)
            cur = [touches[i]]
    segments.append(cur)
    pool = [s for s in segments if any(i >= recent_from for i in s)] if recent_from is not None else segments
    if not pool:
        return []
    pool.sort(key=len, reverse=True)
    return pool[0]


def evaluate_line(bars, start, line, kind, pivots):
    n = len(bars)
    pivot_set = set(pivots)
    touches = []
    for i in range(start, n):
        y = y_at(line, i)
        bar = bars[i]
        tol = abs(y) * TOUCH_TOL_PCT
        bt, bb = body_top(bar), body_bot(bar)
        if kind == "support":
            if bt < y - tol:
                continue
            wick = bar["l"] <= y + tol and bt >= y - tol
            near = abs(bar["l"] - y) <= tol
            pv = i in pivot_set and bar["l"] <= y + tol * 2 and bb >= y - tol * 3
            if wick or near or pv:
                if not touches or i - touches[-1] >= TOUCH_MERGE_BARS:
                    touches.append(i)
                else:
                    prev = touches[-1]
                    if abs(bar["l"] - y) < abs(bars[prev]["l"] - y_at(line, prev)):
                        touches[-1] = i
        else:
            if bb > y + tol:
                continue
            wick = bar["h"] >= y - tol and bb <= y + tol
            near = abs(bar["h"] - y) <= tol
            pv = i in pivot_set and bar["h"] >= y - tol * 2 and bt <= y + tol * 3
            if wick or near or pv:
                if not touches or i - touches[-1] >= TOUCH_MERGE_BARS:
                    touches.append(i)
                else:
                    prev = touches[-1]
                    if abs(bar["h"] - y) < abs(bars[prev]["h"] - y_at(line, prev)):
                        touches[-1] = i
    recent_from = max(0, n - RECENT_TOUCH_BARS)
    kept = compact_touches_by_gap(touches, recent_from)
    body_breaks = wick_breaks = 0
    if kept:
        for i in range(kept[0], n):
            y = y_at(line, i)
            tol = abs(y) * TOUCH_TOL_PCT
            bar = bars[i]
            if kind == "support":
                if body_top(bar) < y - tol:
                    body_breaks += 1
                elif bar["l"] < y - tol:
                    wick_breaks += 1
            elif body_bot(bar) > y + tol:
                body_breaks += 1
            elif bar["h"] > y + tol:
                wick_breaks += 1
    return {
        "touches": kept,
        "bodyBreaks": body_breaks,
        "wickBreaks": wick_breaks,
        "valid": (
            len(kept) >= MIN_TOUCHES
            and body_breaks <= MAX_BODY_BREAKS
            and wick_breaks <= MAX_WICK_BREAKS
        ),
    }


def is_near_horizontal(line, bars, start):
    mid = bars[(start + len(bars) - 1) // 2]
    mid_p = (mid["h"] + mid["l"]) / 2
    span = len(bars) - 1 - start
    return abs(line["m"]) * span / mid_p < HORIZ_SLOPE_PCT


def apply_creek_or_ice(candidate, bars, start, kind, pivots):
    horiz_name = "creek" if kind == "support" else "ice"
    horiz_label = "Creek" if kind == "support" else "Ice"
    prices = sorted(bars[i]["l"] if kind == "support" else bars[i]["h"] for i in candidate["touches"])
    med = prices[len(prices) // 2]
    horiz = evaluate_line(bars, start, {"m": 0, "b": med}, kind, pivots)
    if horiz["valid"] and len(horiz["touches"]) >= len(candidate["touches"]):
        candidate["line"] = {"m": 0, "b": med}
        candidate["touches"] = horiz["touches"]
        candidate["bodyBreaks"] = horiz["bodyBreaks"]
        candidate["wickBreaks"] = horiz["wickBreaks"]
        candidate["drawFrom"] = horiz["touches"][0]
        candidate["drawTo"] = horiz["touches"][-1]
        candidate["score"] = len(horiz["touches"]) * 10 - horiz["bodyBreaks"] * 3 + 5
    candidate["label"] = horiz_label
    candidate["type"] = horiz_name
    return candidate


def find_best_horizontal(bars, window_size, kind):
    n = len(bars)
    start = max(0, n - window_size)
    highs, lows = find_pivots(bars, PIVOT_LEFT, PIVOT_RIGHT)
    pivots = [i for i in (lows if kind == "support" else highs) if i >= start]
    if len(pivots) < MIN_TOUCHES:
        return None
    prices = []
    for i in pivots:
        p = bars[i]["l"] if kind == "support" else bars[i]["h"]
        if not (p == p):
            continue
        if not any(abs(x - p) / p < 0.0015 for x in prices):
            prices.append(p)
    best = None
    horiz_name = "creek" if kind == "support" else "ice"
    horiz_label = "Creek" if kind == "support" else "Ice"
    for price in prices:
        ev = evaluate_line(bars, start, {"m": 0, "b": price}, kind, pivots)
        if not ev["valid"]:
            continue
        score = len(ev["touches"]) * 10 - ev["bodyBreaks"] * 3 + 8
        if best is None or score > best["score"]:
            best = {
                "kind": kind,
                "line": {"m": 0, "b": price},
                "touches": ev["touches"],
                "bodyBreaks": ev["bodyBreaks"],
                "wickBreaks": ev["wickBreaks"],
                "start": start,
                "drawFrom": ev["touches"][0],
                "drawTo": ev["touches"][-1],
                "windowSize": window_size,
                "score": score,
                "label": horiz_label,
                "type": horiz_name,
            }
    return best


def find_best_line(bars, window_size, kind, force_horiz=False):
    n = len(bars)
    start = max(0, n - window_size)
    highs, lows = find_pivots(bars, PIVOT_LEFT, PIVOT_RIGHT)
    pivots = [i for i in (lows if kind == "support" else highs) if i >= start]
    if len(pivots) < 3:
        return find_best_horizontal(bars, window_size, kind) if force_horiz else None
    best = None
    for a in range(len(pivots) - 1):
        for b in range(a + 1, len(pivots)):
            i1, i2 = pivots[a], pivots[b]
            if i2 - i1 < 5:
                continue
            p1 = bars[i1]["l"] if kind == "support" else bars[i1]["h"]
            p2 = bars[i2]["l"] if kind == "support" else bars[i2]["h"]
            line = line_through(i1, p1, i2, p2)
            if kind == "support" and line["m"] < -1e-9:
                continue
            if kind == "resistance" and line["m"] > 1e-9:
                continue
            ev = evaluate_line(bars, min(i1, i2), line, kind, pivots)
            if not ev["valid"]:
                continue
            score = len(ev["touches"]) * 10 - ev["bodyBreaks"] * 3 + (i2 - i1) * 0.01
            candidate = {
                "kind": kind,
                "line": line,
                "touches": ev["touches"],
                "bodyBreaks": ev["bodyBreaks"],
                "wickBreaks": ev["wickBreaks"],
                "start": start,
                "drawFrom": ev["touches"][0],
                "drawTo": ev["touches"][-1],
                "windowSize": window_size,
                "score": score,
                "label": "向上趨勢線" if kind == "support" else "向下趨勢線",
                "type": "uptrend" if kind == "support" else "downtrend",
            }
            near_h = is_near_horizontal(line, bars, start)
            near_h1000 = False
            if force_horiz:
                mid_p = ((bars[start]["h"] + bars[start]["l"]) / 2) or 1
                drift = abs(line["m"]) * (n - 1 - start)
                near_h1000 = drift / mid_p < HORIZ_SLOPE_PCT * 1.5
            if near_h or near_h1000:
                apply_creek_or_ice(candidate, bars, min(i1, i2), kind, pivots)
            if best is None or candidate["score"] > best["score"]:
                best = candidate
    if force_horiz:
        horiz = find_best_horizontal(bars, window_size, kind)
        if horiz and (best is None or horiz["score"] >= (best.get("score") or 0) or best["type"] in ("creek", "ice")):
            if best is None or horiz["score"] > best["score"]:
                best = horiz
            elif best["type"] in ("uptrend", "downtrend") and is_near_horizontal(best["line"], bars, start):
                best = horiz
        if best and best["type"] in ("uptrend", "downtrend") and is_near_horizontal(best["line"], bars, start):
            apply_creek_or_ice(best, bars, start, kind, pivots)
    return best


def make_parallel_channel(L, bars):
    if not L or not L.get("touches"):
        return None
    n = len(bars)
    eval_start = L.get("start", L.get("drawFrom", L["touches"][0]))
    i0 = L.get("drawFrom", L["touches"][0])
    if i0 >= n - 1:
        return None
    opp = "resistance" if L["kind"] == "support" else "support"
    highs, lows = find_pivots(bars, PIVOT_LEFT, PIVOT_RIGHT)
    pivots = [i for i in (lows if opp == "support" else highs) if i >= eval_start]
    m = L["line"]["m"]
    best = None

    def try_candidate(line, bonus=0):
        nonlocal best
        mid = (i0 + n - 1) // 2
        primary_y = y_at(L["line"], mid)
        cand_y = y_at(line, mid)
        if L["kind"] == "support" and cand_y <= primary_y + 1e-9:
            return
        if L["kind"] == "resistance" and cand_y >= primary_y - 1e-9:
            return
        ev = evaluate_line(bars, eval_start, line, opp, pivots)
        if not ev["valid"]:
            return
        score = len(ev["touches"]) * 10 - ev["bodyBreaks"] * 3 + bonus
        if best is None or score > best["score"]:
            best = {
                "kind": opp,
                "line": line,
                "touches": ev["touches"],
                "bodyBreaks": ev["bodyBreaks"],
                "wickBreaks": ev["wickBreaks"],
                "drawFrom": min(i0, ev["touches"][0]),
                "drawTo": n - 1,
                "start": eval_start,
                "windowSize": L["windowSize"],
                "score": score,
                "label": "平行通道",
                "type": "channel",
                "isParallel": True,
                "pairOf": L["type"],
            }

    for i in pivots:
        if i < i0:
            continue
        price = bars[i]["l"] if opp == "support" else bars[i]["h"]
        try_candidate({"m": m, "b": price - m * i})
    extreme_offset, extreme_i = float("-inf"), i0
    for i in range(i0, n):
        y = y_at(L["line"], i)
        offset = bars[i]["h"] - y if L["kind"] == "support" else y - bars[i]["l"]
        if offset > extreme_offset:
            extreme_offset, extreme_i = offset, i
    if extreme_offset > 0:
        line = {"m": m, "b": L["line"]["b"] + extreme_offset} if L["kind"] == "support" else {"m": m, "b": L["line"]["b"] - extreme_offset}
        try_candidate(line, 1)
    return best


def analyze_symbol(bars):
    up = down = None
    used = None
    for w in WINDOWS:
        if len(bars) < min(w, 80):
            continue
        fh = w >= 1000
        if not up:
            up = find_best_line(bars, w, "support", fh)
        if not down:
            down = find_best_line(bars, w, "resistance", fh)
        if up and down:
            used = w
            break
        if used is None and (up or down):
            used = w
    if not up or not down:
        for w in WINDOWS:
            fh = w >= 1000
            if not up:
                up = find_best_line(bars, w, "support", fh)
            if not down:
                down = find_best_line(bars, w, "resistance", fh)
            if up and down:
                used = max(used or 0, w)
                break
    if not up or not down:
        if not up:
            up = find_best_horizontal(bars, 1000, "support")
        if not down:
            down = find_best_horizontal(bars, 1000, "resistance")
        if (up or down) and used is None:
            used = 1000
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


def recent_signal(row: dict, lookback: int = 5) -> str:
    bars = row.get("bars") or []
    analysis = row.get("analysis") or {}
    if not bars:
        return row.get("error") or "無資料"
    n = len(bars)
    frm = max(0, n - lookback)
    breakout = touch = False
    for key in ("up", "down", "channel"):
        L = analysis.get(key)
        if not L or not L.get("line"):
            continue
        kind = L.get("kind")
        for i in range(frm, n):
            y = y_at(L["line"], i)
            bar = bars[i]
            tol = abs(y) * TOUCH_TOL_PCT
            if kind == "support":
                if body_top(bar) < y - tol:
                    breakout = True
                elif (bar["l"] <= y + tol and body_top(bar) >= y - tol) or abs(bar["l"] - y) <= tol:
                    touch = True
            else:
                if body_bot(bar) > y + tol:
                    breakout = True
                elif (bar["h"] >= y - tol and body_bot(bar) <= y + tol) or abs(bar["h"] - y) <= tol:
                    touch = True
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
