# [live/sessions.py](../../live/sessions.py)

**Role:** The single value naming which trading sessions a deployment trades, and where its trading day begins.
**Pipeline:** `SessionPolicy` → **backfill · bars · state · startup** (every session-dependent decision).
**Depends on:** [environment.py](../environment.md) (`classify_session`)   **Used by:** [live/backfill.py](backfill.md), [live/bars.py](bars.md), [live/state.py](state.md), [live/startup.py](startup.md)

## Responsibility
Owns *which sessions we trade* and *when one trading day ends*. Nothing else in
`live/` names a session — filtering, "is this silence expected?", and daily-loss
anchoring all read this object.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `SessionPolicy(sessions, trading_day_start=08:00)` | frozen dataclass | the policy itself; validates at construction |
| `SessionPolicy.night()` / `.day()` / `.both()` / `.of([...])` | classmethod | named constructors |
| `label(ts)` | method | `"DAY"` / `"NIGHT"` / `None`, via `classify_session` |
| `includes(ts)` | method | is this timestamp in a session we trade, on a day the market opens? |
| `is_trading_day(ts)` | method | weekday, and not a listed holiday — tested on the *trading* day |
| `holidays` | field | dates closed despite being a weekday. Empty by default |
| `trading_day(ts)` | method | ISO date of the trading day it belongs to |
| `same_session_instance(a, b)` | method | same *occurrence* of the same session? |
| `trading_day(ts, start)` | function | module-level helper for callers with no policy |

## Why it exists
This module is a direct response to a real defect. [live/backfill.py](backfill.md)
shipped with `sessions=("DAY",)` as a default while the strategy in
`results.tsv` had been tuned on **NIGHT** — a mismatch nothing in the code could
detect, which would have loaded the wrong session's history into the model's
context on every process start. See [SHIOAJI_PLAN.md §2.12](../../SHIOAJI_PLAN.md).

The rule that followed: **no module has a session default.** `fetch_bars`
*requires* a policy; the aggregator and rails take one explicitly.

## Trading day ≠ calendar day
The boundary is a configurable hour, default **08:00**, chosen to sit in the dead
zone before either TXF session opens:

- a NIGHT session (15:00 → 05:00 next date) stays **one** trading day, because
  05:00 falls before the boundary and maps back to the date it started;
- a DAY session (08:45 → 13:45) sits wholly inside its own date;
- it coincides with Shioaji's 08:00 daily traffic reset, so quota accounting
  ([live/startup.py](startup.md)) and risk accounting ([live/state.py](state.md))
  share one calendar.

A midnight boundary would cut a night session in half and hand the second half a
fresh daily loss budget.

## The weekend (§2.25)
`classify_session` reads **time of day and nothing else**, so Saturday 15:00 was
a NIGHT session to every consumer. Measured offline against the real rails, a
soak halted at **Saturday 15:16**:

```
reason : no market data for 960.0s (limit 900.0s)
action : HaltAction.STOP
```

Five consecutive unattended days must contain a weekend, so V5 was unreachable
until this was fixed. `is_trading_day()` is tested inside `includes()`, and that
one site fixes all three consumers at once — the staleness rail stops accruing a
gap against a closed market, [FeedWatch](feedwatch.md) stops spending ~1 680
queries a weekend confirming nothing trades, and the bar filter stops treating
Saturday 15:00 as a session.

- **It tests the *trading* day, not the raw date.** 02:00 on Saturday belongs to
  Friday's night session and must read as trading; a raw `ts.weekday()` would
  halt the loop every Saturday morning mid-session.
- **Re-login is deliberately *not* gated on it.** The token expires 24 h after
  login whether or not the market opens, so a run spanning a weekend must keep
  refreshing through it — see [relogin.md](relogin.md). Only the verify step's
  tick half is gated, because a closed day has no pre-open print to confirm
  against.
- **Residual: a weekday market holiday still halts a soak** — ~10–15 days a year
  against the ~104 this fixes. `holidays` carries them as *data*, empty by
  default: a stale list would halt a run on a day the market was open, which is
  worse than the gap it closes.
- **Make-up trading Saturdays (補班日) are skipped**, costing a session rather
  than breaking anything.

A cleverer design was rejected — feeding [FeedWatch](feedwatch.md)'s QUIET
verdict into the staleness rail would need no calendar at all, since the venue
itself confirms nothing has traded. It was dropped because it depends on how
`snapshots()` behaves across a two-day closure, which has never been observed:
fixing an unmeasured problem with unmeasured behaviour is the trade this project
keeps losing. The weekday test is deterministic and fully testable offline.

## Key points / gotchas
- **`same_session_instance` compares the trading day, not just the label.** 04:59
  tonight and 15:01 tomorrow are *both* `"NIGHT"` but ten hours apart — comparing
  labels alone would make the staleness watchdog treat that gap as a frozen feed
  and halt every morning.
- **Empty or misspelled session sets raise at construction.** An empty policy
  filters every bar away and is indistinguishable from a dead feed.
- Frozen dataclass, so it can be shared between the loop, rails and aggregator
  without any of them mutating it.

## Porting to another session
Change the constructor at the entry point; nothing else:

```python
policy = SessionPolicy.day()      # instead of SessionPolicy.night()
```

`trading_day_start` is a parameter too, so a different market's schedule is also
just an argument.

## Related
[live/backfill.py](backfill.md) · [live/bars.py](bars.md) · [live/state.py](state.md) · [live/startup.py](startup.md) · [environment.py](../environment.md) · [SHIOAJI_PLAN.md §2.12](../../SHIOAJI_PLAN.md)
