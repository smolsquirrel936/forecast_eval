"""Performance-report charts (PNG + interactive HTML).

Loads the parquet artifacts emitted by ``logging_io.write_all`` and renders:
  * equity_drawdown — cumulative net PnL with underwater drawdown panel
  * price_fills     — price series with BUY / SELL fill markers
  * signal_vs_realized — frictionless signal PnL overlaid on realized PnL

PNGs go to ``<run_dir>/report/``; an interactive ``report.html`` bundles
all three figures into a single file.

CLI:
    python -m forecast_eval.reports <run_dir> [--data <txf_1min.csv>]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

# Tech: try to import plotly and record whether it's available.
# Why:  plotly powers the interactive HTML but is optional; gating on a flag lets
#       the PNG path (matplotlib) always work while the HTML is produced only when
#       plotly is installed, instead of making it a hard dependency.
try:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    _HAS_PLOTLY = True
except ImportError:
    _HAS_PLOTLY = False

from .metrics import signal_attribution_curve


# ---------------------------------------------------------------------------
# Data loading.
# ---------------------------------------------------------------------------

def _read_artifact(run_dir: Path, name: str) -> Optional[pd.DataFrame]:
    """Read <run_dir>/<name>.parquet, falling back to .csv. None if absent."""
    # Tech: probe for .parquet then .csv; load whichever exists and coerce any
    #       timestamp-like column to datetime; return None when neither is present.
    # Why:  write_all may have produced either format (parquet or its CSV fallback),
    #       so the reader must accept both; re-parsing timestamps is needed because
    #       CSV loses dtype, and returning None lets callers report what's missing.
    for ext in (".parquet", ".csv"):
        p = run_dir / f"{name}{ext}"
        if p.exists():
            df = pd.read_parquet(p) if ext == ".parquet" else pd.read_csv(p)
            for col in df.columns:
                if "timestamp" in col or col.endswith("_ts"):
                    df[col] = pd.to_datetime(df[col])
            return df
    return None


# ---------------------------------------------------------------------------
# Chart 1: equity + drawdown.
# ---------------------------------------------------------------------------

def _equity_series(trades: pd.DataFrame) -> pd.DataFrame:
    # Tech: from trades, build a per-exit equity curve (cumulative net PnL), its
    #       running peak, and the drawdown (equity − peak); empty in → empty out.
    # Why:  both the matplotlib and plotly equity charts need the same three series,
    #       so deriving them once here keeps the two renderers consistent; sorting by
    #       exit_ts puts the curve in chronological order regardless of fill order.
    if trades.empty:
        return pd.DataFrame(columns=["timestamp", "equity", "drawdown"])
    s = trades.sort_values("exit_ts").copy()
    s["equity"] = s["net_pnl_points"].cumsum()
    s["peak"] = s["equity"].cummax()
    s["drawdown"] = s["equity"] - s["peak"]
    return s.rename(columns={"exit_ts": "timestamp"})[
        ["timestamp", "equity", "drawdown"]
    ]


def plot_equity_drawdown_mpl(trades: pd.DataFrame, out_path: Path) -> None:
    # Tech: a 2-row figure (equity on top 3/4, drawdown below); when there are no
    #       trades, print a centered "no trades" placeholder instead of curves.
    # Why:  the shared-x stacked layout lets you read drawdown directly under the
    #       equity that caused it; the placeholder keeps the report well-formed even
    #       for a run that never traded (so downstream HTML embedding doesn't break).
    eq = _equity_series(trades)
    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(11, 6), sharex=True,
        gridspec_kw={"height_ratios": [3, 1]},
    )
    if eq.empty:
        ax1.text(0.5, 0.5, "no trades", ha="center", va="center",
                 transform=ax1.transAxes)
    else:
        # Tech: plot the equity line with a zero reference, shade drawdown on the
        #       equity panel, and draw the dedicated underwater panel below.
        # Why:  shading drawdown on both panels makes losing stretches visually
        #       obvious; step="post" matches the discrete per-trade cadence (equity
        #       only changes at a close, not continuously).
        ax1.plot(eq["timestamp"], eq["equity"], color="#1f77b4", lw=1.5)
        ax1.axhline(0, color="grey", lw=0.5, ls="--")
        ax1.fill_between(eq["timestamp"], 0, eq["drawdown"],
                         color="#d62728", alpha=0.3, step="post")
        ax2.fill_between(eq["timestamp"], 0, eq["drawdown"],
                         color="#d62728", alpha=0.6, step="post")
        ax2.axhline(0, color="grey", lw=0.5)
    # Tech: label axes/titles and apply a concise auto date formatter to the x-axis.
    # Why:  ConciseDateFormatter keeps tick labels readable across any run length
    #       (seconds to months) without manual locator tuning; tight_layout + a fixed
    #       dpi give a predictable PNG, and closing the figure frees memory in sweeps.
    ax1.set_ylabel("Cumulative net PnL (points)")
    ax1.set_title("Equity curve")
    ax2.set_ylabel("Drawdown")
    ax2.set_xlabel("Time")
    ax2.xaxis.set_major_locator(mdates.AutoDateLocator())
    ax2.xaxis.set_major_formatter(mdates.ConciseDateFormatter(ax2.xaxis.get_major_locator()))
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)


def plot_equity_drawdown_plotly(trades: pd.DataFrame) -> "go.Figure":
    # Tech: the interactive twin of the equity/drawdown chart — same two stacked
    #       panels as scatter traces sharing the x-axis.
    # Why:  plotly gives hover/zoom that static PNGs can't; reusing _equity_series
    #       guarantees the interactive and static versions show identical numbers.
    eq = _equity_series(trades)
    fig = make_subplots(
        rows=2, cols=1, shared_xaxes=True,
        row_heights=[0.72, 0.28], vertical_spacing=0.04,
        subplot_titles=("Cumulative net PnL", "Drawdown"),
    )
    if not eq.empty:
        # Tech: add the equity line to row 1 and the fill-to-zero drawdown to row 2.
        # Why:  guarding on non-empty avoids adding empty traces; fill="tozeroy"
        #       renders the underwater area the same way the PNG shades it.
        fig.add_trace(go.Scatter(
            x=eq["timestamp"], y=eq["equity"], mode="lines",
            name="equity", line=dict(color="#1f77b4", width=2),
        ), row=1, col=1)
        fig.add_trace(go.Scatter(
            x=eq["timestamp"], y=eq["drawdown"], mode="lines",
            name="drawdown", fill="tozeroy",
            line=dict(color="#d62728", width=1),
        ), row=2, col=1)
    # Tech: set axis titles and a compact layout with the legend hidden.
    # Why:  two clearly-titled panels don't need a legend; fixed height/margins keep
    #       the figure consistent when embedded alongside the other two in the HTML.
    fig.update_yaxes(title_text="points", row=1, col=1)
    fig.update_yaxes(title_text="points", row=2, col=1)
    fig.update_layout(height=520, showlegend=False, margin=dict(t=40, l=60, r=20, b=40))
    return fig


# ---------------------------------------------------------------------------
# Chart 2: price + entry/exit markers.
# ---------------------------------------------------------------------------

def plot_price_fills_mpl(
    fills: pd.DataFrame,
    ticks: Optional[pd.DataFrame],
    out_path: Path,
) -> None:
    # Tech: draw the price line — preferring the real tick series, else reconstructing
    #       a rough line from fill prices — then overlay BUY (green ▲) / SELL (red ▼).
    # Why:  the underlying price gives fills context; when no tick data was passed we
    #       degrade to connecting fill prices so the chart is still informative rather
    #       than empty. Up/down triangles read as long/short entries at a glance.
    fig, ax = plt.subplots(figsize=(11, 5))
    if ticks is not None and not ticks.empty:
        ax.plot(ticks["timestamp"], ticks["price"],
                color="#888", lw=0.6, alpha=0.8, label="price")
    elif not fills.empty:
        ax.plot(fills["timestamp"], fills["fill_price"],
                color="#bbb", lw=0.6, alpha=0.7, label="price (from fills)")
    if not fills.empty:
        # Tech: split fills by side and scatter each set with its own marker/color
        #       above the price line (zorder=3).
        # Why:  separating BUY/SELL is what makes the chart legible; the high zorder
        #       keeps markers on top of the price line so they're never hidden.
        buys = fills[fills["side"] == "BUY"]
        sells = fills[fills["side"] == "SELL"]
        ax.scatter(buys["timestamp"], buys["fill_price"],
                   marker="^", color="#2ca02c", s=36, label="BUY", zorder=3)
        ax.scatter(sells["timestamp"], sells["fill_price"],
                   marker="v", color="#d62728", s=36, label="SELL", zorder=3)
    # Tech: label, legend, concise date axis, save at fixed dpi and close.
    # Why:  same rationale as the equity chart — readable dates across any span and
    #       deterministic output that won't leak figures during a sweep.
    ax.set_ylabel("Price")
    ax.set_xlabel("Time")
    ax.set_title("Price with fills")
    ax.legend(loc="best")
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(ax.xaxis.get_major_locator()))
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)


def plot_price_fills_plotly(
    fills: pd.DataFrame,
    ticks: Optional[pd.DataFrame],
) -> "go.Figure":
    # Tech: interactive price-with-fills — same price-source preference and BUY/SELL
    #       marker overlay as the matplotlib version, as plotly traces.
    # Why:  hover reveals exact fill prices/times that are unreadable on a dense PNG;
    #       mirroring the static chart's logic keeps the two views in agreement.
    fig = go.Figure()
    if ticks is not None and not ticks.empty:
        fig.add_trace(go.Scatter(
            x=ticks["timestamp"], y=ticks["price"], mode="lines",
            name="price", line=dict(color="#888", width=1),
        ))
    elif not fills.empty:
        fig.add_trace(go.Scatter(
            x=fills["timestamp"], y=fills["fill_price"], mode="lines",
            name="price (from fills)", line=dict(color="#bbb", width=1),
        ))
    if not fills.empty:
        buys = fills[fills["side"] == "BUY"]
        sells = fills[fills["side"] == "SELL"]
        fig.add_trace(go.Scatter(
            x=buys["timestamp"], y=buys["fill_price"], mode="markers",
            name="BUY",
            marker=dict(symbol="triangle-up", color="#2ca02c", size=9),
        ))
        fig.add_trace(go.Scatter(
            x=sells["timestamp"], y=sells["fill_price"], mode="markers",
            name="SELL",
            marker=dict(symbol="triangle-down", color="#d62728", size=9),
        ))
    fig.update_layout(
        height=460, title="Price with fills",
        xaxis_title="time", yaxis_title="price",
        margin=dict(t=50, l=60, r=20, b=40),
    )
    return fig


# ---------------------------------------------------------------------------
# Chart 3: signal PnL vs realized PnL (execution drag).
# ---------------------------------------------------------------------------

def _curves(signals: pd.DataFrame, trades: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    # Tech: build two aligned cumulative-PnL curves — the frictionless signal curve
    #       (via signal_attribution_curve) and the realized curve (cumsum of trade
    #       net PnL) — each empty-safe.
    # Why:  the whole point of chart 3 is to overlay these two; computing them
    #       together keeps their schemas identical so the renderers can plot/shade
    #       the gap between them, which is the execution + cost drag (SPEC §7).
    sig_curve = (
        signal_attribution_curve(signals.to_dict("records"))
        if not signals.empty else pd.DataFrame(columns=["timestamp", "cum_signal_pnl_points"])
    )
    if not trades.empty:
        real = trades.sort_values("exit_ts").copy()
        real["cum_realized_pnl_points"] = real["net_pnl_points"].cumsum()
        real = real.rename(columns={"exit_ts": "timestamp"})[
            ["timestamp", "cum_realized_pnl_points"]
        ]
    else:
        real = pd.DataFrame(columns=["timestamp", "cum_realized_pnl_points"])
    return sig_curve, real


def plot_signal_vs_realized_mpl(
    signals: pd.DataFrame,
    trades: pd.DataFrame,
    out_path: Path,
) -> None:
    # Tech: plot the frictionless signal curve and the realized curve together.
    # Why:  side by side they show how much theoretical edge survives to realized
    #       PnL — the headline attribution story of the whole framework.
    sig_curve, real = _curves(signals, trades)
    fig, ax = plt.subplots(figsize=(11, 5))
    if not sig_curve.empty:
        ax.plot(sig_curve["timestamp"], sig_curve["cum_signal_pnl_points"],
                color="#1f77b4", lw=1.5, label="signal PnL (frictionless)")
    if not real.empty:
        ax.plot(real["timestamp"], real["cum_realized_pnl_points"],
                color="#d62728", lw=1.5, label="realized PnL")
    if not sig_curve.empty and not real.empty:
        # Tech: as-of merge the realized points onto the latest prior signal value,
        #       then shade the band between the two curves.
        # Why:  the two curves have different timestamps (signals vs. trade exits), so
        #       a backward as-of join aligns them onto a common axis before filling;
        #       the shaded area *is* the execution + cost drag, made visible.
        merged = pd.merge_asof(
            real.sort_values("timestamp"),
            sig_curve.sort_values("timestamp"),
            on="timestamp", direction="backward",
        )
        ax.fill_between(
            merged["timestamp"],
            merged["cum_realized_pnl_points"],
            merged["cum_signal_pnl_points"],
            color="#d62728", alpha=0.15, label="execution + cost drag",
        )
    ax.axhline(0, color="grey", lw=0.5, ls="--")
    ax.set_ylabel("Cumulative PnL (points)")
    ax.set_xlabel("Time")
    ax.set_title("Signal vs. realized PnL — gap = execution + cost drag")
    ax.legend(loc="best")
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(ax.xaxis.get_major_locator()))
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)


def plot_signal_vs_realized_plotly(
    signals: pd.DataFrame,
    trades: pd.DataFrame,
) -> "go.Figure":
    # Tech: interactive twin of chart 3 — the signal and realized curves as two
    #       lines (no shaded band).
    # Why:  hover lets you read the exact gap at any point, which substitutes for the
    #       static fill; skipping the as-of shading keeps the interactive figure light.
    sig_curve, real = _curves(signals, trades)
    fig = go.Figure()
    if not sig_curve.empty:
        fig.add_trace(go.Scatter(
            x=sig_curve["timestamp"], y=sig_curve["cum_signal_pnl_points"],
            mode="lines", name="signal PnL (frictionless)",
            line=dict(color="#1f77b4", width=2),
        ))
    if not real.empty:
        fig.add_trace(go.Scatter(
            x=real["timestamp"], y=real["cum_realized_pnl_points"],
            mode="lines", name="realized PnL",
            line=dict(color="#d62728", width=2),
        ))
    fig.update_layout(
        height=460,
        title="Signal vs. realized PnL — gap = execution + cost drag",
        xaxis_title="time", yaxis_title="cumulative points",
        margin=dict(t=50, l=60, r=20, b=40),
    )
    return fig


# ---------------------------------------------------------------------------
# Top-level driver.
# ---------------------------------------------------------------------------

def _load_ticks(data_path: Optional[Path]) -> Optional[pd.DataFrame]:
    # Tech: optionally load a raw 1-min CSV for the price chart background —
    #       returning None if no path/file or the file lacks a timestamp column,
    #       else dropping NaN closes and projecting to [timestamp, price].
    # Why:  the price overlay is a nice-to-have, so every failure mode degrades to
    #       None (chart falls back to fill-derived price) rather than raising; the
    #       NaN drop matches the loader convention since Toto2 data has ~17% gaps.
    if data_path is None or not data_path.exists():
        return None
    df = pd.read_csv(data_path)
    if "timestamp" not in df.columns:
        return None
    df = df.dropna(subset=["close"]).copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df["price"] = df["close"].astype(float)
    return df[["timestamp", "price"]]


def generate_report(
    run_dir: Path,
    *,
    data_path: Optional[Path] = None,
    n_ticks_for_chart: int = 50_000,
) -> dict:
    """Render PNGs + a combined HTML report from a run directory.

    Returns a dict of artifact name → path written.
    """
    # Tech: validate the run dir exists, then load the three artifacts the charts
    #       need; if any is missing, raise listing exactly which.
    # Why:  this is the public entry (also called automatically by write_all), so a
    #       precise error pointing at the missing artifact saves debugging vs. a bare
    #       KeyError deep inside a plot routine.
    run_dir = Path(run_dir)
    if not run_dir.exists():
        raise FileNotFoundError(run_dir)

    fills = _read_artifact(run_dir, "fills")
    trades = _read_artifact(run_dir, "trades")
    signals = _read_artifact(run_dir, "signals")
    if fills is None or trades is None or signals is None:
        missing = [n for n, df in [("fills", fills), ("trades", trades), ("signals", signals)]
                   if df is None]
        raise FileNotFoundError(
            f"missing artifact(s) in {run_dir}: {missing}. "
            "Run a backtest with logging_io.write_all first."
        )

    # Tech: load the optional price background and, if it's large, clip it to the
    #       fills' time span (plus 2% padding).
    # Why:  a full 2.7M-row tick file would make the chart huge and slow; windowing
    #       to where trades actually happened keeps the price overlay relevant and the
    #       PNG/ HTML light, while the pad gives a little context on each side.
    ticks = _load_ticks(data_path)
    if ticks is not None and len(ticks) > n_ticks_for_chart and not fills.empty:
        lo, hi = fills["timestamp"].min(), fills["timestamp"].max()
        pad = (hi - lo) * 0.02 if hi > lo else pd.Timedelta(minutes=5)
        ticks = ticks[(ticks["timestamp"] >= lo - pad) & (ticks["timestamp"] <= hi + pad)]

    # Tech: create the report/ subdir and render the three PNGs into it, collecting
    #       their paths.
    # Why:  PNGs always work (matplotlib is a hard dep) and are the portable artifact;
    #       grouping them under report/ keeps the run dir tidy.
    out_dir = run_dir / "report"
    out_dir.mkdir(parents=True, exist_ok=True)

    paths = {}
    paths["equity_png"] = out_dir / "equity_drawdown.png"
    plot_equity_drawdown_mpl(trades, paths["equity_png"])

    paths["price_png"] = out_dir / "price_fills.png"
    plot_price_fills_mpl(fills, ticks, paths["price_png"])

    paths["attribution_png"] = out_dir / "signal_vs_realized.png"
    plot_signal_vs_realized_mpl(signals, trades, paths["attribution_png"])

    if _HAS_PLOTLY:
        # Tech: when plotly is present, build the three interactive figures and stitch
        #       their HTML fragments into one self-contained report.html.
        # Why:  bundling all three in one file makes the report easy to share/open;
        #       include_plotlyjs='cdn' only on the first figure embeds the library
        #       once (then False) so the file isn't bloated with three copies of it.
        html_path = run_dir / "report.html"
        figs = [
            plot_equity_drawdown_plotly(trades),
            plot_price_fills_plotly(fills, ticks),
            plot_signal_vs_realized_plotly(signals, trades),
        ]
        html_parts = [
            "<html><head><meta charset='utf-8'><title>forecast_eval report</title>",
            "<style>body{font-family:sans-serif;max-width:1200px;margin:20px auto;}"
            "h1{border-bottom:1px solid #ccc;padding-bottom:6px;}"
            ".fig{margin-bottom:30px;}</style></head><body>",
            f"<h1>forecast_eval report — {run_dir.name}</h1>",
        ]
        for i, fig in enumerate(figs):
            html_parts.append(
                f"<div class='fig'>{fig.to_html(full_html=False, include_plotlyjs=('cdn' if i == 0 else False))}</div>"
            )
        html_parts.append("</body></html>")
        html_path.write_text("\n".join(html_parts), encoding="utf-8")
        paths["html"] = html_path

    return paths


def main(argv=None) -> int:
    # Tech: parse the run-dir argument (+ optional --data overlay) and call
    #       generate_report, turning a missing-artifact error into exit code 2.
    # Why:  this is the standalone CLI for re-rendering charts from an existing run
    #       without re-backtesting (SPEC §4.9); a clean nonzero exit makes it
    #       scriptable, and the FileNotFoundError catch gives a readable message.
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("run_dir", type=Path,
                    help="Directory containing fills/trades/signals parquet")
    ap.add_argument("--data", type=Path, default=None,
                    help="Optional path to TXF_OHLC_1min.csv for price chart background")
    args = ap.parse_args(argv)

    try:
        paths = generate_report(args.run_dir, data_path=args.data)
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    # Tech: print every artifact written, and note when the HTML was skipped.
    # Why:  echoing paths confirms what landed where; the plotly note explains a
    #       missing report.html so it doesn't look like a silent failure.
    print(f"report written under {args.run_dir}/")
    for name, p in paths.items():
        print(f"  {name:15s} -> {p}")
    if not _HAS_PLOTLY:
        print("  (plotly not installed — HTML report skipped)")
    return 0


if __name__ == "__main__":
    # Tech: exit with main()'s return code.
    # Why:  propagates the success/failure code to the shell for scripting.
    raise SystemExit(main())
