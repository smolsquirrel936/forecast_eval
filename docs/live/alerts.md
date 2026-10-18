# [live/alerts.py](../../live/alerts.py)

**Role:** The one door everything that needs a human goes through.
**Pipeline:** [cli.py](cli.md) builds it first and hands it to [state.py](state.md); every terminal path calls it.
**Depends on:** nothing   **Used by:** [live/cli.py](cli.md), [live/owner_cli.py](owner_cli.md), [live/state.py](state.md), [tests/test_live_alerts.py](../tests.md)

## Responsibility
A five-day unattended soak is the case this exists for. When something goes wrong
at 03:00 it has to leave the machine — and before this, it reached a console
nobody was watching and a JSONL nobody reads until morning.

**One method, four severities.** `fatal` / `warn` / `trade` / `info` funnel into `emit`,
and every alert carries a `severity` and a `kind`. **Sinks subscribe to the levels
they want**, so one Telegram chat can take fatals while another takes everything —
without any call site knowing a channel exists.

| Level | Means | Examples |
|---|---|---|
| `FATAL` | the run is over, or a position is exposed — act now | any rails halt, `flatten_incomplete`, startup refused, an unhandled crash, no credentials |
| `WARN` | handled, but you would want to know | feed repaired, feed unreachable, session down, account fingerprint changed |
| `TRADE` | a fill changed the position | position opened, position closed with its PnL |
| `INFO` | milestones | resumed into a position, the routine re-logins, roll started |

**Trades are their own level, between INFO and WARN.** At 0.37 orders/night a
fill is roughly one message every three days — it cannot cause fatigue, and it is
the one event worth reading on a phone. Nothing has gone wrong, so it is not
WARN. Until 2026-10-18 it was INFO, and a chat that wanted fills had to take the
eight routine re-login notices a day with them; as TRADE, a sink can keep one and
drop the other.

```
[TRADE] position_opened: LONG 1 @ 46200
[TRADE] position_closed: closed @ 46350 — +150 points
```

**A recovery goes out at the level of the problem it ends.** `recovery(kind,
message, resolves=(...))` emits WARN when one of the `resolves` kinds was raised
at WARN or FATAL and not resolved since, and INFO otherwise, then marks them
resolved. Capped at WARN: good news is never "act now", and FATAL would put it in
the fatal journal. Without this, a chat filtered to WARN + FATAL saw
`feed_missed` and never `feed_recovered`. The catalogue of which recovery
resolves what is [notifications.md §6.5](../notifications.md).

The alert fires on the **fill**, not the order: an order can be cancelled
unfilled at the next boundary (§4.4), and a notification for a trade that never
happened is worse than none.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `Alerter(*, recorder=None, fatal_journal=None, console=True)` | class | built-in destinations; transports arrive via `add_sink` |
| `add_sink(fn, *, levels=(FATAL,))` | method | register a transport — **the extension point** |
| `fatal / warn / trade / info (kind, message, **detail)` | method | the four ways in |
| `recovery(kind, message, *, resolves, **detail)` | method | WARN if it ends an open WARN/FATAL of a `resolves` kind, else INFO |
| `emit(severity, kind, message, **detail)` | method | what they all funnel into |
| `summary()` | method | counts per level, plus `alert_sink_failures` |
| `Alert` | dataclass | `severity`, `kind`, `message`, `when`, `detail`; `.format()`, `.as_dict()` |
| `Severity`, `ALL` | enum, tuple | `FATAL` / `WARN` / `TRADE` / `INFO` |

## Telegram
Attached by [telegram.py](telegram.md) with one call, `add_sink(notifier,
levels=ALL)`, and no other change here. It subscribes to every level and does
its own filtering, because its level is a live setting (the `/settings` button)
rather than one fixed at registration.

`kind` is the machine-readable tag to route and group on; `message` is the
sentence a human reads on a phone. Keeping both means a transport never has to
parse prose to decide where something goes.

## Key invariants / gotchas
- **A closing alert carries *this trade's* PnL, not the running total.** The
  Portfolio only exposes a cumulative figure, so the delta is taken across
  `apply_fill` in [loop.py](loop.md). Reading it afterwards would report the sum
  of every trade that night — and the second round trip would inherit the first
  one's result. Mutation-checked.
- **A sink that raises is swallowed and counted.** These calls sit inside halt
  paths and exception handlers; an alert that raised there would replace the real
  diagnosis with a traceback from the notifier — the failure mode where adding
  monitoring makes the incident harder to read.
- **Delivery is best-effort, by decision.** The alert most worth sending is often
  about a network fault, which is exactly when the transport fails. Losing that
  message is acceptable; losing the run to the notifier is not. Everything is
  written to the trail regardless of whether any transport worked.
- **`alert_sink_failures` is not decoration.** A sink that silently stopped
  working is indistinguishable from a quiet night — unless the failures are
  counted. It counts *synchronous* raises only; Telegram sends from a background
  thread, so its failures are `telegram_failed` in its own summary.
- **The fatal journal outlives the run directory.** It sits beside
  `live_restart.json`, not inside `outputs/live_<ts>/`, so "what has ever gone
  badly wrong" is one file rather than a search across every `session.jsonl`.
  Opened per write, because it is called while something is already failing.
- **A new sink defaults to fatals only.** Over-notifying trains you to ignore the
  channel, which is worse than not having it; noisy levels are opt-in.
- **The alerter is built before anything that can fail**, with no recorder — the
  earliest failures (missing credentials, a refused restart) happen before
  `session.jsonl` exists, and those are exactly the ones a soak must hear about.
- **Halts notify from inside the latch** ([state.py](state.md)), so a rail added
  later cannot forget, and a latched halt buzzes **once** rather than once per bar.
- **`alerter=None` is the default everywhere.** V0 parity and every offline test
  are untouched.

## Related
[live/cli.py](cli.md) · [live/state.py](state.md) · [live/feedwatch.py](feedwatch.md) · [live/recorder.py](recorder.md) · [SHIOAJI_PLAN.md §2.23](../../SHIOAJI_PLAN.md)
