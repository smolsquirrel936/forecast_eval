# [live/roll.py](../../live/roll.py)

**Role:** Notice that the front month moved, and hand back the contract that replaced it.
**Pipeline:** [cli.py](cli.md) owns the API object and drives this from two triggers.
**Depends on:** [contracts.py](contracts.md) for `contract_month` — no `shioaji` import   **Used by:** [live/cli.py](cli.md), [tests/test_live_roll.py](../tests.md)

## Responsibility
`TXFR1` is a **continuous alias**: it names whichever month is nearest, and
[resolve_txf](contracts.md) dereferences it **once**, at start-up. The object it
returns is what the run holds for its whole life — every `subscribe`, every
order, every reconciliation. A process that outlives a settlement therefore keeps
talking about a contract the venue has retired.

A soak proved it (`outputs/live_20260817_134515`, §2.6):

```
13:44:59  TXFH6's final print ever
15:00:00  feed_check MISSED — the venue is trading September, we are not
   ...     15 resubscribes, 15 minutes, 0 ticks
15:15:00  HALT: no market data for 900.2s
```

**The repair could not work, and that is the finding.** [FeedWatch](feedwatch.md)
re-subscribed the same expired object fifteen times. Meanwhile `api.snapshots()`
on that *same object* kept answering with live prints — volume climbing 575 →
1 796 through the new session. So the two calls resolve differently:

| Call | Binds to | Follows a roll? |
|---|---|---|
| `subscribe(contract, …)` | the month the alias resolved to at start-up (`target_code`) | **no** |
| `snapshots([contract])` | the alias, re-resolved at call time | yes |

One object, two resolutions. Nothing short of re-reading the alias and
subscribing the **new** object repairs it.

## Two mechanisms, and only one of them can roll a position
| | `RollSchedule` — the plan | `RollWatch` — the net |
|---|---|---|
| Driven by | the calendar | the alias's `delivery_month` |
| Fires | 15:30 the day before the last trading day | *after* the old month expired |
| Position | **closed on the old month, re-opened on the new** | settled — the venue already cash-settled it |
| Exists because | you cannot trade an expired contract | the plan can be missed: a restart, a holiday, a process that was down |

The alias only flips once the old month is dead, so a roll driven by it can never
trade the position — it can only notice, settle, and re-open. That is why the
schedule exists, and why the watcher stays behind it.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `RollWatch(resolve, month, refresh=None, min_recheck=5min, recorder=None)` | class | both venue actions injected as callables |
| `check(now, reason="scheduled")` | method | re-read the alias; returns a `Roll` or `None` |
| `summary()` | method | `roll_checks` / `rolls` / `roll_check_errors` |
| `Roll` | dataclass | `contract`, `note`, `from_month`, `to_month` |
| `RollSchedule(month, recorder=None)` | class | the calendar half; `due` / `lapsed` / `done` |
| `last_trading_day(yyyymm)` | function | the third Wednesday |
| `roll_at(yyyymm)` | function | 15:30 the day before it |
| `front_month_at(ts)` | function | which month a timestamp belongs to — **the splice rule** |
| `MIN_RECHECK` / `ROLL_AT` / `ROLL_WINDOW` | constants | 5 min · 15:30 · 1 hour |

## When the scheduled roll fires, and why there
For a NIGHT strategy the expiring month's **final night session starts 15:00 the
day before its last trading day** — the 15:00 session on the last trading day
itself already belongs to the next month. So the roll runs at **15:30 that
evening**, in the first half hour of that session:

- it is the most liquid stretch either contract sees, and the incoming month is
  thin — the roll wants the busiest moment available, not the calmest;
- it is ~13 h clear of the moment the contract stops trading, so a roll that
  fails has the whole session behind it;
- 04:00 would also satisfy "an hour before the close" and is the **worst** hour of
  the tape: 8–12 lot bars on both contracts at once.

The window stays open an hour, because the roll is two orders that both have to
fill and the runtime advances one step per bar.

## No holiday table, on purpose
`last_trading_day` computes the naive third Wednesday. Both ways it can be wrong
are survivable, which is why no table is carried:

| If a holiday moves the real last trading day… | …then |
|---|---|
| **later** | we roll early. Harmless — we trade the next month sooner |
| **earlier** | we roll late, and `RollWatch` catches the expiry, which is the path that already existed |

A table would replace two survivable errors with one silent dependency that has
to be maintained for ever. What *is* checked is the tape: before rolling, the CLI
snapshots both months and requires the incoming one to be printing. The gate is
"is the month we are about to trade alive", **not** "has volume crossed over" —
at the roll instant the expiring month usually still has the larger volume, so a
crossover test would refuse every correct roll. Both volumes are recorded either
way, which is how the calendar gets validated from data rather than from
argument.

