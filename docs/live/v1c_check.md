# [live/v1c_check.py](../../live/v1c_check.py)

**Role:** One-shot read-only probe: does `api.kbars` match the bar convention the model was evaluated on?
**Run:** `uv run --no-sync python -m forecast_eval.live.v1c_check --date <past trading date>`
**Depends on:** [live/backfill.py](backfill.md), [real_data.py](../real_data.md)   **Used by:** humans, before V3

## Responsibility
Every process start backfills its whole context from `kbars`, so if that source
labels bars differently, fills empty minutes, or resolves a different contract,
the model's input is wrong at exactly the moment it is least observable — the
first forecasts after every restart. [bars.py](bars.md) has its own validation;
nothing else validated this path.

**Places no orders and subscribes to nothing.** Simulation login only, always
logs out (connections are capped at 5 per `person_id`).

## What it answers
| § | Question | Result (2026-08-10) |
|---|---|---|
| 1 | **Span** — ≥3 008 bars in one request, or capped? | 9 902 bars / 10 days → no paging needed |
| 1b | **Traffic cost** — brackets the fetch with `api.usage()` | 561 468 bytes for 9 902 bars ≈ 57 B/bar |
| 2 | **Label** — left or right edge? | **RIGHT** (`08:46 … 13:45`) → shift applied in [backfill.py](backfill.md) |
| 3 | **Empty minutes** — omitted or zero-volume filler? | omitted, matching RPT |
| 4 | **Content** — closes vs the RPT file | **not achievable**, see below |

## Key points / gotchas
- **The label section deliberately passes `source_label="left"`** — i.e. *no*
  shift. It exists to *measure* labelling, so it must see raw stamps; applying
  the corrective shift here would make a right-labelled feed report itself as
  left-labelled and permanently hide the thing being checked.
- **Its verdicts are inverted**: RIGHT is now the expected result, and LEFT would
  mean the API changed and the shift in `backfill.py` has become wrong.
- **`--list-contracts`** dumps every TXF contract with its `delivery_month`. Use
  it rather than guessing codes — TXF month letters are **plain alphabetical**
  (A=Jan … L=Dec), *not* the CME `F/G/H/J/K/M/N/Q/U/V/X/Z` convention, so `H` is
  August.
- **§4 cannot be satisfied with this tool.** Expired contracts are delisted, and
  every date the RPT file covers belongs to one. The content check moves into the
  V3 dry run instead: capture a session's ticks, aggregate with
  [TickAggregator](bars.md), and diff against `kbars` for the *same* session —
  cross-validating the two live sources rather than a historical artefact.
- Contract resolution catches **`KeyError`** as well as `AttributeError`: the
  Contracts container implements `__getattr__` as a dict lookup, so
  `getattr(txf, name, default)` does **not** protect you.

## Related
[live/backfill.py](backfill.md) · [live/bars.py](bars.md) · [live/v72_probe.py](v72_probe.md) · [SHIOAJI_PLAN.md §2.8](../../SHIOAJI_PLAN.md)
