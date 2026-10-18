"""FeedWatch — tell a quiet market from a broken feed, and heal the broken one.

Silence is ambiguous. A 23-hour soak died on that ambiguity (§2.21): the night
session went 151 s between prints at 02:35, the 120 s watchdog called it a dead
feed, and the loop stopped **31 s before the market traded again**. Nothing was
wrong with the connection.

The old rail had to guess, because it only had one input — how long we had heard
nothing. Any threshold built on that is a guess, and the measured distribution it
would be calibrated against is itself truncated by the threshold that ended the
run. This module removes the guess by adding a second input: **ask the venue.**

    silence for `interval`
       └─ snapshot
            ├─ venue's last trade is one we already have  → QUIET. do nothing.
            ├─ venue has traded since our newest print    → MISSED. resubscribe.
            └─ the query itself fails                     → UNREACHABLE. back off;
                                                             re-login if it persists.

The threshold stops being a decision and becomes a **polling interval**: getting
it wrong now costs one 40 ms query instead of the run, which is why it can be
aggressive (60 s) where the old halt window had to be timid.

**Two layers, not one.** This heals; it never halts. `SafetyRails`' staleness
window stays exactly as it was and becomes the backstop — set long, it halts only
if the healing did not work. Keeping the halt semantics untouched is deliberate:
a recoverable condition and a human-attention condition are different things, and
merging them is what made a transient gap terminal in the first place.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from enum import Enum
from typing import Any, Callable, Optional

import pandas as pd

# Tech: how far ahead of our newest print the venue's last trade must be before
#       we call it a miss.
# Why:  MEASURED 2026-08-13 — across 9 samples in a live session `snapshot.ts`
#       equalled our newest tick exactly 8 times and was 0.8 s *behind* once. It
#       was never ahead. So any real positive difference is signal, and a 1 s
#       margin absorbs the observed jitter with room to spare. The error this
#       protects against is the expensive one: a spurious resubscribe risks a
#       double tick stream, which corrupts bars.
MISS_MARGIN = timedelta(seconds=1.0)

# Tech: any converted stamp older than this is treated as unusable.
# Why:  `pd.Timestamp` of an integer reads it as **nanoseconds** since the epoch.
#       If the SDK ever hands back seconds or milliseconds instead, the result is
#       a 1970 date — which compares as "older than our newest tick" and would
#       make every check answer QUIET. The rail would then be permanently and
#       *silently* disarmed, which is worse than the crash it replaced. Failing
#       loudly on an implausible date is the same reflex as treating an unknown
#       callback state as an anomaly rather than a no-op.
SANE_AFTER = pd.Timestamp("2000-01-01")


def _stamp(value: Any) -> pd.Timestamp:
    # Tech: normalise whatever the SDK or the feed hands us to a Timestamp.
    # Why:  MEASURED 2026-08-13, the expensive way — `Snapshot.ts` is an **int**
    #       (epoch ns) while a tick's `datetime` is a `datetime`, and comparing
    #       them raises `TypeError: '>' not supported between instances of 'int'
    #       and 'datetime.datetime'`. That crash killed a live soak 42 bars in.
    #       The probe that pinned `ts`'s meaning had already converted it exactly
    #       this way; the conversion just never reached this module, and a test
    #       fake handing over a `datetime` hid the gap. Both sides go through
    #       here now, so the comparison cannot be reached with mixed types.
    return pd.Timestamp(value)


class Verdict(str, Enum):
    # Tech: the three answers a liveness check can produce.
    # Why:  named rather than boolean because the middle case is the only one
    #       that acts, and a caller reading `if broken:` would lose the
    #       distinction between "the market is quiet" and "we cannot tell".
    QUIET = "quiet"                # the venue has nothing we are missing
    MISSED = "missed"              # it traded and we did not receive it
    UNREACHABLE = "unreachable"    # the query failed — we still cannot tell


@dataclass
class FeedWatch:
    # Tech: `snapshot` returns an object with `.ts` and `.total_volume`, raising
    #       on failure; `resubscribe` re-establishes the tick stream.
    # Why:  both are injected as plain callables so every branch here is testable
    #       without a network, and so this module never imports `shioaji`. The
    #       CLI owns the subscription, so the CLI supplies the repair.
    snapshot: Callable[[], Any]
    resubscribe: Callable[[], None]
    interval: float = 60.0
    max_resubscribes: int = 3
    recorder: Any = None
    # Tech: the session-level repair, run after `relogin_after` consecutive
    #       checks whose query raised. `None` leaves UNREACHABLE a warning only.
    # Why:  MEASURED 2026-10-15 — the broker session died at 09:52 with no
    #       `session_down`, every snapshot raised `SessionNotEstablished`, and
    #       the SDK did not reconnect in 5 h. Re-subscribing cannot heal that:
    #       there is no session to subscribe on. A `force_refresh` re-login did
    #       heal it, at the 14:55 anchor, but day and fullday had already halted
    #       on the 900 s backstop at 10:08. Injected for the same reason
    #       `resubscribe` is — the CLI owns the API object.
    relogin: Optional[Callable[[], None]] = None
    # Tech: consecutive failed queries before the first re-login, and between
    #       re-logins while the failures continue.
    # Why:  one failed query is not a dead session — the 2026-10-01 re-login
    #       timeout recovered on its own retry — and a refreshing login is a
    #       new connection against the 5-per-person_id cap (§2.8b), not a free
    #       poke. Three checks at the 60 s default is ~3 min of silence; the
    #       900 s backstop then still leaves room for four attempts before it
    #       halts, so a session that will not come back still reaches a human.
    relogin_after: int = 3
    # Tech: extra failed checks before the *first* re-login of an outage only.
    # Why:  three deployments see a venue-wide drop on the same clock, so equal
    #       counts would refresh all three in one second — the connection spike
    #       `relogin.DEPLOYMENT_STAGGER` exists to avoid. Offsets 0/1/2 put the
    #       first attempts a check (60 s) apart, and since every later attempt
    #       comes `relogin_after` checks after the last, they stay apart: 3, 6,
    #       9, 12 / 4, 7, 10, 13 / 5, 8, 11, 14 — four each before the backstop.
    relogin_offset: int = 0

    def __post_init__(self) -> None:
        self._last_exchange: Any = None   # newest print we received (exchange clock)
        self._last_local: Any = None      # when it arrived (wall clock)
        self._last_check: Any = None
        self._resubscribes_this_silence = 0
        self.n_checks = 0
        self.n_quiet = 0
        self.n_missed = 0
        self.n_unreachable = 0
        self.n_resubscribes = 0
        self._failed_queries = 0          # consecutive checks whose query raised
        self._relogins_this_outage = 0
        self.n_relogins = 0
        self.n_relogin_failures = 0

    def note_tick(self, exchange_ts: Any, now: Any) -> None:
        # Tech: remember the newest print on both clocks, and clear the
        #       per-silence resubscribe budget.
        # Why:  two clocks because they answer different questions. *Silence* is
        #       measured on the wall clock (no event means no event timestamp),
        #       but the comparison against `snapshot.ts` must be exchange-time on
        #       both sides — mixing them would fold the ~0.6-1.1 s delivery lag
        #       into the comparison and manufacture misses. Resetting the budget
        #       on any print means a stream that recovered gets a full allowance
        #       next time rather than inheriting an exhausted one.
        self._last_exchange = _stamp(exchange_ts)
        self._last_local = now
        self._resubscribes_this_silence = 0
        self._failed_queries = 0
        self._relogins_this_outage = 0

    def check(self, now: Any) -> Optional[Verdict]:
        # Tech: run one liveness check if we have been silent for `interval`,
        #       rate-limited to one per interval. Returns the verdict, or None
        #       when no check was due.
        # Why:  rate-limiting off `_last_check` rather than resetting the silence
        #       marker keeps "when did data last arrive" honest — the rails'
        #       backstop reads the same silence, and a marker this module reset
        #       would disarm the very halt that catches an unhealable feed.
        if self._last_exchange is None or self._last_local is None:
            return None
        since = (now - max(self._last_local,
                           self._last_check or self._last_local)).total_seconds()
        if since < self.interval:
            return None
        self._last_check = now
        self.n_checks += 1

        # Tech: any exception from the query is UNREACHABLE, never a miss.
        # Why:  a failed query is not evidence about the market — it is evidence
        #       about us, and the honest answer is "cannot tell". Resubscribing
        #       here would be acting on no information; halting here would hand a
        #       transient API blip the power to end a soak. Backing off leaves the
        #       rails' timer as the thing that eventually decides.
        try:
            snap = self.snapshot()
        except Exception as exc:                      # noqa: BLE001 — see Why
            self.n_unreachable += 1
            self._failed_queries += 1
            self._record(Verdict.UNREACHABLE, error=repr(exc),
                         consecutive=self._failed_queries)
            self._maybe_relogin()
            return Verdict.UNREACHABLE
        # Tech: any answer at all ends the run of failed queries.
        # Why:  only a query that *raised* says the session is gone. A malformed
        #       answer below is still UNREACHABLE, but it came from a session
        #       that answered, and a re-login would not change what it sends.
        self._failed_queries = 0
        self._relogins_this_outage = 0

        raw_ts = getattr(snap, "ts", None)
        total = getattr(snap, "total_volume", None)
        if raw_ts is None:
            self.n_unreachable += 1
            self._record(Verdict.UNREACHABLE, error="snapshot carried no ts")
            return Verdict.UNREACHABLE

        # Tech: convert, then sanity-check the date before trusting it.
        # Why:  a conversion failure and an implausible date are the same kind of
        #       problem — we hold something we cannot compare — and both must read
        #       as "cannot tell" rather than as a quiet market. See `SANE_AFTER`:
        #       the dangerous outcome is not an exception, it is a 1970 stamp that
        #       answers QUIET forever without ever complaining.
        try:
            venue_ts = _stamp(raw_ts)
        except (TypeError, ValueError) as exc:
            self.n_unreachable += 1
            self._record(Verdict.UNREACHABLE,
                         error=f"unreadable snapshot ts {raw_ts!r}: {exc}")
            return Verdict.UNREACHABLE
        if venue_ts < SANE_AFTER:
            self.n_unreachable += 1
            self._record(Verdict.UNREACHABLE,
                         error=f"implausible snapshot ts {raw_ts!r} -> {venue_ts}")
            return Verdict.UNREACHABLE

        # Tech: decide on the timestamps alone; `total_volume` goes in the trail.
        # Why:  a volume *delta* needs a baseline from when we were healthy, and
        #       taking snapshots while healthy would spend quota on every quiet
        #       minute to answer a question the timestamps already answer.
        #       Recorded anyway because it is free here and is the corroborating
        #       number a post-mortem will want.
        missed = venue_ts > self._last_exchange + MISS_MARGIN
        if not missed:
            self.n_quiet += 1
            self._record(Verdict.QUIET, venue_ts=venue_ts, total_volume=total)
            return Verdict.QUIET

        self.n_missed += 1
        self._record(Verdict.MISSED, venue_ts=venue_ts, total_volume=total,
                     resubscribes=self._resubscribes_this_silence)

        # Tech: repair, but at most `max_resubscribes` times per silence.
        # Why:  a stream that does not come back will produce the same verdict
        #       every interval forever, and an unbounded loop of unsubscribe /
        #       subscribe is a submit storm against the quote service. After the
        #       budget, stop trying and let the rails' staleness window halt —
        #       which is the correct outcome, because a feed that will not heal
        #       needs a human.
        if self._resubscribes_this_silence < self.max_resubscribes:
            self._resubscribes_this_silence += 1
            self.n_resubscribes += 1
            try:
                self.resubscribe()
                self._record("feed_resubscribed",
                             attempt=self._resubscribes_this_silence)
            except Exception as exc:                  # noqa: BLE001
                self._record("feed_resubscribe_failed", error=repr(exc))
        return Verdict.MISSED

    def _maybe_relogin(self) -> None:
        # Tech: re-login once `relogin_after` queries in a row have raised, then
        #       restart the count so the next attempt needs as many again.
        # Why:  the count is the throttle — at most one refreshing login per
        #       `relogin_after * interval`, with the staleness backstop bounding
        #       the total, so no separate budget can drift from it. This runs
        #       in session, which the scheduled anchors deliberately never do
        #       (`relogin.py`); that rule protects live bars, and here none are
        #       arriving — the alternative to a mid-session login is the halt.
        #       Exceptions are recorded, never raised: this runs on the consumer
        #       thread, and an escape would end the process it is trying to save.
        if self.relogin is None:
            return
        needed = self.relogin_after + (
            self.relogin_offset if self._relogins_this_outage == 0 else 0)
        if self._failed_queries < needed:
            return
        failed = self._failed_queries
        self._failed_queries = 0
        self._relogins_this_outage += 1
        try:
            self.relogin()
        except Exception as exc:                      # noqa: BLE001 — see Why
            self.n_relogin_failures += 1
            self._record("feed_relogin_failed", after=failed, error=repr(exc))
            return
        self.n_relogins += 1
        self._record("feed_relogin", after=failed)

    def _record(self, event: Any, **fields: Any) -> None:
        if self.recorder is None:
            return
        name = event.value if isinstance(event, Verdict) else str(event)
        if isinstance(event, Verdict):
            self.recorder.record("feed_check", verdict=name, **fields)
        else:
            self.recorder.record(name, **fields)

    def summary(self) -> dict:
        return {
            "feed_checks": self.n_checks,
            "feed_quiet": self.n_quiet,
            "feed_missed": self.n_missed,
            "feed_unreachable": self.n_unreachable,
            "feed_resubscribes": self.n_resubscribes,
            "feed_relogins": self.n_relogins,
            "feed_relogin_failures": self.n_relogin_failures,
        }
