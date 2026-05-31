"""Log writers (SPEC §6 Phase 4, §8).

Persists the four backtest artifacts as parquet (fallback to CSV when
``pyarrow``/``fastparquet`` aren't available):
  * fills.parquet      — every executed FillEvent
  * orders.parquet     — every OrderEvent submitted (open, close, exit, forced)
  * forecasts.parquet  — every Forecast (with predicted_return / price)
  * signals.parquet    — every SignalEmitter output + tick price at emit
  * trades.parquet     — round-trips (entry + exit pairs)
"""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence, Union

import pandas as pd

from .events import FillEvent, Forecast, OrderEvent
from .metrics import TradeRecord, build_trades
from .environment import classify_session


PathLike = Union[str, Path]

# Anchor default outputs to <myPaper>/outputs/, regardless of CWD.
# forecast_eval/ lives directly under myPaper/, so parent.parent of this file
# is the project root.
# Tech: resolve outputs/ relative to this file's location, not the process CWD.
# Why:  runs are launched from various directories; anchoring to the source tree
#       means artifacts always land in the same place rather than scattering.
DEFAULT_OUTPUT_ROOT = Path(__file__).resolve().parent.parent / "outputs"


def timestamped_run_dir(name: str, root: Optional[PathLike] = None) -> Path:
    """Return ``<root>/<name>_<YYYYMMDD_HHMMSS>/``. Does not create the dir."""
    # Tech: join the chosen root with "<name>_<timestamp>" and return the Path
    #       without creating it on disk.
    # Why:  a second-resolution timestamp keeps successive runs from overwriting
    #       each other; not creating the dir lets the caller decide whether the run
    #       actually proceeds before any directory appears.
    base = Path(root) if root is not None else DEFAULT_OUTPUT_ROOT
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return base / f"{name}_{stamp}"


def write_params(params: Mapping[str, Any], path: PathLike) -> Path:
    """Dump hyperparameters as pretty JSON. Non-serializable values become str()."""
    # Tech: ensure the parent dir exists, then write indented JSON with a str()
    #       fallback for anything not natively serializable.
    # Why:  params.json is the run's reproducibility record; default=str means a
    #       Path or numpy scalar won't crash the dump — a readable string is far
    #       better than a failed write that loses the whole provenance record.
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(dict(params), f, indent=2, default=str)
    return p


def _write(df: pd.DataFrame, path: Path) -> Path:
    # Tech: make the parent dir; for a .parquet target, try to_parquet and on any
    #       failure fall back to a sibling .csv; non-parquet targets write CSV.
    # Why:  parquet needs an engine (pyarrow/fastparquet) that may be absent; the
    #       silent CSV fallback means a missing optional dep degrades the format
    #       rather than aborting a finished backtest. Returns the path actually used.
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == ".parquet":
        try:
            df.to_parquet(path, index=False)
            return path
        except Exception:
            csv_path = path.with_suffix(".csv")
            df.to_csv(csv_path, index=False)
            return csv_path
    df.to_csv(path, index=False)
    return path


def write_fills(fills: Sequence[FillEvent], path: PathLike) -> Path:
    # Tech: turn each FillEvent dataclass into a row and write the frame.
    # Why:  asdict flattens the dataclass to columns automatically, so the schema
    #       tracks the event definition with no manual field list to keep in sync.
    df = pd.DataFrame([asdict(f) for f in fills])
    return _write(df, Path(path))


def write_orders(orders: Sequence[OrderEvent], path: PathLike) -> Path:
    # Tech: same dataclass→rows conversion for OrderEvents.
    # Why:  the orders log captures intent (including orders that never filled),
    #       which fills alone can't show — useful for debugging missed executions.
    df = pd.DataFrame([asdict(o) for o in orders])
    return _write(df, Path(path))


