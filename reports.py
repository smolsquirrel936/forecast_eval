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
import json
import math
import sys
from pathlib import Path
from typing import Mapping, Optional

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

# Tech: the train/test boundary's colour and caption, shared by all six
#       renderers so the static and interactive charts mark it identically.
# Why:  one constant means the PNG and the HTML cannot drift apart on the one
#       element that is not a data series — a reader comparing them would read a
#       colour change as meaning something.
_SPLIT_COLOR = "#ff7f0e"
_SPLIT_LABEL = "train ends"


def _split_ts(value) -> Optional[pd.Timestamp]:
    # Tech: coerce a stored `train_split_ts` to a Timestamp, or None if it is
    #       absent or unparseable.
    # Why:  the value arrives from params.json as a string, and every run dir
    #       written before the train/test split existed has no such key at all —
    #       those must render exactly as they always did rather than raise, which
    #       is what makes re-rendering an old bundle safe.
    if value is None:
        return None
    try:
        ts = pd.Timestamp(value)
    except (ValueError, TypeError):
        return None
    return None if pd.isna(ts) else ts


def _active_split_ts(settings: Optional[Mapping]):
    # Tech: the boundary to mark — the stored `train_split_ts`, but only when the
    #       run actually held a tail back (`train_split_bars < eval_bars`).
    # Why:  a run at the default `--train-frac 1.0` still records a split
    #       timestamp, because the "boundary" is then simply the last bar. Drawing
    #       that would put a rule flush against the right edge of all three charts
    #       on every ordinary run, marking nothing. The line exists to say "the
    #       held-out region starts here", so it must appear only when one exists.
    if not settings:
        return None
    bars, total = settings.get("train_split_bars"), settings.get("eval_bars")
    if isinstance(bars, int) and isinstance(total, int) and bars >= total:
        return None
    return _split_ts(settings.get("train_split_ts"))


def _axvline_split(ax, split_ts, *, annotate: bool = True) -> None:
    # Tech: draw the boundary as a dashed orange rule on a matplotlib axis, with
    #       the caption riding just inside the top of the axes.
    # Why:  `label=` is deliberately not passed, so the line never joins the
    #       legend — the equity panel already carries strategy + buy-and-hold and
    #       the price panel price + BUY + SELL. The annotation says what it is
    #       without competing for that space. `clip_on=False` keeps the caption
    #       visible when the line lands near the right edge of a heavily-split run.
    if split_ts is None:
        return
    ax.axvline(split_ts, color=_SPLIT_COLOR, lw=1.3, ls="--", zorder=4)
    if annotate:
        ax.annotate(_SPLIT_LABEL, xy=(split_ts, 1.0), xycoords=("data", "axes fraction"),
                    xytext=(4, -10), textcoords="offset points",
                    color=_SPLIT_COLOR, fontsize=8, ha="left", va="top",
                    clip_on=False, zorder=4)


def _add_vline_split(fig, split_ts, **kwargs) -> None:
    # Tech: the plotly twin — a dashed orange vline carrying the same caption.
    # Why:  `add_vline` draws a shape, not a trace, so it stays out of the legend
    #       by construction, which is the behaviour chosen for the static charts
    #       too. `kwargs` forwards row/col for the two-panel equity figure.
    if split_ts is None:
        return
    fig.add_vline(
        x=split_ts, line_width=1.3, line_dash="dash", line_color=_SPLIT_COLOR,
        annotation_text=_SPLIT_LABEL, annotation_position="top right",
        annotation_font_color=_SPLIT_COLOR, annotation_font_size=11,
        **kwargs,
    )


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
    out = s.rename(columns={"exit_ts": "timestamp"})[
        ["timestamp", "equity", "drawdown"]
    ]
    return _with_origin(out, _first_entry(trades), ["equity", "drawdown"])


def _with_origin(curve: pd.DataFrame, start, value_cols: list) -> pd.DataFrame:
    # Tech: prepend a zero row at `start`, so a cumulative curve opens from flat
    #       rather than from the value of its first close.
    # Why:  MEASURED 2026-09-02 — a run with ONE closed round trip produced a
    #       one-point curve, and a `mode="lines"` trace through one point draws
    #       nothing at all: the Cumulative PnL panel rendered empty on a run that
    #       had made +359.59. The origin is not a display trick, it is the missing
    #       datum — the curve was at zero before the first trade, and one that
    #       opens at +359.59 hides the very step it exists to show. It also gives
    #       `_buy_and_hold_curve` a window with width, which a single-trade run
    #       otherwise collapsed to one print. Every curve in the report takes its
    #       origin from here so the panels can be read against each other; a
    #       `start` that is missing or not before the curve is left alone rather
    #       than invented.
    if curve.empty or start is None or pd.isna(start):
        return curve
    if start >= curve["timestamp"].iloc[0]:
        return curve
    origin = {"timestamp": start, **{c: 0.0 for c in value_cols}}
    return pd.concat([pd.DataFrame([origin]), curve], ignore_index=True)


def _first_entry(trades: pd.DataFrame):
    # Tech: the earliest `entry_ts` in a trades frame, or None when the column is
    #       not there.
    # Why:  it is the moment the curve was last at zero. Bundles written before
    #       `build_trades` carried `entry_ts` simply get no origin.
    if trades.empty or "entry_ts" not in trades.columns:
        return None
    return trades["entry_ts"].min()


