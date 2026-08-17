"""LiveRuntime — assembles a live session from one config, and holds it together.

Everything network-facing lives in `cli.py`; everything *decidable* lives here,
so the whole assembly is testable without credentials, a feed, or a GPU.

Two jobs, both of which were previously nobody's:

**1. One policy, injected everywhere.** `SessionPolicy` used to be handed
separately to the aggregator, the backfill and the rails, with nothing checking
they agreed. A mismatch would splice the model's context from two regimes and
never raise. The runtime now owns the single policy, builds the aggregator with
it, fetches warm-up with it, and **validates** that any bars handed to it satisfy
it. A mismatched pair is no longer expressible.

**2. Warm-up that leaves nothing cold.** Three distinct things must be warm
before the first live decision, and only the first is obvious:

  * the **forecaster's context** — `context_length` bars, backfilled, not waited
    for (SHIOAJI_PLAN §7.5);
  * the **model itself** — the first forecast costs ~18.8 s against ~0.09 s
    warm (§2.7), so a throwaway forecast is issued during start-up rather than
    on the first real signal;
  * the **emitter's rolling deques** — `ThresholdEmitter._trend_confirms` fails
    **open** (trades) while it holds fewer than 3 samples, so an un-primed
    restart runs with a risk filter silently disabled. Primed by replaying
    historical forecasts through `emit_signal` and discarding the results, which
    is correct for any emitter because the emitter's own code does the
    accumulating (§2.8).
"""
from __future__ import annotations

from typing import Any, List, Optional

import pandas as pd

from ..execution import Execution
from ..portfolio import Portfolio
from ..trader import Trader
from .backfill import fetch_bars
from .broker.base import Broker
from .broker.dryrun import DryRunBroker
from .broker.sim import SimBroker
from .config import LiveConfig
from .loop import LiveLoop
from .sessions import SessionPolicy
from .state import RailsConfig, SafetyRails


