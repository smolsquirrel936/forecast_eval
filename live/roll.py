"""RollWatch — notice that the front month moved, and hand back the new contract.

A soak died on this (2026-08-19, §2.6). `TXFR1` is a *continuous alias*: it names
whichever month is nearest, and `resolve_txf` dereferences it **once**, at
start-up. The object that comes back is the one the run holds for its whole life
— every `subscribe`, every order, every reconciliation. So a process that outlives
a settlement keeps talking about a contract the venue has retired:

```
13:44:59  TXFH6's final print ever
15:00:00  feed_check MISSED — the venue is trading September, we are not
   ...     15 resubscribes, 15 minutes, 0 ticks
15:15:00  HALT: no market data for 900.2s
```

**The repair could not work, and that is the finding.** FeedWatch re-subscribed
the same expired object fifteen times. Meanwhile `api.snapshots()` on that *same
object* kept answering with live prints — because `subscribe` binds to the month
the alias resolved to at start-up (`target_code`), while `snapshots` re-resolves
the alias at call time. One object, two resolutions, and only one of them follows
the roll. Nothing short of re-reading the alias and subscribing the **new** object
repairs it.

Hence this module: re-read, compare `delivery_month`, and say so. It decides
nothing about what the caller then does with the position — [cli.py](cli.py) owns
that, the same seam [FeedWatch](feedwatch.py) and [relogin](relogin.py) use, and
for the same reason: no `shioaji` import may appear here, so every branch is
reachable from a test with no network.

**When to ask.** Two triggers, both cheap and neither on a timer of its own:

  * after a successful re-login, which is already in the pre-open quiet, and is
    also the moment the SDK's contract table is most likely to have been
    refreshed;
  * on a FeedWatch `MISSED` — the reactive path. That is the verdict the dead
    soak produced fifteen times, and it is the one that would have saved it.

A minimum interval throttles the venue call behind the re-read, because `MISSED`
can repeat once a minute and a roll cannot.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Callable, Optional, Tuple

from .contracts import contract_month

# Tech: the shortest gap between two re-reads of the alias.
# Why:  the reactive trigger is a once-a-minute verdict and the thing it looks
#       for happens eight times a year, so an unthrottled check would spend a
#       venue round trip a minute to re-learn what it already knows. Five minutes
#       still notices a roll inside the pre-open quiet, which is the only window
#       where noticing early has any value.
MIN_RECHECK = timedelta(minutes=5)


@dataclass(frozen=True)
class Roll:
    # Tech: what a detected roll consists of — the new contract object, the
    #       description `resolve` produced for it, and both months.
    # Why:  the caller needs the object (to subscribe and to trade), and the audit
    #       trail needs the two months side by side. A roll recorded as "rolled"
    #       without saying from what to what cannot be checked against the venue's
    #       calendar afterwards, which is the only way to tell a genuine roll from
    #       a resolver bug that returned the wrong month.
    contract: Any
    note: str
    from_month: str
    to_month: str


@dataclass
class RollWatch:
    # Tech: `resolve` re-reads the alias and returns `(contract, description)`;
    #       `refresh` optionally refreshes the SDK's contract table first.
    # Why:  both are injected for the reason `ReloginScheduler`'s actions are —
    #       the CLI owns the API object. `refresh` is separate and optional
    #       because whether a re-login already re-downloads the contract table is
    #       **unmeasured**: the SDK is a compiled `_core.pyd` and cannot be read.
    #       Splitting it means the roll works whether or not it does, and the
    #       record says which path produced the answer.
    resolve: Callable[[], Tuple[Any, str]]
    month: str
    refresh: Optional[Callable[[], None]] = None
    min_recheck: timedelta = MIN_RECHECK
    recorder: Any = None

    def __post_init__(self) -> None:
        self._last_check: Optional[datetime] = None
        self.n_checks = 0
        self.n_rolls = 0
        self.n_errors = 0

    def check(self, now: datetime, *, reason: str = "scheduled") -> Optional[Roll]:
        # Tech: re-read the alias and return a `Roll` when its delivery month
        #       differs from the one held; `None` when nothing moved, when the
        #       throttle is closed, or when the read failed.
        # Why:  returning `None` for every uninteresting case keeps the caller's
        #       branch to one `if`, and makes a quiet month produce no records at
        #       all. A failed read is *not* an exception out of here: this runs
        #       inside a liveness path, and a resolver that raises must not end a
        #       run that is otherwise healthy — it is recorded and retried at the
        #       next trigger.
        if self._last_check is not None and now - self._last_check < self.min_recheck:
            return None
        self._last_check = now
        self.n_checks += 1

        if self.refresh is not None:
            try:
                self.refresh()
            except Exception as exc:                   # noqa: BLE001 — see Why
                # Tech: a failed refresh is recorded and then ignored.
                # Why:  the refresh is an optimisation — it makes the table more
                #       likely to be current. The re-read below is the real test,
                #       and it is worth doing against a possibly-stale table: a
                #       stale answer looks exactly like "no roll", which is the
                #       safe direction.
                self._record("roll_refresh_failed", when=now.isoformat(),
                             error=repr(exc))

        try:
            contract, note = self.resolve()
        except Exception as exc:                       # noqa: BLE001 — see Why
            self.n_errors += 1
            self._record("roll_check_failed", when=now.isoformat(),
                         reason=reason, error=repr(exc))
            return None

        found = contract_month(contract)
        if not found or found == self.month:
            return None

        # Tech: accept only a *forward* move, and adopt the new month as held.
        # Why:  a roll goes one way. A backwards month means the resolver or the
        #       contract table is wrong, and following it would re-subscribe to
        #       something already expired — the exact state this module exists to
        #       leave. Recording it rather than acting on it keeps a bad table
        #       from being laundered into a trade.
        if found < self.month:
            self.n_errors += 1
            self._record("roll_backwards_ignored", when=now.isoformat(),
                         held=self.month, found=found, note=note)
            return None

        roll = Roll(contract=contract, note=note,
                    from_month=self.month, to_month=found)
        self.month = found
        self.n_rolls += 1
        self._record("contract_rolled", when=now.isoformat(), reason=reason,
                     from_month=roll.from_month, to_month=roll.to_month,
                     note=note)
        return roll

    def _record(self, event: str, **fields: Any) -> None:
        if self.recorder is None:
            return
        self.recorder.record(event, **fields)

    def summary(self) -> dict:
        # Tech: the counters worth printing at shutdown.
        # Why:  `rolls` is the direct evidence this ran at all, and `check_errors`
        #       separates "the front month never moved" from "we could not tell" —
        #       two readings a bare zero cannot distinguish.
        return {
            "roll_checks": self.n_checks,
            "rolls": self.n_rolls,
            "roll_check_errors": self.n_errors,
        }


# ---- the scheduled roll (§2.6) ---------------------------------------------
#
# `RollWatch` above is the net: it fires *after* the venue has retired a month,
# which is the only thing the alias can tell us. The schedule below is the plan —
# it moves while both months still trade, so the position can actually be rolled
# rather than settled.

# Tech: how long before the expiring contract's last trading day the roll runs,
#       and at what time of day.
# Why:  MEASURED. For a NIGHT strategy the expiring month's final night session
#       starts 15:00 on the day *before* its last trading day, and its first half
#       hour is the most liquid stretch either month sees — the incoming month is
#       thin, so the roll wants the busiest moment available, not the calmest.
#       04:00 would also satisfy "an hour before the close" and is the worst hour
#       of the tape: 8–12 lot bars on both contracts at once.
ROLL_LEAD_DAYS = 1
ROLL_AT = time(15, 30)

# Tech: how long the roll window stays open after `ROLL_AT`.
# Why:  the roll is two orders that must both fill, and a limit that does not
#       fill needs somewhere to be retried. An hour is ~13 h clear of the moment
#       the contract stops trading, so a failed roll still has the whole session
#       behind it — and if the window closes unrolled, `RollWatch` catches the
#       expiry as it always did.
ROLL_WINDOW = timedelta(hours=1)

# Tech: when an expiring month stops trading on its last trading day.
# Why:  TAIFEX closes the expiring TXF at 13:30, a quarter hour before the rest
#       of the day session; from then on the exchange's front month — what
#       `TXFR1` names — is the next one.
EXPIRY_CLOSE = time(13, 30)


def exchange_front_month(ts: Any) -> str:
    """The month the EXCHANGE calls front at `ts` — `TXFR1`'s month, not ours.

    Not `front_month_at`: that is the run's roll schedule, which moves a day
    early. Between our roll and the expiry the run holds a month the exchange
    still treats as the back one, and this is how to tell that window apart.
    """
    this_month = f"{ts.year}{ts.month:02d}"
    expiry = datetime.combine(last_trading_day(this_month), EXPIRY_CLOSE)
    if datetime(ts.year, ts.month, ts.day, ts.hour, ts.minute) < expiry:
        return this_month
    return _next_month(this_month)


def last_trading_day(yyyymm: str) -> date:
    """The third Wednesday of a delivery month — TAIFEX's last trading day."""
    # Tech: enumerate the month's Wednesdays and take the third.
    # Why:  computed rather than tabulated so a new year needs no maintenance.
    #       **Holidays are deliberately not modelled** — see `roll_at`.
    y, m = int(yyyymm[:4]), int(yyyymm[4:6])
    weds = [d for d in range(1, calendar.monthrange(y, m)[1] + 1)
            if date(y, m, d).weekday() == calendar.WEDNESDAY]
    return date(y, m, weds[2])