def _buy_and_hold_curve(
    eq: pd.DataFrame,
    ticks: Optional[pd.DataFrame],
    fee_rate: float,
) -> pd.DataFrame:
    # Tech: build a net mark-to-market buy-and-hold curve over the equity curve's
    #       own [start, end] span — entry at the first price in that window, then
    #       cum = (price − entry) − (entry + price)·fee_rate at each later tick.
    # Why:  gives an apples-to-apples reference on the same x-axis as the strategy's
    #       net equity (same points units, same per-side fee as buy_and_hold_pnl, so
    #       the line's endpoint equals that floor over the *trading* window). Returns
    #       empty whenever there's no price series or no trades, so the overlay just
    #       vanishes rather than erroring.
    if ticks is None or ticks.empty or eq.empty:
        return pd.DataFrame(columns=["timestamp", "cum_bh_pnl_points"])
    lo, hi = eq["timestamp"].min(), eq["timestamp"].max()
    sl = ticks[(ticks["timestamp"] >= lo) & (ticks["timestamp"] <= hi)].sort_values("timestamp")
    if sl.empty:
        return pd.DataFrame(columns=["timestamp", "cum_bh_pnl_points"])
    entry = float(sl["price"].iloc[0])
    price = sl["price"].astype(float)
    cum = (price - entry) - (entry + price) * fee_rate
    return pd.DataFrame({"timestamp": sl["timestamp"].values, "cum_bh_pnl_points": cum.values})


def plot_equity_drawdown_mpl(
    trades: pd.DataFrame,
    out_path: Path,
    *,
    ticks: Optional[pd.DataFrame] = None,
    fee_rate: float = 0.0002,
    point_value: float = 200.0,
    split_ts=None,
) -> None:
    # Tech: a 2-row figure (equity on top 3/4, drawdown below); when there are no
    #       trades, print a centered "no trades" placeholder instead of curves.
    # Why:  the shared-x stacked layout lets you read drawdown directly under the
    #       equity that caused it; the placeholder keeps the report well-formed even
    #       for a run that never traded (so downstream HTML embedding doesn't break).
    eq = _equity_series(trades)
    bh = _buy_and_hold_curve(eq, ticks, fee_rate)
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
        ax1.plot(eq["timestamp"], eq["equity"], color="#1f77b4", lw=1.5,
                 label="strategy")
        ax1.axhline(0, color="grey", lw=0.5, ls="--")
        ax1.fill_between(eq["timestamp"], 0, eq["drawdown"],
                         color="#d62728", alpha=0.3, step="post")
        ax2.fill_between(eq["timestamp"], 0, eq["drawdown"],
                         color="#d62728", alpha=0.6, step="post")
        ax2.axhline(0, color="grey", lw=0.5)
        # Tech: overlay the net buy-and-hold mark-to-market curve over the same span,
        #       when a price series is available, and show a legend to tell them apart.
        # Why:  a passive-hold reference makes the strategy's edge (or lack of it) read
        #       at a glance; it's omitted silently when there's no price file.
        if not bh.empty:
            ax1.plot(bh["timestamp"], bh["cum_bh_pnl_points"],
                     color="#7f7f7f", lw=1.2, ls="--", label="buy & hold")
            ax1.legend(loc="best")
        # Tech: pin the equity y-axis to a span that fits the strategy (and buy &
        #       hold) and add a right-hand currency axis that is the exact point_value
        #       rescale of it, so the two axes stay linked like the interactive chart.
        # Why:  matching the plotly pin keeps the PNG and HTML reading the same; without
        #       a pinned currency twin the NT$ scale would drift independently.
        eq_span = _currency_axis_range([eq["equity"], bh["cum_bh_pnl_points"] if not bh.empty else None], point_value)
        if eq_span is not None:
            (lo, hi), (clo, chi) = eq_span
            ax1.set_ylim(lo, hi)
            ax_cur = ax1.twinx()
            ax_cur.set_ylim(clo, chi)
            ax_cur.set_ylabel(f"currency @ {point_value:g}")
    # Tech: label axes/titles and apply a concise auto date formatter to the x-axis.
    # Why:  ConciseDateFormatter keeps tick labels readable across any run length
    #       (seconds to months) without manual locator tuning; tight_layout + a fixed
    #       dpi give a predictable PNG, and closing the figure frees memory in sweeps.
    # Tech: the boundary goes on both panels, captioned only on the top one.
    # Why:  the panels share an x-axis and are read as one stacked chart, so a
    #       rule on only the equity half would look like a drawing error; two
    #       captions on one vertical line is just duplication.
    _axvline_split(ax1, split_ts)
    _axvline_split(ax2, split_ts, annotate=False)
    ax1.set_ylabel("Cumulative net PnL (points)")
    ax1.set_title("Equity curve")
    ax2.set_ylabel("Drawdown")
    ax2.set_xlabel("Time")
    ax2.xaxis.set_major_locator(mdates.AutoDateLocator())
    ax2.xaxis.set_major_formatter(mdates.ConciseDateFormatter(ax2.xaxis.get_major_locator()))
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)


