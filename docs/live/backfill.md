# [live/backfill.py](../../live/backfill.py)

**Role:** Turns Shioaji `kbars` history into the bar frame the model was evaluated on.
**Pipeline:** `api.kbars` → **kbars_to_bars** → `LiveLoop` history buffers (warm-up).
**Depends on:** [live/sessions.py](sessions.md), [live/bars.py](bars.md) (`BAR_LENGTH`)   **Used by:** the live entry point (V3), [live/v1c_check.py](v1c_check.md)

## Responsibility
Warm-up and live trading draw bars from **different sources**: live bars come
from ticks via [bars.py](bars.md); warm-up bars come from `api.kbars`. Both must
land on the identical convention, or the model's context at every process start
differs from everything it was evaluated on — and after a restart the *first*
forecasts all come from this path.

Output is schema-identical to `real_data.load_minute_bars`.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `kbars_to_bars(kbars, *, drop_zero_volume=True, policy=None, source_label="right")` | function | pure conversion; `policy=None` keeps every bar |
| `fetch_bars(api, contract, start, end, *, policy, now=None)` | function | one `kbars` call + conversion + in-progress-bar truncation; **`policy` is required** |

## Three corrections `kbars` needs (all measured against the live API)
- **It is RIGHT-labelled** (2026-08-10). A day session returns `08:46 … 13:45`
  where RPT returns `08:45 … 13:44` — the same 300 intervals, stamped one minute
  later. `source_label="right"` (the default) subtracts a minute. The shift
  renames bars; it moves no prices.
- **It carries both sessions** (00:00–23:59), while the evaluated file is one
  session only. Corrected by passing a [SessionPolicy](sessions.md).
- **It returns the in-progress minute as a finished bar** (2026-08-11). A fetch
  at 04:11:29 came back with a final bar covering only 04:11:00–04:11:29, and
  [bars.py](bars.md) then emitted its *own* bar for that same minute — so one
  timestamp entered the model context twice, each copy a different fragment.
  `fetch_bars` keeps only bars whose minute has fully elapsed.

Empty minutes *are* omitted, matching RPT — no correction needed.

## Key invariants / gotchas
- **The shift runs *before* session filtering, and that ordering is
  load-bearing.** DAY is `08:45 <= t < 13:45`, so an unshifted `13:45` bar — the
  session's genuine last minute — classifies as `None` and would be silently
  dropped, costing one real bar every day while the other 299 were each
  misattributed by a minute. The night session has the symmetric case at 05:00.
- **`fetch_bars` has no session default, deliberately.** It previously defaulted
  to `("DAY",)` while the strategy was tuned on NIGHT — a mismatch nothing could
  detect. Requiring the caller to state the session makes that unrepresentable.
- **Zero-volume rows are dropped.** The RPT file provably contains none, so any
  filler `kbars` returns is not an observation the model ever saw.
- **Truncation compares against the bar's right edge, not a floored `now`.** Bar
  `T` is finished exactly when `T + 1min <= now`, so at 04:12:00 the 04:11 bar is
  kept. Flooring would discard a complete bar whenever a fetch landed on a minute
  edge — swapping one silent defect for another. `now` is injectable so the
  boundary is pinned in tests instead of raced against the clock.
- **Callers must charge the request to [RestartGuard](startup.md) first**
  (`record_backfill`) and check headroom (`check_usage`) — over quota Shioaji
  returns *empty* history rather than erroring, so an unchecked call silently
  yields an empty context.
- One request returns ~9 900 bars for a 10-day span (measured), so **no paging is
  needed** for a 3 008-bar context.

## Related
[live/sessions.py](sessions.md) · [live/bars.py](bars.md) · [live/startup.py](startup.md) · [live/v1c_check.py](v1c_check.md) · [real_data.py](../real_data.md)
