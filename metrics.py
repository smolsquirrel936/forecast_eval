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
from typing import (Any, Callable, Collection, Dict, List, Optional,
                    Sequence)

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
    rolled: int = 0            # contract rolls this round-trip survived (§2.6)


def build_trades(
    fills: Sequence[FillEvent],
    *,
    session_lookup: Optional[Callable[[pd.Timestamp], str]] = None,
    roll_fills: Optional[Collection[Any]] = None,
    fill_bar_idx: Optional[Sequence[int]] = None,
) -> List[TradeRecord]:
    """Walk fills chronologically; pair opening fills with closing fills.

    Assumes ``max_position == 1`` (v1 invariant) — every non-flat fill is
    either purely opening or purely closing.

    ``roll_fills`` holds the timestamps of fills that are contract-roll legs
    (§2.6). A closing fill listed there does not end the round-trip: its PnL is
    banked and the trade continues at the re-entry's price, so one position held
    across a roll stays one trade. Leave it ``None`` and nothing changes.
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
    # Tech: PnL and fees banked from legs already closed by a roll, the number of
    #       rolls this round-trip has survived, and the leg it was suspended on.
    # Why:  §2.6 — rolling closes one contract and opens another, which is two
    #       fills for one economic position. Booking them as two trades would
    #       split a median 4.5-day hold into two shorter ones and inflate the
    #       trade count, so per-trade metrics would stop being comparable with the
    #       backtest, where a held position simply crosses a splice. `suspended`
    #       keeps the last roll-close so an exposure that never comes back is
    #       still booked, at the price it actually left at.
    rolls = set() if roll_fills is None else set(roll_fills)
    carry_pnl = 0.0
    carry_fees = 0.0
    n_rolled = 0
    suspended: Optional[FillEvent] = None

    for k, f in enumerate(fills):
        signed = 1 if f.side == "BUY" else -1
        if pos == 0 and suspended is not None and f.timestamp in rolls:
            # Tech: the roll's opening leg — resume the suspended trade at the new
            #       contract's price, keeping entry time, side and index.
            # Why:  this is what makes the pair one trade. The entry *price* has
            #       to move to the new month or the PnL would be measured across
            #       two instruments; the entry *time* must not, because the hold
            #       began when the position did.
            pos = signed
            entry_price = f.fill_price
            entry_fee = f.fee
            suspended = None
            continue

        if pos == 0:
            # Tech: flat → this fill opens a position; capture all entry details.
            # Why:  the optional fill_bar_idx lets us record holding period later;
            #       when it's absent we store -1 and report 0 bars held.
            if suspended is not None:
                # Tech: a roll whose re-entry never arrived, and something else
                #       has opened instead — book the suspended trade first.
                # Why:  the exposure did end, at the roll's closing price. Letting
                #       the new position inherit the banked PnL would attribute one
                #       trade's result to another.
                trades.append(_book(
                    entry_ts, suspended, side, entry_price, pos_before=side,
                    carry_pnl=carry_pnl, carry_fees=carry_fees,
                    n_rolled=n_rolled, session_lookup=session_lookup,
                    entry_idx=entry_idx, fill_bar_idx=fill_bar_idx))
            carry_pnl = carry_fees = 0.0
            n_rolled = 0
            suspended = None
            pos = signed
            entry_price = f.fill_price
            entry_ts = f.timestamp
            entry_fee = f.fee
            entry_idx = fill_bar_idx[k] if fill_bar_idx is not None else -1
            side = "LONG" if signed > 0 else "SHORT"
        elif signed != pos and f.timestamp in rolls:
            # Tech: a roll's closing leg — bank its PnL and fees, remember it,
            #       and go flat without emitting a trade.
            # Why:  the position is economically continuous; only the instrument
            #       under it changed. Everything about the entry is kept, so the
            #       round-trip that eventually closes reports the original entry
            #       time and side, with `rolled` saying how many boundaries it
            #       crossed.
            carry_pnl += (f.fill_price - entry_price) * pos
            carry_fees += entry_fee + f.fee
            n_rolled += 1
            suspended = f
            pos = 0
        elif signed != pos:  # closing fill
            # Tech: opposite-side fill → close the round-trip: PnL is (exit-entry)
            #       signed by direction, fees sum both legs, session is the exit's,
            #       and bars_held is the index span when available.
            # Why:  this is the only place a TradeRecord is born; pnl*pos folds
            #       long/short into one formula, and session_lookup defers session
            #       classification so this stays independent of the Environment.
            pnl = (f.fill_price - entry_price) * pos + carry_pnl
            fees = (f.fee if n_rolled else entry_fee + f.fee) + carry_fees
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
                rolled=n_rolled,
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

    # Tech: a roll left suspended when the fills run out is still a closed
    #       position — book it at the roll's closing price.
    # Why:  otherwise a session that rolls and then ends (a soak stopped in the
    #       roll window, or a re-entry that never filled) silently loses the whole
    #       round-trip from the metrics, which is the one place it would be
    #       noticed.
    if suspended is not None:
        trades.append(_book(
            entry_ts, suspended, side, entry_price, pos_before=side,
            carry_pnl=carry_pnl, carry_fees=carry_fees, n_rolled=n_rolled,
            session_lookup=session_lookup, entry_idx=entry_idx,
            fill_bar_idx=fill_bar_idx))

    return trades


def _book(entry_ts, exit_fill, side, entry_price, *, pos_before, carry_pnl,
          carry_fees, n_rolled, session_lookup, entry_idx, fill_bar_idx):
    # Tech: build the TradeRecord for a trade whose last leg was a roll close,
    #       using only the PnL already banked.
    # Why:  the closing leg's arithmetic happened when it was banked, so redoing
    #       it here would double-count. This exists so the two places that can
    #       discover an unresumed roll — a fresh entry, and the end of the
    #       stream — cannot drift apart.
    sess = session_lookup(exit_fill.timestamp) if session_lookup else "UNKNOWN"
    return TradeRecord(
        entry_ts=entry_ts,
        exit_ts=exit_fill.timestamp,
        side=side,
        entry_price=entry_price,
        exit_price=exit_fill.fill_price,
        pnl_points=carry_pnl,
        fees_points=carry_fees,
        net_pnl_points=carry_pnl - carry_fees,
        session=sess,
        bars_held=0,
        rolled=n_rolled,
    )


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
# Annual returns (per calendar year of exit).
# ---------------------------------------------------------------------------


def annual_returns(
    trade_records: Sequence[Dict],
    *,
    point_value: float = 200.0,
    capital: float = 1_000_000.0,
) -> Dict[str, list]:
    """Per-calendar-year net PnL and rate of return, grouped by exit year.

    Takes the dict records ``compute_metrics`` packs under
    ``trades.records``. ``rate_of_return`` is the year's net PnL in currency
    (points x ``point_value``) divided by ``capital`` — the backtest models
    no account equity, so the base is whatever the caller declares it to be.
    ``partial_year`` marks years the trading window does not fully cover, so
    their rate must not be read as a whole-year rate.
    """
    # Tech: keep only records with a usable exit timestamp and net PnL, and
    #       return the empty pack when nothing remains.
    # Why:  a year cannot be assigned without an exit time; the empty pack (not
    #       a KeyError or NaN row) is what makes the report omit the section
    #       cleanly on a run that never traded.
    usable = [r for r in (trade_records or [])
              if r.get("exit_ts") is not None
              and r.get("net_pnl_points") is not None]
    if not usable:
        return {"rows": []}

    # Tech: parse exits/entries to Timestamps, and collect the net points
    #       alongside; entries fall back to their own exit when absent.
    # Why:  pd.Timestamp accepts datetime objects (fresh records) and the
    #       strings a re-read trades.csv would carry, so the function is safe
    #       at either call site; the booking convention is "by calendar year
    #       of exit" because TradeRecord books PnL to the exit, and the year
    #       follows it.
    exits = [pd.Timestamp(r["exit_ts"]) for r in usable]
    entries = [pd.Timestamp(r["exit_ts"] if r.get("entry_ts") is None
                            else r["entry_ts"]) for r in usable]
    nets = [float(r["net_pnl_points"]) for r in usable]
    years = [int(t.year) for t in exits]

    # Tech: the trading window is first entry -> last exit; a year is partial
    #       unless that window spans its whole [Jan 1, Dec 31] range.
    # Why:  the first and last years of a run are cut short by the warmup split
    #       or the tape's end, and their rates would read as full-year
    #       performance; only a year the window covers completely is one.
    window_start, window_end = min(entries), max(exits)

    rows: List[Dict] = []
    for year in sorted(set(years)):
        # Tech: sum that year's net points, convert to currency, and divide by
        #       the capital base (NaN when the base is zero, matching the
        #       denominator-guard convention of trading_metrics).
        # Why:  points x point_value is the same rescale the report's currency
        #       axis uses, so the table agrees with the charts; guarding the
        #       base keeps a bad --capital from crashing a finished run's
        #       report instead of rendering an undefined rate.
        net = sum(n for n, y in zip(nets, years) if y == year)
        currency = net * point_value
        rate = currency / capital if capital else float("nan")
        # Tech: the year's last representable instant is the next year's first
        #       nanosecond minus one, built by int arithmetic on .value rather
        #       than by subtracting a pd.Timedelta.
        # Why:  ns math on the underlying int is exact to the calendar boundary,
        #       and the equivalent Timedelta subtraction fires NumPy's
        #       generic-unit DeprecationWarning on this stack (pandas 2.3.3) —
        #       noise the suite already carries enough of.
        year_start = pd.Timestamp(year=year, month=1, day=1)
        year_end = pd.Timestamp(pd.Timestamp(year=year + 1, month=1, day=1).value - 1)
        partial = not (window_start <= year_start and window_end >= year_end)
        rows.append({
            "year": year,
            "rate_of_return": rate,
            "net_pnl_currency": currency,
            "partial_year": bool(partial),
        })
    return {"rows": rows}


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
    fee_rate: float = 0.0002,
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
    roll_fills: Optional[Collection[Any]] = None,
) -> Dict[str, Dict]:
    """Full metric pack. Bucketed by session iff ``forced_close``."""
    # Tech: rebuild round-trips from the fills, tagging each by the exit's session.
    # Why:  trades are the unit every trading metric needs; classify_session is the
    #       same function the Environment used, so booking is consistent end to end.
    # Tech: `roll_fills` keeps a position held across a contract roll one trade.
    # Why:  §2.6 — rolling closes one month and opens another, two fills for one
    #       economic position. Passing the legs through is what keeps a live
    #       session's per-trade numbers comparable with a backtest, where a held
    #       position crosses a splice instead of being closed and re-opened.
    session_lookup = classify_session
    trades = build_trades(result.fills, session_lookup=session_lookup,
                          roll_fills=roll_fills)

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