def roll_at(yyyymm: str, policy: Any = None) -> datetime:
    """The instant the run switches off delivery month `yyyymm`.

    `policy` supplies the time of day; without one the module default applies,
    which is NIGHT's 15:30."""
    # Tech: `ROLL_AT` on the day `ROLL_LEAD_DAYS` before the last trading day.
    # Why:  both failure modes of the naive third Wednesday are survivable, which
    #       is why no holiday table is carried (§2.6). If a holiday pushes the
    #       real last trading day *later*, this rolls early — harmless, we simply
    #       trade the next month sooner. If it lands earlier, this rolls late and
    #       `RollWatch` catches the expiry, which is exactly the path that
    #       already exists. A holiday table would replace two survivable errors
    #       with one silent dependency that has to be maintained for ever.
    at = ROLL_AT if policy is None else policy.roll_at_time()
    return datetime.combine(last_trading_day(yyyymm), at) - timedelta(
        days=ROLL_LEAD_DAYS)


def _next_month(yyyymm: str) -> str:
    # Tech: the delivery month after `yyyymm`.
    # Why:  one place for the December wrap, which three call sites had inline.
    y, m = int(yyyymm[:4]), int(yyyymm[4:6])
    return f"{y + 1}01" if m == 12 else f"{y}{m + 1:02d}"