## The sequence
Every transition is driven by something *observed*, never by a timer — a roll
whose close does not fill stays visible as `closing` rather than advancing into a
switch that would strand the old contract's lot.

```
schedule.due()      → rt.request_roll()
  next bar          → submit_exit on the OLD month          state = closing
  close fills       → roll_fills += that fill               state = switch
  CLI               → _apply_roll(): unsubscribe, swap, switch_contract, subscribe
                    → rt.resume_roll()                      state = reopening
  next bar          → entry on the NEW month
  entry fills       → roll_fills += that fill               state = None
```

`roll_fills` is what `build_trades(roll_fills=…)` reads to book the two legs as
**one** round-trip — otherwise a median 4.5-day hold would be split into two
shorter trades and the trade count inflated, and per-trade metrics would stop
being comparable with the backtest, where a held position simply crosses a
splice. The set is written to the trail at shutdown so a session can be re-scored
months later.

The strategy's own signal can land inside the roll window — stride 30 makes it
uncommon, not impossible. If it opens a position before the re-entry fires, that
position is the one kept (it is a live decision; this is bookkeeping) and
`roll_resume_skipped` is recorded, because a silent skip would leave no trace of
a roll that never restored what it closed.

## The two triggers
Neither is a timer of its own — both hang off something the CLI already does:

- **after a successful re-login**, which is already in the pre-open quiet
  (08:40 / 14:55, see [relogin](relogin.md)) and is the moment the SDK's contract
  table is most likely to have been refreshed;
- **on a FeedWatch `MISSED`** — the reactive path, and the one that would have
  saved the dead soak, which produced that verdict fifteen times.

`MISSED` repeats once a minute and a roll happens eight times a year, so
`min_recheck` throttles the venue read behind the second trigger. Five minutes
still notices a roll inside the pre-open quiet, which is the only window where
noticing early is worth anything.

## Design notes
- **`refresh` is separate from `resolve`, and optional.** Whether a
  `force_refresh` re-login re-downloads the contract table is **unmeasured** —
  the SDK is a compiled `_core.pyd` and cannot be read. `cli.py` passes
  `api.fetch_contracts`, so the answer does not depend on that assumption; a
  refresh that raises is recorded and the re-read happens anyway, because a stale
  table reads as "no roll", which is the safe direction.
- **A backwards month is ignored, not followed.** A roll goes one way; a month that
  moves backwards means the table or the resolver is wrong, and following it
  would re-subscribe something already expired — the exact state this module
  exists to leave.
- **Nothing raises out of `check`.** It runs inside a liveness path, so a
  resolver that fails is recorded, counted and retried at the next trigger rather
  than ending a run that is otherwise healthy. `roll_check_errors` is what
  separates "the front month never moved" from "we could not tell".
- **The roll is adopted on the way out.** `check` updates `month` before
  returning, so the caller is handed each roll exactly once — it re-subscribes and
  re-points the broker on every `Roll` it receives, and a repeat would do that
  twice.
- **Alerting and acting are the caller's job**, the same seam
  [FeedWatch](feedwatch.md) and [relogin](relogin.md) use, and for the same
  reason: no `shioaji` import here means every branch is reachable from a test
  with no network.

## What the caller has to move
`cli.py:_apply_roll` is the one place, and it moves **four** holders together —
the four that held the expired object at once:

```
api.unsubscribe(old)          # best effort; the month may already be gone
contract = rolled.contract    # the name every closure reads
live_broker.switch_contract() # broker contract + accepted code set
_subscribe(drop_first=False)  # the new object, on a fresh subscription
ticks["at_relogin"] = …       # re-baseline the verify window's tick counter
```

Then the position: [runtime](runtime.md)`.settle_rolled_leg()` closes the
expiring leg in both mirrors and arms the same exposure for re-entry on the first
bar of the new month. The alias moves only *after* the old month is dead, so the
venue has already cash-settled anything held — there is no order that could close
it, and the settlement price is recorded as an estimate because the true one
comes from an auction this process never sees.

## The ladder — how a leg actually gets filled
A roll leg that does not fill is the only failure here that matters: missing the
close means holding an expiring contract into a cash settlement whose price we do
not control. So the price escalates until it goes through.

| attempt | 1 | 2 | 3 | 4 | 5 | 6+ |
|---|---|---|---|---|---|---|
| ticks through the touch | 5 | 10 | 15 | 20 | 25 | 25 |

- **Equal 5-tick steps**, not doubling — every rung's cost is knowable in advance.
- **Capped at 25.** §2.19 measured that the venue matches on *prints*: in a minute
  with no trade, no price fills. Past a point more aggression buys nothing while
  giving up the bound that makes this a limit order and not an `MKT`. Beyond the
  cap the ladder keeps retrying *at* the cap.
