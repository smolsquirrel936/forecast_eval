"""Status report for the live simtrades — one self-contained HTML page.

Reads only what the deployments already write — run folders, the shared state
files beside them, ``shioaji.log`` — plus a snapshot of OS counters, and renders
``status_report_template.html`` with the result embedded as JSON. It never
imports a strategy, never talks to the broker, and never writes anywhere but
``--output``, so it is safe to run beside a live soak.

    uv run --no-sync python -m forecast_eval.live.status_report
    uv run --no-sync python -m forecast_eval.live.status_report --json status.json

Run from the repo root (``night/``), like every other module.
"""
from __future__ import annotations

import argparse
import ast
import csv
import html
import json
import math
import re
import shutil
import statistics
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from ..environment import DAY_CLOSE, DAY_OPEN, NIGHT_CLOSE, NIGHT_OPEN, classify_session
from .artifacts import strategy_sha
from .sessions import MINUTES_PER_SESSION

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PACKAGE_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = Path(__file__).resolve().parent / "status_report_template.html"

# Tech: display order of the known session packages; anything else sorts after.
# Why:  colour follows the entity, never its rank — `day` must be the same blue
#       whether or not `night` is running, so the slot is keyed by name.
KNOWN_ORDER = ("day", "night", "fullday")

# Tech: thresholds at which a finding turns from ✓ to !.
# Why:  a report generated unattended cannot say "this looks high" by judgement;
#       it needs a fixed line. 85 % VRAM leaves ~5 GiB on a 32 GiB card — about
#       one more Toto2-1B process — and a heartbeat is written every few seconds,
#       so five minutes of silence means the loop is not running.
VRAM_WARN = 0.85
COMMIT_WARN = 0.80
HEARTBEAT_STALE_S = 300.0


# --------------------------------------------------------------------------- processes

@dataclass(frozen=True)
class Proc:
    pid: int
    ppid: int
    created: Optional[str]
    cmd: str


def list_processes() -> List[Proc]:
    # Tech: every python.exe with its parent, start time and command line, via CIM.
    # Why:  `uv run` starts three processes per deployment (uv → venv shim → the
    #       real interpreter) and only the leaf holds the GPU and the shioaji
    #       login, so the parent links are needed to pick it.
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
          "Select-Object @{n='pid';e={$_.ProcessId}}, @{n='ppid';e={$_.ParentProcessId}}, "
          "@{n='created';e={if ($_.CreationDate) {$_.CreationDate.ToString('s')}}}, "
          "@{n='cmd';e={$_.CommandLine}} | ConvertTo-Json -Compress")
    rows = _powershell_json(ps)
    if rows is None:
        return []
    if isinstance(rows, dict):
        rows = [rows]
    return [Proc(int(r["pid"]), int(r["ppid"]), r.get("created"), r.get("cmd") or "")
            for r in rows]


def find_processes(procs: Iterable[Proc]) -> Dict[str, Any]:
    """The owner process and one leaf process per `--strategy`, from a process list."""
    procs = list(procs)
    live = [p for p in procs if re.search(r"-m\s+forecast_eval\.live\.cli\b", p.cmd)]
    owner = [p for p in procs if re.search(r"-m\s+forecast_eval\.live\.owner_cli\b", p.cmd)]

    def leaves(group: List[Proc]) -> List[Proc]:
        parents = {p.ppid for p in group}
        return [p for p in group if p.pid not in parents]

    deployments: Dict[str, Proc] = {}
    for p in leaves(live):
        m = re.search(r"--(?:strategy|config)[ =](\S+)", p.cmd)
        if m:
            deployments.setdefault(m.group(1).strip('"'), p)
    own = leaves(owner)
    return {"owner": own[0] if own else None, "deployments": deployments}


# --------------------------------------------------------------------------- sessions

def session_of(ts: datetime) -> Optional[Dict[str, Any]]:
    # Tech: the TAIFEX session a timestamp falls in, keyed by the date it OPENED.
    # Why:  a night session runs 15:00 → 05:00, so 02:00 on 10-01 belongs to the
    #       session that opened on 09-30; labelling by calendar date would split
    #       one session in two at midnight.
    kind = classify_session(ts)
    if kind is None:
        return None
    if kind == "DAY":
        opened = ts.date()
        start = datetime.combine(opened, DAY_OPEN)
        end = datetime.combine(opened, DAY_CLOSE)
    else:
        opened = ts.date() if ts.time() >= NIGHT_OPEN else ts.date() - timedelta(days=1)
        start = datetime.combine(opened, NIGHT_OPEN)
        end = datetime.combine(opened + timedelta(days=1), NIGHT_CLOSE)
    return {"label": f"{kind} {opened:%m-%d}", "kind": kind, "start": start, "end": end}


# --------------------------------------------------------------------------- deployments

@dataclass(frozen=True)
class Deployment:
    slug: str
    run_dir: Path                       # the newest folder: the one being written to
    params: Dict[str, Any]
    soak: Dict[str, Any]
    restart: Dict[str, Any]
    run_dirs: Tuple[Path, ...] = ()     # every folder of the soak, oldest first


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _iter_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except ValueError:
                    continue            # a line being written right now
    except OSError:
        return


