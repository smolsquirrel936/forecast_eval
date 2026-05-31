# [data/loader.py](../../data/loader.py)

**Role:** Tick-CSV loaders that produce the canonical `[timestamp, price, volume]` frame.
**Pipeline:** raw CSV → **loader** → frame for [Environment](../environment.md).
**Depends on:** `pandas`   **Used by:** real-data entry points (and ad-hoc loading)

## Responsibility
Reads two on-disk tick formats and normalizes both to the single schema the
`Environment` expects, sorted chronologically.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `load_rpt_ticks(path, contract=None, nrows=None)` | function | RPT layout (`trading_date, contract, time, price, quantity`) |
| `load_ticks(path, nrows=None)` | function | generic layout (already `timestamp, price, volume`) |

## Key points / gotchas
- `load_rpt_ticks` forces `trading_date` / `time` / `contract` to **strings** —
  pandas would otherwise strip the leading zeros the fixed-width `HHMMSSff` parse
  depends on, and pads the time to 8 chars before parsing.
- `quantity` is renamed to `volume` so downstream code sees one tick schema
  regardless of source.
- Both loaders sort by timestamp — `Environment`'s entire model assumes time order.
- The RPT file's Big5 Chinese headers are expected to be **pre-renamed to English**
  by the user's preprocessing (not part of this folder); see [CLAUDE.md](../../CLAUDE.md).

## Related
[environment.py](../environment.md) · [real_data_demo.py](../real_data_demo.md) · [SPEC §8](../../SPEC.md)