def roll_calendar(first_month: str, last_month: str, policy: Any = None):
    """Every roll instant from `first_month` to `last_month`, ascending.

    Returns `(boundaries, months)` where `months[i]` is the delivery month in
    force *until* `boundaries[i]`, and `months[-1]` after the last one. This is
    the whole splice rule expressed once, as data — `front_month_at` reads it
    for one timestamp and `front_month_series` for millions.

    **TAPE_REBUILD_PLAN T0.** The tape builder and `front_month_at` used to be
    two rules that were supposed to agree and did not (§0.3: the tape spliced
    1 d 8 h 30 m after this one, every month). Two rules cannot diverge if there
    is only one, so the notebook and the live path now both come here.
    """
    months = [first_month]
    while months[-1] < last_month:
        months.append(_next_month(months[-1]))
    return [roll_at(m, policy) for m in months], months


def front_month_at(ts: Any, policy: Any) -> str:
    """Which delivery month a timestamp belongs to, under the roll schedule.

    A timestamp belongs to its own calendar month until that month's
    `roll_at`, and to the following month afterwards.

    **`policy` is REQUIRED — there is no default.** It used to default to None,
    meaning the module's `ROLL_AT` (NIGHT's 15:30), on the argument that
    threading a policy through would move a *pinned* deployment's context as a
    side effect of a day-session decision. That protected NIGHT and broke DAY,
    and nothing could detect it: `live/cli.py` passed this function to the
    warm-up backfill **unbound**, so a DAY run rolled its position at 12:45
    (`RollSchedule`, policy-keyed and correct) while splicing its history at
    15:30. MEASURED 2026-09-01 by `tools/roll_parity.py` — 183 of 183 DAY rolls
    that traded, the 60 minutes 12:45–13:44 of each, `|Δclose|` median 27 points
    and p95 183 (TAPE_REBUILD_PLAN §5.3).

    The default is gone rather than merely corrected at that one call site,
    because "the caller must say which deployment is asking" is the same repair
    `fetch_bars` already made for the same class of bug: it once defaulted to
    `("DAY",)` while the strategy was tuned on NIGHT, and nothing could detect
    that either. Requiring the argument makes the mistake unrepresentable
    instead of absent. Passing `None` still means the module default — the point
    is that a caller has to *say so*.

    §0.3's divergence (this rule flipping 1 d 8 h 30 m before the tape spliced)
    is not fixed by changing this function — it is fixed by the tape adopting
    it. **The tape adopted it on 2026-09-01** (TAPE_REBUILD_PLAN T2): all three
    session tapes are now `front_month_series(ts, policy)` applied to the
    per-contract intermediate, so there is one rule and it is this one. See
    `roll_calendar` and `tools/build_session_tapes.py`.
    """
    # Why:  the bar cache and the live stream must switch on the *same* rule, or
    #       the warm-up context and the tape disagree for a day and a half every
    #       month in a way nothing would detect. MEASURED 2026-08-23: the
    #       evaluated series carries exactly one contract per minute and switches
    #       on the calendar, so `load_minute_bars`'s highest-volume collapse
    #       never chose anything there — volume is not the convention the model
    #       was evaluated on, and a calendar rule is. (Which calendar instant was
    #       the defect: the tape's was one day *after* expiry until T2 rebuilt it
    #       on this function.)
    ts = datetime(ts.year, ts.month, ts.day, ts.hour, ts.minute)
    this_month = f"{ts.year}{ts.month:02d}"
    if ts < roll_at(this_month, policy):
        return this_month
    return _next_month(this_month)


