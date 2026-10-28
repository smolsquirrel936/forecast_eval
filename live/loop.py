"""LiveLoop — the SPEC §4.1 per-tick body, restructured to be pushed.

`run.run_backtest` owns a `for market in env.stream()` loop: it *pulls* events.
A live feed pushes them, one callback at a time, and the process must survive
across days. `LiveLoop` holds exactly the state that loop kept in local
variables and exposes the body as `on_bar(market)`.

The four steps, their order, and the fresh-DataFrame look-ahead defense are
transcribed unchanged — that equivalence is what `tests/test_live_parity.py`
asserts against `run_backtest` over real ticks (SHIOAJI_PLAN.md phase V0).

Deliberately *not* carried over from `run_backtest`:

  * the forecast precompute/cache path — it batches over future bars, which do
    not exist live;
  * the model-less dummy-emitter path — a live run always has a forecaster.

Threading: this class is not thread-safe and must be driven from a single
thread. Broker callbacks should enqueue, never call `on_bar` directly — a
forecast is seconds of GPU work and would stall the venue's quote handler.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pandas as pd

from ..events import FillEvent, Forecast, MarketEvent, OrderEvent
from ..exits.base import ExitRule
from ..forecaster.base import Forecaster
from ..portfolio import Portfolio
from ..run import BacktestResult
from ..strategy.base import SignalEmitter
from ..trader import Trader
from .broker.base import Broker
from .recorder import Recorder
from .state import SafetyRails


class LiveLoop:
    def __init__(
        self,
        *,
        forecaster: Forecaster,
        emitter: SignalEmitter,
        broker: Broker,
        trader: Trader,
        portfolio: Portfolio,
        exit_rule: Optional[ExitRule] = None,
        forced_close_on_session_end: bool = False,
        history_window: Optional[int] = None,
        recorder: Optional[Recorder] = None,
        rails: Optional[SafetyRails] = None,
        alerter: Any = None,
        expected_position: Any = None,   # callable() -> Optional[int]
    ):
        # Tech: hold the injected collaborators and read the forecast schedule
        #       (warm-up + stride) off the forecaster.
        # Why:  `run_backtest` constructs these from scalars; live they must be
        #       injected, because a restart has to rebuild a Trader/Portfolio from
        #       reconciled broker state rather than from defaults. Reading
        #       stride/warmup from the forecaster keeps the SPEC §3 contract that
        #       a model declares its own cadence.
        self.forecaster = forecaster
        self.emitter = emitter
        self.broker = broker
        self.trader = trader
        self.portfolio = portfolio
        self.exit_rule = exit_rule
        self.forced_close_on_session_end = forced_close_on_session_end
        self.history_window = history_window
        # Tech: all three optional and all three inert when None.
        # Why:  the V0 parity harness constructs the loop without any of them, so
        #       the "identical to run_backtest" guarantee is asserted on a loop
        #       that carries no observer at all — and then again with a recorder
        #       attached, proving observation cannot perturb decisions. The
        #       alerter is the same kind of passenger: it reads state after a fill
        #       and never influences one.
        self.recorder = recorder
        self.rails = rails
        self.alerter = alerter
        # Tech: an optional callable returning what WE believe the whole account
        #       holds; `None` keeps `self.trader.position` as the reconciliation
        #       operand, exactly as before.
        # Why:  STRATEGY_SPLIT_PLAN §16, and it is the fourth optional passenger
        #       for the same reason the other three are optional — the V0 parity
        #       harness builds this loop with none of them, so "identical to
        #       `run_backtest`" is still asserted on a loop that carries no
        #       observer. A single-deployment run has nothing to sum and is
        #       unaffected either way; this only matters once two deployments
        #       share a venue book (§14).
        self.expected_position = expected_position
        # Tech: an optional writer with `update(position_state_or_None, market)`,
        #       called after every bar's exit check; None by default and set by
        #       the entry point (`live/exit_state.py` `ExitStateFile`).
        # Why:  a fifth passenger, optional for the same parity reason as the
        #       four above. It persists a restorable exit rule's per-trade state
        #       so a restart can resume the lot instead of refusing it, and it
        #       only reads the rule after the rule has decided.
        self.exit_state_sink: Any = None
        self._stride = forecaster.forecast_stride_bars
        self._warmup = forecaster.warmup_bars

        # Tech: rolling history buffers (parallel lists of ts/price/volume).
        # Why:  same structure `run_backtest` keeps — a fresh DataFrame slice is
        #       rebuilt from them at each forecast boundary, which is the
        #       structural look-ahead defense. Plain lists are far cheaper to
        #       append to than a DataFrame. `history_window` trims them (see
        #       `_trim_history`) so a multi-day live process does not grow without
        #       bound; offline this only ever removes rows the forecaster's
        #       context would not have reached anyway.
        self._history_ts: List[Any] = []
        self._history_px: List[float] = []
        self._history_vol: List[int] = []

        # Tech: per-run counters and the full event logs.
        # Why:  the loop only records; metrics and reporting run afterwards off
        #       these lists, exactly as in `run_backtest`, so the same analysis
        #       and the same `BacktestResult` consumers work on a live session.
        self._n_bars = 0
        self._n_signals = 0
        self._n_orders = 0
        self._n_forecasts = 0
        self._n_exits = 0
        self._n_forced_closes = 0
        self.fills: List[FillEvent] = []
        self.orders: List[OrderEvent] = []
        self.forecasts: List[Forecast] = []
        self.signals: List[Dict[str, Any]] = []
        self._last_price: Optional[float] = None

        # Tech: previous tick's session, plus a latch naming the session an
        #       in-flight forced-close fill should be booked to.
        # Why:  a session change is detected by comparing consecutive ticks; the
        #       boundary close fills on the *next* session's first tick but its
        #       PnL belongs to the session it was earned in (SPEC §4.7).
        self._prev_session: Optional[str] = None
        self._force_close_pending_session: Optional[str] = None

    # ---- warm-up -----------------------------------------------------------

    def seed_history(self, bars: pd.DataFrame) -> None:
        # Tech: load backfilled bars straight into the history buffers without
        #       running the per-bar body, then trim to `history_window`.
        # Why:  warm-up bars are context, not events. Pushing them through
        #       `on_bar` would emit signals and submit orders for history that has
        #       already happened — the exact look-ahead-shaped mistake the loop is
        #       built to prevent. Seeding leaves the counters at zero, so the first
        #       *live* bar is bar_idx 0 and (with `warmup_bars=0`) forecasts start
        #       immediately against a full context.
        if list(bars.columns[:3]) != ["timestamp", "price", "volume"]:
            raise ValueError(
                f"seed_history expects timestamp/price/volume columns, "
                f"got {list(bars.columns)}"
            )
        if self._n_bars:
            raise RuntimeError("seed_history must be called before any live bar")
        self._history_ts = list(bars["timestamp"])
        self._history_px = [float(p) for p in bars["price"]]
        self._history_vol = [int(v) for v in bars["volume"]]
        self._trim_history()

    def history_len(self) -> int:
        # Tech: how many bars of context are currently buffered.
        # Why:  the runtime asserts this reaches the model's context length before
        #       trading — a short context silently produces plausible forecasts
        #       from an input distribution the strategy was never evaluated on.
        return len(self._history_px)

    # ---- the per-tick body -------------------------------------------------

    def on_bar(self, market: MarketEvent) -> None:
        # Tech: the current bar's index is the count of bars already processed.
        # Why:  reproduces `enumerate(stream)` in `run_backtest` — 0-based and
        #       counting only bars that reached the loop. ExitRules measure
        #       holding period in these units, so the origin must match.
        bar_idx = self._n_bars

        # Tech: remember the latest price for end-of-session unrealized marking.
        # Why:  the loop can stop with a position open; this is the only mark
        #       available to value it.
        self._last_price = market.price

        # Step 1 — fills.
        # Tech: drain every fill the broker now knows about; for each, decide
        #       which session to book PnL to, then apply it to Portfolio and
        #       Trader and log it.
        # Why:  fills run first each tick (§4.1) so a just-closed position is flat
        #       before new decisions. The `will_be_flat` test is evaluated per
        #       fill against the running position, so a venue that reports a close
        #       in several partials still releases the forced-close latch on the
        #       leg that actually reaches flat, not on the first one.
        for fill in self.broker.poll_fills(market):
            signed = 1 if fill.side == "BUY" else -1
            will_be_flat = (self.trader.position + signed * fill.quantity) == 0
            session_for_pnl = market.session
            if self._force_close_pending_session is not None and will_be_flat:
                session_for_pnl = self._force_close_pending_session
                self._force_close_pending_session = None
            # Tech: remember the position and realized PnL from *before* the fill
            #       is applied.
            # Why:  both are needed to describe the trade and neither survives the
            #       apply. `was_flat` is what distinguishes an entry from an exit,
            #       and the realized-PnL delta is this round trip's result — the
            #       Portfolio only exposes the running total, so an alert reading
            #       it afterwards would report the sum of every trade tonight.
            was_flat = self.trader.position == 0
            pnl_before = self.portfolio.realized_pnl
            self.portfolio.apply_fill(fill, session=session_for_pnl)
            self.trader.on_fill(
                fill.side,
                fill.quantity,
                fill_price=fill.fill_price,
                bar_idx=bar_idx,
                timestamp=fill.timestamp,
            )
            self.fills.append(fill)
            if self.recorder is not None:
                self.recorder.record(
                    "fill", bar_idx=bar_idx, timestamp=fill.timestamp,
                    side=fill.side, fill_price=fill.fill_price,
                    quantity=fill.quantity, fee=fill.fee,
                    session_for_pnl=session_for_pnl,
                    position_after=self.trader.position,
                )
            # Tech: one TRADE alert per fill, phrased as an entry or an exit, with
            #       the round trip's own PnL on the exit.
            # Why:  a fill is the only thing in a soak that is both rare and
            #       *interesting* — at 0.37 orders/night this is roughly one
            #       message every three days, so it cannot cause alert fatigue and
            #       it is the one event worth reading on a phone. TRADE, not WARN:
            #       nothing has gone wrong, this is the system doing its job; and
            #       not INFO, so a chat set to drop the routine re-logins still
            #       hears it (the `/settings` "Trades + WARN + FATAL" button).
            #       Alerting on the fill rather than the order is deliberate — an
            #       order can be cancelled unfilled at the next boundary (§4.4),
            #       and a notification for a trade that never happened is worse
            #       than none.
            if self.alerter is not None:
                pos = self.trader.position
                if was_flat:
                    self.alerter.trade(
                        "position_opened",
                        f"{'LONG' if pos > 0 else 'SHORT'} {abs(pos)} @ "
                        f"{fill.fill_price:.0f}",
                        side=fill.side, quantity=fill.quantity,
                        price=fill.fill_price, bar_idx=bar_idx)
                else:
                    trade_pnl = self.portfolio.realized_pnl - pnl_before
                    self.alerter.trade(
                        "position_closed",
                        f"closed @ {fill.fill_price:.0f} — "
                        f"{trade_pnl:+.0f} points",
                        side=fill.side, quantity=fill.quantity,
                        price=fill.fill_price, points=round(trade_pnl, 2),
                        position_after=pos, bar_idx=bar_idx)

        # Tech: flag — but do not act on — a bar that does not advance the
        #       history's clock, i.e. one stamped at or before the newest bar
        #       already buffered.
        # Why:  §2.14. The backfill and the live stream meet at a seam, and a
        #       source that hands over an in-progress minute makes both sides
        #       claim the same timestamp — the context then holds one minute
        #       twice, each copy a different fragment of it. `fetch_bars` removes
        #       that cause; this is the detector for any other route to the same
        #       state, and it must not be a `raise`: a data quirk should surface
        #       in the audit trail, not kill a process holding a live position.
        #       Recording rather than dropping keeps `bar_idx` — and therefore
        #       every stride boundary and holding-period count — untouched.
        if (self.recorder is not None and self._history_ts
                and market.timestamp <= self._history_ts[-1]):
            self.recorder.record(
                "bar_anomaly", reason="non_monotonic_bar", bar_idx=bar_idx,
                timestamp=market.timestamp, previous=self._history_ts[-1],
            )

        # Tech: append this bar to the history buffers *after* fills resolve, then
        #       trim to the configured window.
        # Why:  history through bar_idx must represent everything observed up to
        #       and including time t; recording here (post-fill, pre-forecast) is
        #       what makes the forecaster's slice causally honest.
        self._history_ts.append(market.timestamp)
        self._history_px.append(market.price)
        self._history_vol.append(market.volume)
        self._trim_history()

        if self.recorder is not None:
            self.recorder.record(
                "bar", bar_idx=bar_idx, timestamp=market.timestamp,
                price=market.price, volume=market.volume,
                session=market.session, position=self.trader.position,
            )

        # Safety rails — reconcile against the venue, then run the per-bar rails.
        # Tech: reconcile the position mirror first (a no-op when the broker
        #       cannot answer), then hand the rails this bar and the current net
        #       PnL; if anything has latched a halt, stop making decisions.
        # Why:  reconciliation runs *after* fills so it compares the mirror the
        #       fills just updated, and *before* any new decision so a desync can
        #       never produce another order (§2.9). Steps 2–4 are skipped wholesale
        #       when halted: a halted loop makes no new decisions, it only unwinds.
        if self.rails is not None:
            # Tech: the mirror is what WE believe the ACCOUNT holds — the sum of
            #       every deployment's fill ledger when `expected_position` is
            #       supplied, this deployment's own position otherwise. A
            #       supplier that returns None (no ledger, or one unreadable)
            #       falls back to the same own-position value.
            # Why:  §16. MEASURED (§14): a deployment hears only its own fills,
            #       but `broker_position()` returns the COMBINED book. Comparing
            #       one deployment's mirror against the whole account's size is
            #       not a desync check, it is a category error — it halts both
            #       deployments the first time they hold at once, with nothing
            #       actually wrong. Summing our own ledgers makes both sides of
            #       the comparison mean "the account", and a disagreement mean
            #       what it was always supposed to: a fill we missed or counted
            #       twice.
            mirror = self.trader.position
            if self.expected_position is not None:
                declared = self.expected_position()
                if declared is not None:
                    mirror = declared
            self.rails.on_reconcile(
                mirror=mirror,
                venue=self.broker.broker_position(),
            )
            self.rails.on_bar(market, net_pnl=self.portfolio.net_pnl())
            if self.rails.halted:
                self._on_halted(market, bar_idx)
                self._prev_session = market.session
                self._n_bars += 1
                return

        # Step 2 — exit rule.
        # Tech: if a rule is configured and a position is open, query it; on True
        #       submit a closing limit and bump the exit/order counters.
        # Why:  risk exits are checked every bar, not just on forecast boundaries,
        #       so a stop can fire intra-stride; `position_state()` is None when
        #       flat, which short-circuits the query.
        if self.exit_rule is not None:
            pos = self.trader.position_state()
            if pos is not None and self.exit_rule.should_exit(
                pos, market, bar_idx=bar_idx
            ):
                order = self.trader.submit_exit(market)
                if order is not None:
                    self.orders.append(order)
                    self._n_orders += 1
                    self._n_exits += 1
                    self._note_order(order, "exit", bar_idx)
            # Tech: hand the writer this bar's position (None when flat) once
            #       the rule has seen the bar.
            # Why:  after `should_exit`, so the saved state includes this bar's
            #       price — exactly what the rule would carry into the next one.
            #       Skipped with the rest of Step 2 on a halted bar, when the
            #       rule is not queried and its state does not move either.
            if self.exit_state_sink is not None:
                self.exit_state_sink.update(pos, market)

        # Step 3 — forecast boundary.
        # Tech: fire only once warm-up is complete and we are exactly on a stride
        #       boundary counted from the end of warm-up.
        # Why:  warm-up gives the model enough context before it trades (§4.8);
        #       the modulo against (bar_idx - warmup) puts the first forecast on
        #       the warm-up boundary itself, then every `stride` bars after.
        if bar_idx >= self._warmup and (bar_idx - self._warmup) % self._stride == 0:
            self._on_forecast_boundary(market, bar_idx)

        # Step 4 — session-boundary forced close.
        # Tech: when enabled and the session just flipped while holding a
        #       position, submit a closing limit and arm the latch with the prior
        #       session.
        # Why:  forced close flattens risk over the gap (§4.7); the latch is what
        #       later books the resulting PnL to the previous session even though
        #       the fill prints in the new one.
        if (
            self.forced_close_on_session_end
            and self._prev_session is not None
            and market.session != self._prev_session
            and self.trader.position != 0
        ):
            order = self.trader.submit_exit(market)
            if order is not None:
                self.orders.append(order)
                self._n_orders += 1
                self._n_forced_closes += 1
                self._force_close_pending_session = self._prev_session
                self._note_order(order, "forced_close", bar_idx)

        # Tech: advance the session tracker and the bar counter.
        # Why:  `_prev_session` must update every bar so the next boundary compare
        #       is correct; `_n_bars` is the authoritative processed-bar count and
        #       the source of the next `bar_idx`.
        self._prev_session = market.session
        self._n_bars += 1

    # ---- step 3 internals --------------------------------------------------

    def _on_forecast_boundary(self, market: MarketEvent, bar_idx: int) -> None:
        # Tech: rebuild a fresh DataFrame from the history-through-t buffers and
        #       run the model on it.
        # Why:  passing only rows <= t is the structural look-ahead defense (§6).
        #       A fresh frame each time also denies the forecaster any object it
        #       could have retained from a previous, longer call.
        hist_df = pd.DataFrame({
            "timestamp": list(self._history_ts),
            "price": list(self._history_px),
            "volume": list(self._history_vol),
        })
        forecast = self.forecaster.forecast(hist_df)

        # Tech: reject a forecast stamped ahead of the current bar.
        # Why:  belt-and-suspenders against a model that reaches outside its
        #       input; a future timestamp is a direct symptom of a leak (§6). Live
        #       it doubles as a clock sanity check between the venue's bar
        #       timestamps and whatever the forecaster stamps.
        if forecast.timestamp > market.timestamp:
            raise RuntimeError(
                f"Look-ahead leak: forecast.timestamp {forecast.timestamp} > "
                f"current bar {market.timestamp}"
            )
        self.forecasts.append(forecast)
        self._n_forecasts += 1
        if self.recorder is not None:
            self.recorder.record(
                "forecast", bar_idx=bar_idx, timestamp=forecast.timestamp,
                horizon_bars=forecast.horizon_bars, payload=forecast.payload,
                context_rows=len(hist_df),
            )

        # Tech: convert the forecast to a Signal (direction + conviction), log it
        #       with the emit-time price and session, then let the Trader act.
        # Why:  the signal record is what frictionless attribution replays later
        #       (SPEC §7); strength carries confidence into the Trader for
        #       aggression and sizing. Recording before acting means a signal is
        #       in the audit trail even if order submission then raises.
        sig = self.emitter.emit_signal(forecast)
        self._n_signals += 1
        self.signals.append({
            "timestamp": market.timestamp,
            "direction": sig.direction,
            "price": market.price,
            "session": market.session,
            "strength": sig.strength,
        })
        if self.recorder is not None:
            self.recorder.record(
                "signal", bar_idx=bar_idx, timestamp=market.timestamp,
                direction=sig.direction, strength=sig.strength,
                price=market.price, session=market.session,
            )
        order = self.trader.on_signal(sig.direction, market, strength=sig.strength)
        if order is not None:
            self._n_orders += 1
            self.orders.append(order)
            self._note_order(order, "signal", bar_idx)

    # ---- observation + halt handling ---------------------------------------

    def _note_order(self, order: OrderEvent, reason: str, bar_idx: int) -> None:
        # Tech: record an order and tell the rails one was submitted.
        # Why:  single funnel for both observers, so a future order path cannot
        #       be added that escapes the rate limit or the audit trail. `reason`
        #       is what lets the V3 shadow-parity diff line each order up with the
        #       decision that produced it (signal / exit / forced close / halt).
        if self.recorder is not None:
            self.recorder.record(
                "order", bar_idx=bar_idx, timestamp=order.timestamp,
                side=order.side, limit_price=order.limit_price,
                intent=order.intent, quantity=order.quantity, reason=reason,
            )
        if self.rails is not None:
            self.rails.on_order_submitted(order.timestamp)

    def _on_halted(self, market: MarketEvent, bar_idx: int) -> None:
        # Tech: while halted, submit a closing order only when the latched halt
        #       calls for flattening, a position is actually open, and nothing is
        #       already working.
        # Why:  a STOP halt sends nothing at all — the whole point is that the
        #       inputs (feed, or the position mirror) are untrustworthy. For a
        #       FLATTEN halt, gating on `has_pending()` makes the retry
        #       self-healing without becoming a storm: at most one order per bar,
        #       and none at all while a close is still working. A one-shot submit
        #       would silently leave risk on if that single order never filled.
        assert self.rails is not None
        if not self.rails.flatten_required:
            return
        if self.trader.position == 0 or self.broker.has_pending():
            return
        # Tech: issue the closing order inside the broker's `urgent` scope.
        # Why:  §5 — a FLATTEN halt exists to get risk off, so this order must
        #       prioritise filling over price. A live broker prices it through the
        #       touch and sends it IOC; offline brokers ignore the scope entirely,
        #       which is what keeps the V0 parity comparison unchanged.
        with self.broker.urgent():
            order = self.trader.submit_exit(market)
        if order is not None:
            self.orders.append(order)
            self._n_orders += 1
            self._note_order(order, "halt_flatten", bar_idx)

    # ---- history retention -------------------------------------------------

    def _trim_history(self) -> None:
        # Tech: when `history_window` is set, drop the oldest rows beyond it.
        # Why:  a live process runs for days and would otherwise grow one row per
        #       bar forever. This is only safe because the forecaster consumes a
        #       fixed trailing context — set `history_window >= context_length`
        #       (and larger still if a `bar_freq` aggregation is enabled, since
        #       raw rows then outnumber bars) or the model silently gets a shorter
        #       input than it would have offline.
        if self.history_window is None:
            return
        excess = len(self._history_px) - self.history_window
        if excess > 0:
            del self._history_ts[:excess]
            del self._history_px[:excess]
            del self._history_vol[:excess]

    # ---- results -----------------------------------------------------------

    def result(self) -> BacktestResult:
        # Tech: pack the counters and event logs into the same result object
        #       `run_backtest` returns.
        # Why:  every metric, report and log writer already consumes
        #       `BacktestResult`; reusing it means a live session is analysable
        #       with the exact same tooling — and it is what lets the V0 parity
        #       test compare the two paths field by field.
        return BacktestResult(
            portfolio=self.portfolio,
            n_bars=self._n_bars,
            n_signals=self._n_signals,
            n_orders=self._n_orders,
            n_forecasts=self._n_forecasts,
            n_exits=self._n_exits,
            n_forced_closes=self._n_forced_closes,
            fills=self.fills,
            orders=self.orders,
            forecasts=self.forecasts,
            signals=self.signals,
            last_price=self._last_price,
        )