class LiveRuntime:
    def __init__(
        self,
        config: LiveConfig,
        *,
        broker: Optional[Broker] = None,
        recorder: Any = None,
        rails: Optional[SafetyRails] = None,
        rails_config: Optional[RailsConfig] = None,
        simulation: bool = True,
        emitter_prime_boundaries: int = 500,
        tick_capture: Any = None,
        alerter: Any = None,
    ):
        # Tech: build the whole stack from `config`, deriving the session policy
        #       from it rather than accepting one separately.
        # Why:  taking a policy argument would immediately reintroduce the
        #       mismatch this class exists to prevent. There is exactly one policy
        #       in a live session and it comes from the pinned config.
        self.config = config
        self.policy = config.policy
        self.recorder = recorder
        self.simulation = simulation
        self.emitter_prime_boundaries = emitter_prime_boundaries
        self.tick_capture = tick_capture
        self.alerter = alerter

        self.rails = rails or SafetyRails(
            rails_config or RailsConfig(),
            simulation=simulation,
            policy=self.policy,
            recorder=recorder,
        )
        # Tech: fail before the first bar if the trader is sized beyond what the
        #       accounting supports.
        # Why:  §2.10 — `Trader.on_fill` has no average-entry branch for a second
        #       partial fill, so anything above 1 is unsound rather than merely
        #       risky.
        self.rails.check_startup(max_position=config.max_position)

        self.execution = Execution(fee_rate=config.fee_rate)
        self.broker: Broker = broker or DryRunBroker(self.execution,
                                                     recorder=recorder)
        self.trader = Trader(
            execution=self.broker,
            tick_size=config.tick_size,
            aggression_ticks=config.aggression_ticks,
            max_position=config.max_position,
            max_aggression_ticks=config.max_aggression_ticks,
        )
        self.portfolio = Portfolio(contract_multiplier=config.contract_multiplier)
        self.emitter = config.build_emitter()
        # Tech: warmup_bars=0 because the context arrives via `seed_history`.
        # Why:  in the backtest `warmup_bars` is the train/eval split; live it
        #       would only delay trading behind bars we already hold.
        self.forecaster = config.build_forecaster(warmup_bars=0)

        # Tech: the aggregator is built with *this* policy, from `TickAggregator`
        #       rather than by the caller.
        # Why:  one construction site for the live bar stream, so the aggregator
        #       and the backfill cannot disagree about the session.
        from .bars import LIVE_FLUSH_GRACE, TickAggregator
        self.aggregator = TickAggregator(policy=self.policy,
                                         flush_grace=LIVE_FLUSH_GRACE)

        self.loop = LiveLoop(
            forecaster=self.forecaster,
            emitter=self.emitter,
            broker=self.broker,
            trader=self.trader,
            portfolio=self.portfolio,
            exit_rule=None,
            forced_close_on_session_end=config.forced_close_on_session_end,
            history_window=config.context_length,
            recorder=recorder,
            rails=self.rails,
            alerter=alerter,
        )
        self.warm = False

    # ---- resuming into an open position ------------------------------------

    def adopt_position(self, *, timestamp: Any) -> Optional[dict]:
        # Tech: read what the venue holds and seed the Trader's and Portfolio's
        #       mirrors from it. Returns a summary of what was adopted, or None
        #       when the account is flat or the broker cannot answer.
        # Why:  §2.20. Both mirrors start at zero, which is only true for a
        #       process that starts flat. The strategy holds a median 4.5 days, so
        #       *any* restart during a soak lands mid-position — and a zero mirror
        #       there is not merely stale: the reconciliation rail compares 0
        #       against the venue's N and halts on the first bar (§2.9), and the
        #       no-flip state machine would read "flat" and open a second lot.
        #       Adopting makes a restart continue the trade rather than refuse it.
        held = self.broker.broker_position_detail()
        if held is None or held.signed_qty == 0:
            return None

        # Tech: refuse rather than adopt when the size exceeds what the accounting
        #       supports, or when a bar-counting exit rule is configured.
        # Why:  two ways adoption could quietly produce a wrong number instead of
        #       an error. Above `max_position`, `Trader.on_fill` has no
        #       average-entry branch (§2.10) — the same reason `check_startup`
        #       raises. And the true entry *bar index* is unknowable here: it
        #       belongs to a process that is gone, so any rule measuring bars held
        #       would measure from a sentinel. Live runs configure no exit rule
        #       today; the day one is added, this must fail loudly rather than
        #       silently time a stop from the wrong origin.
        if abs(held.signed_qty) > self.config.max_position:
            raise ValueError(
                f"the account holds {held.signed_qty} lot(s), above "
                f"max_position={self.config.max_position}. Trader.on_fill cannot "
                f"average a second lot's entry (§2.10), so this cannot be adopted "
                f"— flatten with `python -m forecast_eval.live.flatten`, then "
                f"restart.")
        if self.loop.exit_rule is not None:
            raise ValueError(
                "cannot adopt a position while an ExitRule is configured: the "
                "entry bar index belongs to the previous process and is not "
                "recoverable, so any bars-held rule would measure from a "
                "sentinel.")
        if self.trader.position != 0:
            raise ValueError(
                f"adopt_position ran with the mirror already at "
                f"{self.trader.position}; adoption seeds a fresh mirror and must "
                f"happen once, before the first bar.")

        # Tech: seed both mirrors — the Trader's (which drives the no-flip machine
        #       and any exit rule) and the Portfolio's (which realizes the PnL).
        # Why:  the venue's average entry, not the current price, is what goes in.
        #       It means an inherited loss lands on the day the position closes,
        #       which can put a restart's first close straight through the daily
        #       loss limit — but that is the money that was actually made or lost,
        #       and it is what the backtest books too. The alternative, resetting
        #       entry to the market, would understate a losing resume and flatter
        #       every restart. `entry_bar_idx` gets the flat sentinel because this
        #       run has no bar for an entry that predates it; the guard above is
        #       what keeps that sentinel from being read as a real index.
        self.trader.position = held.signed_qty
        self.trader.entry_price = held.avg_price
        self.trader.entry_bar_idx = -1
        self.trader.entry_timestamp = timestamp
        self.portfolio.position = held.signed_qty
        self.portfolio.avg_entry_price = held.avg_price

        adopted = {
            "position": held.signed_qty,
            "entry_price": held.avg_price,
            "adopted_at": timestamp,
        }
        if self.recorder is not None:
            self.recorder.record("position_adopted", **adopted)
        return adopted

    # ---- warm-up -----------------------------------------------------------

    def fetch_warmup_bars(self, api: Any, contract: Any,
                          start: str, end: str) -> pd.DataFrame:
        # Tech: backfill through `fetch_bars`, supplying this runtime's own policy.
        # Why:  `fetch_bars` deliberately has no session default. Routing the call
        #       through here means the caller never gets to choose one, so the
        #       aggregator and the backfill agree by construction rather than by
        #       convention — the mismatch this class exists to prevent.
        return fetch_bars(api, contract, start, end, policy=self.policy)

    def warm_up(self, bars: pd.DataFrame) -> None:
        # Tech: validate the bars against the policy, prime the emitter by
        #       replaying historical forecasts, pre-warm the model, then seed the
        #       loop's context with the trailing `context_length` bars.
        # Why:  ordering matters — priming consumes the *earlier* bars so the
        #       emitter sees the same forecast sequence it would have seen had the
        #       process been running, and the context is seeded last so it ends at
        #       the newest bar.
        if bars.empty:
            raise ValueError("warm_up got no bars — backfill returned nothing "
                             "(over quota returns EMPTY history, not an error)")
        off = [ts for ts in bars["timestamp"] if not self.policy.includes(ts)]
        if off:
            raise ValueError(
                f"{len(off)} warm-up bar(s) fall outside {sorted(self.policy.sessions)} "
                f"(first: {off[0]}). The backfill used a different SessionPolicy "
                f"than this runtime — that mismatch would splice the model context "
                f"from two regimes."
            )

        ctx = self.config.context_length
        if len(bars) < ctx:
            raise ValueError(
                f"warm_up needs at least context_length={ctx} bars, got {len(bars)}. "
                f"A short context still produces plausible forecasts, from an input "
                f"distribution the strategy was never evaluated on."
            )

        self._prime_emitter(bars)
        self._check_primed(len(bars))
        self._prewarm_model(bars.iloc[-ctx:])
        self.loop.seed_history(bars.iloc[-ctx:].reset_index(drop=True))
        self.warm = True
        if self.recorder is not None:
            self.recorder.record(
                "warm_up", bars=len(bars), context=self.loop.history_len(),
                first=bars["timestamp"].iloc[0], last=bars["timestamp"].iloc[-1],
                emitter_primed=self._emitter_samples(),
            )

    def _prime_emitter(self, bars: pd.DataFrame) -> None:
        # Tech: forecast at the last N stride boundaries that still have a full
        #       context behind them, and push each through `emit_signal`,
        #       discarding the result.
        # Why:  replaying through the emitter (rather than poking its deques) is
        #       correct for *any* emitter, because the accumulation lives in the
        #       emitter's own code — `ThresholdEmitter` only appends on BUY/SELL
        #       while `VolMultiTfEmitter` appends on every forecast, and replay
        #       reproduces either faithfully. The results must be discarded: these
        #       are historical bars and acting on them would be trading the past.
        ctx = self.config.context_length
        stride = self.config.forecast_stride_bars
        n = len(bars)
        boundaries = sorted(
            b for b in range(n - 1, ctx - 2, -stride)
            if b >= ctx - 1
        )[-self.emitter_prime_boundaries:]
        if not boundaries:
            return
        timestamps = list(bars["timestamp"])
        prices = [float(p) for p in bars["price"]]
        forecasts = self.forecaster.forecast_series_batch(
            timestamps, prices, boundaries
        )
        for f in forecasts:
            self.emitter.emit_signal(f)

    def _priming_required(self) -> bool:
        # Tech: priming only matters for an emitter that (a) keeps a rolling
        #       sample window and (b) has the filter that reads it enabled.
        # Why:  `VolMultiTfEmitter` appends on every forecast so it primes
        #       trivially; a `ThresholdEmitter` with `trend_filter=False` has no
        #       filter to leave open. Checking the condition rather than the class
        #       keeps this from becoming an emitter registry.
        return (
            getattr(self.emitter, "_short_prices", None) is not None
            and bool(getattr(self.emitter, "trend_filter", False))
        )

    def _check_primed(self, n_bars: int) -> None:
        # Tech: refuse to start when replay failed to accumulate enough samples
        #       for the trend filter to actually evaluate.
        # Why:  MEASURED from the pinned backtest's 40 443 signals — only 3.74 %
        #       are non-HOLD, and `_trend_confirms` appends on those alone. So a
        #       quiet stretch primes nothing: the longest observed run of
        #       consecutive HOLDs is **385 boundaries** (~11 550 bars, ~14 night
        #       sessions), which is why the default window is 500 rather than the
        #       ~80 the average rate would suggest.
        #
        #       Starting anyway is the worse option: below 3 samples
        #       `_trend_confirms` returns True — it fails **open**, trading signals
        #       the filter exists to veto. Refusing is recoverable (widen the
        #       window); trading unfiltered is not.
        if not self._priming_required():
            return
        need = 3  # `_trend_confirms` needs an early/late split to compare
        have = self._emitter_samples()
        if have < need:
            raise RuntimeError(
                f"emitter priming incomplete: {have}/{need} samples from "
                f"{n_bars} bars over {self.emitter_prime_boundaries} boundaries. "
                f"The trend filter fails OPEN below {need} samples, so starting "
                f"now would trade signals it exists to veto. Widen the backfill "
                f"window (the longest observed HOLD run is 385 boundaries) or "
                f"raise emitter_prime_boundaries."
            )

    def _prewarm_model(self, window: pd.DataFrame) -> None:
        # Tech: issue one throwaway forecast so the checkpoint is resident.
        # Why:  measured at ~18.8 s cold versus ~0.09 s warm (§2.7). Without this
        #       the *first real signal* pays the load. Skipped when the emitter
        #       priming already forced a load.
        if self.forecaster._model is None:
            self.forecaster.forecast(window.reset_index(drop=True).copy())

    def _emitter_samples(self) -> int:
        deque_ = getattr(self.emitter, "_short_prices", None)
        return len(deque_) if deque_ is not None else -1

    # ---- the live event paths ----------------------------------------------

    def on_tick(self, timestamp: Any, price: float, volume: int = 1, *,
                simtrade: bool = False, code: str = "") -> None:
        # Tech: capture the raw print first, then feed the aggregator and run the
        #       loop for any completed bar; note genuine data for the watchdog.
        # Why:  capture comes **before** every filter on purpose — simtrade and
        #       out-of-session ticks are written too, because a file containing
        #       only what survived the filter cannot be used to audit the filter
        #       (V1's live half). Refuses to run before `warm_up`: trading on a
        #       partial context is what the whole start-up sequence prevents.
        if not self.warm:
            raise RuntimeError("on_tick before warm_up — context is not loaded")
        if self.tick_capture is not None:
            self.tick_capture.write(timestamp, price, volume,
                                    simtrade=simtrade, code=code)
        if not (simtrade and self.aggregator.drop_simtrade):
            self.rails.note_data(timestamp)
        for bar in self.aggregator.add_tick(timestamp, price, volume,
                                            simtrade=simtrade):
            self.loop.on_bar(bar)

    def on_clock(self, now: Any) -> None:
        # Tech: wall-clock driven work — close a quiet bar, then run the staleness
        #       watchdog.
        # Why:  anything that must happen when bars *stop* cannot be driven by
        #       bars. A quiet minute would otherwise hold a completed bar hostage
        #       until the next trade, and a frozen feed produces no event at all.
        for bar in self.aggregator.flush(now):
            self.loop.on_bar(bar)
        self.rails.check_staleness(now)

    def shutdown(self, now: Any = None) -> dict:
        # Tech: close the session out and report what state it ended in, flagging
        #       a position left open after a FLATTEN halt as a distinct failure.
        # Why:  "submitted a close" is not "flat" (§2.12). An order is `LMT`+`ROD`,
        #       and ROD orders are cancelled at session end — so a flatten fired
        #       near the close can silently fail, leaving the position on with the
        #       loop blind until the next session. Whatever else happens, that
        #       must never look like success: it is reported here, recorded, and
        #       surfaced as a non-zero exit by the CLI.
        pos = self.trader.position
        unflattened = bool(pos != 0 and self.rails.flatten_required)
        status = {
            "position_at_close": pos,
            "halted": self.rails.halted,
            "halt_reason": self.rails.halt_reason,
            "halt_action": getattr(self.rails.halt_action, "value", None),
            "flatten_incomplete": unflattened,
            "pending_order": self.broker.pending_order() is not None,
        }
        if self.recorder is not None:
            # Merged, not double-splatted: both dicts carry `halted`/`halt_reason`,
            # and status wins.
            self.recorder.record("session_end", **{**self.summary(), **status})
        return status

    def result(self):
        return self.loop.result()

    def summary(self) -> dict:
        r = self.loop.result()
        return {
            "config": self.config.describe(),
            "simulation": self.simulation,
            "bars": r.n_bars,
            "forecasts": r.n_forecasts,
            "signals": r.n_signals,
            "orders": r.n_orders,
            "fills": len(r.fills),
            "position": self.trader.position,
            "net_pnl_points": self.portfolio.net_pnl(),
            "halted": self.rails.halted,
            "halt_reason": self.rails.halt_reason,
            "ticks": self.aggregator.n_ticks,
            "simtrade_dropped": self.aggregator.n_simtrade_dropped,
            "late_dropped": self.aggregator.n_late_dropped,
            "off_session_dropped": self.aggregator.n_bars_dropped_off_session,
            "ticks_captured": getattr(self.tick_capture, "n", 0),
        }


def make_sim_runtime(config: LiveConfig, **kw) -> LiveRuntime:
    """A runtime backed by the offline fill model — for replay and parity work."""
    execution = Execution(fee_rate=config.fee_rate)
    return LiveRuntime(config, broker=SimBroker(execution), **kw)