def find_deployments(outputs: Path) -> List[Deployment]:
    # Tech: one deployment per `soak_epoch_<slug>.json`, matched to every
    #       `live_*` run folder whose `params.json` carries the same `soak_id`,
    #       oldest first; the newest is also kept as `run_dir`.
    # Why:  the soak id is the only key that ties the shared state files (named
    #       by slug — fullday's is `day_night`) to a run folder (named by
    #       timestamp). A `--resume` restart writes a new folder under the same
    #       id, so the soak is ALL of them. Reading only the newest put every
    #       count back to zero at each restart (MEASURED 2026-10-03: 0 forecasts
    #       on the page, 453 on disk). The newest is the one being written to,
    #       and the only one whose fills/trades cover the whole soak.
    by_soak: Dict[str, List[Path]] = defaultdict(list)
    for d in outputs.glob("live_*"):
        if d.is_dir():
            p = _read_json(d / "params.json")
            if p and p.get("soak_id"):
                by_soak[p["soak_id"]].append(d)
    out = []
    for epoch in sorted(outputs.glob("soak_epoch_*.json")):
        slug = epoch.stem[len("soak_epoch_"):]
        soak = _read_json(epoch)
        if not soak or soak.get("soak_id") not in by_soak:
            continue
        run_dirs = tuple(sorted(by_soak[soak["soak_id"]], key=lambda d: d.name))
        run_dir = run_dirs[-1]
        params = _read_json(run_dir / "params.json") or {}
        restart = _read_json(outputs / f"live_restart_{slug}.json") or {}
        out.append(Deployment(slug, run_dir, params, soak, restart, run_dirs))
    return out


def package_params(package: str) -> Dict[str, Any]:
    # Tech: the package's `PARAMS` literal, read from `wiring.py` with `ast`.
    # Why:  knobs such as night's `long_only` are not in `params.json`, and
    #       importing the wiring would import the forecaster (torch). Parsing the
    #       literal reads the same values with no side effects; `strategy_sha` is
    #       compared separately so a package edited since the run started is
    #       flagged rather than silently described.
    path = PACKAGE_ROOT / "strategy" / package / "wiring.py"
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return {}
    for node in tree.body:
        target = node.targets[0] if isinstance(node, ast.Assign) else getattr(node, "target", None)
        if isinstance(target, ast.Name) and target.id == "PARAMS" and node.value is not None:
            try:
                return ast.literal_eval(node.value)
            except ValueError:
                return {}
    return {}


def _pct(xs: List[float], q: float) -> Optional[float]:
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))]


def _rows(path: Path) -> List[Dict[str, str]]:
    try:
        with open(path, encoding="utf-8", newline="") as f:
            return list(csv.DictReader(f))
    except OSError:
        return []


def collect_performance(dep: Deployment) -> Dict[str, Any]:
    """The per-run report's trading metrics and two charts, for one deployment."""
    # Tech: imported here, not at module level.
    # Why:  `reports` pulls matplotlib and plotly, and `metrics` pulls pandas;
    #       the rest of this module reads plain CSV and JSON, so the heavy
    #       imports stay with the one section that needs them.
    import pandas as pd

    from .. import reports
    from ..metrics import TradeRecord, trading_metrics
    from .prices import minute_series

    # Tech: trades and fills from the newest folder, read by the per-run report's
    #       own reader; `point_value` and `fee_rate` from params.json with the
    #       same TXF defaults `reports.generate_report` falls back to.
    # Why:  the newest folder's files already cover the whole soak (`--resume`
    #       replays its fills — see `collect_run`), and the same reader and the
    #       same defaults are what make these numbers the per-run report's
    #       numbers rather than a second implementation of them.
    trades = reports._read_artifact(dep.run_dir, "trades")
    fills = reports._read_artifact(dep.run_dir, "fills")
    point_value = float(dep.params.get("point_value") or 200.0)
    fee_rate = float(dep.params.get("fee_rate") or 0.0002)
    m = trading_metrics([TradeRecord(**t) for t in trades.to_dict("records")])
    # Why:  NaN is not JSON; null renders as "—" like every other missing value.
    m = {k: None if isinstance(v, float) and math.isnan(v) else v for k, v in m.items()}

    # Tech: the price line is every soak folder's capture at one row a minute,
    #       through the helper the per-run report's price cache is built from.
    # Why:  `prices.soak_price_series` would also give this line, but it writes
    #       `prices_<slug>.csv`, and this report must never write anywhere but
    #       its own output. The soak's folders are the ones its fills came from.
    frames = [f for f in (minute_series(rd / "ticks.csv") for rd in dep.run_dirs or (dep.run_dir,))
              if f is not None]
    ticks = pd.concat(frames).sort_values("timestamp", ignore_index=True) if frames else None

    # Tech: the two plotly figures, as JSON for `Plotly.newPlot` in the page.
    # Why:  the same functions the per-run report draws with, so the charts are
    #       identical to it; without plotly the section keeps its table.
    figs = {"equity": None, "price": None}
    if reports._HAS_PLOTLY:
        figs["equity"] = json.loads(reports.plot_equity_drawdown_plotly(
            trades, ticks=ticks, fee_rate=fee_rate, point_value=point_value).to_json())
        figs["price"] = json.loads(reports.plot_price_fills_plotly(fills, ticks).to_json())
    # Why:  pairs, not a dict — the page JSON is written with sorted keys, and the
    #       table should keep the per-run report's row order.
    return {"metrics": list(m.items()), "point_value": point_value, "figs": figs}


