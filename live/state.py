"""SafetyRails — the checks that stop a live loop before it does damage.

Every rail here defaults to *stopping*, never to *continuing*. The failure mode
this file exists to prevent is a process that keeps trading on a stale mirror or
a frozen feed, which is how a capped-at-one-contract strategy turns into an
unbounded position.

**Two halt actions, deliberately distinguished** (SHIOAJI_PLAN.md §5). Flatten
is not universally the safe choice:

  * ``FLATTEN`` — close the open position, then stop. Correct when the position
    itself is the problem and prices are still trustworthy: daily loss limit,
    operator kill file.
  * ``STOP`` — send no orders at all. Correct whenever the *inputs* are
    untrustworthy: a frozen quote feed (a limit priced off a stale print is a
    blind order), a position mirror that disagrees with the venue (§2.9 — we do
    not know what we hold, so we cannot know what closing means), a rejected
    order, or an order-rate breach that suggests a submit storm.

Auto-correcting a mirror desync is explicitly *not* a rail: it would paper over
a missed or double-counted fill. Halt, and let a human look.

All time enters through explicit ``now`` arguments — the rails never read a
clock themselves, so every branch is deterministically testable.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Deque, Optional

from .sessions import SessionPolicy


class HaltAction(str, Enum):
    # Tech: the two terminal states a rail can put the loop into.
    # Why:  see the module docstring — collapsing these into one action would
    #       either flatten into an unpriceable book or leave a losing position
    #       open past its limit.
    STOP = "stop"
    FLATTEN = "flatten"


@dataclass
class RailsConfig:
    # Tech: the tunable limits; `None` disables an individual rail.
    # Why:  defaults are the conservative live-v1 posture. max_position is 1
    #       because `Trader.on_fill` has no average-entry branch for a second
    #       partial fill (§2.10), so anything larger is unsound today rather than
    #       merely risky. Disabling a rail must be an explicit act, hence None
    #       rather than 0/-1 sentinels.
    max_position: int = 1
    max_daily_loss_points: Optional[float] = None
    max_orders_per_minute: Optional[int] = 10
    quote_staleness_seconds: Optional[float] = 120.0
    kill_file: Optional[Path] = None


class SafetyRails:
    def __init__(self, config: RailsConfig, *, simulation: bool,
                 policy: Optional[SessionPolicy] = None,
                 recorder: Any = None, alerter: Any = None):
        # Tech: hold the config, the simulation flag, an optional Recorder and an
        #       optional Alerter; start un-halted with an empty order-rate window.
        # Why:  `simulation` is required (not defaulted) so no caller can create
        #       rails without stating whether orders are real — the banner and
        #       every halt record carry it. The recorder is optional so the rails
        #       stay unit-testable with no filesystem; the alerter likewise, and
        #       defaulting it to None keeps every offline test and V0 parity run
        #       byte-identical.
        self.config = config
        self.simulation = simulation
        self.recorder = recorder
        self.alerter = alerter
        # Tech: the deployment's session policy; defaults to "both sessions".
        # Why:  the permissive default keeps a policy-less caller behaving as
        #       before, but every session-dependent rail below reads it rather
        #       than naming a session — which is what lets this same code run the
        #       night strategy here and a day-session port elsewhere with one
        #       argument changed.
        self.policy = policy or SessionPolicy.both()

        self.halted = False
        self.halt_reason: Optional[str] = None
        self.halt_action: Optional[HaltAction] = None

        # Tech: sliding window of recent order-submission timestamps.
        # Why:  a rate limit needs the times themselves, not a counter, so the
        #       window can expire; a plain counter would never reset.
        self._order_times: Deque[Any] = deque()

        # Tech: trading-day anchor for the loss limit, plus last-data wall time.
        # Why:  "daily" loss is measured from the net PnL at the start of the
        #       current trading day, where the day is `policy.trading_day` (see
        #       `on_bar`).
        self._day_anchor_pnl: Optional[float] = None
        self._anchor_day: Optional[str] = None
        self._last_data_wall: Optional[Any] = None

    # ---- startup -----------------------------------------------------------

    def check_startup(self, *, max_position: int) -> None:
        # Tech: refuse to start when the trader is configured above the rails'
        #       max_position; raises rather than halting.
        # Why:  this is a misconfiguration, not a runtime event — failing loudly
        #       before the first bar is strictly better than halting after one.
        #       §2.10: sizes above 1 need average-entry accounting that does not
        #       exist yet, so this is a correctness guard, not a risk preference.
        if max_position > self.config.max_position:
            raise ValueError(
                f"max_position={max_position} exceeds the rails' limit of "
                f"{self.config.max_position}. Trader.on_fill has no average-entry "
                f"branch for a second partial fill (SHIOAJI_PLAN.md §2.10) — "
                f"implement it before raising this."
            )

    def banner(self) -> str:
        # Tech: one-line human summary of the mode and the active limits.
        # Why:  printed at startup so a production run can never be mistaken for
        #       a simulation one at a glance (§5).
        mode = "SIMULATION" if self.simulation else "*** PRODUCTION — REAL ORDERS ***"
        c = self.config
        return (
            f"[{mode}] max_position={c.max_position} "
            f"daily_loss={c.max_daily_loss_points} "
            f"orders/min={c.max_orders_per_minute} "
            f"staleness={c.quote_staleness_seconds}s "
            f"kill_file={c.kill_file}"
        )

    # ---- per-bar checks ----------------------------------------------------

    def on_bar(self, market: Any, *, net_pnl: float) -> None:
        # Tech: re-anchor the daily-loss baseline whenever the bar's trading day
        #       changes, then run the kill-file and daily-loss rails.
        # Why:  DEFECT FIXED (§2.12). This previously re-anchored on *entry into
        #       the DAY session*, which never happens on a night-only feed — the
        #       anchor was set once on the first bar and the "daily" loss limit
        #       silently became a since-inception limit, tightening every day
        #       until it halted the loop for no reason. Keying on
        #       `policy.trading_day` is session-agnostic and correct for both
        #       deployments: a 15:00→05:00 night is one trading day, and so is an
        #       08:45→13:45 day. Driven by the bar's exchange timestamp, not the
        #       wall clock, so it stays deterministic and replayable.
        day = self.policy.trading_day(market.timestamp)
        if self._anchor_day != day:
            self._anchor_day = day
            self._day_anchor_pnl = net_pnl

        if self.halted:
            return

        # Tech: an operator kill file halts and flattens at this bar.
        # Why:  the out-of-band stop switch — it must work when the process is
        #       unreachable any other way, so it is a filesystem check, and it
        #       flattens because an operator pulling the switch wants risk off.
        kf = self.config.kill_file
        if kf is not None and Path(kf).exists():
            self.halt(f"kill file present: {kf}", HaltAction.FLATTEN)
            return

        # Tech: halt and flatten once the loss from the day's anchor exceeds the
        #       configured point limit.
        # Why:  the position is the problem and prices are still good, so closing
        #       is both possible and correct. Compared in points, matching the
        #       Portfolio's native unit (SPEC §4.6) — no multiplier ambiguity.
        limit = self.config.max_daily_loss_points
        if limit is not None:
            loss = self._day_anchor_pnl - net_pnl
            if loss >= limit:
                self.halt(
                    f"daily loss {loss:.4f} pts >= limit {limit:.4f} pts",
                    HaltAction.FLATTEN,
                )

    # ---- event-driven checks -----------------------------------------------

    def on_order_submitted(self, timestamp: Any) -> None:
        # Tech: append to the sliding window, expire entries older than 60s, and
        #       halt (STOP) if the window exceeds the cap.
        # Why:  catches a submit storm — a cancel/resubmit loop or a repeated
        #       exit — before it reaches the venue's own throttles. STOP, not
        #       FLATTEN: a storm means the order path is misbehaving, and
        #       flattening would push one more order down that same path.
        cap = self.config.max_orders_per_minute
        if cap is None:
            return
        self._order_times.append(timestamp)
        cutoff = timestamp - timedelta(seconds=60)
        while self._order_times and self._order_times[0] < cutoff:
            self._order_times.popleft()
        if len(self._order_times) > cap:
            self.halt(
                f"order rate {len(self._order_times)}/min exceeds cap {cap}",
                HaltAction.STOP,
            )

    def note_data(self, now: Any) -> None:
        # Tech: record the wall-clock time at which real market data last arrived.
        # Why:  the staleness rail measures *absence*, so it cannot key off event
        #       timestamps — no event means no timestamp. Callers must only call
        #       this for genuine prints; a simtrade tick is not data (§2.5).
        self._last_data_wall = now

    def check_staleness(self, now: Any) -> None:
        # Tech: halt (STOP) when no data has arrived for longer than the
        #       configured window — but only while we are inside one continuous
        #       occurrence of a session we trade. Outside that, silence is
        #       expected: the marker is refreshed and no gap accrues.
        # Why:  DEFECT FIXED (§2.12). Without the session test this halts the loop
        #       on the first morning of any night-session deployment: the feed goes
        #       quiet from 05:00 to 15:00, ten hours against a 120-second window.
        #       `same_session_instance` is what makes it correct rather than merely
        #       quiet — comparing session *labels* alone would treat 04:59 tonight
        #       and 15:01 tomorrow as the same NIGHT and halt on the gap between
        #       them, so the trading day has to match too.
        #
        #       Refreshing the marker (rather than just returning) means the
        #       watchdog re-arms cleanly at the next open instead of measuring the
        #       whole out-of-session silence against the first in-session tick.
        #
        #       `now` is local wall-clock and is compared against exchange session
        #       hours, so the host must run on exchange local time.
        window = self.config.quote_staleness_seconds
        if window is None or self.halted or self._last_data_wall is None:
            return
        if not self.policy.same_session_instance(self._last_data_wall, now):
            self._last_data_wall = now
            return
        gap = (now - self._last_data_wall).total_seconds()
        if gap > window:
            self.halt(
                f"no market data for {gap:.1f}s (limit {window:.1f}s)",
                HaltAction.STOP,
            )

    def on_reconcile(self, *, mirror: int, venue: Optional[int]) -> None:
        # Tech: compare our position mirror against the venue's and halt (STOP)
        #       on any disagreement; skip entirely when the venue cannot answer.
        # Why:  §2.9 — a mismatch means a fill was missed or counted twice, so we
        #       do not know what we hold. Never auto-correct, and never flatten:
        #       "close the position" is undefined when the size is in dispute.
        #       `venue is None` is the offline sim, which has nothing to compare.
        if venue is None or self.halted:
            return
        if mirror != venue:
            self.halt(
                f"position mirror {mirror} != venue {venue}",
                HaltAction.STOP,
            )

    def on_order_report(self, *, op_code: str, op_msg: str = "") -> None:
        # Tech: treat any operation code other than "00" as a rejection and halt
        #       (STOP).
        # Why:  Shioaji reports success as op_code "00"; anything else means the
        #       venue refused the operation (margin, session, contract, throttle).
        #       Retrying blindly is how a rejection becomes a storm, so it stops
        #       for human review instead.
        if self.halted:
            return
        if op_code != "00":
            self.halt(f"order rejected op_code={op_code} {op_msg}".strip(),
                      HaltAction.STOP)

    # ---- halting -----------------------------------------------------------

    def halt(self, reason: str, action: HaltAction) -> None:
        # Tech: latch the halt with its reason and action, recording it once.
        # Why:  first reason wins — later rails firing during shutdown must not
        #       overwrite the diagnosis of what actually went wrong. Latching also
        #       makes the flatten one-shot rather than re-submitted every bar.
        if self.halted:
            return
        self.halted = True
        self.halt_reason = reason
        self.halt_action = action
        if self.recorder is not None:
            self.recorder.record("halt", reason=reason, action=action.value)
        # Tech: every halt is a fatal alert, raised from inside the latch so it
        #       fires exactly once.
        # Why:  a halt is *the* definition of "needs a human" — the run makes no
        #       further decisions, and a FLATTEN halt may leave a position that
        #       the close failed to clear (§2.12). Raising it here rather than at
        #       each call site means a rail added later cannot forget to notify.
        if self.alerter is not None:
            self.alerter.fatal("halt", reason, action=action.value,
                               simulation=self.simulation)

    @property
    def flatten_required(self) -> bool:
        # Tech: True when the latched halt calls for closing the position.
        # Why:  lets the loop ask one question instead of matching on the enum,
        #       and keeps the STOP-means-send-nothing rule in one place.
        return self.halted and self.halt_action is HaltAction.FLATTEN
