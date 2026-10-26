"""ReloginScheduler — get a fresh token before the 24-hour wall, in the quiet.

The session token expires **24 hours after login** and nothing in the SDK
refreshes it: a 24 h soak died at 24 h 0 m 36 s when `list_positions` came back
`TokenError: Token is expired` (§2.24). The quote feed was still healthy at the
time — it is the REST token that dies, so the failure surfaces on the
reconciliation path rather than as silence.

Re-login is the only lever, which makes *when* the whole design. Each session
open gets two sequential windows in the quiet before it:

    open − 5 min ............ open − 1 min ...... open
    └── re-login, retried ──┘ └── verify ──┘
        08:40 → 08:44           08:44 → 08:45
        14:55 → 14:59           14:59 → 15:00

Both sit inside a measured `simtrade`-only window (08:30–08:45 and 14:50–15:00,
reproduced on two independent days), so blocking this thread for a few seconds
costs nothing real. Re-login intervals are 6 h 15 m and 17 h 45 m, so token age
never exceeds **17 h 45 m** against the 24 h wall.

**The verify window is the last look before trading.** One minute before the
open it calls an injected health check — the same kind of authenticated REST
call that actually failed in the soak — and if that raises, it takes one more
re-login attempt right there. This replaces an age-based backstop, which was
considered and rejected: a backstop fires on a clock reading, mid-session, on
suspicion; this fires on evidence, in the quiet, at the last moment it is still
free. It is also the only thing standing behind an abandoned re-login window.

**Anchors deliberately ignore `SessionPolicy`.** A night-only deployment still
re-logins at the *day* session's anchor, because a single anchor per day would
re-login every exactly 24 h — a dead heat with the wall, every day. The policy
says what we *trade*; it does not get to say when we refresh a credential.

**The window is the retry budget.** A re-login window runs only from its start to
the verify window, and every backoff retry has to fit inside that. There is no
attempt counter because the window already bounds attempts to three at the
default backoff — a structural limit rather than a tuned one, and it bounds them
for the reason that actually matters: nothing chases a login onto a live bar.
Six attempts a day against a 1 000/day cap is nowhere near the login storm §2.8b
warns about.

**A wholly lost window is still a gap, now narrowed.** If every attempt fails
*and* the verify call then fails too, the next successful login is one anchor
later and token age can reach 24 h. The verify window is what makes that require
two independent failures instead of one; see
`test_a_lost_window_still_reaches_the_wall_when_verify_fails_too`.

Threading: driven from the CLI's 1 Hz clock tick, same as `FeedWatch`. Alerting
is the caller's job; this module returns an outcome and records, so every branch
is testable without a transport.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from enum import Enum
from typing import Any, Callable, Dict, Optional, Tuple

from ..environment import DAY_OPEN, NIGHT_OPEN

# Tech: how far ahead of a session open the re-login is attempted.
# Why:  MEASURED — `simtrade` prints run 08:30:10–08:44:55 and 14:50:15–14:59:55
#       (2026-08-14, reproduced within 10 s on 2026-08-12), and the first genuine
#       print of each session lands exactly on the open. 5 minutes puts the
#       attempt inside the trial-print window with clearance on both sides, and
#       leaves the rest of the window as retry room before real trading starts.
RELOGIN_LEAD = timedelta(minutes=5)

# Tech: how far ahead of a session open the health check runs.
# Why:  late enough that it is the *last* look before real prints, early enough
#       that a recovery re-login still lands in the trial-print window. One
#       minute is ~1 200x the measured 32–82 ms round trip (§2.22), so even a
#       badly degraded call finishes before the open.
VERIFY_LEAD = timedelta(minutes=1)

# Tech: how long the recovery re-login is given before the check is re-run.
# Why:  MEASURED THE HARD WAY (§2.6). The check's tick half asserts a print has
#       arrived *since* the last re-login, and the recovery re-login resets that
#       baseline — so re-checking immediately would fail by construction, every
#       time, and turn every recovery into a FATAL. 20 s is long enough for a
#       subscription to deliver on a tape that is printing (the pre-open windows
#       carry trial prints continuously) and short enough that the verdict still
#       lands ~40 s before the open, which is the whole point of checking here.
RECHECK_GRACE = timedelta(seconds=20)

# Tech: how far apart the deployments' re-logins start, and in which order.
# Why:  relogin_probe Q9 measured a `force_refresh=True` login opening its new
#       connection ~30 s before the old one drops. With the owner and three
#       deployments connected, three refreshes in the same second briefly hold 7
#       connections against the cap of 5 per person_id (§2.8b), and what Shioaji
#       does above 5 has never been measured. 40 s apart, each spike has cleared
#       before the next starts, so the peak is 5. The offsets run *later* than
#       the unstaggered anchor, never earlier, so the owner's window (open-8 to
#       open-6 min, `owner_cli.OWNER_RELOGIN_LEAD`) stays clear of all three.
# Tech: `hybrid` takes the fourth slot, 2 min later than the anchor (08:42:00 /
#       14:57:00).
# Why:  autoresearch_trading_plan_hybrid_split07.md "Going live" step 2. An
#       unlisted name gets slot 0, so hybrid would refresh in the same second as
#       day. With four deployments the steady state is already 5 and each spike
#       makes 6, which the plan's step-3 probe must clear before hybrid starts.
DEPLOYMENT_STAGGER = timedelta(seconds=40)
STAGGER_ORDER = ("day", "night", "fullday", "hybrid")


def stagger_slot(deployment: Optional[str]) -> int:
    # Tech: a deployment's place in `STAGGER_ORDER`; 0 for anything else.
    # Why:  one number drives both staggers — the anchors here and FeedWatch's
    #       lost-session re-login — so the two cannot order the deployments
    #       differently. An unknown name gets slot 0, the unstaggered schedule.
    return STAGGER_ORDER.index(deployment) if deployment in STAGGER_ORDER else 0


def deployment_relogin_anchors(deployment: Optional[str]
                               ) -> Tuple[Tuple[time, time, time], ...]:
    # Tech: `session_open_anchors` with the re-login start moved later by the
    #       deployment's slot; the verify window does not move.
    # Why:  a later start shortens the retry window rather than the verify one,
    #       which is the last look before the open and must stay whole. At the
    #       default backoff the last slot (hybrid's, 2 min) still fits two
    #       attempts.
    return session_open_anchors(
        relogin_lead=RELOGIN_LEAD - stagger_slot(deployment) * DEPLOYMENT_STAGGER)


# Tech: a window key — a date plus the window's start time of day.
# Why:  each window must fire once per day, so "which window" alone is not
#       enough to remember what has been settled. Re-login and verify have
#       different start times, so one key space covers both.
WindowKey = Tuple[date, time]


class Outcome(str, Enum):
    # Tech: the five things a live window can end in.
    # Why:  named rather than boolean because the caller alerts differently on
    #       each, and "failed once, will retry" is a materially different state
    #       from "the window closed on us" even though both mean we have no new
    #       token this second.
    RELOGGED = "relogged"          # fresh token in hand
    FAILED = "failed"              # attempt raised; another is scheduled
    ABANDONED = "abandoned"        # the re-login window closed with no token
    VERIFIED = "verified"          # the health check answered; session is good
    VERIFY_FAILED = "verify_failed"  # check failed and recovery failed too


def session_open_anchors(
    relogin_lead: timedelta = RELOGIN_LEAD,
    verify_lead: timedelta = VERIFY_LEAD,
) -> Tuple[Tuple[time, time, time], ...]:
    # Tech: turn every TXF session open into a `(relogin_at, verify_at, open_at)`
    #       triple, the first two being `relogin_lead` / `verify_lead` before it.
    # Why:  derived from `environment.DAY_OPEN` / `NIGHT_OPEN` rather than
    #       written out, so the two places that define a session edge cannot
    #       drift — and so the day-session port inherits the same schedule with
    #       no code change (§2.24). The triple is what makes the two windows
    #       *sequential*: re-login ends exactly where verify begins, so a retry
    #       can never still be in flight when the health check runs.
    today = date(2000, 1, 1)  # any date; only the time-of-day arithmetic matters
    out = []
    for open_time in (DAY_OPEN, NIGHT_OPEN):
        at_open = datetime.combine(today, open_time)
        out.append(((at_open - relogin_lead).time(),
                    (at_open - verify_lead).time(),
                    open_time))
    return tuple(sorted(out))


@dataclass
class ReloginScheduler:
    # Tech: `relogin` performs the actual venue call and raises on failure;
    #       everything else is policy. No `shioaji` import anywhere in here.
    # Why:  same seam as `FeedWatch` — the CLI owns the API object, so the CLI
    #       supplies the action. That keeps every branch below reachable from a
    #       test with no network, and keeps this module honest about being a
    #       scheduler rather than a client.
    relogin: Callable[[], None]
    # Tech: an authenticated call that raises when the session is not usable.
    # Why:  the check has to exercise the thing that actually broke. The soak
    #       died on `list_positions` returning 401 while the quote feed was still
    #       delivering — so a feed-side check would have passed. Injected rather
    #       than named here for the same reason `relogin` is: the CLI owns the
    #       API object, and this module must stay importable without `shioaji`.
    #       `None` disables the verify window entirely.
    verify: Optional[Callable[[], Any]] = None
    anchors: Tuple[Tuple[time, time, time], ...] = field(
        default_factory=session_open_anchors)
    recheck_grace: timedelta = RECHECK_GRACE
    base_backoff_seconds: float = 60.0
    max_backoff_seconds: float = 900.0
    recorder: Any = None

    def __post_init__(self) -> None:
        self._last_login: Optional[datetime] = None
        self._done: set = set()                     # windows already settled
        self._pending: Optional[WindowKey] = None   # re-login being retried
        self._attempts = 0
        self._next_attempt: Optional[datetime] = None
        # Tech: a verify window whose recovery re-login has happened and whose
        #       re-check is still owed, with the moment it becomes due.
        # Why:  the window is no longer settled by the recovery itself (§2.6) —
        #       a login that succeeds says nothing about the feed it was supposed
        #       to repair. Holding the key here is what keeps the window open for
        #       the one thing that does say something.
        self._recheck: Optional[WindowKey] = None
        self._recheck_at: Optional[datetime] = None
        self.n_relogins = 0
        self.n_failures = 0
        self.n_abandoned = 0
        self.n_verified = 0
        self.n_verify_failures = 0

    def note_login(self, now: datetime) -> None:
        # Tech: record when a token was issued.
        # Why:  the CLI's startup login must report itself here, or token age is
        #       measured from nothing. A successful re-login calls it too, which
        #       is what makes the spacing between anchors the token's real age.
        self._last_login = now

    def token_age(self, now: datetime) -> Optional[timedelta]:
        # Tech: how long ago the current token was issued.
        # Why:  exposed so the caller can print it at shutdown and so the
        #       week-long property test can assert the 24 h invariant directly
        #       rather than inferring it from re-login counts.
        if self._last_login is None:
            return None
        return now - self._last_login

    def in_window(self, now: datetime) -> bool:
        # Tech: whether `now` falls in a re-login or verify window.
        # Why:  a caller with its own session repair (the owner's lost-session
        #       watch) stands aside while this scheduler is already logging in,
        #       so the two never stack logins inside one window.
        return self._live_window(now) is not None

    def check(self, now: datetime) -> Optional[Outcome]:
        # Tech: attempt a re-login if an anchor window is live; return the
        #       outcome, or None when there is nothing to do.
        # Why:  called once a second from the CLI's clock block. Returning None
        #       for the overwhelmingly common case keeps the caller's branch
        #       trivial and means a quiet day produces no records at all.
        if self._last_login is None:
            return None

        window = self._live_window(now)

        # Tech: a pending re-login whose window no longer contains us is given up.
        # Why:  found by test — without this the retry state (`_attempts`, the
        #       backoff deadline) survives into the *next* window, which would
        #       then inherit a spent budget and a deadline set yesterday. Every
        #       window must end settled, including the ones that end badly.
        if self._pending is not None and (
                window is None or window[1] != self._pending):
            attempts = self._attempts
            key = self._pending
            self.n_abandoned += 1
            self._settle(key, "relogin_abandoned", now, attempts=attempts)
            return Outcome.ABANDONED

        # Tech: a re-check still owed when its window closes is a failure.
        # Why:  the session is about to open and the last thing we know about it
        #       is that its health check failed. Letting the window lapse quietly
        #       would restore exactly the silence this fix removes.
        if self._recheck is not None and (
                window is None or window[1] != self._recheck):
            key = self._recheck
            self.n_verify_failures += 1
            self._settle(key, "verify_recheck_lapsed", now)
            return Outcome.VERIFY_FAILED

        if window is None:
            return None
        kind, key = window
        if key in self._done:
            return None

        if kind == "verify":
            return self._run_verify(key, now)

        # Tech: hold off until the backoff deadline when a retry is in flight.
        # Why:  the deadline is the whole point of the backoff — without this
        #       test the 1 Hz caller would retry a failing login once a second,
        #       which is precisely the login storm §2.8b warns extends a
        #       rate-limit suspension.
        if self._next_attempt is not None and now < self._next_attempt:
            return None

        return self._attempt(key, now)

    # ---- internals ---------------------------------------------------------

    def _live_window(self, now: datetime
                     ) -> Optional[Tuple[str, WindowKey]]:
        # Tech: which window `now` falls in — "relogin" or "verify" — with its
        #       key, or None outside every window.
        # Why:  the two are adjacent and non-overlapping by construction, so a
        #       retry can never be in flight when the health check runs. Bounding
        #       both at the session open is what guarantees every venue call here
        #       happens while the tape carries only trial prints; a window missed
        #       wholesale is skipped rather than fired late onto a live bar.
        t = now.time()
        for relogin_at, verify_at, open_at in self.anchors:
            if relogin_at <= t < verify_at:
                return "relogin", (now.date(), relogin_at)
            if verify_at <= t < open_at:
                return "verify", (now.date(), verify_at)
        return None

    def _run_verify(self, key: WindowKey, now: datetime) -> Optional[Outcome]:
        # Tech: call the health check once; on failure take a single recovery
        #       re-login attempt, and settle the window either way.
        # Why:  this is the last look before real prints, so it gets one shot at
        #       each — there is no room for a backoff ladder inside a minute, and
        #       a check that is still retrying when the session opens has failed
        #       at its only job. One recovery attempt is worth taking because we
        #       are still in the quiet and the alternative is trading on a
        #       session we have just proven is broken.
        if self.verify is None:
            return None

        # Tech: a re-check owed from an earlier tick in this window waits out its
        #       grace, then decides the window by itself.
        # Why:  MEASURED (§2.6). The old code settled on the *recovery re-login*
        #       succeeding, so a session with a live token and a dead feed
        #       reported RELOGGED/INFO and the run bled to a staleness halt
        #       sixteen minutes later — the FATAL this window exists to raise
        #       never fired. A login proves a login; only the check proves the
        #       session.
        if self._recheck == key:
            if self._recheck_at is not None and now < self._recheck_at:
                return None
            try:
                self.verify()
            except Exception as exc:                  # noqa: BLE001
                self.n_verify_failures += 1
                self._settle(key, "verify_failed_after_recovery", now,
                             error=repr(exc))
                return Outcome.VERIFY_FAILED
            self.n_verified += 1
            self._settle(key, "verified_after_recovery", now)
            return Outcome.VERIFIED

        try:
            self.verify()
        except Exception as exc:                      # noqa: BLE001 — see Why
            # Tech: any exception means "cannot confirm the session is usable".
            # Why:  same reasoning as the re-login catch — a `TokenError`, a
            #       timeout and an unfamiliar SDK error all mean the same thing
            #       here, and letting one escape would end a run over a check
            #       that exists to protect it.
            self._record("verify_failed", when=now.isoformat(),
                         anchor=key[1].isoformat(), error=repr(exc))
            try:
                self.relogin()
            except Exception as exc2:                 # noqa: BLE001
                self.n_verify_failures += 1
                self._settle(key, "verify_recovery_failed", now,
                             error=repr(exc2))
                return Outcome.VERIFY_FAILED
            # Tech: count the login, re-anchor token age, and arm the re-check
            #       — deliberately *without* settling the window.
            # Why:  the token really is fresh, so the counters and the age clock
            #       must reflect it. What is not established is that the session
            #       works, and the window stays open until the re-check says so.
            self.n_relogins += 1
            self.note_login(now)
            self._recheck = key
            self._recheck_at = now + self.recheck_grace
            self._record("relogin_after_failed_verify", when=now.isoformat(),
                         anchor=key[1].isoformat(),
                         recheck_at=self._recheck_at.isoformat())
            return Outcome.RELOGGED

        self.n_verified += 1
        age = self.token_age(now)
        self._settle(key, "verified", now,
                     token_age_seconds=None if age is None else age.total_seconds())
        return Outcome.VERIFIED

    def _attempt(self, key: WindowKey, now: datetime) -> Outcome:
        # Tech: run the injected call; on success settle the anchor, on failure
        #       schedule the next attempt inside the same window.
        # Why:  the two exits map onto `Outcome` one-to-one, so the caller's
        #       alerting cannot disagree with what actually happened here.
        self._pending = key
        self._attempts += 1
        try:
            self.relogin()
        except Exception as exc:                      # noqa: BLE001 — see Why
            # Tech: any exception is a failed attempt, whatever its type.
            # Why:  a re-login can fail as `TokenError`, a transport timeout, or
            #       something the SDK has not shown us yet — and the response is
            #       the same for all of them: back off and try again inside this
            #       window. Narrowing the catch here would let an unfamiliar
            #       error escape into the CLI's crash handler and end a run that
            #       still holds a perfectly valid token.
            self.n_failures += 1
            backoff = min(
                self.base_backoff_seconds * (2 ** (self._attempts - 1)),
                self.max_backoff_seconds,
            )
            self._next_attempt = now + timedelta(seconds=backoff)
            self._record("relogin_failed", when=now.isoformat(),
                         anchor=key[1].isoformat(), attempt=self._attempts,
                         backoff_seconds=backoff,
                         next_attempt=self._next_attempt.isoformat(),
                         error=repr(exc))
            return Outcome.FAILED

        self.n_relogins += 1
        attempts = self._attempts
        self.note_login(now)
        self._settle(key, "relogin", now, attempts=attempts)
        return Outcome.RELOGGED

    def _settle(self, key: WindowKey, event: str, now: datetime,
                **fields: Any) -> None:
        # Tech: mark a window finished — however it finished — and clear every
        #       piece of retry state, then record.
        # Why:  one exit path for success, abandonment and every verify ending
        #       alike. Anything that leaves `_pending` or `_next_attempt` set
        #       would carry retry state into the next window, where it would
        #       either suppress a needed attempt or spend a budget already spent.
        self._done.add(key)
        self._pending = None
        self._next_attempt = None
        self._attempts = 0
        self._recheck = None
        self._recheck_at = None
        self._record(event, when=now.isoformat(), anchor=key[1].isoformat(),
                     **fields)

    def _record(self, event: str, **fields: Any) -> None:
        if self.recorder is None:
            return
        self.recorder.record(event, **fields)

    def summary(self) -> Dict[str, int]:
        # Tech: the counters worth printing at shutdown.
        # Why:  `relogins` is the direct evidence the §2.24 fix ran at all — a
        #       soak that crossed 24 h with zero of them proves the token
        #       lifetime hypothesis wrong rather than the fix right.
        return {
            "relogins": self.n_relogins,
            "relogin_failures": self.n_failures,
            "relogin_abandoned": self.n_abandoned,
            "session_verified": self.n_verified,
            "session_verify_failures": self.n_verify_failures,
        }