- **IOC, not ROD.** The two want opposite things: a FLATTEN wants to rest until
  the next print, a roll leg wants to expire so it can be re-priced. IOC leaves
  nothing resting, so re-pricing needs no cancel and §2.2's asynchronous
  cancel/replace race never happens — **the retry is the re-price.** This is only
  safe *because* there is a retry; §2.19 chose ROD for the flatten precisely
  because nothing retried it there.
- **10-second interval, not 5.** The order-rate rail caps submissions at 10/minute
  and a breach is a **STOP halt** — halted mid-roll while still holding the
  expiring contract is the worst state this can reach. 10 s is 6/min for one leg,
  inside the cap with room for the other. It is also longer than the broker's
  `IOC_TTL_SECONDS`, so attempt N+1 can never race attempt N's expiry.
- **Driven by the clock, not by bars.** A bar is a minute wide and the whole point
  is to retry inside one. Submitting off the bar path is safe: fill *collection*
  stays the loop's, so the single-consumer invariant is untouched.
- **Each leg starts from rung 1.** The close's escalation says nothing about the
  open's, and starting the second leg at the cap would pay for the first leg's
  problem twice.

### The gate this hangs on, and why it is defensive
The ladder only submits when nothing is pending. §2.19 measured that **an expired
IOC left `has_pending()` True** — if that still holds and the venue never reports
the expiry, every rung is blocked and the ladder is inert while logging
beautifully, the same shape as the plain-`login()` no-op.

Whether the venue reports it is **UNMEASURED**: [ioc_probe.py](ioc_probe.md)
answers it and needs a live session. So the broker does not depend on the answer —
an IOC unresolved after `IOC_TTL_SECONDS` (3 s) stops counting as pending on our
side. It is **retired, not forgotten**: it stays tracked so a fill arriving after
our deadline is still booked, exactly as `cancel()` does, because the venue may
simply be slower than we are. If the probe later shows the venue does report, this
becomes a backstop and nothing about the ladder changes.

## The splice follows the same instant
`front_month_at` is what [backfill](backfill.md) collapses the bar cache with, and
it is deliberately the *same* rule the live stream switches on. If the two
differed, the warm-up context and the tape would disagree for a day and a half
every month and nothing would detect it.

**MEASURED 2026-08-23**, which is why the rule is a calendar and not volume: all
four RPT tapes — including `TXF_RPT_minute_night.csv`, the one the pinned config
is evaluated on — carry **exactly one contract per minute**, so
`load_minute_bars`'s highest-volume collapse never chose anything. Volume was
never the convention; a calendar is.

**The tape adopted *this* calendar on 2026-09-01** (`TAPE_REBUILD_PLAN.md` T2).
It used to splice one day *after* each expiry; it now splices at
`roll_at(month, policy)` — the instant the deployment actually rolls. Measured on
the night tape, over the same 112 roll seams, that made the seam markedly
smaller:

| | median \|jump\| at a roll seam | in points | median ordinary minute |
|---|---|---|---|
| pre-rebuild (one day after expiry) | 0.7267 % | ~116 | 0.0096 % |
| **rebuilt (`roll_at`)** | **0.2533 %** | **~40** | 0.0096 % |

Splicing *before* expiry rather than after lands the seam where the two delivery
months are closer, so the evaluated series is now not only aligned with live but
discontinuous by about a third as much.

Live's seam is smaller than the evaluated one — the switch happens mid-session, so
the jump is pure basis rather than basis plus a whole day session's move — and it
lands ~1.5 days earlier, which is unavoidable: an expired contract cannot be
traded, and the marks must sit in the same contract as the position.

## Status — built, not demonstrated
Everything on this page is covered by offline tests (52 in
`tests/test_live_roll.py`) and **none of it has executed against a real roll.**
The last soak died *because* this did not exist; the first live exercise is:

| | |
|---|---|
| Next last trading day | **2026-09-16** (third Wednesday) |
| The roll therefore fires | **2026-09-15 15:30** |

Two things to watch on that night, in the trail: `roll_evidence` (both months'
volumes, which is how the naive third-Wednesday calendar gets validated from
data) and `roll_ladder_attempt` (which rung actually filled — the first real
measurement of whether 5 ticks is enough, or whether the cap wants raising).

One assumption inside the ladder is still ours rather than the venue's: whether
an expired IOC is reported at all. [ioc_probe.py](ioc_probe.md) settles it and
needs only an open session — running it is cheaper than learning the answer
during a roll.

## Related
[live/cli.py](cli.md) · [live/contracts.py](contracts.md) · [live/backfill.py](backfill.md) · [live/feedwatch.py](feedwatch.md) · [live/runtime.py](runtime.md) · [SHIOAJI_PLAN.md §2.6](../../SHIOAJI_PLAN.md)