def front_month_series(timestamps: Any, policy: Any):
    """`front_month_at` for a whole column, as a numpy array of month strings.

    Same rule, same answers — asserted against the scalar form over every minute
    of the tape's span. Vectorised because the tape builder applies it to
    288 943 166 tick rows, where a per-row Python call is not an option.

    `policy` is required here for the same reason it is required above, and it
    matters more: this is the form the **tape builder** calls, so a defaulted
    argument here would splice a deployment's evaluated series on another
    deployment's instant.
    """
    # Tech: the front month is the first one whose roll instant is still ahead,
    #       so a right-side `searchsorted` into the ascending roll calendar is
    #       the whole computation.
    # Why:  equivalent to the scalar branch above — a timestamp in month m is
    #       before `roll_at(m)` exactly when `searchsorted` places it at m's
    #       index, and `roll_at(m-1)` already lies in the previous calendar
    #       month so it can never be the answer.
    import numpy as np
    import pandas as pd

    ts = pd.DatetimeIndex(pd.to_datetime(timestamps)).floor("min")
    if len(ts) == 0:
        return np.array([], dtype=object)
    first = f"{ts.min().year}{ts.min().month:02d}"
    last = _next_month(f"{ts.max().year}{ts.max().month:02d}")
    bounds, months = roll_calendar(first, last, policy)
    idx = np.searchsorted(np.array(bounds, dtype="datetime64[ns]"),
                          ts.to_numpy(), side="right")
    return np.asarray(months, dtype=object)[idx]


@dataclass
class RollSchedule:
    # Tech: the calendar half of the roll — when to move, and whether we already
    #       have for the month being held.
    # Why:  separate from `RollWatch` because the two answer different questions.
    #       The watcher asks "did the venue move without us?"; this asks "is now
    #       the moment we planned to move?". Keeping them apart is what lets the
    #       plan be tested against a clock with no venue at all.
    month: str
    recorder: Any = None
    # Tech: the deployment's policy, which supplies `ROLL_AT`.
    # Why:  C4 — 15:30 is outside a DAY session, so a schedule with no policy
    #       would run a DAY deployment's roll against a tape it is not
    #       following. `None` keeps the module default (NIGHT's 15:30).
    policy: Any = None

    def __post_init__(self) -> None:
        self._rolled: set = set()

    def due(self, now: datetime) -> bool:
        # Tech: true inside `[roll_at(month), roll_at(month) + ROLL_WINDOW)`,
        #       once per delivery month.
        # Why:  a window rather than an instant because the roll is two orders
        #       that have to fill, and the caller re-asks on every clock tick
        #       until they do. `_rolled` is what stops a completed roll from
        #       firing again for the rest of the window.
        if self.month in self._rolled:
            return False
        start = roll_at(self.month, self.policy)
        return start <= now < start + ROLL_WINDOW

    def lapsed(self, now: datetime) -> bool:
        # Tech: true once the window has closed on an unrolled month.
        # Why:  worth an alert rather than silence — the plan did not happen and
        #       the run is now relying on `RollWatch` catching the expiry, which
        #       settles the position instead of trading it.
        return (self.month not in self._rolled
                and now >= roll_at(self.month, self.policy) + ROLL_WINDOW)

    def done(self, month: str, to_month: str, now: datetime) -> None:
        # Tech: mark the month rolled and adopt the new one.
        # Why:  called by the CLI only after both legs are through, so a roll that
        #       half-completed is retried inside the window rather than recorded
        #       as finished.
        self._rolled.add(month)
        self.month = to_month
        if self.recorder is not None:
            self.recorder.record("roll_scheduled_done", when=now.isoformat(),
                                 from_month=month, to_month=to_month)
