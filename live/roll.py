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


def last_trading_day(yyyymm: str) -> date:
    """The third Wednesday of a delivery month — TAIFEX's last trading day."""
    # Tech: enumerate the month's Wednesdays and take the third.
    # Why:  computed rather than tabulated so a new year needs no maintenance.
    #       **Holidays are deliberately not modelled** — see `roll_at`.
    y, m = int(yyyymm[:4]), int(yyyymm[4:6])
    weds = [d for d in range(1, calendar.monthrange(y, m)[1] + 1)
            if date(y, m, d).weekday() == calendar.WEDNESDAY]
    return date(y, m, weds[2])


def roll_at(yyyymm: str) -> datetime:
    """The instant the run switches off delivery month `yyyymm`."""
    # Tech: `ROLL_AT` on the day `ROLL_LEAD_DAYS` before the last trading day.
    # Why:  both failure modes of the naive third Wednesday are survivable, which
    #       is why no holiday table is carried (§2.6). If a holiday pushes the
    #       real last trading day *later*, this rolls early — harmless, we simply
    #       trade the next month sooner. If it lands earlier, this rolls late and
    #       `RollWatch` catches the expiry, which is exactly the path that
    #       already exists. A holiday table would replace two survivable errors
    #       with one silent dependency that has to be maintained for ever.
    return datetime.combine(last_trading_day(yyyymm), ROLL_AT) - timedelta(
        days=ROLL_LEAD_DAYS)


def front_month_at(ts: Any) -> str:
    """Which delivery month a timestamp belongs to, under the roll schedule."""
    # Tech: a timestamp belongs to its own calendar month until that month's
    #       roll instant, and to the following month afterwards.
    # Why:  this is the splice rule the bar cache uses, and it has to be the
    #       *same* rule the live stream switches on — otherwise the warm-up
    #       context and the tape disagree for a day and a half every month, in a
    #       way nothing would detect. MEASURED 2026-08-23: the evaluated series
    #       (`TXF_RPT_minute_night.csv`) carries exactly one contract per minute
    #       and switches on the calendar, so `load_minute_bars`'s highest-volume
    #       collapse never chose anything there — volume is not the convention the
    #       model was evaluated on, and a calendar rule is.
    ts = datetime(ts.year, ts.month, ts.day, ts.hour, ts.minute)
    this_month = f"{ts.year}{ts.month:02d}"
    if ts < roll_at(this_month):
        return this_month
    y, m = (ts.year + 1, 1) if ts.month == 12 else (ts.year, ts.month + 1)
    return f"{y}{m:02d}"


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
        start = roll_at(self.month)
        return start <= now < start + ROLL_WINDOW

    def lapsed(self, now: datetime) -> bool:
        # Tech: true once the window has closed on an unrolled month.
        # Why:  worth an alert rather than silence — the plan did not happen and
        #       the run is now relying on `RollWatch` catching the expiry, which
        #       settles the position instead of trading it.
        return (self.month not in self._rolled
                and now >= roll_at(self.month) + ROLL_WINDOW)

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
