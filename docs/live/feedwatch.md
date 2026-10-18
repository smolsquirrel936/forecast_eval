# [live/feedwatch.py](../../live/feedwatch.py)

**Role:** Tell a quiet market from a broken feed, and repair the broken one.
**Pipeline:** [cli.py](cli.md) owns the subscription and drives this from its clock tick.
**Depends on:** nothing — callables only, no `shioaji` import   **Used by:** [live/cli.py](cli.md), [tests/test_live_feedwatch.py](../tests.md)

## Responsibility
Silence is ambiguous, and a 23-hour soak died on that ambiguity (§2.21): the
night session went **151 s** between prints at 02:35, the 120 s watchdog called
it a dead feed, and the loop stopped **31 s before the market traded again**.

The old rail had to guess because it had one input — how long we had heard
nothing. This adds a second: **ask the venue.**

```
silence for `interval`
   └─ snapshot
        ├─ venue's last trade is one we already have  → QUIET.  do nothing.
        ├─ venue traded since our newest print        → MISSED. unsub + resub.
        └─ the query itself fails                     → UNREACHABLE. back off;
                                                         re-login after 3 in a row.
```

The threshold stops being a decision and becomes a **polling interval**. Getting
it wrong now costs one ~40 ms query instead of the run — which is why it can be
aggressive (60 s) where the old halt window had to be timid.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `FeedWatch(snapshot, resubscribe, interval=60.0, max_resubscribes=3, recorder=None, relogin=None, relogin_after=3, relogin_offset=0)` | class | every action injected as a callable; `relogin=None` leaves UNREACHABLE a warning only |
| `note_tick(exchange_ts, now)` | method | record the newest genuine print, on both clocks |
| `check(now)` | method | run a check if one is due; returns the `Verdict` or `None` |
| `summary()` | method | the counters the CLI prints and records |
| `Verdict` | enum | `QUIET` / `MISSED` / `UNREACHABLE` |

## Two layers, not one
| Layer | Window | Does |
|---|---|---|
| **FeedWatch** | 60 s | asks, and heals. **Never halts.** |
| [SafetyRails](state.md) staleness | 900 s live | halts if the healing did not work |

Halt semantics are untouched by design. A recoverable condition and a
human-attention condition are different things, and merging them is what made a
transient gap terminal.

## Key invariants / gotchas
- **A QUIET verdict must never resubscribe.** Subscribing on top of a live
  subscription risks a **doubled tick stream**, and duplicate prints inflate bar
  volume — silently breaking the bar equality V1 proved. The repair is strictly
  unsubscribe-then-subscribe, and only on positive evidence.
- **`MISS_MARGIN` is 1 s, and it is measured.** Across 9 live samples
  `snapshot.ts` equalled our newest tick exactly 8 times and was 0.8 s *behind*
  once — never ahead. A margin below that jitter would resubscribe on healthy
  noise.
- **`Snapshot.ts` is an `int` of epoch nanoseconds, not a datetime.** MEASURED
  the expensive way (2026-08-13): comparing it against a tick's `datetime` raises
  `TypeError` and killed a live soak 42 bars in. Everything goes through `_stamp`
  now, so the comparison cannot be reached with mixed types.
- **An implausible date is refused, not trusted.** `pd.Timestamp` of an integer
  reads it as *nanoseconds*; if the SDK ever sends seconds or milliseconds, the
  result is a **1970** date, which is older than our newest tick and would make
  every check answer QUIET — permanently and silently disarming the rail. Worse
  than the crash it replaced, so anything before `SANE_AFTER` is UNREACHABLE.
- **Both clocks are kept, and they are not interchangeable.** Silence is measured
  on the **wall** clock (no event means no event timestamp); the comparison
  against `snapshot.ts` is **exchange** clock on both sides. Mixing them folds
  the ~0.6–1.1 s delivery lag into the comparison and manufactures misses.
- **A failed query is never a miss.** It is evidence about us, not the market.
  Resubscribing there acts on no information; halting there hands an API blip the
  power to end a soak.
- **A snapshot with no `ts` is UNREACHABLE, not QUIET.** Defaulting to quiet on a
  malformed answer would disarm the check permanently, and the failure would look
  like a healthy market.
- **The repair is capped per silence** (3). A stream that will not come back
  returns MISSED forever, and unbounded unsubscribe/subscribe is a submit storm.
  After the budget it stops and lets the backstop halt — which is correct, since
  a feed that will not heal needs a human. Any print refills the budget.
- **A query that keeps raising means the session is gone, and gets a re-login.**
  MEASURED 2026-10-15: the session died at 09:52 with no `session_down`, every
  snapshot raised `SessionNotEstablished`, and the SDK had not reconnected 5 h
  later. Re-subscribing cannot fix that, and UNREACHABLE only warned, so day and
  fullday halted on the backstop at 10:08. Now `relogin_after` (3) consecutive
  *raised* queries call `relogin` — in the CLI, the anchors' `force_refresh`
  path, which healed night at 14:55 that day. The count restarts after each
  attempt, so a dead session costs one login per 3 checks: 4 attempts at 3, 6, 9
  and 12 min before the 900 s backstop. A malformed answer, a QUIET or MISSED
  verdict, or any print restarts the count, since each proves the session
  answered. A failing re-login is recorded (`feed_relogin_failed`), never raised.
  This is the one login that happens in session; the anchors' "not on a live
  bar" rule protects bars that, here, are not arriving.
- **The first attempt is staggered per deployment** (`relogin_offset`, the
  deployment's `relogin.stagger_slot`: day 0, night 1, fullday 2). A venue-wide
  drop reaches all three on the same clock, so equal counts would refresh them in
  one second, up to 7 connections against the cap of 5. Attempts land at checks
  3/6/9/12, 4/7/10/13 and 5/8/11/14: always a minute apart, four each before the
  backstop. A new outage is staggered again.
- **It never resets the silence marker.** The rails read the same silence, and a
  marker this reset would disarm the halt that catches an unhealable feed.
- **Only genuine prints count.** A simtrade tick is not evidence the market
  traded (§2.5), so counting it would let trial-matching keep a dead feed looking
  alive.
- **The CLI only calls `check` inside a session it trades.** Probing through the
  05:00–15:00 gap would spend ~600 queries a day to confirm the market is closed.
- **`snapshot` is given a 5 s timeout, not the SDK's 30 s default.** It runs on
  the consumer thread; the default would stall tick processing for half a minute
  exactly when the feed is already suspect.

## `MISSED` is also what a roll looks like
A retired contract produces exactly this verdict — the venue trades, we receive
nothing — and the repair here cannot fix it: re-subscribing an expired object is
inert (measured, 15 attempts for 0 ticks, §2.6). So `MISSED` is also the reactive
trigger for [RollWatch](roll.md), which re-reads the alias and hands back the
month that replaced it. The repair and the roll check are complementary: one
fixes a dropped subscription, the other fixes a subscription to something that no
longer exists.

## Related
[live/cli.py](cli.md) · [live/state.py](state.md) · [live/bars.py](bars.md) · [SHIOAJI_PLAN.md §2.21–§2.22](../../SHIOAJI_PLAN.md)
