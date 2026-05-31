"""Metrics module (SPEC §7).

Three blocks:
  * Trading metrics  — built from closed round-trips and the fee log.
  * Forecast-quality — predicted vs. realized return, direction hit rate,
                        Spearman information coefficient.
  * Attribution      — frictionless signal PnL vs. realized PnL; the gap
                        is execution + cost drag.

Session bucketing: when the backtest was run with
``forced_close_on_session_end=True``, ``compute_metrics`` returns separate
DAY / NIGHT trading-metric blocks (forecast-quality and attribution stay
global, because the SPEC defines them at the forecast/signal level).

Annualization is intentionally NOT applied — Sharpe / Sortino are
reported per-trade. Multiply by sqrt(trades_per_period) externally if you
need an annualized number.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Callable, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from .environment import classify_session
from .events import FillEvent, Forecast


# ---------------------------------------------------------------------------
# Trades (round-trips) built from a sequence of fills.
# ---------------------------------------------------------------------------


@dataclass
class TradeRecord:
    # Tech: one closed round-trip — entry/exit times and prices, gross PnL, both
    #       legs' fees, net PnL, the session it's booked to, and bars held.
    # Why:  trading metrics operate on completed round-trips, not raw fills; pre-
    #       splitting gross/fees/net here keeps every downstream metric a simple
    #       column read. `session` is the EXIT's session — that's where PnL lands.
    entry_ts: datetime
    exit_ts: datetime
    side: str                  # "LONG" / "SHORT"
    entry_price: float
    exit_price: float
    pnl_points: float          # gross, excludes fees
    fees_points: float         # entry + exit fees, both legs
    net_pnl_points: float      # pnl_points - fees_points
    session: str               # session of the EXIT (where PnL is booked)
    bars_held: int             # exit_idx - entry_idx (if available)


def build_trades(
    fills: Sequence[FillEvent],
    *,
    session_lookup: Optional[Callable[[pd.Timestamp], str]] = None,
    fill_bar_idx: Optional[Sequence[int]] = None,
) -> List[TradeRecord]:
    """Walk fills chronologically; pair opening fills with closing fills.

    Assumes ``max_position == 1`` (v1 invariant) — every non-flat fill is
    either purely opening or purely closing.
    """
    # Tech: running state for the open leg as we scan fills (position sign, entry
    #       price/time/index/fee, and side label).
    # Why:  a single forward pass pairs each open with its next close; holding the
    #       open leg's details lets us emit a complete TradeRecord the moment the
    #       closing fill arrives.
    trades: List[TradeRecord] = []
    pos = 0
    entry_price = 0.0
    entry_ts: Optional[datetime] = None
    entry_idx = -1
    entry_fee = 0.0
    side = ""

    for k, f in enumerate(fills):
        signed = 1 if f.side == "BUY" else -1
        if pos == 0:
            # Tech: flat → this fill opens a position; capture all entry details.
            # Why:  the optional fill_bar_idx lets us record holding period later;
            #       when it's absent we store -1 and report 0 bars held.
            pos = signed
            entry_price = f.fill_price
            entry_ts = f.timestamp
            entry_fee = f.fee
            entry_idx = fill_bar_idx[k] if fill_bar_idx is not None else -1
            side = "LONG" if signed > 0 else "SHORT"
        elif signed != pos:  # closing fill
            # Tech: opposite-side fill → close the round-trip: PnL is (exit-entry)
            #       signed by direction, fees sum both legs, session is the exit's,
            #       and bars_held is the index span when available.
            # Why:  this is the only place a TradeRecord is born; pnl*pos folds
            #       long/short into one formula, and session_lookup defers session
            #       classification so this stays independent of the Environment.
            pnl = (f.fill_price - entry_price) * pos
            fees = entry_fee + f.fee
            sess = session_lookup(f.timestamp) if session_lookup else "UNKNOWN"
            bars = (
                (fill_bar_idx[k] - entry_idx)
                if (fill_bar_idx is not None and entry_idx >= 0)
                else 0
            )
            trades.append(TradeRecord(
                entry_ts=entry_ts,  # type: ignore[arg-type]
                exit_ts=f.timestamp,
                side=side,
                entry_price=entry_price,
                exit_price=f.fill_price,
                pnl_points=pnl,
                fees_points=fees,
                net_pnl_points=pnl - fees,
                session=sess,
                bars_held=bars,
            ))
            # Tech: reset all open-leg state back to flat.
            # Why:  prevents the just-closed trade from bleeding into the next pair;
            #       the scanner is now ready to treat the following fill as an open.
            pos = 0
            entry_price = 0.0
            entry_ts = None
            entry_idx = -1
            entry_fee = 0.0
            side = ""
        # signed == pos would be pyramiding; v1 disallows.

    return trades


# ---------------------------------------------------------------------------
# Trading metrics.
# ---------------------------------------------------------------------------


def trading_metrics(trades: Sequence[TradeRecord]) -> Dict[str, float]:
    # Tech: with no trades, return a fully-populated dict of zeros/NaNs.
    # Why:  downstream printers and CSV writers expect a fixed schema; returning the
    #       same keys (NaN where a ratio is undefined) avoids KeyErrors on empty runs.
    n = len(trades)
    if n == 0:
        return {
            "n_trades": 0,
            "gross_pnl_points": 0.0,
            "total_fees_points": 0.0,
            "net_pnl_points": 0.0,
            "hit_rate": float("nan"),
            "avg_win_points": float("nan"),
            "avg_loss_points": float("nan"),
            "turnover_points": 0.0,
            "fee_drag": float("nan"),
            "max_drawdown_points": 0.0,
            "max_drawdown_duration_trades": 0,
            "sharpe_per_trade": float("nan"),
            "sortino_per_trade": float("nan"),
        }

    # Tech: vectorize the three per-trade series and split net into wins/losses.
    # Why:  numpy arrays make the aggregate stats (mean/std/cumsum) cheap and
    #       readable; wins/losses are needed for hit rate and average win/loss.
    gross = np.array([t.pnl_points for t in trades])
    fees = np.array([t.fees_points for t in trades])
    net = np.array([t.net_pnl_points for t in trades])

    wins = net[net > 0]
    losses = net[net < 0]

    # Tech: build the per-trade equity curve and its drawdown vs. running peak.
    # Why:  drawdown is the gap below the high-water mark; computing it on the
    #       cumulative net series (one point per trade) matches the per-trade,
    #       unannualized convention this module reports in.
    equity = net.cumsum()
    peaks = np.maximum.accumulate(equity)
    drawdown = equity - peaks
    max_dd = float(drawdown.min())
    # DD duration in trades: longest consecutive run of (equity < running peak).
    # Tech: scan the underwater flags, tracking the current and best run lengths.
    # Why:  "duration" is the longest stretch spent below a prior peak; a simple
    #       linear pass over the boolean mask is enough and avoids extra deps.
    in_dd = drawdown < 0
    dur = 0
    best = 0
    for v in in_dd:
        if v:
            dur += 1
            if dur > best:
                best = dur
        else:
            dur = 0

    # Tech: sample std of net PnL, and the downside-only std for Sortino.
    # Why:  ddof=1 (sample, not population) is correct for a finite sample of
    #       trades; guarding n>1 / len(down)>1 avoids a divide-by-zero std.
    std = net.std(ddof=1) if n > 1 else 0.0
    down = net[net < 0]
    down_std = down.std(ddof=1) if len(down) > 1 else 0.0

    gross_sum = float(gross.sum())
    fee_sum = float(fees.sum())

    # Tech: assemble the full metric dict — totals, hit rate, avg win/loss,
    #       turnover, fee drag, drawdown, and per-trade Sharpe/Sortino.
    # Why:  every ratio guards its denominator (NaN when undefined) so the dict is
    #       always well-formed; Sharpe/Sortino are mean/std with no annualization,
    #       as the module docstring promises.
    return {
        "n_trades": n,
        "gross_pnl_points": gross_sum,
        "total_fees_points": fee_sum,
        "net_pnl_points": float(net.sum()),
        "hit_rate": float((net > 0).mean()),
        "avg_win_points": float(wins.mean()) if len(wins) else float("nan"),
        "avg_loss_points": float(losses.mean()) if len(losses) else float("nan"),
        # Turnover ≈ sum of notional traded across both legs of each trade.
        "turnover_points": float(sum(t.entry_price + t.exit_price for t in trades)),
        "fee_drag": fee_sum / gross_sum if gross_sum != 0 else float("nan"),
        "max_drawdown_points": max_dd,
        "max_drawdown_duration_trades": int(best),
        "sharpe_per_trade": float(net.mean() / std) if std > 0 else float("nan"),
        "sortino_per_trade": (
            float(net.mean() / down_std) if down_std > 0 else float("nan")
        ),
    }


# ---------------------------------------------------------------------------
# Forecast-quality metrics.
# ---------------------------------------------------------------------------


def _spearman_corr(x: np.ndarray, y: np.ndarray) -> float:
    """Spearman rank correlation, no SciPy dependency."""
    # Tech: rank both series, bail to NaN if too short or either is constant, else
    #       return the Pearson correlation of the ranks.
    # Why:  Spearman *is* Pearson-on-ranks, so pandas .rank() + np.corrcoef gives it
    #       without SciPy; a zero-variance rank vector has undefined correlation, so
    #       we return NaN rather than let corrcoef emit a warning/garbage.
    if len(x) < 2:
        return float("nan")
    rx = pd.Series(x).rank().to_numpy()
    ry = pd.Series(y).rank().to_numpy()
    if rx.std() == 0 or ry.std() == 0:
        return float("nan")
    return float(np.corrcoef(rx, ry)[0, 1])


def forecast_quality(
    forecasts: Sequence[Forecast],
    tick_df: pd.DataFrame,
) -> Dict[str, float]:
    """Per-forecast direction hit rate + Spearman IC.

    Realized return is measured over ``forecast.horizon_bars`` TICKS in
    ``tick_df``. If the forecast horizon is expressed in bars at a
    different aggregation level (e.g. a Toto2Forecaster with
    ``bar_freq='1min'``), the metric is approximate — the realized window
    is taken in ticks, not in the model's bar unit.
    """
    # Tech: the default return shape (all NaN), used for the empty/degenerate cases.
    # Why:  same fixed-schema reasoning as trading_metrics — callers and CSV writers
    #       rely on these keys always existing.
    out_default = {
        "n_forecasts": len(forecasts),
        "n_forecasts_evaluated": 0,
        "direction_hit_rate": float("nan"),
        "information_coefficient": float("nan"),
        "mean_predicted_return": float("nan"),
        "mean_realized_return": float("nan"),
    }
    if not forecasts:
        return out_default

    # Tech: index the tick frame by timestamp so a forecast time maps to a row.
    # Why:  realized return is read at i+horizon, so we need O(1) timestamp→position
    #       lookup; building the dict once is far cheaper than searching per forecast.
    ts = tick_df["timestamp"].to_list()
    px = tick_df["price"].to_numpy(dtype=float)
    ts_to_idx = {t: i for i, t in enumerate(ts)}

    # Tech: collect (predicted, realized) pairs, skipping forecasts we can't score —
    #       non-dict payloads, missing predicted_return, unknown timestamps, horizons
    #       that run off the end, or a zero base price.
    # Why:  forecasts are only comparable to a realized move when the horizon row
    #       actually exists in the data and the base price is nonzero; filtering here
    #       keeps the correlation/hit-rate honest rather than seeding it with junk.
    pred: List[float] = []
    real: List[float] = []
    for fc in forecasts:
        if not isinstance(fc.payload, dict):
            continue
        pr = fc.payload.get("predicted_return")
        if pr is None:
            continue
        i = ts_to_idx.get(fc.timestamp)
        if i is None:
            continue
        j = i + fc.horizon_bars
        if j >= len(px) or px[i] == 0:
            continue
        pred.append(float(pr))
        real.append(float((px[j] - px[i]) / px[i]))

    if not pred:
        return out_default

    p = np.asarray(pred)
    r = np.asarray(real)
    # Direction hit: same sign. Predictions of exactly 0 are excluded from
    # the hit-rate denominator (no directional bet).
    # Tech: among nonzero predictions, the hit rate is the fraction whose sign
    #       matches the realized sign.
    # Why:  a predicted return of exactly 0 is "no bet", so including it would
    #       unfairly dilute the directional accuracy; we exclude it from the base.
    nonzero = p != 0
    if nonzero.any():
        dir_hit = float(((p[nonzero] > 0) == (r[nonzero] > 0)).mean())
    else:
        dir_hit = float("nan")

    # Tech: report counts, hit rate, the Spearman IC, and mean predicted/realized.
    # Why:  IC (rank correlation) measures monotonic skill independent of scale,
    #       complementing the binary hit rate; the means give a quick bias read.
    return {
        "n_forecasts": len(forecasts),
        "n_forecasts_evaluated": len(p),
        "direction_hit_rate": dir_hit,
        "information_coefficient": _spearman_corr(p, r),
        "mean_predicted_return": float(p.mean()),
        "mean_realized_return": float(r.mean()),
    }


# ---------------------------------------------------------------------------
# Attribution: frictionless signal PnL vs. realized PnL.
# ---------------------------------------------------------------------------


def signal_attribution(
    signals: Sequence[Dict],
    realized_net_pnl_points: float,
) -> Dict[str, float]:
    """Frictionless PnL from the SignalEmitter's outputs.

    Each signal "executes" at its tick's price with zero fees and no
    slippage; the state machine is the same no-flip rule the Trader uses
    (§4.2). The gap ``signal_pnl - realized_pnl`` is what the trader
    plumbing (slippage + fees) ate.
    """
    # Tech: replay signals through the no-flip state machine, accumulating PnL at
    #       each close as if every signal filled instantly at its tick price.
    # Why:  this isolates the *model's* theoretical edge from execution reality;
    #       mirroring the Trader's exact no-flip rule means the only difference vs.
    #       realized PnL is slippage + fees, which is precisely the drag we want.
    pos = 0
    entry = 0.0
    pnl = 0.0
    round_trips = 0
    for s in signals:
        d = s["direction"]
        p = float(s["price"])
        if d == "HOLD":
            continue
        if pos == 0:
            pos = 1 if d == "BUY" else -1
            entry = p
        elif pos > 0 and d == "SELL":
            pnl += p - entry
            pos = 0
            round_trips += 1
        elif pos < 0 and d == "BUY":
            pnl += entry - p
            pos = 0
            round_trips += 1
        # same-direction signal while in position -> no action (no pyramiding).

    # Tech: report frictionless PnL, round-trip count, any leftover open position,
    #       the realized net, and the gap between them.
    # Why:  the drag (signal_pnl − realized_pnl) is the headline attribution number
    #       from SPEC §7 — how much of the model's edge the trader plumbing eats.
    return {
        "signal_pnl_points": float(pnl),
        "signal_round_trips": int(round_trips),
        "unrealized_signal_position": int(pos),
        "realized_net_pnl_points": float(realized_net_pnl_points),
        "execution_and_cost_drag_points": float(pnl - realized_net_pnl_points),
    }


def signal_attribution_curve(signals: Sequence[Dict]) -> pd.DataFrame:
    """Per-round-trip cumulative frictionless PnL from SignalEmitter outputs.

    Same no-flip state machine as ``signal_attribution``; emits one row at
    each close so the result can be plotted against the realized equity
    curve (which also has one row per round-trip).
    """
    # Tech: same replay as signal_attribution, but append a (timestamp, cumulative
    #       PnL) row at every close instead of only returning the final total.
    # Why:  the report overlays this frictionless curve on the realized equity
    #       curve; emitting one row per close gives both series the same cadence so
    #       the shaded gap between them reads as execution drag over time.
    pos = 0
    entry = 0.0
    cum = 0.0
    rows = []
    for s in signals:
        d = s["direction"]
        p = float(s["price"])
        if d == "HOLD":
            continue
        if pos == 0:
            pos = 1 if d == "BUY" else -1
            entry = p
        elif pos > 0 and d == "SELL":
            cum += p - entry
            rows.append({"timestamp": s["timestamp"], "cum_signal_pnl_points": cum})
            pos = 0
        elif pos < 0 and d == "BUY":
            cum += entry - p
            rows.append({"timestamp": s["timestamp"], "cum_signal_pnl_points": cum})
            pos = 0
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Buy-and-hold baseline (SPEC §6 Phase 5: "as a floor").
# ---------------------------------------------------------------------------


def buy_and_hold_pnl(
    tick_df: pd.DataFrame,
    *,
    fee_rate: float = 0.00015,
) -> Dict[str, float]:
    """PnL of buying the first tick and selling the last, in price points.

    Charges the same per-side fee as the live trader (``price * fee_rate``).
    """
    # Tech: gross = last − first price; fees = both legs at the same per-side rate;
    #       net = gross − fees.
    # Why:  the simplest possible benchmark — "what if you just held?" — charged the
    #       identical fee schedule so the strategy is compared on equal cost terms
    #       (SPEC §6 Phase 5 floor).
    first = float(tick_df["price"].iloc[0])
    last = float(tick_df["price"].iloc[-1])
    gross = last - first
    fees = (first + last) * fee_rate
    return {
        "entry_price": first,
        "exit_price": last,
        "gross_pnl_points": gross,
        "total_fees_points": fees,
        "net_pnl_points": gross - fees,
    }


# ---------------------------------------------------------------------------
# Top-level entry.
# ---------------------------------------------------------------------------


def compute_metrics(
    result,                       # BacktestResult; avoids circular import
    tick_df: pd.DataFrame,
    *,
    forced_close: bool = False,
) -> Dict[str, Dict]:
    """Full metric pack. Bucketed by session iff ``forced_close``."""
    # Tech: rebuild round-trips from the fills, tagging each by the exit's session.
    # Why:  trades are the unit every trading metric needs; classify_session is the
    #       same function the Environment used, so booking is consistent end to end.
    session_lookup = classify_session
    trades = build_trades(result.fills, session_lookup=session_lookup)

    out: Dict[str, Dict] = {}
    if forced_close:
        # Tech: split trades by session and compute a trading block per session.
        # Why:  under forced close, DAY and NIGHT are independent trading regimes
        #       (positions never cross the boundary), so SPEC §7 reports them apart.
        for sess in ("DAY", "NIGHT"):
            sess_trades = [t for t in trades if t.session == sess]
            out[f"trading_{sess.lower()}"] = trading_metrics(sess_trades)
    else:
        # Tech: one combined trading block across all trades.
        # Why:  without forced close, positions carry across sessions, so a single
        #       unified series is the meaningful view.
        out["trading"] = trading_metrics(trades)

    # Tech: forecast-quality and attribution stay global, then attach the raw trade
    #       records as plain dicts.
    # Why:  the SPEC defines forecast/signal metrics at the model level, not per
    #       session, so they aren't bucketed; asdict() makes trades JSON/CSV-friendly
    #       for the logging layer.
    out["forecast"] = forecast_quality(result.forecasts, tick_df)
    out["attribution"] = signal_attribution(
        result.signals,
        realized_net_pnl_points=result.portfolio.net_pnl(),
    )
    out["trades"] = {"records": [asdict(t) for t in trades]}
    return out
