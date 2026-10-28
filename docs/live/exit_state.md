# [live/exit_state.py](../../live/exit_state.py)

**Role:** keep a stateful exit rule's per-trade state on disk, so `--resume` can continue a held lot instead of refusing it.
**Pipeline:** [loop.py](loop.md) Step 2 → `ExitStateFile.update` → `outputs/exit_state_<slug>.json` → next start: [cli.py](cli.md) → [runtime.py](runtime.md) `adopt_position` → `resolve` → `ExitRule.restore_state`.
**Depends on:** nothing in `live/` — JSON on disk and the rule's own `export_state`.   **Used by:** [live/cli.py](cli.md), [live/runtime.py](runtime.md)

> Built 2026-10-29. A network drop stopped every deployment while fullday held
> a long, and `--resume` refused it: its break-even lock tracks the best price
> since entry, and that number lived only in the dead process. The account's
> netting (day short 1 against fullday long 1) also meant
> [flatten.py](flatten.md) saw a flat account and could not close fullday's lot
> on its own.

## Responsibility
`adopt_position` recovers a held lot's side and average entry from the venue,
and nothing else. A rule that reads only those is `adoptable`
([exits/base.py](../exits/base.md)). A rule with per-trade state is not, and
used to be refused outright. A rule that declares itself **`restorable`**
names its state (`state_keys`) and round-trips it through `export_state` /
`restore_state`. This module stores that state while live and gives it back at
the next resume.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `ExitStateFile(path, *, rule, slug, contract, soak_id, recorder)` | class | the live writer; `update(position_or_None, market)` once per bar |
| `resolve(*, keys, side, entry_price, path, injected)` | function | `(state, "injected" | "file")`, or `ValueError` with the `--exit-state` form to type |
| `load(path)` | function | the record, `None` when absent, `ValueError` when torn |
| `parse_injection(items)` | function | `["best_price=47376"]` → `{"best_price": 47376.0}` |

## The file
`outputs/exit_state_<slug>.json`, beside `live_restart_<slug>.json`:

```json
{"slug": "day_night", "side": "LONG", "entry_price": 46616.0,
 "state": {"best_price": 47376.0},
 "bar_timestamp": "2026-10-28 15:23:00", "written_at": "...",
 "contract": "TXFK6", "soak_id": "soak_..."}
```

- **Written on change only**, after the bar's exit check, so it includes that
  bar's price. A best price moves only on a new extreme, so this is a handful
  of writes per trade.
- **Atomic:** a temp file, then `os.replace`. A kill mid-write leaves the old
  record or the new one.
- **Deleted on the first flat bar**, and on a flat start. A file on disk always
  describes a lot that was open when it was written.
- **A failed write is recorded (`exit_state_write_failed`), never raised.** The
  loop holds a live position, and a full disk must not stop it trading.
- Attached only when the configured rule is `restorable`. No other deployment
  writes one.

## How `--resume` chooses the state
| Situation | Result |
|---|---|
| `--exit-state KEY=VALUE` given, keys exactly the rule's | used, **overriding the file** (`exit_state_source: "injected"`) |
| `--exit-state` with missing or extra keys | refused (exit 5) |
| No `--exit-state`; the file matches the adopted side and entry, with the rule's keys | used (`"file"`) |
| No file | refused, naming the `--exit-state` form to type |
| File for another lot (other side, or entry off by more than 1e-6) | refused: a crash between an entry fill and its first write, or a roll that moved the venue's average entry |
| File that does not parse | refused, not read as absent |
| `--exit-state` with a rule that is not restorable | refused |
| `--exit-state` without `--resume` | refused before the restart guard |
| Flat account | nothing adopted; a typed state raises an `exit_state_unused` warning |

The state is resolved **before** the mirrors are seeded, so a refusal leaves
the runtime untouched. It is restored against the adopted snapshot's identity
(entry bar `-1`, the venue's entry, side), which is the key a stateful rule
checks on its first bar. Under any other key, the rule would reset itself there.

## Related
[exits/base.py](../exits/base.md) · [runtime.py](runtime.md) · [loop.py](loop.md) · [cli.py](cli.md) · [simtrade_operations §5, §6.2](../simtrade_operations.md)
