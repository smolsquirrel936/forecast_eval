# [live/state.py](../../live/state.py)

**Role:** The checks that stop a *running* live loop before it does damage.
**Pipeline:** LiveLoop / ShioajiBroker → **SafetyRails** → halt latch → loop stops deciding.
**Depends on:** [live/sessions.py](sessions.md)   **Used by:** [live/loop.py](loop.md), [live/broker/shioaji.py](broker/shioaji.md)

## Responsibility
Every rail here defaults to **stopping**, never to continuing. The failure mode
it exists to prevent is a process that keeps trading on a stale mirror or a
frozen feed — which is how a capped-at-one-contract strategy becomes an unbounded
position.

Pure: in-memory, no I/O, and **it never reads a clock** — all time enters through
explicit `now` arguments, so every branch is deterministically testable. (Rails
that must survive the process *dying* live in [startup.py](startup.md) instead.)

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `HaltAction.STOP` / `.FLATTEN` | enum | the two terminal states |
| `RailsConfig(...)` | dataclass | the tunables; `None` disables an individual rail |
| `SafetyRails(config, *, simulation, policy=None, recorder=None)` | class | `simulation` is required, never defaulted |
| `check_startup(max_position)` | method | raises on misconfiguration |
| `banner()` | method | one-line mode + limits summary |
| `on_bar(market, *, net_pnl)` | method | kill file + daily loss |
| `on_order_submitted(ts)` / `note_data(now)` / `check_staleness(now)` | method | rate limit, feed watchdog |
| `on_reconcile(mirror, venue)` / `on_order_report(op_code, op_msg)` | method | desync, rejection |
| `halted` / `halt_reason` / `halt_action` / `flatten_required` | property | the latch |

## Two halt actions, deliberately distinguished
Flatten is **not** universally the safe response:

- **`FLATTEN`** — close the position, then stop. Correct when the *position* is
  the problem and prices are still trustworthy.
- **`STOP`** — send no orders at all. Correct when the *inputs* are untrustworthy:
  a frozen feed (a limit priced off a stale print is a blind order, *including*
  the closing one), a disputed position mirror ("close the position" is undefined
  when the size is in dispute), a rejection, or a submit storm.

| Rail | Action |
|---|---|
| `max_position > 1` at startup | **raises** — never reaches a bar |
| Daily loss limit | `FLATTEN` |
| Kill file | `FLATTEN` |
| Quote staleness | `STOP` |
| Position-mirror mismatch | `STOP` |
| Order rejection (`op_code != "00"`) | `STOP` |
| Order rate breach | `STOP` |

## Key points / gotchas
- **Every halt raises a fatal alert**, from inside the latch ([alerts.md](alerts.md)).
  A halt is the definition of "needs a human", and notifying from the latch means
  a rail added later cannot forget — and that a latched halt notifies **once**
  rather than once per bar. `alerter=None` is the default, so offline tests and
  V0 parity are untouched.
- **The staleness watchdog is session-aware.** It skips (and refreshes its marker)
  unless the last tick and `now` are in the same *occurrence* of a traded session.
  Without this, a night-only deployment halts on its first morning — the feed is
  quiet 05:00→15:00, ten hours against a 120-second window. `same_session_instance`
  compares the trading day too, because 04:59 tonight and 15:01 tomorrow are both
  `"NIGHT"`.
- **The daily-loss anchor re-bases on `policy.trading_day` change**, driven by the
  bar's exchange timestamp. It previously keyed on *entry into the DAY session*,
  which never happens on a night-only feed — the "daily" limit silently became a
  since-inception limit that tightened every day.
- **Auto-correcting a mirror desync is explicitly not a rail.** It would paper
  over a missed or double-counted fill.
- **First halt reason wins** — later rails firing during shutdown must not
  overwrite the diagnosis. Latching also makes the flatten one-shot rather than
  re-submitted every bar.
- **`simulation` is a required argument**, because `api.simulation` is `None` —
  the SDK does not expose the flag, so mode cannot be verified programmatically.
  The banner and every log line must come from our own config.
- `now` is local wall-clock compared against exchange session hours, so **the host
  must run on exchange local time.**

## Related
[live/sessions.py](sessions.md) · [live/startup.py](startup.md) · [live/loop.py](loop.md) · [tests/test_live_state.py](../tests.md) · [SHIOAJI_PLAN.md §5](../../SHIOAJI_PLAN.md)
