"""RestartGuard — the rails that must survive the process dying.

`SafetyRails` (state.py) protects a *running* loop and is deliberately pure:
in-memory, clock-free, no I/O. These rails protect against the process itself
being restarted, so they must persist across its death and therefore own a file.
Different lifecycle, different failure mode, separate class.

The failure mode being guarded (SHIOAJI_PLAN.md §2.8b): every process start
performs a `kbars` backfill of the full 3 008-bar context (§7.5), so an
unsupervised crash-restart loop becomes a login + historical-query loop. Shioaji
answers repeated rate-limit violations with *"IP and ID access suspended"*, and
repeated `login()` during a suspension **extends the penalty**. So the outcome is
not a slow system — it is a banned account that fights its own recovery.

Four rails:

  * **exponential restart backoff**, keyed on consecutive *unclean* exits;
  * **per-trading-day backfill cap**;
  * **`api.usage()` headroom check** before the first query;
  * **no automatic `login()` retry** — deliberately absent from this API. There
    is no retry helper here to reach for; a failed login must surface and stop.

The trading day rolls at **08:00**, matching Shioaji's own daily traffic reset,
so the backfill cap and the quota it protects share one boundary.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Optional

from .sessions import trading_day as _trading_day

# Tech: the wall-clock hour at which the trading day (and the quota) rolls over.
# Why:  Shioaji resets the daily traffic allowance at 08:00 on trading days;
#       aligning our own per-day counter to the same edge means the cap can never
#       be counting a different "day" than the limit it exists to protect.
QUOTA_RESET_HOUR = time(8, 0)


class RestartBlocked(RuntimeError):
    """Raised when a rail refuses to let this process start."""


@dataclass
class RestartPolicy:
    # Tech: the state file plus the four tunables; the file is the only required
    #       field because everything else has a defensible default.
    # Why:  defaults are chosen so a *legitimate* restart is never blocked and a
    #       *runaway* one is stopped within a couple of attempts. 10 backfills in
    #       a trading day is far more than any real operator needs and far below
    #       anything that threatens the 500 MB floor quota (§2.8b).
    state_file: Path
    max_backfills_per_day: int = 10
    base_backoff_seconds: float = 60.0
    max_backoff_seconds: float = 3600.0
    healthy_runtime_seconds: float = 600.0
    min_remaining_bytes: int = 50 * 1024 * 1024   # 50 MB headroom


class RestartGuard:
    def __init__(self, policy: RestartPolicy):
        # Tech: hold the policy; all state lives in the file, not on the instance.
        # Why:  the instance dies with the process — that is the whole problem
        #       being solved — so anything worth remembering must be on disk
        #       before it matters.
        self.policy = policy

    # ---- persistence -------------------------------------------------------

    def _load(self) -> dict:
        # Tech: read the JSON state; on a missing file return a clean slate, and
        #       on an unreadable one return a slate marked as a failed run.
        # Why:  a *missing* file is a genuine first run. A *corrupt* file is not
        #       evidence of health, and treating it as a clean slate would let a
        #       crash loop that damages the file bypass the backoff entirely — so
        #       corruption is charged as one failure and costs a base backoff.
        p = self.policy.state_file
        if not p.exists():
            return {"run_in_flight": False, "scored": True,
                    "consecutive_failures": 0}
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {"run_in_flight": True, "scored": False,
                    "consecutive_failures": 0, "corrupt": True}

    def _save(self, state: dict) -> None:
        # Tech: write the state file, creating its parent directory.
        # Why:  written synchronously at each transition — this file only has
        #       value if it is already on disk when the process is killed.
        p = self.policy.state_file
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")

    # ---- trading day -------------------------------------------------------

    @staticmethod
    def trading_day(now: datetime) -> str:
        # Tech: delegate to the shared trading-day rule in `live.sessions`.
        # Why:  the same boundary governs the daily loss limit (`SafetyRails`) and
        #       the backfill cap here. Two implementations of one rule is how they
        #       drift apart, so there is exactly one — and it lives with the rest
        #       of the session logic rather than being reinvented in a module that
        #       runs before any policy exists.
        return _trading_day(now, QUOTA_RESET_HOUR)

    # ---- the rails ---------------------------------------------------------

    def begin(self, now: datetime) -> None:
        # Tech: the single startup gate — roll the day, score the previous run,
        #       enforce backoff and the backfill cap, then mark this run as
        #       in-flight. Raises RestartBlocked rather than returning a status.
        # Why:  one call the caller cannot forget half of. Raising means a blocked
        #       start cannot be accidentally ignored the way a False return can.
        state = self._load()
        day = self.trading_day(now)

        # Tech: reset the per-day backfill counter when the trading day changes;
        #       the failure counter deliberately survives the roll.
        # Why:  the quota is what resets daily, not the health of the process — a
        #       crash loop that spans 08:00 must not be handed a clean bill.
        if state.get("trading_day") != day:
            state["trading_day"] = day
            state["backfills_today"] = 0

        # Tech: score the previous run at most ONCE — only when a run was left
        #       in flight and has not already been scored. An unclean run either
        #       resets the count to a single failure (if it had been running
        #       healthily) or increments it (if it died young); a clean exit has
        #       already zeroed the count in `end()`.
        # Why:  one crash after eight healthy hours is not the fourth crash in two
        #       minutes and must not inherit its backoff — hence
        #       `last_heartbeat - last_start` as the survival measure. The `scored`
        #       latch is what keeps a *blocked* start from being counted as another
        #       failure: without it, every rejected attempt would double the wait
        #       it was already serving, so a supervisor retrying on a fixed timer
        #       could never get back in. The penalty must track dead runs, not
        #       rejected attempts.
        if state.get("run_in_flight") and not state.get("scored"):
            ran_for = self._previous_runtime(state)
            if ran_for is not None and ran_for >= self.policy.healthy_runtime_seconds:
                state["consecutive_failures"] = 1
            else:
                state["consecutive_failures"] = int(
                    state.get("consecutive_failures", 0)
                ) + 1
            state["scored"] = True

        # Tech: with failures on record, require a minimum gap since the last
        #       sign of life, doubling per consecutive failure up to the cap.
        # Why:  measured from the last heartbeat rather than from `now` so the
        #       wait is a property of when the previous run died, not of how often
        #       this check is called — repeatedly invoking `begin` cannot compound
        #       or reset the penalty.
        failures = int(state.get("consecutive_failures", 0))
        if failures > 0:
            wait = min(
                self.policy.base_backoff_seconds * (2 ** (failures - 1)),
                self.policy.max_backoff_seconds,
            )
            since = self._seconds_since_last_signal(state, now)
            if since is not None and since < wait:
                self._save(state)
                raise RestartBlocked(
                    f"restart backoff: {failures} consecutive failed run(s), "
                    f"must wait {wait:.0f}s since last activity "
                    f"({since:.0f}s elapsed). Investigate before restarting — "
                    f"repeated logins during a Shioaji suspension extend it."
                )

        # Tech: refuse to start once the trading day's backfill budget is spent.
        # Why:  the backstop if backoff is somehow escaped (e.g. a supervisor that
        #       restarts on a long timer). Bounds total `kbars` traffic per day
        #       regardless of how the restarts are spaced.
        if int(state.get("backfills_today", 0)) >= self.policy.max_backfills_per_day:
            self._save(state)
            raise RestartBlocked(
                f"backfill cap reached: {state['backfills_today']} backfills "
                f"on trading day {day} (max {self.policy.max_backfills_per_day}). "
                f"Something is restarting this process repeatedly."
            )

        # Tech: mark the run in flight, clear the scored latch, and stamp both
        #       start and heartbeat.
        # Why:  `run_in_flight=True` is what makes a death detectable — if we never
        #       reach `end()`, the next `begin()` sees it and charges a failure.
        #       `scored=False` arms that charge for *this* run specifically, which
        #       is why it is set only here, on a start that was actually allowed.
        state["run_in_flight"] = True
        state["scored"] = False
        state["last_start"] = now.isoformat()
        state["last_heartbeat"] = now.isoformat()
        state.pop("corrupt", None)
        self._save(state)

    def record_backfill(self, now: datetime) -> None:
        # Tech: increment the trading day's backfill counter.
        # Why:  called by the warm-up path immediately before it queries `kbars`,
        #       not after — a backfill that is started and then crashes still
        #       consumed quota and must still be counted.
        state = self._load()
        day = self.trading_day(now)
        if state.get("trading_day") != day:
            state["trading_day"] = day
            state["backfills_today"] = 0
        state["backfills_today"] = int(state.get("backfills_today", 0)) + 1
        self._save(state)

    def heartbeat(self, now: datetime) -> None:
        # Tech: stamp the last time the loop was known alive.
        # Why:  supplies the "how long did it run before dying" measurement the
        #       healthy-run reset depends on, and anchors the backoff window. Once
        #       per bar (1/min) is ample; the write is trivially cheap next to a
        #       forecast.
        state = self._load()
        state["last_heartbeat"] = now.isoformat()
        self._save(state)

    def end(self, now: datetime) -> None:
        # Tech: mark an orderly shutdown and clear the failure count.
        # Why:  the only thing that distinguishes "stopped" from "died". An
        #       operator-driven restart therefore pays no backoff at all.
        state = self._load()
        state["run_in_flight"] = False
        state["scored"] = True
        state["consecutive_failures"] = 0
        state["last_heartbeat"] = now.isoformat()
        self._save(state)

    def check_usage(self, usage: Any) -> None:
        # Tech: refuse to start when the account's remaining daily traffic is
        #       below the configured margin; accepts anything exposing
        #       `remaining_bytes` (attribute or key), and skips when it cannot.
        # Why:  duck-typed so this module never imports shioaji and stays testable
        #       without it. Starting with near-zero headroom is pointless — over
        #       quota, historical queries return *empty* rather than erroring, so
        #       the backfill would silently produce an empty context instead of
        #       failing loudly. Refusing up front converts that into a clear stop.
        remaining = None
        if usage is not None:
            remaining = getattr(usage, "remaining_bytes", None)
            if remaining is None and isinstance(usage, dict):
                remaining = usage.get("remaining_bytes")
        if remaining is None:
            return
        if int(remaining) < self.policy.min_remaining_bytes:
            raise RestartBlocked(
                f"insufficient API traffic headroom: {int(remaining)} bytes "
                f"remaining, need {self.policy.min_remaining_bytes}. Over quota "
                f"Shioaji returns EMPTY history rather than an error, which would "
                f"silently produce an empty model context."
            )

    # ---- helpers -----------------------------------------------------------

    @staticmethod
    def _parse(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    def _previous_runtime(self, state: dict) -> Optional[float]:
        start = self._parse(state.get("last_start"))
        beat = self._parse(state.get("last_heartbeat"))
        if start is None or beat is None:
            return None
        return (beat - start).total_seconds()

    def _seconds_since_last_signal(
        self, state: dict, now: datetime
    ) -> Optional[float]:
        ref = self._parse(state.get("last_heartbeat")) or self._parse(
            state.get("last_start")
        )
        if ref is None:
            return None
        return (now - ref).total_seconds()