def plot_equity_drawdown_plotly(
    trades: pd.DataFrame,
    *,
    ticks: Optional[pd.DataFrame] = None,
    fee_rate: float = 0.0002,
    point_value: float = 200.0,
    split_ts=None,
) -> "go.Figure":
    # Tech: the interactive twin of the equity/drawdown chart — two stacked panels,
    #       each with a left points axis and a right currency axis that is the exact
    #       point_value rescale of it, kept linked by pinning both ranges together.
    # Why:  plotly gives hover/zoom that static PNGs can't; reusing _equity_series
    #       guarantees the interactive and static versions show identical numbers.
    #       The right-hand currency axis is the regression this whole chart guards:
    #       nothing in plotly links two axes automatically, so an edit could silently
    #       unlink them — pinning the ranges as a pair and binding a carrier trace to
    #       each currency axis (otherwise plotly won't render an axis with no trace)
    #       is what keeps them visible and in lock-step.
    eq = _equity_series(trades)
    bh = _buy_and_hold_curve(eq, ticks, fee_rate)
    fig = make_subplots(
        rows=2, cols=1, shared_xaxes=True,
        specs=[[{"secondary_y": True}], [{"secondary_y": True}]],
        row_heights=[0.72, 0.28], vertical_spacing=0.04,
        subplot_titles=("Cumulative net PnL", "Drawdown"),
    )
    if not eq.empty:
        # Tech: strategy equity and the optional buy-and-hold overlay go on the row-1
        #       points axis (primary); a transparent, hover-less carrier trace bound
        #       to the row-1 currency axis (secondary) carries the equity × point_value
        #       series so that axis actually renders.
        # Why:  the carrier is the only thing keeping the currency axis on screen —
        #       plotly hides an axis with no bound trace; keeping it invisible means
        #       it contributes the axis but not a visible line.
        fig.add_trace(go.Scatter(
            x=eq["timestamp"], y=eq["equity"], mode="lines",
            name="strategy", line=dict(color="#1f77b4", width=2),
        ), row=1, col=1, secondary_y=False)
        if not bh.empty:
            fig.add_trace(go.Scatter(
                x=bh["timestamp"], y=bh["cum_bh_pnl_points"], mode="lines",
                name="buy & hold", line=dict(color="#7f7f7f", width=1.5, dash="dash"),
            ), row=1, col=1, secondary_y=False)
        fig.add_trace(go.Scatter(
            x=eq["timestamp"], y=eq["equity"] * point_value, mode="lines",
            name="strategy (currency)", showlegend=False, hoverinfo="skip",
            line=dict(color="rgba(0,0,0,0)"),
        ), row=1, col=1, secondary_y=True)
        # Tech: row-2 drawdown on its points axis plus a matching currency carrier on
        #       its secondary axis; the drawdown span always includes zero so the
        #       fill-to-zero baseline sits inside the pinned range.
        # Why:  the underwater panel fills to zero, so a range that omits zero would
        #       clip the fill; the carrier again only exists to make the currency
        #       axis render.
        fig.add_trace(go.Scatter(
            x=eq["timestamp"], y=eq["drawdown"], mode="lines",
            name="drawdown", fill="tozeroy", showlegend=False,
            line=dict(color="#d62728", width=1),
        ), row=2, col=1, secondary_y=False)
        fig.add_trace(go.Scatter(
            x=eq["timestamp"], y=eq["drawdown"] * point_value, mode="lines",
            name="drawdown (currency)", showlegend=False, hoverinfo="skip",
            line=dict(color="rgba(0,0,0,0)"),
        ), row=2, col=1, secondary_y=True)

        # Tech: pin both panels' point/currency ranges as exact rescale pairs, derived
        #       from the curves they display (equity span over strategy + buy & hold;
        #       drawdown span extended to zero).
        # Why:  this is the link between the axes — the currency range must equal the
        #       points range × point_value or the two scales drift apart; deriving it
        #       from the displayed data also guarantees the curves fit inside.
        eq_span = _currency_axis_range(
            [eq["equity"], bh["cum_bh_pnl_points"] if not bh.empty else None],
            point_value,
        )
        if eq_span is not None:
            (lo, hi), (clo, chi) = eq_span
            fig.update_yaxes(range=[lo, hi], row=1, col=1, secondary_y=False)
            fig.update_yaxes(range=[clo, chi], row=1, col=1, secondary_y=True)
        dd_span = _currency_axis_range([eq["drawdown"]], point_value, include_zero=True)
        if dd_span is not None:
            (lo, hi), (clo, chi) = dd_span
            fig.update_yaxes(range=[lo, hi], row=2, col=1, secondary_y=False)
            fig.update_yaxes(range=[clo, chi], row=2, col=1, secondary_y=True)

    # Tech: label the four axes; the currency side carries the point_value so the
    #       reader knows the rescale factor.
    # Why:  without titles the two right-hand axes read as unlabeled numbers; fixed
    #       height/margins keep the figure consistent when embedded alongside the
    #       other two in the HTML.
    fig.update_yaxes(title_text="points", row=1, col=1, secondary_y=False)
    fig.update_yaxes(title_text=f"currency @ {point_value:g}", row=1, col=1, secondary_y=True)
    fig.update_yaxes(title_text="points", row=2, col=1, secondary_y=False)
    fig.update_yaxes(title_text=f"currency @ {point_value:g}", row=2, col=1, secondary_y=True)
    # Tech: the boundary on both stacked panels, captioned only on the top one.
    # Why:  same reason as the PNG twin — the panels share an x-axis and read as
    #       one chart, so the rule must cross both, and one caption is enough.
    _add_vline_split(fig, split_ts, row=1, col=1)
    if split_ts is not None:
        fig.add_vline(x=split_ts, line_width=1.3, line_dash="dash",
                      line_color=_SPLIT_COLOR, row=2, col=1)
    fig.update_layout(height=520, showlegend=not bh.empty,
                      margin=dict(t=40, l=60, r=70, b=40))

    # Tech: when buy & hold is present, add a button group that toggles its trace —
    #       the masks must cover *every* trace (incl. the two invisible currency
    #       carriers), so they are built from the live fig.data, not a hard-coded
    #       length; "Strategy only" hides only the buy-&-hold trace.
    # Why:  lets the reader switch between the standalone strategy curve and the
    #       comparison-against-hold view; a stale mask shorter than len(fig.data)
    #       would blank a carrier (killing a currency axis) or the drawdown, so the
    #       masks are recomputed from the actual trace list and the buy-&-hold index.
    if not bh.empty:
        names = [t.name for t in fig.data]
        bh_idx = names.index("buy & hold")
        n = len(fig.data)
        both = [True] * n
        strategy_only = [i != bh_idx for i in range(n)]
        fig.update_layout(
            updatemenus=[dict(
                type="buttons", direction="right",
                x=0, xanchor="left", y=1.12, yanchor="top",
                showactive=True, active=0,
                buttons=[
                    dict(label="Strategy + Buy & Hold", method="update",
                         args=[{"visible": both}]),
                    dict(label="Strategy only", method="update",
                         args=[{"visible": strategy_only}]),
                ],
            )],
        )
    return fig