def write_forecasts(forecasts: Sequence[Forecast], path: PathLike) -> Path:
    # Tech: flatten each Forecast, lifting the scalar payload fields (predicted
    #       return/price, last price) to top-level columns and dropping the rest.
    # Why:  payload is an opaque dict (it can hold a whole median_path array); a flat
    #       tabular file only wants the scalars, and guarding isinstance(dict) keeps a
    #       non-dict payload from blowing up the writer.
    rows = []
    for f in forecasts:
        payload = f.payload if isinstance(f.payload, dict) else {}
        rows.append({
            "timestamp": f.timestamp,
            "horizon_bars": f.horizon_bars,
            "predicted_return": payload.get("predicted_return"),
            "predicted_price": payload.get("predicted_price"),
            "last_price": payload.get("last_price"),
        })
    return _write(pd.DataFrame(rows), Path(path))


def write_signals(signals: Iterable[dict], path: PathLike) -> Path:
    # Tech: signals are already plain dicts, so build the frame directly.
    # Why:  the run loop records signals as dicts (with emit-time price/session); no
    #       conversion is needed, and materializing the iterable to a list lets pandas
    #       infer columns in one shot.
    return _write(pd.DataFrame(list(signals)), Path(path))


def write_trades(trades: Sequence[TradeRecord], path: PathLike) -> Path:
    # Tech: flatten each TradeRecord dataclass to a row.
    # Why:  round-trips are the unit of trading analysis; persisting them means
    #       reports/notebooks can read PnL per trade without re-pairing fills.
    df = pd.DataFrame([asdict(t) for t in trades])
    return _write(df, Path(path))


def write_all(
    result,
    output_dir: PathLike,
    *,
    ext: str = ".parquet",
    params: Optional[Mapping[str, Any]] = None,
    auto_report: bool = True,
    report_data_path: Optional[PathLike] = None,
) -> dict:
    """Convenience: dump every artifact under ``output_dir/<name>{ext}``.

    If ``params`` is given, also writes ``output_dir/params.json`` so the
    hyperparameters that produced the run are saved alongside the data.

    When ``auto_report`` is True (default), runs ``reports.generate_report``
    on the just-written bundle (SPEC §4.9). Set False to skip — useful in
    sweeps where only aggregate numbers matter. ``report_data_path`` is an
    optional ticks CSV used as the price-chart background.

    Returns a dict mapping artifact name → written path (may be the CSV
    fallback if parquet libs are missing).
    """
    # Tech: rebuild trades from the fills, then write all five artifacts, recording
    #       the actual path each landed at.
    # Why:  trades aren't stored on the result, so they're derived here once; keeping
    #       the returned paths lets callers report exactly what was written (parquet
    #       or the CSV fallback).
    d = Path(output_dir)
    trades = build_trades(result.fills, session_lookup=classify_session)
    written = {
        "fills":     write_fills(result.fills, d / f"fills{ext}"),
        "orders":    write_orders(result.orders, d / f"orders{ext}"),
        "forecasts": write_forecasts(result.forecasts, d / f"forecasts{ext}"),
        "signals":   write_signals(result.signals, d / f"signals{ext}"),
        "trades":    write_trades(trades, d / f"trades{ext}"),
    }
    # Tech: when params are supplied, persist them next to the data.
    # Why:  co-locating params.json with the artifacts makes each run dir fully
    #       self-describing and reproducible.
    if params is not None:
        written["params"] = write_params(params, d / "params.json")

    if auto_report:
        # Imported lazily: reports.py pulls matplotlib (and optionally
        # plotly), and not every callsite of write_all wants those at
        # import time (tests, headless sweeps with auto_report=False).
        # Tech: import generate_report on demand and run it over the bundle, folding
        #       the chart paths into the result; swallow and log any chart exception.
        # Why:  SPEC §4.9 wants a report after every backtest, but a plotting failure
        #       (bad backend, missing dep) must never invalidate logs that already
        #       wrote successfully — so it degrades to a printed warning.
        from .reports import generate_report
        try:
            report_paths = generate_report(
                d, data_path=Path(report_data_path) if report_data_path else None,
            )
            written.update({f"report_{k}": v for k, v in report_paths.items()})
        except Exception as exc:
            # Don't let a chart failure invalidate a finished backtest's logs.
            print(f"[write_all] report generation skipped: "
                  f"{type(exc).__name__}: {exc}")

    return written