def _num(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _sec(ts: str) -> str:
    # Why:  `orders.csv` writes `2026-10-15 19:33:00` and `fills.csv` adds
    #       nanoseconds; cut to the second as ISO text they compare correctly,
    #       and every browser's `Date` parses them (a space and nine fraction
    #       digits are not part of the format JavaScript guarantees).
    return ts[:19].replace(" ", "T")


def collect_transactions(orders: List[Dict[str, str]], fills: List[Dict[str, str]],
                         trades: List[Dict[str, str]]) -> Dict[str, Any]:
    """One deployment's transaction record: orders with outcomes, closed trades, open lot."""
    fl = sorted(({"ts": _sec(f.get("timestamp", "")), "side": f.get("side", ""),
                  "price": _num(f.get("fill_price")), "qty": int(_num(f.get("quantity")) or 0),
                  "fee": _num(f.get("fee"))} for f in fills), key=lambda f: f["ts"])
    od = sorted(({"ts": _sec(o.get("timestamp", "")), "side": o.get("side", ""),
                  "intent": o.get("intent", ""), "limit": _num(o.get("limit_price")),
                  "qty": int(_num(o.get("quantity")) or 0)} for o in orders), key=lambda o: o["ts"])

    # Tech: each order takes the first unclaimed fill on its side at or after its
    #       own timestamp and before the next order's; an order with none is
    #       "no fill".
    # Why:  `orders.csv` and `fills.csv` share no order id — the venue's `ordno`
    #       lives only in the owner's log. The window is sound because a
    #       deployment holds at most `max_position` and sends its next order only
    #       after the last one filled or was cancelled (the owner cancels an
    #       unfilled limit after 15 min). An unmatched order is the one a reader
    #       most needs to see: on 2026-10-17 night's two closes were cancelled
    #       and its long stayed open.
    claimed: set = set()
    for i, o in enumerate(od):
        end = od[i + 1]["ts"] if i + 1 < len(od) else None
        hit = next((j for j, f in enumerate(fl) if j not in claimed
                    and f["side"] == o["side"] and f["ts"] >= o["ts"]
                    and (end is None or f["ts"] < end)), None)
        if hit is not None:
            claimed.add(hit)
            o.update(outcome="filled", fill_ts=fl[hit]["ts"], fill_price=fl[hit]["price"])
        else:
            o.update(outcome="no fill", fill_ts=None, fill_price=None)

    # Tech: the open position is what FIFO matching of the fills leaves over.
    # Why:  `trades.csv` holds only closed round trips, so a held lot appears in
    #       no file as such; the fills are the one record that covers both.
    lots: List[Dict[str, Any]] = []
    for f in fl:
        sign, q = (1 if f["side"] == "BUY" else -1), f["qty"]
        while q and lots and lots[0]["sign"] != sign:
            take = min(q, lots[0]["qty"])
            lots[0]["qty"] -= take
            q -= take
            if not lots[0]["qty"]:
                lots.pop(0)
        if q:
            lots.append({"sign": sign, "qty": q, "price": f["price"], "ts": f["ts"]})
    open_lots = [{"side": "LONG" if l["sign"] > 0 else "SHORT", "qty": l["qty"],
                  "entry": l["price"], "since": l["ts"]} for l in lots]

    tr = [{"entry_ts": _sec(t.get("entry_ts") or ""), "exit_ts": _sec(t.get("exit_ts") or ""), "side": t.get("side"),
           "entry": _num(t.get("entry_price")), "exit": _num(t.get("exit_price")),
           "points": _num(t.get("pnl_points")), "fees": _num(t.get("fees_points")),
           "net": _num(t.get("net_pnl_points"))} for t in trades]
    return {
        "orders": od, "fills": len(fl), "trades": tr, "open": open_lots,
        "unfilled": sum(o["outcome"] != "filled" for o in od),
        # Tech: fills no order claimed. Never negative: a fill is claimed once.
        # Why:  zero means every fill is explained by an order. More means an
        #       order filled in parts, a fill added by hand (2026-10-06), or a
        #       fill landing after the next order's bar, which the matching
        #       window then misreads — each one a reason to read the files.
        "unmatched_fills": len(fl) - len(claimed),
        "net_points": sum(t["net"] or 0.0 for t in tr),
        "fees_points": sum(f["fee"] or 0.0 for f in fl),
        "wins": sum((t["net"] or 0) > 0 for t in tr),
    }


def collect_run(dep: Deployment, outputs: Path, now: datetime) -> Dict[str, Any]:
    """Everything the report shows about one deployment, as plain JSON-able data."""
    params, d = dep.params, dep.run_dir
    dirs = list(dep.run_dirs or (d,))
    package = params.get("strategy_package") or dep.slug
    # Tech: every run folder's session.jsonl, oldest first, each record tagged
    #       with the index of the folder it came from (`_run`).
    # Why:  the soak is all of its folders (`find_deployments`). `bar_idx`
    #       restarts at 0 in every folder, so a bar is identified by
    #       (folder, bar_idx), never by bar_idx alone.
    events = [{**e, "_run": i} for i, rd in enumerate(dirs)
              for e in _iter_jsonl(rd / "session.jsonl")]
    started = datetime.fromisoformat(dep.soak["started_at"])

    # ---- bars and sessions
    bars = [e for e in events if e.get("event") == "bar"]
    bar_minutes = {e["timestamp"][:16] for e in bars}
    by_sess: Dict[str, Dict[str, Any]] = {}
    for b in bars:
        s = session_of(datetime.fromisoformat(b["timestamp"]))
        if s is None:
            continue
        entry = by_sess.setdefault(s["label"], {**s, "bars": 0, "first": b["timestamp"]})
        entry["bars"] += 1
        entry["last"] = b["timestamp"]

    # ---- ticks
    ticks = {"total": 0, "volume": 0, "trial": 0, "first": None, "last": None,
             "codes": Counter(), "by_session": Counter(), "by_hour": Counter(),
             "lat": [], "max_gap": {}}
    tick_minutes = set()
    prev: Dict[str, datetime] = {}
    for rd in dirs:
        try:
            with open(rd / "ticks.csv", encoding="utf-8", newline="") as f:
                for r in csv.DictReader(f):
                    try:
                        ts = datetime.fromisoformat(r["timestamp"])
                        rc = datetime.fromisoformat(r["received"])
                    except (KeyError, ValueError):
                        continue
                    ticks["total"] += 1
                    ticks["first"] = ticks["first"] or r["timestamp"]
                    ticks["last"] = r["timestamp"]
                    ticks["codes"][r.get("code", "")] += 1
                    ticks["volume"] += int(float(r.get("volume") or 0))
                    ticks["by_hour"][ts.strftime("%Y-%m-%dT%H:00")] += 1
                    ticks["lat"].append((rc - ts).total_seconds())
                    if r.get("simtrade") == "1":
                        ticks["trial"] += 1
                    s = session_of(ts)
                    key = s["label"] if s else "outside"
                    ticks["by_session"][key] += 1
                    if s:
                        tick_minutes.add(r["timestamp"][:16])
                        if key in prev:
                            gap = (ts - prev[key]).total_seconds()
                            ticks["max_gap"][key] = max(ticks["max_gap"].get(key, 0.0), gap)
                        prev[key] = ts
        except OSError:
            pass

    # Tech: per session, the minutes that had a trade but no bar.
    # Why:  a bar is only built for a minute with at least one print, so a bar
    #       count below 300/840 is normally a quiet market, not a loss. A minute
    #       the tick capture HAS but the bars do not is a real gap. Minutes at
    #       or after the newest bar are excluded — that bar may not be flushed yet.
    newest_bar = bars[-1]["timestamp"][:16] if bars else ""
    sessions = []
    for lab, s in sorted(by_sess.items(), key=lambda kv: kv[1]["start"]):
        lost = sorted(m for m in tick_minutes
                      if m < newest_bar and m not in bar_minutes
                      and s["start"] <= datetime.fromisoformat(m) < s["end"])
        sessions.append({
            "label": lab, "kind": s["kind"], "bars": s["bars"],
            "expected": MINUTES_PER_SESSION[s["kind"]],
            "first": s["first"], "last": s["last"],
            "start": s["start"].isoformat(), "end": s["end"].isoformat(),
            "complete": now >= s["end"], "lost_minutes": lost,
        })

    # ---- forecasts and decisions
    pkg = package_params(package)
    long_only = bool(pkg.get("long_only", False))
    buy, sell = params.get("buy_threshold"), params.get("sell_threshold")
    min_prob, horizon = params.get("min_prob"), params.get("horizon_bars") or 30
    # Tech: a routed package's per-session band and gate, `{"day": {...}, ...}`,
    #       read from its `<half>_` knobs; None for a single-strategy package.
    # Why:  hybrid's unprefixed band knobs are None, so every forecast read as
    #       inside the band. Each forecast is judged by the half of its own
    #       session. Read from the package like `long_only`, so a `--param` on
    #       a half knob is not reflected (`overridden` flags that run).
    halves = {h: {"buy": pkg.get(f"{h}_buy_threshold"),
                  "sell": pkg.get(f"{h}_sell_threshold"),
                  "min_prob": pkg.get(f"{h}_min_prob")}
              for h in ("day", "night") if f"{h}_buy_threshold" in pkg} or None
    # Tech: each bar's position in the soak's bar series, keyed (folder, bar_idx).
    # Why:  the realized return is the price `horizon` bars after the forecast's
    #       bar. Counting along the whole series rather than within one folder
    #       lets a forecast whose horizon crosses a restart still be scored: the
    #       bars after a restart are the next bars the model saw, the same splice
    #       the live context makes from the bar cache.
    bar_pos = {(b["_run"], b["bar_idx"]): i for i, b in enumerate(bars)}
    signals = {e["timestamp"]: e for e in events if e.get("event") == "signal"}
    rows, band_pass, prob_pass, hits, n_eval, abs_err = [], 0, 0, 0, 0, []
    vetoes: Counter = Counter()
    for e in (e for e in events if e.get("event") == "forecast"):
        p = e.get("payload", {})
        r, pu = p.get("predicted_return"), p.get("prob_up")
        if r is None:
            continue
        if halves:
            sess = classify_session(datetime.fromisoformat(e["timestamp"]))
            half = halves.get((sess or "").lower(), {})
            buy, sell, min_prob = half.get("buy"), half.get("sell"), half.get("min_prob")
        direction = "BUY" if buy is not None and r >= buy else (
            "SELL" if sell is not None and r <= sell else None)
        crossed = direction is not None
        prob_ok = crossed and (min_prob is None or (
            pu is not None and (pu if direction == "BUY" else 1 - pu) >= min_prob))
        band_pass += crossed
        prob_pass += prob_ok
        s = signals.get(e["timestamp"], {})
        # Tech: for a forecast that passed band and min_prob but whose logged
        #       signal is HOLD, name the emitter veto that held it: "trend" when
        #       the median path's midpoint sits on the signal's side of
        #       last_price (the path check passes), else "trend or path".
        # Why:  `ThresholdEmitter._decide` runs the trend filter, then the path
        #       check, and nothing else after min_prob. The path check is
        #       stateless, so the payload re-runs it exactly; the trend filter's
        #       price window holds warm-up samples that are never logged, so it
        #       is named only by elimination. When both checks fail, the trend
        #       filter, which runs first, may have been the one that blocked it.
        veto = None
        if prob_ok and s.get("direction") == "HOLD":
            path, last = p.get("median_path"), p.get("last_price")
            path_ok = (not path or last is None or len(path) < 2
                       or (path[len(path) // 2] >= last if direction == "BUY"
                           else path[len(path) // 2] <= last))
            veto = "trend" if path_ok else "trend or path"
            vetoes[veto] += 1
        i = bar_pos.get((e["_run"], e["bar_idx"]))
        future = (bars[i + horizon]["price"]
                  if i is not None and i + horizon < len(bars) else None)
        last = p.get("last_price")
        realized = (future / last - 1) if (future and last) else None
        if realized is not None and r != 0 and realized != 0:
            n_eval += 1
            hits += (r > 0) == (realized > 0)
            if p.get("predicted_price") is not None:
                abs_err.append(abs(p["predicted_price"] - future))
        rows.append({"ts": e["timestamp"], "ret": r, "prob": pu, "realized": realized,
                     "sig": s.get("direction", "HOLD"), "crossed": crossed, "veto": veto})
    sig_counts = Counter(s.get("direction") for s in signals.values())
    # Tech: orders summed over every folder; fills and trades from the newest.
    # Why:  a `--resume` seeds the soak's earlier fills into the new run
    #       (`live/cli.py`, `fills_replayed`), so the newest folder's fills.csv
    #       and trades.csv already cover the whole soak and summing them would
    #       double-count. Orders are not replayed, so each folder holds only its own.
    orders = [r for rd in dirs for r in _rows(rd / "orders.csv")]
    fills, trades = (_rows(d / f) for f in ("fills.csv", "trades.csv"))

    # ---- positions: the deployment's view and the owner's
    pos = [b.get("position", 0) for b in bars]
    owner_pos: Counter = Counter()
    owner_last = None
    for o in _iter_jsonl(outputs / f"events_{dep.slug}.jsonl"):
        if o.get("kind") == "position":
            owner_pos[o.get("signed_qty")] += 1
            owner_last = o.get("signed_qty")

    # ---- stability
    relogins = [e for e in events if e.get("event") == "relogin"]
    failed = [e for e in events if e.get("event") == "relogin_failed"]
    verified = [e for e in events if e.get("event") == "verified"]
    alerts = Counter(f'{a.get("severity")}:{a.get("kind")}' for a in events
                     if a.get("event") == "alert")
    warm = next((e for e in reversed(events) if e.get("event") == "warm_up"), {})
    hb = dep.restart.get("last_heartbeat")
    lat = ticks.pop("lat")

    return {
        "slug": dep.slug, "package": package, "dir": d.name, "dirs": [x.name for x in dirs],
        "started": started.isoformat(), "uptime_h": (now - started).total_seconds() / 3600,
        "heartbeat": hb,
        "heartbeat_age_s": max(0.0, (now - datetime.fromisoformat(hb)).total_seconds()) if hb else None,
        "params": {
            "sessions": params.get("sessions", []),
            "buy": None if halves else buy, "sell": None if halves else sell,
            "min_prob": None if halves else min_prob, "halves": halves, "horizon": horizon, "stride": params.get("stride_bars"),
            "ctx": params.get("context_length"), "ckpt": params.get("checkpoint"),
            "max_position": params.get("max_position"), "long_only": long_only,
            "git": params.get("git_commit"), "dirty": params.get("git_dirty"),
            "strategy_sha": params.get("strategy_sha"),
            "package_sha_now": strategy_sha(package) if (PACKAGE_ROOT / "strategy" / package).is_dir() else None,
            "overridden": params.get("strategy_overridden"), "soak_id": params.get("soak_id"),
        },
        "warm_up": {k: warm.get(k) for k in ("bars", "first", "last", "emitter_primed")},
        "sessions": sessions,
        "bars": len(bars),
        "ticks": {**ticks, "codes": dict(ticks["codes"]), "by_session": dict(ticks["by_session"]),
                  "by_hour": sorted(ticks["by_hour"].items()),
                  "lat_med": _pct(lat, .5), "lat_p95": _pct(lat, .95), "lat_p99": _pct(lat, .99)},
        "decisions": {
            "forecasts": len(rows), "signals": dict(sig_counts),
            "nonhold": sum(v for k, v in sig_counts.items() if k != "HOLD"),
            "band_pass": band_pass, "prob_pass": prob_pass, "vetoes": dict(vetoes),
            "orders": len(orders), "fills": len(fills), "trades": len(trades),
            "pnl_points": sum(float(t.get("net_pnl_points") or 0) for t in trades),
            "hit": hits, "n_eval": n_eval,
            "mae_pts": statistics.mean(abs_err) if abs_err else None, "rows": rows,
        },
        "position": {
            "now": pos[-1] if pos else None, "values": sorted(set(pos)),
            "changes": sum(1 for a, b in zip(pos, pos[1:]) if a != b),
            "owner_events": sum(owner_pos.values()),
            "owner_values": {str(k): v for k, v in owner_pos.items()}, "owner_now": owner_last,
        },
        "performance": collect_performance(dep),
        "transactions": collect_transactions(orders, fills, trades),
        "stability": {
            "relogins": [e.get("when") for e in relogins],
            "relogin_failed": [{"when": e.get("wall"), "backoff": e.get("backoff_seconds")} for e in failed],
            "verified": len(verified),
            "token_age": [round(e["token_age_seconds"]) for e in verified if "token_age_seconds" in e],
            "alerts": dict(alerts),
            "feed": dict(Counter(e.get("verdict") for e in events if e.get("event") == "feed_check")),
            "consecutive_failures": dep.restart.get("consecutive_failures"),
            "backfills": dep.restart.get("backfills_today"),
            "retired": dep.soak.get("retired", []),
        },
    }


# --------------------------------------------------------------------------- shioaji.log

_LOG_LINE = re.compile(r"^\[pid:(\d+)\] (\S+?)Z\s+(\w+) (\S+): (.*)$")


def parse_shioaji_log(path: Path, pids: Iterable[int],
                      tz: Optional[tzinfo] = None) -> Dict[str, Dict[str, Any]]:
    # Tech: per PID, every "Stored new token" line (a successful login), every
    #       "performing new login" line (an attempt) and every ERROR line, with
    #       the UTC stamp converted to local time.
    # Why:  the owner prints only to its console, so the SDK's own log is the one
    #       record of its logins that survives; it is also the only place a
    #       deployment's login is visible from outside its session.jsonl.
    wanted = {str(p) for p in pids}
    out: Dict[str, Dict[str, Any]] = {p: {"logins": [], "attempts": 0, "errors": []} for p in wanted}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return out
    for line in lines:
        m = _LOG_LINE.match(line)
        if not m or m.group(1) not in wanted:
            continue
        pid, stamp, level, _module, msg = m.groups()
        try:
            utc = datetime.fromisoformat(stamp[:26] + "+00:00")
        except ValueError:
            continue
        local = utc.astimezone(tz).replace(tzinfo=None).isoformat(timespec="seconds")
        if "performing new login" in msg:
            out[pid]["attempts"] += 1
        elif "Stored new token" in msg:
            out[pid]["logins"].append(local)
        elif level == "ERROR":
            out[pid]["errors"].append({"when": local, "msg": msg[:200]})
    return out


# --------------------------------------------------------------------------- hardware

def _powershell_json(script: str) -> Any:
    exe = shutil.which("pwsh") or shutil.which("powershell")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-Command", script],
                             capture_output=True, text=True, timeout=60, check=True).stdout
        return json.loads(out) if out.strip() else None
    except (subprocess.SubprocessError, ValueError, OSError):
        return None


def read_hardware(pids: Iterable[int]) -> Dict[str, Any]:
    # Tech: one snapshot — per-PID dedicated/shared VRAM from the Windows
    #       `GPU Process Memory` counters, RAM/CPU from Get-Process, the card
    #       totals from nvidia-smi, and the system commit charge from CIM.
    # Why:  on a WDDM display GPU nvidia-smi reports per-process memory as N/A,
    #       so the OS counters are the only per-process source. Each probe
    #       degrades to None on its own, so a missing tool blanks one tile rather
    #       than failing the report.
    pids = [int(p) for p in pids]
    procs = None
    if pids:
        plist = ",".join(map(str, pids))
        procs = _powershell_json(rf"""
$d = (Get-Counter '\GPU Process Memory(*)\Dedicated Usage' -ErrorAction SilentlyContinue).CounterSamples
$s = (Get-Counter '\GPU Process Memory(*)\Shared Usage' -ErrorAction SilentlyContinue).CounterSamples
@(foreach ($id in @({plist})) {{
  $pr = Get-Process -Id $id -ErrorAction SilentlyContinue
  if (-not $pr) {{ continue }}
  $dd = ($d | Where-Object {{ $_.InstanceName -match "pid_${{id}}_" }} | Measure-Object CookedValue -Sum).Sum
  $ss = ($s | Where-Object {{ $_.InstanceName -match "pid_${{id}}_" }} | Measure-Object CookedValue -Sum).Sum
  [pscustomobject]@{{pid=$id; ded=[math]::Round($dd/1MB,1); shr=[math]::Round($ss/1MB,1);
    ws=[math]::Round($pr.WorkingSet64/1MB); priv=[math]::Round($pr.PrivateMemorySize64/1MB);
    cpu=[math]::Round($pr.CPU); threads=$pr.Threads.Count; handles=$pr.HandleCount}}
}}) | ConvertTo-Json -Compress
""")
        if isinstance(procs, dict):
            procs = [procs]
    mem = _powershell_json(
        "$o=Get-CimInstance Win32_OperatingSystem; @{ram=$o.TotalVisibleMemorySize/1MB;"
        "ram_free=$o.FreePhysicalMemory/1MB; commit=$o.TotalVirtualMemorySize/1MB;"
        "commit_free=$o.FreeVirtualMemory/1MB; cpu=(Get-CimInstance Win32_Processor | "
        "Select-Object -First 1).Name.Trim()} | ConvertTo-Json -Compress")
    gpu = None
    smi = shutil.which("nvidia-smi")
    if smi:
        try:
            q = subprocess.run([smi, "--query-gpu=name,memory.used,memory.total,utilization.gpu,"
                                "temperature.gpu,power.draw,power.limit,driver_version",
                                "--format=csv,noheader,nounits"],
                               capture_output=True, text=True, timeout=30, check=True)
            f = [x.strip() for x in q.stdout.strip().splitlines()[0].split(",")]
            gpu = {"name": f[0], "used": float(f[1]), "total": float(f[2]), "util": float(f[3]),
                   "temp": float(f[4]), "power": float(f[5]), "power_limit": float(f[6]),
                   "driver": f[7]}
        except (subprocess.SubprocessError, ValueError, IndexError, OSError):
            gpu = None
    return {"procs": procs or [], "gpu": gpu, "mem": mem}


# --------------------------------------------------------------------------- findings

def _dur(hours: float) -> str:
    return f"{int(hours // 24)}d {int(hours % 24)}h {int(round(hours * 60 % 60)):02d}m"


def _hhmm(iso: str) -> str:
    return iso[5:16].replace("T", " ")


def findings(data: Mapping[str, Any]) -> List[Dict[str, str]]:
    # Tech: the "Key findings" list — each item a fixed rule over the collected
    #       data, levelled ok / warn / info.
    # Why:  the page is meant to be regenerated unattended, so nothing in it may
    #       depend on someone's reading of the numbers. Every sentence here is a
    #       template filled from data; there is no free-form commentary.
    runs = data["runs"]
    if not runs:
        return [{"level": "warn", "html": "<b>No deployments found</b> under the outputs folder."}]
    out = []
    esc = html.escape
    names = {r["package"]: r["package"].capitalize() for r in runs}

    stopped = [r for r in runs if not r["running"]]
    stale = [r for r in runs if r["heartbeat_age_s"] is not None and r["heartbeat_age_s"] > HEARTBEAT_STALE_S]
    fails = [r for r in runs if (r["stability"]["consecutive_failures"] or 0) > 0]
    up = [r["uptime_h"] for r in runs]
    if stopped or stale or fails:
        bits = []
        if stopped:
            bits.append("no live process for " + ", ".join(names[r["package"]] for r in stopped))
        if stale:
            bits.append("stale heartbeat for " + ", ".join(
                f'{names[r["package"]]} ({r["heartbeat_age_s"] / 60:.0f} min)' for r in stale))
        if fails:
            bits.append("consecutive failures recorded for " + ", ".join(names[r["package"]] for r in fails))
        out.append({"level": "warn", "html": f"<b>Not every deployment is healthy:</b> {esc('; '.join(bits))}."})
    else:
        newest = max(r["heartbeat_age_s"] or 0 for r in runs)
        out.append({"level": "ok", "html":
                    f"<b>All {len(runs)} deployments are running with fresh heartbeats.</b> Up "
                    f"{_dur(min(up))}–{_dur(max(up))}; the oldest heartbeat is {newest:.0f} s old and "
                    f"no consecutive failures are recorded."})

    parts = []
    for r in runs:
        done = [s for s in r["sessions"] if s["complete"]]
        kinds = Counter(s["kind"].lower() for s in done)
        open_ = any(not s["complete"] for s in r["sessions"])
        parts.append(f'{names[r["package"]]} {len(done)} ('
                     + " + ".join(f"{v} {k}" for k, v in kinds.items()) + ")"
                     + (" + 1 open" if open_ else ""))
    out.append({"level": "ok", "html": "<b>Sessions passed:</b> " + esc(", ".join(parts)) + "."})

    lost = {r["package"]: sum(len(s["lost_minutes"]) for s in r["sessions"]) for r in runs}
    ticks = ", ".join(f'{names[r["package"]]} {r["ticks"]["total"]:,}' for r in runs)
    if any(lost.values()):
        out.append({"level": "warn", "html":
                    f"<b>Data gaps found.</b> Minutes with trades on the tape but no bar: "
                    + esc(", ".join(f"{names[k]} {v}" for k, v in lost.items() if v))
                    + f". Ticks received: {esc(ticks)}."})
    else:
        out.append({"level": "ok", "html":
                    f"<b>No data gaps.</b> Ticks received: {esc(ticks)}. Every minute that had a trade "
                    f"on the tape has a bar; sessions short of 300/840 bars are minutes with no trade."})

    dec = []
    for r in runs:
        d, n = r["decisions"], names[r["package"]]
        v = d["vetoes"]
        vetoed = f'{v.get("trend", 0)} vetoed by the trend filter'
        if v.get("trend or path"):
            vetoed += f', {v["trend or path"]} by the trend filter or path check'
        s = (f'{n}: {d["forecasts"]} forecasts, {d["band_pass"]} crossed the band, '
             f'{d["band_pass"] - d["prob_pass"]} failed min_prob, {vetoed}, '
             f'{d["nonhold"]} signals, {d["orders"]} orders')
        sells = d["signals"].get("SELL", 0)
        if r["params"]["long_only"] and sells and d["orders"] == 0:
            s += f' — long-only, so its {sells} SELL signal(s) while flat stood aside'
        dec.append(s + ".")
    total_orders = sum(r["decisions"]["orders"] for r in runs)
    out.append({"level": "info", "html": f"<b>{'No orders yet' if total_orders == 0 else f'{total_orders} orders placed'}.</b> "
                + esc(" ".join(dec))})

    hit = ", ".join(f'{names[r["package"]]} {r["decisions"]["hit"] / r["decisions"]["n_eval"]:.0%} '
                    f'({r["decisions"]["hit"]}/{r["decisions"]["n_eval"]})'
                    for r in runs if r["decisions"]["n_eval"])
    if hit:
        out.append({"level": "info", "html": f"<b>Forecast direction hit rate</b> (sign of the "
                    f"{runs[0]['params']['horizon']}-bar return, horizon finished): {esc(hit)}."})

    nonflat = [r for r in runs if any(v != 0 for v in r["position"]["values"])
               or any(k not in ("0", "None") for k in r["position"]["owner_values"])]
    out.append({"level": "info" if nonflat else "ok", "html":
                ("<b>Positions taken:</b> " + esc(", ".join(names[r["package"]] for r in nonflat))
                 + f". Now: " + esc(", ".join(f'{names[r["package"]]} {r["position"]["now"]}' for r in runs)) + "."
                 if nonflat else
                 "<b>Flat the whole time.</b> Every bar recorded position 0 and every owner position "
                 f"event reported 0 ({sum(r['position']['owner_events'] for r in runs):,} events).")})

    unmatched = [r for r in runs if r["transactions"]["unmatched_fills"]]
    if unmatched:
        out.append({"level": "warn", "html":
                    "<b>Fill records that no order explains:</b> "
                    + esc(", ".join(f'{names[r["package"]]} {r["transactions"]["unmatched_fills"]} of '
                                    f'{r["transactions"]["fills"]}' for r in unmatched))
                    + ". An order filled in parts, a fill added to the ledger by hand, or a fill that "
                      "arrived after the next order's bar; compare fills.csv with orders.csv (§6)."})

    logins = data["logins"]
    procs = [("Owner", data["owner"]["pid"])] if data["owner"] else []
    procs += [(names[r["package"]], r["pid"]) for r in runs if r["pid"]]
    counts = ", ".join(f"{n} {len(logins.get(str(p), {}).get('logins', []))}" for n, p in procs)
    failed = [(names[r["package"]], f) for r in runs for f in r["stability"]["relogin_failed"]]
    errors = [(n, e) for n, p in procs for e in logins.get(str(p), {}).get("errors", [])]
    unverified = [names[r["package"]] for r in runs
                  if r["stability"]["verified"] < len(r["stability"]["relogins"])]
    text = f"<b>Logins</b> (successful, from shioaji.log): {esc(counts)}."
    if failed:
        text += " Failed re-login attempts: " + esc(", ".join(f"{n} at {_hhmm(f['when'])}" for n, f in failed)) + "."
    if unverified:
        text += " Re-login not confirmed by a later check: " + esc(", ".join(unverified)) + "."
    if errors:
        text += " SDK errors: " + esc(", ".join(f"{n} at {_hhmm(e['when'])}" for n, e in errors)) + "."
    if not (failed or unverified or errors):
        text += " Every re-login was confirmed and no SDK errors were logged."
    out.append({"level": "warn" if (failed or unverified or errors) else "ok", "html": text})

    hw = data["hardware"]
    gpu, mem = hw.get("gpu"), hw.get("mem")
    if gpu:
        frac = gpu["used"] / gpu["total"]
        sim = sum(p["ded"] for p in hw["procs"] if p["pid"] in {r["pid"] for r in runs})
        text = (f"<b>VRAM {frac:.0%} used</b> ({gpu['used'] / 1024:.1f} of {gpu['total'] / 1024:.1f} GiB); "
                f"the deployments hold {sim / 1024:.1f} GiB and {(gpu['total'] - gpu['used']) / 1024:.1f} GiB is free.")
        level = "warn" if frac >= VRAM_WARN else "ok"
        if mem:
            cfrac = (mem["commit"] - mem["commit_free"]) / mem["commit"]
            text += f" System commit {cfrac:.0%} ({mem['commit'] - mem['commit_free']:.0f} of {mem['commit']:.0f} GiB)."
            if cfrac >= COMMIT_WARN:
                level = "warn"
        if level == "warn":
            text += f" Above the {VRAM_WARN:.0%} VRAM / {COMMIT_WARN:.0%} commit warning line."
        out.append({"level": level, "html": text})
    else:
        out.append({"level": "info", "html": "<b>Hardware snapshot unavailable</b> (nvidia-smi not found)."})
    return out


def attribute_fatal(fatal: List[Dict[str, Any]], runs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    # Tech: each `fatal.jsonl` entry, tagged with the retired soak whose lifetime
    #       contains it, else left unattributed.
    # Why:  `fatal.jsonl` is shared by every deployment and carries no slug, so
    #       "which run crashed" can only be recovered from the soak epochs'
    #       timing; when the timing does not settle it, saying so beats a guess.
    out = []
    for f in fatal:
        when = f.get("when", "")
        tag = None
        for r in runs:
            for old in r["stability"]["retired"]:
                if old.get("started_at", "") <= when <= old.get("retired_at", ""):
                    tag = (f'{r["package"]}: soak {old["soak_id"]}, '
                           f'replaced at {old["retired_at"][:19].replace("T", " ")}')
        out.append({"when": when[:19], "kind": f.get("kind"), "message": f.get("message", "")[:400],
                    "attributed": tag})
    return out


# --------------------------------------------------------------------------- assembly

def collect(outputs: Path, log: Path, now: datetime, *,
            processes: Optional[List[Proc]] = None,
            hardware: Optional[Dict[str, Any]] = None,
            tz: Optional[tzinfo] = None) -> Dict[str, Any]:
    """Collect every input into one dict. `processes`/`hardware` are injectable for tests."""
    found = find_processes(list_processes() if processes is None else processes)
    runs = []
    for dep in find_deployments(outputs):
        r = collect_run(dep, outputs, now)
        proc = found["deployments"].get(r["package"])
        r["pid"] = proc.pid if proc else None
        r["running"] = proc is not None
        runs.append(r)
    runs.sort(key=lambda r: (KNOWN_ORDER.index(r["package"]) if r["package"] in KNOWN_ORDER
                             else len(KNOWN_ORDER), r["package"]))
    owner = found["owner"]
    pids = [r["pid"] for r in runs if r["pid"]] + ([owner.pid] if owner else [])
    data = {
        "generated": now.isoformat(timespec="seconds"),
        "outputs": str(outputs),
        "session_hours": {"day_open": DAY_OPEN.strftime("%H:%M"), "day_close": DAY_CLOSE.strftime("%H:%M"),
                          "night_open": NIGHT_OPEN.strftime("%H:%M"), "night_close": NIGHT_CLOSE.strftime("%H:%M")},
        "runs": runs,
        "owner": {"pid": owner.pid, "started": owner.created} if owner else None,
        "logins": parse_shioaji_log(log, pids, tz),
        "hardware": read_hardware(pids) if hardware is None else hardware,
    }
    data["fatal"] = attribute_fatal(list(_iter_jsonl(outputs / "fatal.jsonl")), runs)
    data["findings"] = findings(data)
    return data


def render(data: Mapping[str, Any], template: Path = TEMPLATE) -> str:
    # Tech: substitute the data, as JSON with `</` escaped, into the template.
    # Why:  the page must open offline from a file share, so it carries its data
    #       and code inline; escaping `</` stops a log message containing
    #       `</script>` from ending the script block early.
    payload = json.dumps(data, default=str, sort_keys=True).replace("</", "<\\/")
    # Tech: plotly.js goes in inline, once, before the data; empty without plotly.
    # Why:  the same offline rule — a CDN link would leave the performance charts
    #       blank on a machine without internet. Substituted first so a data
    #       string that happens to contain the placeholder is never expanded.
    try:
        from plotly.offline import get_plotlyjs
        plotly_js = get_plotlyjs()
    except ImportError:
        plotly_js = ""
    page = template.read_text(encoding="utf-8").replace("__PLOTLY_JS__", plotly_js)
    return page.replace("__STATUS_DATA__", payload)


def default_output(now: datetime, root: Optional[Path] = None) -> Path:
    # Tech: `simtrade_status_report_<YYYYMMDD_HHMMSS>.html` at the repo root,
    #       stamped with the report time.
    # Why:  one fixed name meant every run overwrote the last, so a page could
    #       not be compared with an earlier one. The stamp is the report time
    #       rather than the wall clock, so `--now` gives the same name as well as
    #       the same page, and the names sort in time order.
    return (root or REPO_ROOT) / f"simtrade_status_report_{now:%Y%m%d_%H%M%S}.html"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--outputs", type=Path, default=REPO_ROOT / "outputs",
                    help="folder holding the run folders and soak_epoch_<slug>.json (default: outputs/)")
    ap.add_argument("--log", type=Path, default=REPO_ROOT / "shioaji.log",
                    help="the shioaji SDK log (default: shioaji.log at the repo root)")
    ap.add_argument("--output", type=Path, default=None,
                    help="where to write the HTML (default: simtrade_status_report_<YYYYMMDD_HHMMSS>.html "
                         "at the repo root, stamped with the report time)")
    ap.add_argument("--json", type=Path, default=None, help="also write the collected data as JSON")
    ap.add_argument("--now", type=datetime.fromisoformat, default=None,
                    help="report time (ISO, local); default now. Fixes uptime/ages for a reproducible build")
    args = ap.parse_args(argv)

    now = args.now or datetime.now().replace(microsecond=0)
    output = args.output or default_output(now)
    data = collect(args.outputs, args.log, now)
    output.write_text(render(data), encoding="utf-8")
    if args.json:
        args.json.write_text(json.dumps(data, default=str, indent=1, sort_keys=True), encoding="utf-8")
    for f in data["findings"]:
        mark = {"ok": "ok  ", "warn": "WARN", "info": "info"}[f["level"]]
        print(f"[{mark}] {re.sub(r'<[^>]+>', '', html.unescape(f['html']))}")
    print(f"\nreport: {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