# ---------------------------------------------------------------------------
# Chart 2: price + entry/exit markers.
# ---------------------------------------------------------------------------

def plot_price_fills_mpl(
    fills: pd.DataFrame,
    ticks: Optional[pd.DataFrame],
    out_path: Path,
    *,
    split_ts=None,
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
    _axvline_split(ax, split_ts)
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
    *,
    split_ts=None,
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
    _add_vline_split(fig, split_ts)
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
    # Tech: open the signal curve at the first signal that took a side.
    # Why:  `signal_attribution_curve` emits one row per *close*, so it opens at
    #       its first round trip's PnL while the realized curve now opens at zero.
    #       Read against each other — which is the entire panel — that would put
    #       the gap at the wrong height for the first trade, and on a run with one
    #       round trip it leaves a one-point line that draws nothing.
    if not sig_curve.empty and not signals.empty:
        took_a_side = signals[signals["direction"] != "HOLD"]
        if not took_a_side.empty:
            sig_curve = _with_origin(sig_curve, took_a_side["timestamp"].min(),
                                     ["cum_signal_pnl_points"])
    if not trades.empty:
        real = trades.sort_values("exit_ts").copy()
        real["cum_realized_pnl_points"] = real["net_pnl_points"].cumsum()
        real = real.rename(columns={"exit_ts": "timestamp"})[
            ["timestamp", "cum_realized_pnl_points"]
        ]
        # Tech: same zero origin the equity curve gets.
        # Why:  the two are read against each other, so a realized line starting
        #       at its first exit while the signal line starts at flat would put
        #       the gap — which is the whole point of the panel — at the wrong
        #       height for the first trade.
        real = _with_origin(real, _first_entry(trades),
                            ["cum_realized_pnl_points"])
    else:
        real = pd.DataFrame(columns=["timestamp", "cum_realized_pnl_points"])
    return sig_curve, real


def plot_signal_vs_realized_mpl(
    signals: pd.DataFrame,
    trades: pd.DataFrame,
    out_path: Path,
    *,
    split_ts=None,
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
    _axvline_split(ax, split_ts)
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
    *,
    split_ts=None,
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
    _add_vline_split(fig, split_ts)
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


# ---------------------------------------------------------------------------
# Settings + result-summary HTML sections (rendered above the charts).
# ---------------------------------------------------------------------------

# Tech: the metric keys whose value is a *fraction in [0,1]* that the report must
#       render as a percentage — multiplying by 100 and suffixing `%`.
# Why:  the terminal print and the HTML report must agree, and a bare 0.4857 reads
#       as "0.49" not "48.57%"; listing the keys keeps the scaling in one place so a
#       new fraction field only needs a row here rather than a renderer change.
_FRACTION_PERCENT_KEYS = frozenset({
    "hit_rate", "direction_hit_rate", "annual_rate_of_return",
    "ratio_avg_win_avg_loss", "fee_drag",
})

# Tech: keys whose value is *already scaled to a percentage* (e.g. percent_profitable
#       is computed ×100 upstream) — must NOT be rescaled, only suffixed.
# Why:  double-scaling would turn 48.57 into 4857%; keeping this set separate from the
#       fraction set is what prevents that.
_ALREADY_PERCENT_KEYS = frozenset({"percent_profitable"})

# Tech: fractions rendered as an UNSIGNED percentage with trailing zeros trimmed
#       — 0.8 becomes "80%", 0.755 becomes "75.5%".
# Why:  `train_frac` is a share of a window, not a measurement that can come out
#       negative, so the leading "+" every other float carries reads as noise on
#       it; and "+80.00%" is three characters of false precision for a number the
#       user typed as 0.8. Separate from _FRACTION_PERCENT_KEYS because those are
#       signed metrics where the sign is load-bearing (a negative fee_drag means
#       something).
_UNSIGNED_PERCENT_KEYS = frozenset({"train_frac"})


# Tech: keys whose value is a left-aligned string rather than a right-aligned
#       number — the long, path-like Settings fields.
# Why:  long values read left-to-right. A right-aligned command or `data` path
#       either pins to the far edge of the table, disconnected from its key, or
#       wraps into lines that read bottom-up; both read as mistakes. Numbers
#       keep the right alignment so the tables still mirror the terminal's
#       columnar convention.
_LEFT_ALIGN_KEYS = frozenset({"command", "data"})


def fmt_value(v, key: Optional[str] = None) -> str:
    # Tech: format a scalar for the report — ints/None/str pass through unchanged,
    #       floats get a sign; for the fraction/percent keys the float is scaled to a
    #       2-decimal percentage, every other float keeps the 6-decimal terminal form.
    # Why:  the report's tables must mirror the terminal output exactly, but a handful
    #       of fields are shares/proportions that only read well as percentages; doing
    #       the dispatch here (instead of at every call site) keeps the two renderers
    #       (terminal-style 6-decimals vs. percent) consistent and keyed by field name.
    if not isinstance(v, float):
        return str(v)
    if math.isinf(v) or math.isnan(v):
        return f"{v:+}"
    if key in _UNSIGNED_PERCENT_KEYS:
        # Tech: 2-decimal percent, then strip trailing zeros and a bare point.
        # Why:  0.8 -> "80%" rather than "80.00%", while 0.755 keeps its "75.5%".
        return f"{v * 100:.2f}".rstrip("0").rstrip(".") + "%"
    if key in _FRACTION_PERCENT_KEYS:
        return f"{v * 100:+.2f}%"
    if key in _ALREADY_PERCENT_KEYS:
        return f"{v:+.2f}%"
    return f"{v:+.6f}"


def _currency_axis_range(
    series_iter,
    point_value: float,
    *,
    include_zero: bool = False,
):
    # Tech: from an iterable of point-valued Series, compute a (lo, hi) span that fits
    #       every finite value, optionally extended to contain zero, with a minimum
    #       width for flat data; return None when there's nothing to plot. The currency
    #       span is the exact point_value rescale of the points span.
    # Why:  the report pins matching points/currency axes on both equity panels so the
    #       right-hand NT$ axis stays an exact rescale of the left-hand points axis —
    #       nothing links them automatically, so computing the pair together is what
    #       keeps an edit from silently unlinking them; include_zero lets the drawdown
    #       panel pull its fill-to-zero baseline inside the range.
    finite = []
    for s in series_iter:
        if s is None:
            continue
        if not isinstance(s, pd.Series):
            continue
        if s.empty:
            continue
        finite.extend(float(x) for x in s if isinstance(x, (int, float)) and math.isfinite(x))
    if not finite:
        return None
    lo, hi = min(finite), max(finite)
    if include_zero:
        lo = min(lo, 0.0)
        hi = max(hi, 0.0)
    if lo == hi:  # flat data still needs a visible width
        lo -= 1.0
        hi += 1.0
    return (lo, hi), (lo * point_value, hi * point_value)


# Tech: the annual-returns columns whose value is a signed number — the two
#       that render sign-aligned (sign parked in a fixed column, magnitude
#       right-justified to the column's widest value).
# Why:  a bare right-aligned "+82,135.830" next to "-5,696.760" leaves the
#       signs drifting with each value's digit count; parking the sign and
#       padding the magnitude is what makes the column read as one block of
#       numbers (user request 2026-09-08). Only these two columns carry signs,
#       so only they take the split-formatter path.
_SIGNED_ANNUAL_COLS = ("rate_of_return", "net_pnl_currency")


def _annual_sign_magnitude(v, col: str):
    # Tech: split a finite numeric into its sign character and its unsigned,
    #       column-formatted magnitude — rate as a 3-decimal percentage,
    #       currency as a 3-decimal comma-grouped number; None for anything
    #       else (other columns, non-numeric, non-finite).
    # Why:  sign-aligned cells are built from the two halves separately: the
    #       sign parks in a fixed column and the magnitude is right-justified
    #       per column, which one pre-joined "+1,234.568" string cannot
    #       express. Non-finite values return None so the caller falls back to
    #       the plain form — there is no magnitude to align for a NaN.
    if not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    sign = "+" if v >= 0 else "-"
    if col == "rate_of_return":
        return sign, f"{abs(v) * 100:.3f}%"
    if col == "net_pnl_currency":
        return sign, f"{abs(v):,.3f}"
    return None


def _fmt_annual_value(v, col: str) -> str:
    # Tech: the annual-returns table's convention for the non-signed cells —
    #       the year as a literal integer (never comma-grouped into "2,022"),
    #       everything else through fmt_value.
    # Why:  the two signed numeric columns are formatted via
    #       _annual_sign_magnitude for sign-aligned rendering, so what remains
    #       here is the year and the fallbacks (partial_year's True/False, a
    #       NaN rate) — cells with no sign to park.
    if col == "year":
        return str(int(v))
    return fmt_value(v, col)


def _kv_table_html(mapping: Mapping) -> str:
    # Tech: render any mapping as a two-column key/value HTML table, formatting
    #       each value with fmt_value keyed by its own field name; every value
    #       cell holds an inner div, and the cells of the left-aligned fields
    #       (_LEFT_ALIGN_KEYS) carry the extra class "left".
    # Why:  the Settings block and every metric block share the same shape (a
    #       flat dict of scalars), so one renderer keeps them visually
    #       consistent, and passing the key to fmt_value is what makes fraction
    #       fields render as percentages. The div is the width limiter: an
    #       auto-layout table cell ignores max-width, but the block inside it
    #       is a normal box, so the stylesheet can cap the value column at
    #       50ch while short values still size the table naturally. The left
    #       class lets the stylesheet left-align just the long path-like
    #       fields — wrapped under the column's numeric right-alignment their
    #       lines would read bottom-up.
    rows = []
    for k, v in mapping.items():
        cls = "v left" if k in _LEFT_ALIGN_KEYS else "v"
        rows.append(
            f"<tr><td class='k'>{k}</td>"
            f"<td class='{cls}'><div>{fmt_value(_derived(k, v, mapping), k)}</div></td></tr>"
        )
    return f"<table class='kv'>{''.join(rows)}</table>"


def _derived(key: str, value, mapping: Mapping):
    # Tech: substitute a value the stored record cannot hold on its own —
    #       currently only `train_frac` on a run whose split was pinned in bars,
    #       where it is stored as None and the share is train_split_bars/eval_bars.
    # Why:  `params.json` records which FLAG was used (None means "--train-frac
    #       was never consulted"), and that record is deliberate — but the reader
    #       of a report wants the share that was actually scored, and on a pinned
    #       run those are the runs they were told to record. Deriving it here,
    #       rather than storing an effective fraction, keeps the file honest about
    #       the flag while the table stays honest about the window. Anything
    #       missing or unusable falls through to the stored value untouched.
    if key != "train_frac" or value is not None:
        return value
    bars, total = mapping.get("train_split_bars"), mapping.get("eval_bars")
    if not isinstance(bars, int) or not isinstance(total, int) or total <= 0:
        return value
    return bars / total


def _annual_returns_html(rows) -> str:
    # Tech: render the annual-returns table — one <th> per column, one <tr> per
    #       year; the two signed numeric columns emit "sign + space + magnitude
    #       right-justified to that column's widest magnitude", every other
    #       cell goes through _fmt_annual_value.
    # Why:  the mixed types (fraction, currency, integer year, bool) need
    #       per-column formatting, and the signed columns need the sign parked
    #       in a fixed column so "+ 82,135.830" and "-  5,696.760" read as one
    #       aligned block of numbers — the whole point of the table. The pad
    #       spaces only survive rendering because the stylesheet gives these
    #       cells white-space:pre and a monospace font.
    cols = ["year", "rate_of_return", "net_pnl_currency", "partial_year"]
    head = "".join(f"<th>{c}</th>" for c in cols)

    # Tech: first pass — format every cell, keeping the signed numerics as
    #       (sign, magnitude) pairs and recording each such column's widest
    #       magnitude.
    # Why:  a cell cannot be padded until the width of its whole column is
    #       known, so the pass exists only to measure; non-signed and
    #       non-finite cells format straight to their final string.
    widths = dict.fromkeys(_SIGNED_ANNUAL_COLS, 0)
    rendered = []
    for r in rows:
        cells = {}
        for c in cols:
            sm = _annual_sign_magnitude(r.get(c), c)
            if sm is None:
                cells[c] = _fmt_annual_value(r.get(c), c)
            else:
                cells[c] = sm
                widths[c] = max(widths[c], len(sm[1]))
        rendered.append(cells)

    # Tech: second pass — join each row's cells, expanding every (sign,
    #       magnitude) pair to "{sign} {magnitude:>width}".
    # Why:  equal-length cell strings under right alignment are what put every
    #       sign in the same column; the literal spaces are preserved by the
    #       white-space:pre cell style.
    body = []
    for cells in rendered:
        tds = []
        for c in cols:
            v = cells[c]
            if isinstance(v, tuple):
                sign, mag = v
                tds.append(f"<td>{sign} {mag.rjust(widths[c])}</td>")
            else:
                tds.append(f"<td>{v}</td>")
        body.append(f"<tr>{''.join(tds)}</tr>")
    return (
        "<h3>Annual returns (by calendar year of exit)</h3>"
        f"<table class='annual'><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"
    )


def _summary_sections_html(
    settings: Optional[Mapping],
    metrics: Optional[Mapping],
    buy_and_hold: Optional[Mapping],
) -> str:
    # Tech: build the Settings + Result-summary HTML, one labelled table per block,
    #       omitting any section whose source is missing or empty.
    # Why:  this is the headline the user wants at the top of the report — the run's
    #       parameters and the same metric blocks printed to the terminal; rendering
    #       it here (not in the callers) keeps the HTML report self-contained.
    parts: list[str] = []

    if settings:
        parts.append("<h2>Settings</h2>")
        parts.append(_kv_table_html(settings))

    if metrics or buy_and_hold:
        parts.append("<h2>Result summary</h2>")
        if metrics:
            # Tech: emit each trading block (single 'trading', or day/night under
            #       forced close), then the trade-analysis and annual-returns
            #       breakdowns, then forecast quality and attribution; skip the
            #       'trades' record list which has no scalar table form.
            # Why:  matches exactly the blocks the terminal prints (SPEC §7); the
            #       skip mirrors the console printers, which never tabulate trades,
            #       and the trade-analysis/annual-returns blocks only render when the
            #       metrics pack actually carried them.
            for key in [k for k in metrics if k.startswith("trading")]:
                parts.append(f"<h3>Trading metrics [{key}]</h3>")
                parts.append(_kv_table_html(metrics[key]))
            if metrics.get("trade_analysis"):
                parts.append("<h3>Trade analysis</h3>")
                parts.append(_kv_table_html(metrics["trade_analysis"]))
            ar = metrics.get("annual_returns")
            if ar and ar.get("rows"):
                parts.append(_annual_returns_html(ar["rows"]))
            if "forecast" in metrics:
                parts.append("<h3>Forecast quality</h3>")
                parts.append(_kv_table_html(metrics["forecast"]))
            if "attribution" in metrics:
                parts.append("<h3>Attribution (signal vs. realized)</h3>")
                parts.append(_kv_table_html(metrics["attribution"]))
        if buy_and_hold:
            parts.append("<h3>Buy-and-hold floor (full data, same fee)</h3>")
            parts.append(_kv_table_html(buy_and_hold))

    return "".join(parts)


def _load_params(run_dir: Path) -> Optional[dict]:
    # Tech: read params.json from the run dir, returning None if absent/unreadable.
    # Why:  lets the standalone CLI re-render still show the Settings table from the
    #       on-disk record when no settings are passed in-process; a bad/missing file
    #       must never break the report, so failures degrade to None.
    p = run_dir / "params.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def generate_report(
    run_dir: Path,
    *,
    data_path: Optional[Path] = None,
    ticks: Optional[pd.DataFrame] = None,
    n_ticks_for_chart: int = 50_000,
    settings: Optional[Mapping] = None,
    metrics: Optional[Mapping] = None,
    buy_and_hold: Optional[Mapping] = None,
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

    # Tech: fall back to the on-disk params.json for the Settings block when no
    #       settings were passed in-process.
    # Why:  the standalone CLI re-render has no live params, but params.json was
    #       written next to the artifacts — reading it keeps Settings populated there.
    if settings is None:
        settings = _load_params(run_dir)

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

    # Tech: prefer an in-memory price series passed by the caller; only re-read the
    #       raw CSV (via _load_ticks) when none was given. Then, if it's large, clip
    #       it to the fills' time span (plus 2% padding).
    # Why:  the caller's loader already handles the dataset's real schema (datetime
    #       rename, per-minute contract dedup), which the simple CSV reader can't —
    #       so threading the df is what makes the price overlay and buy-and-hold line
    #       actually appear for the RPT data. Windowing keeps a multi-million-row
    #       series light, with a pad for a little context on each side.
    if ticks is None:
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

    # Tech: pull the per-side fee and the contract point_value from settings
    #       (params.json), defaulting to the framework defaults (0.0002 / 200 for
    #       TXF) when absent or unparseable.
    # Why:  the reference line must be charged the same fee as the strategy to be a
    #       fair comparison, and the currency axis must rescale by the run's own
    #       point_value; reading both from the run's params keeps them in sync, while
    #       older bundles written before point_value existed fall back to the TXF value.
    try:
        fee_rate = float(settings["fee_rate"]) if settings and "fee_rate" in settings else 0.0002
    except (TypeError, ValueError):
        fee_rate = 0.0002
    try:
        point_value = float(settings["point_value"]) if settings and "point_value" in settings else 200.0
    except (TypeError, ValueError):
        point_value = 200.0

    paths = {}
    paths["equity_png"] = out_dir / "equity_drawdown.png"
    # Tech: read the train/test boundary out of the run's own settings; None when
    #       the run had no split, or when the bundle predates the feature.
    # Why:  taking it from `params.json` rather than a parameter is what makes the
    #       standalone `python -m forecast_eval.reports <dir>` re-render mark the
    #       boundary too — the run dir is self-describing, so a report rebuilt
    #       months later says the same thing as the one written with the run.
    split_ts = _active_split_ts(settings)

    plot_equity_drawdown_mpl(trades, paths["equity_png"], ticks=ticks,
                             fee_rate=fee_rate, point_value=point_value,
                             split_ts=split_ts)

    paths["price_png"] = out_dir / "price_fills.png"
    plot_price_fills_mpl(fills, ticks, paths["price_png"], split_ts=split_ts)

    paths["attribution_png"] = out_dir / "signal_vs_realized.png"
    plot_signal_vs_realized_mpl(signals, trades, paths["attribution_png"],
                                split_ts=split_ts)

    if _HAS_PLOTLY:
        # Tech: when plotly is present, build the three interactive figures and stitch
        #       their HTML fragments into one self-contained report.html.
        # Why:  bundling all three in one file makes the report easy to share/open;
        #       include_plotlyjs='cdn' only on the first figure embeds the library
        #       once (then False) so the file isn't bloated with three copies of it.
        html_path = run_dir / "report.html"
        figs = [
            plot_equity_drawdown_plotly(trades, ticks=ticks, fee_rate=fee_rate,
                                        point_value=point_value,
                                        split_ts=split_ts),
            plot_price_fills_plotly(fills, ticks, split_ts=split_ts),
            plot_signal_vs_realized_plotly(signals, trades, split_ts=split_ts),
        ]
        html_parts = [
            "<html><head><meta charset='utf-8'><title>forecast_eval report</title>",
            "<style>body{font-family:sans-serif;max-width:1200px;margin:20px auto;}"
            "h1{border-bottom:1px solid #ccc;padding-bottom:6px;}"
            "h2{margin-top:28px;border-bottom:1px solid #eee;padding-bottom:4px;}"
            "h3{margin-top:18px;color:#444;}"
            "table.kv{border-collapse:collapse;margin:6px 0 18px;}"
            "table.kv td{border:1px solid #ddd;padding:3px 10px;}"
            "table.kv td.k{color:#555;}"
            "table.kv td.v{font-family:monospace;text-align:right;}"
            # Tech: the 50ch cap on the Settings value column lives on the div
            #       inside the cell (see _kv_table_html), with overflow-wrap to
            #       let unbreakable tokens (a 60ch UNC path) break; the cells of
            #       the left-aligned fields (_LEFT_ALIGN_KEYS) are left-aligned.
            # Why:  an auto-layout table cell ignores max-width — the inner
            #       block is the only reliable width limiter — and wrapped long
            #       values (the command, the data path) under the column's
            #       numeric right-alignment would read bottom-up, so their
            #       cells override the alignment.
            "table.kv td.v div{max-width:50ch;overflow-wrap:anywhere;}"
            "table.kv td.v.left{text-align:left;}"
            # Tech: the annual-returns table gets its own class with per-column
            #       widths — 15ch each, 20ch for net_pnl_currency — right-aligned
            #       columns, monospace td cells with white-space:pre.
            # Why:  the per-column ch widths (user spec 2026-09-08) keep the
            #       table compact instead of stretching to the page width; the
            #       currency column is 20ch because the widest realistic value
            #       ('+ 16,427,166.000') is 17ch — a 15ch cap would clip the
            #       sign-aligned format that column exists to carry (auto
            #       layout still grows a column past its width if a value ever
            #       exceeds it, so nothing is ever cut off). The monospace +
            #       pre combination is what makes the sign-aligned cells
            #       actually align: pre keeps the pad spaces from collapsing,
            #       and only a fixed-width font turns equal-length strings
            #       into equal-width cells.
            "table.annual{border-collapse:collapse;margin:6px 0 18px;}"
            "table.annual th{border:1px solid #ddd;padding:4px 12px;"
            "text-align:right;}"
            "table.annual td{border:1px solid #ddd;padding:4px 12px;"
            "font-family:monospace;text-align:right;white-space:pre;}"
            "table.annual td:nth-child(1){width:15ch;}"
            "table.annual td:nth-child(2){width:15ch;}"
            "table.annual td:nth-child(3){width:20ch;}"
            "table.annual td:nth-child(4){width:15ch;}"
            ".fig{margin-bottom:30px;}</style></head><body>",
            f"<h1>forecast_eval report — {run_dir.name}</h1>",
            f"<p>currency @ {point_value:g}</p>",
            # Tech: render the Settings + Result-summary tables at the very top.
            # Why:  the user wants the run's parameters and headline metrics to open
            #       the report, above the charts (returns "" when nothing was passed).
            _summary_sections_html(settings, metrics, buy_and_hold),
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

    # Tech: with no `--data`, fall back to a live run's own `ticks.csv`.
    # Why:  `--data` takes the *dataset's* minute file, which a live directory
    #       does not have — it has the capture the run wrote itself, in the
    #       capture schema. Without this, re-rendering a live run gives fills
    #       drawn against an empty axis, and the panel still renders, so it reads
    #       as "this run had no prices" rather than "nobody passed any".
    #
    #       The series spans the whole soak, not this directory — a restart opens
    #       a new one, and a resumed run charts fills executed in an earlier
    #       segment (`live/prices.py`). Imported here rather than at module
    #       scope: `live` depends on this module, not the other way round, and
    #       the fallback is a convenience of the CLI rather than a property of
    #       `generate_report`. A tree without the live package still renders.
    ticks = None
    if args.data is None:
        try:
            from .live.prices import series_for_run_dir
            ticks = series_for_run_dir(args.run_dir)
        except Exception:                              # noqa: BLE001 — see Why
            ticks = None
        if ticks is not None:
            print(f"price background: {len(ticks):,} minute rows, "
                  f"{ticks['timestamp'].min()} .. {ticks['timestamp'].max()} "
                  f"(the whole soak, not just this run)")

    try:
        paths = generate_report(args.run_dir, data_path=args.data, ticks=ticks)
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
