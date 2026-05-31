"""End-to-end driver (SPEC §4.1).

Per-tick event loop:
  1. check pending-order fills
  2. check ExitRule (if configured and a position is open)
  3. on forecast boundary AND past warm-up: Forecaster -> SignalEmitter -> Trader
  4. session-boundary forced close (if configured)
  5. record fills / signals / forecasts

Phase 3 wires all five steps. The session-boundary close tags realized
PnL to the *closing* session even though the fill itself executes on the
first tick of the new session (SPEC §4.7).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

import pandas as pd

from .environment import Environment
from .events import FillEvent, Forecast, OrderEvent
from .execution import Execution
from .exits.base import ExitRule
from .forecaster.base import Forecaster
from .portfolio import Portfolio
from .strategy.base import SignalEmitter
from .strategy.dummy import DummyAlternatingEmitter
from .trader import Trader


@dataclass
class BacktestResult:
    # Tech: the full record of one backtest — final Portfolio, event counts, and
    #       the raw event lists (fills/orders/forecasts/signals) plus last price.
    # Why:  metrics and reporting are computed *after* the run from these lists, so
    #       the loop stays pure (just records) and analysis can be re-run without
    #       re-simulating; signals carries the emit-time price needed by attribution.
    portfolio: Portfolio
    n_bars: int
    n_signals: int
    n_orders: int
    n_forecasts: int
    n_exits: int = 0
    n_forced_closes: int = 0
    fills: List[FillEvent] = field(default_factory=list)
    orders: List[OrderEvent] = field(default_factory=list)
    forecasts: List[Forecast] = field(default_factory=list)
    # Per-emission signal record: {timestamp, direction, price, session}.
    # Needed by metrics.signal_attribution for the frictionless PnL gap.
    signals: List[Dict[str, Any]] = field(default_factory=list)
    last_price: Optional[float] = None

    def summary(self) -> Dict[str, Any]:
        # Tech: start from the Portfolio's headline numbers, then fold in the loop's
        #       own counters and, if a last price is known, the open unrealized PnL.
        # Why:  one flat dict is what every demo/printer consumes; unrealized PnL is
        #       only added when meaningful (a price to mark against exists) so a
        #       flat-at-end run doesn't report a spurious figure.
        out = self.portfolio.summary()
        out.update({
            "n_bars": self.n_bars,
            "n_signals": self.n_signals,
            "n_orders": self.n_orders,
            "n_forecasts": self.n_forecasts,
            "n_exits": self.n_exits,
            "n_forced_closes": self.n_forced_closes,
        })
        if self.last_price is not None:
            out["unrealized_pnl_points"] = self.portfolio.unrealized_pnl(
                self.last_price
            )
        return out


def run_backtest(
    ticks: pd.DataFrame,
    *,
    tick_size: float = 1.0,
    aggression_ticks: int = 3,
    fee_rate: float = 0.00015,
    contract_multiplier: float = 200.0,
    max_position: int = 1,
    forecaster: Optional[Forecaster] = None,
    emitter: Optional[SignalEmitter] = None,
    exit_rule: Optional[ExitRule] = None,
    forced_close_on_session_end: bool = False,
    # Used only when no forecaster is supplied (Phase 1 dummy-signal path).
    forecast_stride_bars: int = 12,
    warmup_bars: int = 0,
    progress: bool = False,
    progress_desc: str = "backtest",
) -> BacktestResult:
    # Tech: wire up the four collaborating objects for this run.
    # Why:  each is constructed fresh per backtest so no state leaks between runs;
    #       this is the dependency-injection seam that lets the harness stay model-
    #       and strategy-agnostic (everything below talks to abstract interfaces).
    env = Environment(ticks)
    execution = Execution(fee_rate=fee_rate)
    portfolio = Portfolio(contract_multiplier=contract_multiplier)
    trader = Trader(
        execution=execution,
        tick_size=tick_size,
        aggression_ticks=aggression_ticks,
        max_position=max_position,
    )
    # Tech: default to the alternating dummy emitter when none is supplied.
    # Why:  the Phase 1 path validates execution/fees without a model; making the
    #       dummy the default keeps `run_backtest(ticks)` runnable out of the box.
    if emitter is None:
        emitter = DummyAlternatingEmitter()

    # Tech: when a forecaster is present, take stride/warmup from it; otherwise use
    #       the explicit kwargs.
    # Why:  a real forecaster *declares* its own cadence and context need (SPEC §3),
    #       which must win; the kwargs only matter for the model-less dummy path.
    use_forecaster = forecaster is not None
    if use_forecaster:
        stride = forecaster.forecast_stride_bars
        warmup = forecaster.warmup_bars
    else:
        stride = forecast_stride_bars
        warmup = warmup_bars

    # Tech: rolling history buffers (parallel lists of ts/price/volume).
    # Why:  rebuilt into a DataFrame slice at each forecast boundary; growing plain
    #       lists is far cheaper than repeatedly appending to a DataFrame, and the
    #       fresh-slice-per-forecast pattern is the structural look-ahead defense.
    history_ts: List[Any] = []
    history_px: List[float] = []
    history_vol: List[int] = []

    # Tech: per-run counters and event logs, all starting empty.
    # Why:  collected as the loop runs and packed into BacktestResult at the end;
    #       counters are cheap running totals while the lists hold the full audit trail.
    n_bars = 0
    n_signals = 0
    n_orders = 0
    n_forecasts = 0
    n_exits = 0
    n_forced_closes = 0
    fills: List[FillEvent] = []
    orders: List[OrderEvent] = []
    forecasts: List[Forecast] = []
    signals: List[Dict[str, Any]] = []
    last_price: Optional[float] = None

    # Tech: track the previous tick's session and a latch for an in-flight forced close.
    # Why:  a session change is detected by comparing consecutive ticks; the latch
    #       remembers which (prior) session a forced-close fill should be booked to,
    #       since that fill lands on the *next* session's first tick (SPEC §4.7).
    prev_session: Optional[str] = None
    # When a forced close is in flight, the next fill that brings us to
    # flat is tagged to this session instead of the fill's own market.session.
    force_close_pending_session: Optional[str] = None

    # Tech: get the event generator, optionally wrapping it in a tqdm progress bar.
    # Why:  real-data runs over millions of ticks are slow enough to want a bar;
    #       tqdm is imported lazily so it isn't a hard dependency for quick demos.
    stream = env.stream()
    if progress:
        from tqdm.auto import tqdm
        stream = tqdm(stream, total=len(ticks), desc=progress_desc, unit="bar")

    for bar_idx, market in enumerate(stream):
        # Tech: remember the latest price for end-of-run unrealized-PnL marking.
        # Why:  the loop may end with an open position; last_price is the only mark
        #       available to value it.
        last_price = market.price

        # Step 1 — check fills (orders placed this tick can't fill on it).
        # Tech: ask Execution to resolve any resting order against this print; if it
        #       fills, decide which session to book PnL to, apply it to the
        #       Portfolio and Trader, and log the fill.
        # Why:  fills run first each tick (§4.1) so a just-closed position is flat
        #       before new decisions; the force_close_pending_session override
        #       redirects a boundary-crossing close's PnL back to the session it was
        #       earned in rather than the new session the fill physically prints in.
        fill = execution.check_fill(market)
        if fill is not None:
            signed = 1 if fill.side == "BUY" else -1
            will_be_flat = (trader.position + signed * fill.quantity) == 0
            session_for_pnl = market.session
            if force_close_pending_session is not None and will_be_flat:
                session_for_pnl = force_close_pending_session
                force_close_pending_session = None
            portfolio.apply_fill(fill, session=session_for_pnl)
            trader.on_fill(
                fill.side,
                fill.quantity,
                fill_price=fill.fill_price,
                bar_idx=bar_idx,
                timestamp=fill.timestamp,
            )
            fills.append(fill)

        # Tech: append this tick to the history buffers *after* fills resolve.
        # Why:  history through bar_idx must represent everything observed up to and
        #       including time t; recording here (post-fill, pre-forecast) is what
        #       makes the forecaster's slice causally honest.
        history_ts.append(market.timestamp)
        history_px.append(market.price)
        history_vol.append(market.volume)

        # Step 2 — exit rule.
        # Tech: if an ExitRule is configured and a position is open, query it; on
        #       True, submit a closing limit and bump the exit/order counters.
        # Why:  risk exits are checked every tick (not just on forecast boundaries)
        #       so a stop can fire intra-stride; position_state() is None when flat,
        #       which short-circuits the query.
        if exit_rule is not None:
            pos = trader.position_state()
            if pos is not None and exit_rule.should_exit(
                pos, market, bar_idx=bar_idx
            ):
                order = trader.submit_exit(market)
                if order is not None:
                    orders.append(order)
                    n_orders += 1
                    n_exits += 1

        # Step 3 — forecast boundary.
        # Tech: fire only once warm-up is complete and we're exactly on a stride
        #       boundary (counted from the end of warm-up).
        # Why:  warm-up gives a model enough context before it trades (SPEC §4.8);
        #       the modulo against (bar_idx - warmup) makes the first forecast land
        #       on the warm-up boundary itself, then every `stride` ticks after.
        if bar_idx >= warmup and (bar_idx - warmup) % stride == 0:
            forecast: Optional[Forecast] = None
            if use_forecaster:
                # Tech: build a fresh DataFrame from the history-through-t buffers and
                #       run the model; raise if the returned forecast looks ahead of t.
                # Why:  passing only rows ≤ t is the structural look-ahead defense; the
                #       runtime timestamp check is a belt-and-suspenders guard that
                #       catches a forecaster which fabricates a future timestamp.
                hist_df = pd.DataFrame({
                    "timestamp": history_ts,
                    "price": history_px,
                    "volume": history_vol,
                })
                forecast = forecaster.forecast(hist_df)
                if forecast.timestamp > market.timestamp:
                    raise RuntimeError(
                        f"Look-ahead leak: forecast.timestamp "
                        f"{forecast.timestamp} > current tick "
                        f"{market.timestamp}"
                    )
                forecasts.append(forecast)
                n_forecasts += 1

            # Tech: convert the forecast to a direction, log the signal with its
            #       emit-time price, and let the Trader act on it.
            # Why:  the signal record (price + session at emit) is what frictionless
            #       attribution replays later; on_signal applies the no-flip state
            #       machine and may or may not produce an order.
            direction = emitter.emit(forecast)
            n_signals += 1
            signals.append({
                "timestamp": market.timestamp,
                "direction": direction,
                "price": market.price,
                "session": market.session,
            })
            order = trader.on_signal(direction, market)
            if order is not None:
                n_orders += 1
                orders.append(order)

        # Step 4 — session-boundary forced close.
        # Detected on the first tick of the new session. Submitting here
        # places the limit at the new-session price; the actual fill arrives
        # on the next tick, but its realized PnL is tagged to prev_session
        # via force_close_pending_session.
        # Tech: when enabled and the session just flipped while holding a position,
        #       submit a closing limit and arm the latch with the prior session.
        # Why:  forced close flattens risk overnight/over the gap (SPEC §4.7); the
        #       latch is what later books the resulting PnL to prev_session even
        #       though the fill prints in the new one.
        if (
            forced_close_on_session_end
            and prev_session is not None
            and market.session != prev_session
            and trader.position != 0
        ):
            order = trader.submit_exit(market)
            if order is not None:
                orders.append(order)
                n_orders += 1
                n_forced_closes += 1
                force_close_pending_session = prev_session

        # Tech: advance the session tracker and bar counter for the next iteration.
        # Why:  prev_session must update every tick so the next boundary compare is
        #       correct; n_bars is the authoritative processed-tick count.
        prev_session = market.session
        n_bars += 1

    # Tech: pack every counter and event log into the immutable result.
    # Why:  a single return object keeps the loop's outputs together for metrics,
    #       logging, and reporting downstream.
    return BacktestResult(
        portfolio=portfolio,
        n_bars=n_bars,
        n_signals=n_signals,
        n_orders=n_orders,
        n_forecasts=n_forecasts,
        n_exits=n_exits,
        n_forced_closes=n_forced_closes,
        fills=fills,
        orders=orders,
        forecasts=forecasts,
        signals=signals,
        last_price=last_price,
    )


# ---------------------------------------------------------------------------
# Synthetic-data helpers + demos
# ---------------------------------------------------------------------------


def _synthetic_ticks(n: int = 2000, start_price: float = 17500.0,
                     seed: int = 0) -> pd.DataFrame:
    """Random-walk tick stream within a single DAY session."""
    # Tech: build a ±1/0 random walk of length n, then attach 1-second timestamps
    #       inside DAY hours and random volumes.
    # Why:  a seeded RNG makes demos reproducible; staying inside one DAY session
    #       keeps session logic out of the picture for the simplest sanity demos.
    import numpy as np
    rng = np.random.default_rng(seed)
    steps = rng.choice([-1, 0, 1], size=n, p=[0.33, 0.34, 0.33])
    prices = start_price + steps.cumsum()
    base = pd.Timestamp("2024-01-02 09:00:00")
    timestamps = pd.date_range(base, periods=n, freq="1s")
    return pd.DataFrame({
        "timestamp": timestamps,
        "price": prices.astype(float),
        "volume": rng.integers(1, 10, size=n),
    })


def _synthetic_ticks_two_sessions(seed: int = 1) -> pd.DataFrame:
    """600 DAY ticks + 600 NIGHT ticks for the forced-close demo."""
    # Tech: a local helper walks a price series; build a DAY block (09:00) and a
    #       NIGHT block (16:00) that continues from the DAY close, then concatenate.
    # Why:  the forced-close demo needs a genuine DAY→NIGHT boundary; chaining the
    #       night start price to the day's last keeps the series continuous so PnL
    #       across the gap is meaningful rather than a synthetic jump.
    import numpy as np
    rng = np.random.default_rng(seed)

    def walk(n, start):
        steps = rng.choice([-1, 0, 1], size=n, p=[0.33, 0.34, 0.33])
        return (start + steps.cumsum()).astype(float)

    day_ts = pd.date_range("2024-01-02 09:00:00", periods=600, freq="1s")
    night_ts = pd.date_range("2024-01-02 16:00:00", periods=600, freq="1s")
    day_px = walk(600, 17500)
    night_px = walk(600, day_px[-1])
    df = pd.DataFrame({
        "timestamp": list(day_ts) + list(night_ts),
        "price":     list(day_px) + list(night_px),
        "volume":    list(rng.integers(1, 10, size=1200)),
    })
    return df


def demo() -> BacktestResult:
    """Phase 1 regression — dummy alternating signal, no exits."""
    # Tech: run the default (model-less) backtest on 2000 synthetic ticks.
    # Why:  exercises fills/fees/state-machine end to end with no model, which is
    #       the Phase 1 acceptance check (hand-verifiable round-trips).
    ticks = _synthetic_ticks(n=2000)
    return run_backtest(
        ticks,
        tick_size=1.0,
        aggression_ticks=3,
        fee_rate=0.00015,
        contract_multiplier=200.0,
        forecast_stride_bars=120,
        warmup_bars=0,
    )


def demo_naive() -> BacktestResult:
    """Phase 2 demo — NaiveLastPrice + ThresholdEmitter (no trades)."""
    # Tech: wire the predict-last-price forecaster to a threshold emitter and run.
    # Why:  the naive model predicts zero return, so the threshold emitter holds —
    #       the demo proves the forecaster→emitter→trader plumbing works and that a
    #       no-edge model correctly produces no trades (SPEC §9 milestone).
    from .forecaster.naive import NaiveLastPrice
    from .strategy.threshold import ThresholdEmitter

    ticks = _synthetic_ticks(n=2000)
    fc = NaiveLastPrice(
        warmup_bars=50,
        forecast_stride_bars=120,
        forecast_horizon_bars=120,
    )
    em = ThresholdEmitter(buy_threshold=0.001, sell_threshold=-0.001)
    return run_backtest(
        ticks,
        forecaster=fc,
        emitter=em,
        tick_size=1.0,
        aggression_ticks=3,
        fee_rate=0.00015,
    )


def demo_stop_loss() -> BacktestResult:
    """Phase 3 demo — dummy alternating + 5-tick fixed stop-loss."""
    # Tech: same dummy-signal run as demo() but with a FixedStopLoss exit attached.
    # Why:  exercises the Step-2 exit path — verifies a stop can fire between
    #       forecast boundaries and that submit_exit/cancel behave (Phase 3).
    from .exits.stop_loss import FixedStopLoss

    ticks = _synthetic_ticks(n=2000)
    return run_backtest(
        ticks,
        tick_size=1.0,
        aggression_ticks=3,
        fee_rate=0.00015,
        contract_multiplier=200.0,
        forecast_stride_bars=120,
        warmup_bars=0,
        exit_rule=FixedStopLoss(stop_loss_ticks=5, tick_size=1.0),
    )


def demo_forced_close() -> BacktestResult:
    """Phase 3 demo — DAY + NIGHT sessions; force-close at boundary."""
    # Tech: run on the two-session synthetic data with forced_close enabled.
    # Why:  the only demo that crosses a session boundary — checks the latch logic
    #       that books a boundary close's PnL to the prior session (SPEC §4.7).
    ticks = _synthetic_ticks_two_sessions()
    return run_backtest(
        ticks,
        tick_size=1.0,
        aggression_ticks=3,
        fee_rate=0.00015,
        contract_multiplier=200.0,
        forecast_stride_bars=120,
        warmup_bars=0,
        forced_close_on_session_end=True,
    )


def fee_sensitivity_sweep(
    ticks: pd.DataFrame,
    *,
    fee_rates: Sequence[float] = (0.0, 0.00015, 0.0003),
    make_emitter: Optional[Any] = None,     # callable -> SignalEmitter
    make_forecaster: Optional[Any] = None,  # callable -> Forecaster
    **kwargs: Any,
) -> pd.DataFrame:
    """Run the same backtest at multiple fee rates; return a DataFrame.

    SPEC §6 Phase 5: ``fee_rate ∈ {0, 0.00015, 0.0003}``. Useful to see
    how much of the strategy's edge survives reasonable execution costs.

    Pass ``make_emitter`` / ``make_forecaster`` (callables) when those
    objects carry per-run state (cycles, iterators, model caches) — a
    fresh instance is built for each fee rate.
    """
    # Tech: for each fee rate, clone the base kwargs, override fee_rate, and build
    #       fresh stateful objects from the factories before running; collect the
    #       headline numbers into one row per rate.
    # Why:  emitters/forecasters can hold per-run state (iterator cycles, model
    #       caches), so reusing one instance across rates would silently corrupt
    #       results — the factory pattern guarantees a clean object each iteration.
    rows = []
    for fr in fee_rates:
        kw = dict(kwargs)
        kw["fee_rate"] = fr
        if make_emitter is not None:
            kw["emitter"] = make_emitter()
        if make_forecaster is not None:
            kw["forecaster"] = make_forecaster()
        res = run_backtest(ticks, **kw)
        s = res.summary()
        rows.append({
            "fee_rate": fr,
            "n_fills": s["n_fills"],
            "realized_pnl_points": s["realized_pnl_points"],
            "total_fees_points": s["total_fees_points"],
            "net_pnl_points": s["net_pnl_points"],
            "net_pnl_ntd": s["net_pnl_ntd"],
        })
    return pd.DataFrame(rows)


def demo_with_logs_and_metrics(output_dir: "str | Path | None" = None) -> BacktestResult:
    """Phase 4 demo — dummy alternating + stop-loss; writes logs + metrics."""
    # Tech: lazy-import the logging/metrics helpers and pick a timestamped output
    #       dir when none is given.
    # Why:  imported here (not at module top) so the lightweight demos above don't
    #       drag in parquet/matplotlib; a timestamped dir keeps runs from clobbering
    #       each other.
    from .exits.stop_loss import FixedStopLoss
    from .logging_io import timestamped_run_dir, write_all
    from .metrics import compute_metrics

    if output_dir is None:
        output_dir = timestamped_run_dir("phase4_demo")

    # Tech: record every hyperparameter in a dict and drive the run from it.
    # Why:  the same dict is persisted as params.json, so the run is exactly
    #       reproducible from its own output — no guessing which knobs produced it.
    params = {
        "entry_point": "demo_with_logs_and_metrics",
        "n_synthetic_ticks": 2000,
        "tick_size": 1.0,
        "aggression_ticks": 3,
        "fee_rate": 0.00015,
        "contract_multiplier": 200.0,
        "forecast_stride_bars": 120,
        "warmup_bars": 0,
        "exit_rule": "FixedStopLoss(stop_loss_ticks=5, tick_size=1.0)",
    }
    ticks = _synthetic_ticks(n=params["n_synthetic_ticks"])
    res = run_backtest(
        ticks,
        tick_size=params["tick_size"],
        aggression_ticks=params["aggression_ticks"],
        fee_rate=params["fee_rate"],
        contract_multiplier=params["contract_multiplier"],
        forecast_stride_bars=params["forecast_stride_bars"],
        warmup_bars=params["warmup_bars"],
        exit_rule=FixedStopLoss(stop_loss_ticks=5, tick_size=1.0),
    )
    # Tech: dump the full artifact bundle (auto-generates charts) and print paths.
    # Why:  write_all is the single sink for fills/orders/forecasts/signals/trades
    #       plus params.json and the report, so Phase 4 is one call.
    paths = write_all(res, output_dir, params=params)
    print(f"Phase 4 — logs written to {output_dir}/")
    for name, p in paths.items():
        print(f"  {name:10s} -> {p}")

    # Tech: compute the metric pack and pretty-print each section, special-casing
    #       the trades block to show only a count.
    # Why:  the demo's purpose is to eyeball the numbers; the trades list is long
    #       and uninteresting in console output, so it's collapsed to its size.
    metrics = compute_metrics(res, ticks, forced_close=False)
    print("\nPhase 4 — metric pack (forced_close=False)")
    for section, body in metrics.items():
        if section == "trades":
            print(f"  {section}: {len(body['records'])} round-trips")
            continue
        print(f"  [{section}]")
        for k, v in body.items():
            if isinstance(v, float):
                print(f"    {k:38s} {v:+.6f}")
            else:
                print(f"    {k:38s} {v}")
    return res


def _print_summary(label: str, res: BacktestResult) -> None:
    # Tech: pull the summary dict and print the headline counters and PnL figures,
    #       including the DAY/NIGHT split, under a label.
    # Why:  a shared printer keeps the __main__ block's four demos formatted
    #       identically; .get() on the session keys tolerates single-session runs
    #       where NIGHT is absent.
    s = res.summary()
    print(label)
    print(f"  bars:               {s['n_bars']}")
    print(f"  forecasts:          {s.get('n_forecasts', 0)}")
    print(f"  signals:            {s['n_signals']}")
    print(f"  orders submitted:   {s['n_orders']}")
    print(f"    of which exits:   {s.get('n_exits', 0)}")
    print(f"    forced closes:    {s.get('n_forced_closes', 0)}")
    print(f"  fills:              {s['n_fills']}")
    print(f"  ending position:    {s['position']}")
    print(f"  realized PnL (pts): {s['realized_pnl_points']:+.2f}")
    print(f"  total fees   (pts): {s['total_fees_points']:.4f}")
    print(f"  net PnL      (pts): {s['net_pnl_points']:+.2f}")
    print(f"  net PnL      (NT$): {s['net_pnl_ntd']:+,.2f}")
    pnl_day = res.portfolio.realized_pnl_by_session.get("DAY", 0.0)
    pnl_night = res.portfolio.realized_pnl_by_session.get("NIGHT", 0.0)
    print(f"  DAY/NIGHT PnL pts:  {pnl_day:+.2f} / {pnl_night:+.2f}")


if __name__ == "__main__":
    # Tech: run all four demos in order, then the Phase 4 logging/metrics demo.
    # Why:  `python -m forecast_eval.run` is the one-command smoke test of Phases
    #       1–4 on synthetic data — no model, no real data, finishes in seconds.
    _print_summary("Phase 1 — dummy alternating signal", demo())
    print()
    _print_summary("Phase 2 — naive forecaster + threshold emitter", demo_naive())
    print()
    _print_summary("Phase 3 — alternating + fixed stop-loss (5 ticks)", demo_stop_loss())
    print()
    _print_summary("Phase 3 — alternating + forced session close", demo_forced_close())
    print()
    demo_with_logs_and_metrics()
