"""SessionPolicy — the single place that says which sessions we trade.

Every session-dependent decision in the live path routes through here: which
bars the backfill keeps, which bars the tick aggregator emits, when silence on
the feed is expected rather than alarming, and where one trading day ends and the
next begins.

It exists because the alternative — a session baked into each module's default —
already went wrong once. `fetch_bars` shipped with `sessions=("DAY",)` while the
strategy was tuned on NIGHT, and nothing in the code could have caught it.
Nothing here has a session default; callers state what they trade.

**Trading day, not calendar day.** The boundary is a configurable hour, default
08:00, chosen so it sits in the dead zone before either TXF session opens:

  * a NIGHT session (15:00 → 05:00 next date) stays **one** trading day, because
    05:00 falls before the 08:00 boundary and maps back to the date it started;
  * a DAY session (08:45 → 13:45) sits wholly inside its own date;
  * it coincides with Shioaji's own 08:00 daily traffic reset, so quota
    accounting and risk accounting share one calendar.

A midnight boundary would cut a night session in half and hand the second half a
fresh daily loss budget.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, FrozenSet, Iterable, Optional

from ..environment import classify_session

# Tech: the hour at which one trading day gives way to the next.
# Why:  08:00 is before the day session opens (08:45) and after the night session
#       closes (05:00), so it never splits a live session; it is also Shioaji's
#       quota reset (§2.8b). Overridable per policy for a different market.
DEFAULT_TRADING_DAY_START = time(8, 0)

VALID_SESSIONS = ("DAY", "NIGHT")

# Tech: Monday-Friday, as `date.weekday()` numbers them.
# Why:  §2.25 — `classify_session` reads time-of-day only, so Saturday 15:00 was
#       a NIGHT session to every consumer and the staleness backstop halted a
#       soak at **Saturday 15:16** (measured offline against the real rails).
#       Five consecutive unattended days must contain a weekend, so V5 was
#       unreachable without this.
WEEKDAYS = frozenset(range(5))

# Tech: minutes of trading in one occurrence of each session.
# Why:  DAY_SESSION_PLAN C6 — NIGHT (15:00 -> 05:00) is 14 h = 840 min; DAY
#       (08:45 -> 13:45) is 5 h = 300 min. The raw fact the warm-up-window
#       derivation (`LiveConfig.warmup_days_default`) needs. Carried as data
#       here rather than derived from session boundary times parsed elsewhere,
#       since nothing else in this module reaches into `environment.py` for
#       the boundaries either — `classify_session` is a black box to it.
MINUTES_PER_SESSION = {"NIGHT": 840, "DAY": 300}


@dataclass(frozen=True)
class SessionPolicy:
    # Tech: the set of sessions this deployment trades, plus the trading-day
    #       boundary. Frozen so it can be shared freely between the loop, the
    #       rails and the aggregator without any of them mutating it.
    # Why:  one object threaded everywhere means a deployment is described by a
    #       single value. Porting this strategy to the day session is then
    #       `SessionPolicy.day()` at the entry point and nothing else — which is
    #       the whole point of not hardcoding it.
    sessions: FrozenSet[str]
    trading_day_start: time = DEFAULT_TRADING_DAY_START
    # Tech: dates the market is closed despite being a weekday. Empty by default.
    # Why:  the weekday test below is right about ~104 days a year and wrong
    #       about ~10-15 — Taiwan's weekday market holidays. Carrying them as
    #       *data* rather than code means closing that gap is a list, not a
    #       redesign, on the day it first costs something. Empty by default
    #       because an unmaintained list that silently goes stale would be worse
    #       than none: it would halt a run on a day the market was open.
    holidays: FrozenSet[date] = frozenset()

    def __post_init__(self) -> None:
        # Tech: reject an empty or unknown session set at construction.
        # Why:  an empty policy would silently filter every bar away and look like
        #       a dead feed; a typo ("NIGTH") would do the same. Both are far
        #       cheaper to catch here than at 15:00 on a live night.
        if not self.sessions:
            raise ValueError("SessionPolicy needs at least one session")
        unknown = set(self.sessions) - set(VALID_SESSIONS)
        if unknown:
            raise ValueError(
                f"unknown sessions {sorted(unknown)}; valid: {list(VALID_SESSIONS)}"
            )

    # ---- constructors ------------------------------------------------------

    @classmethod
    def of(cls, sessions: Iterable[str], **kw) -> "SessionPolicy":
        return cls(frozenset(sessions), **kw)

    @classmethod
    def night(cls, **kw) -> "SessionPolicy":
        """The configuration `results.tsv` was tuned on."""
        return cls(frozenset({"NIGHT"}), **kw)

    @classmethod
    def day(cls, **kw) -> "SessionPolicy":
        """For the day-session port."""
        return cls(frozenset({"DAY"}), **kw)

    @classmethod
    def both(cls, **kw) -> "SessionPolicy":
        return cls(frozenset(VALID_SESSIONS), **kw)

    # ---- queries -----------------------------------------------------------

    def label(self, ts: Any) -> Optional[str]:
        # Tech: the session a timestamp falls in, or None outside trading hours.
        # Why:  delegates to the same `classify_session` the backtest uses, so a
        #       live bar and a replayed bar can never disagree about what session
        #       they belong to.
        return classify_session(ts)

    def is_trading_day(self, ts: Any) -> bool:
        # Tech: True on a weekday that is not a listed holiday. The *trading* day
        #       is what is tested, so a night session's post-midnight half is
        #       judged by the date it started on.
        # Why:  §2.25. Using `trading_day` rather than the raw date is the whole
        #       point — Saturday 02:00 belongs to Friday's night session and must
        #       read as a trading moment, while Saturday 15:00 starts a session
        #       that does not exist. A raw `ts.weekday()` would get the first of
        #       those backwards and halt every Saturday morning mid-session.
        d = date.fromisoformat(self.trading_day(ts))
        return d.weekday() in WEEKDAYS and d not in self.holidays

    def includes(self, ts: Any) -> bool:
        # Tech: True when the timestamp lies in a session we trade, on a day the
        #       market is open.
        # Why:  the single predicate behind bar filtering, the staleness rail's
        #       "is silence expected?" question, and FeedWatch's polling gate —
        #       one definition, so those three can never drift. Adding the
        #       trading-day test *here* is what fixes all three at once: the rail
        #       stops accruing a gap against a closed market (§2.25), and
        #       FeedWatch stops spending ~1 680 queries a weekend confirming that
        #       nothing is trading.
        if not self.is_trading_day(ts):
            return False
        return self.label(ts) in self.sessions

    def trading_day(self, ts: Any) -> str:
        # Tech: the ISO date of the trading day a timestamp belongs to; anything
        #       before `trading_day_start` belongs to the previous date.
        # Why:  see the module docstring — this is what keeps a night session that
        #       crosses midnight inside one trading day for loss limits and
        #       backfill counting alike.
        moment = ts if isinstance(ts, datetime) else datetime.fromisoformat(str(ts))
        d: date = moment.date()
        if moment.time() < self.trading_day_start:
            d = d - timedelta(days=1)
        return d.isoformat()

    def roll_at_time(self) -> time:
        # Tech: the time of day this deployment swaps delivery months.
        # Why:  DAY_SESSION_PLAN C4. 15:30 was measured for a NIGHT strategy and
        #       is *outside* a DAY session, so a DAY deployment sharing it would
        #       run its roll ladder against a tape it is not following. Carried
        #       on the policy so the two cannot share one value.
        #
        #       DAY = 12:45, one hour before the 13:45 close, so `ROLL_WINDOW`
        #       fills exactly the rest of the session and cannot overrun it.
        #       MEASURED 2026-08-25 over the three most recent rolls: 12:45 is
        #       the calmest stretch of the day session (26.8 pts mean
        #       intra-minute range against 132.2 at the open), which is what
        #       matters when the ladder's whole escalation budget is 25 ticks.
        #       Depth is not the constraint at `max_position=1`.
        #
        #       NIGHT keeps 15:30 on the runway argument in `roll.py`. (The
        #       liquidity claim beside it does not survive measurement — 15:30 is
        #       the 6th busiest half hour, not the busiest — but the first live
        #       roll is 2026-09-15 and this is not the change to make first.)
        if self.sessions == frozenset({"DAY"}):
            return time(12, 45)
        return time(15, 30)

    def bars_per_session(self) -> int:
        # Tech: sum `MINUTES_PER_SESSION` across `self.sessions`.
        # Why:  DAY_SESSION_PLAN C6. A `both()` policy trades one NIGHT *and*
        #       one DAY occurrence per calendar day, so its bars are the two
        #       stacked, not either alone — the same reasoning `roll_at_time`
        #       and `cache_slug` apply to their own per-policy values.
        return sum(MINUTES_PER_SESSION[s] for s in self.sessions)

    def cache_slug(self) -> str:
        # Tech: a filesystem-safe name for this policy, for artefacts that must
        #       not be shared between deployments.
        # Why:  DAY_SESSION_PLAN C5. `outputs/bars_cache.csv` was one path for
        #       every deployment, so a DAY run wrote DAY bars into the file a
        #       NIGHT run reads back as its model context. `warm_up` does raise
        #       on it (`runtime.py`'s policy check), so it is not silent — but it
        #       blocks whichever soak starts second, and the cache is the only
        #       source for months the venue has delisted (§2.8 Finding D), so the
        #       collision costs context that cannot be refetched.
        #
        #       Derived from the policy rather than passed in, so a new
        #       deployment cannot forget to change it.
        return "_".join(sorted(x.lower() for x in self.sessions))

    def same_session_instance(self, a: Any, b: Any) -> bool:
        # Tech: True when two timestamps fall in the same *occurrence* of the same
        #       traded session.
        # Why:  the label alone is not enough. 04:59 tonight and 15:01 tomorrow are
        #       both "NIGHT" but are two different sessions with a ten-hour gap
        #       between them — comparing only labels would make the staleness
        #       watchdog treat that gap as a frozen feed and halt every morning.
        if not (self.includes(a) and self.includes(b)):
            return False
        if self.label(a) != self.label(b):
            return False
        return self.trading_day(a) == self.trading_day(b)


def trading_day(ts: Any,
                start: time = DEFAULT_TRADING_DAY_START) -> str:
    # Tech: module-level trading-day helper for callers that have no policy.
    # Why:  `RestartGuard` needs the boundary before any policy exists (it runs
    #       before the loop is built), and two independent implementations of the
    #       same rule is how they drift apart.
    return SessionPolicy(frozenset(VALID_SESSIONS),
                         trading_day_start=start).trading_day(ts)
