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
| `fetch_bars_continuous(api, contracts, start, end, *, policy, cache_path=None, now=None)` | function | every listed month + the cache, spliced into one front-month series |
| `collapse_front_month(frames, *, front_month_at=None)` | function | one row per minute, from the **calendar's** front month; volume breaks the tie |
| `load_bar_cache(path)` / `save_bar_cache(path, bars, keep=30 000)` | functions | the local tail that survives a delisting |
| `CACHE_KEEP_BARS` | constant | 30 000 — ten contexts' worth |

## Splicing across a roll (§2.6)
`kbars` is fetched **per contract**, and `TXFR1` names only the front month. The
month *behind* it matters at exactly one moment — a warm-up that spans a roll:

- the new front month's own history, before it became front, is thin back-month
  trading, so 3 008 of those bars span far more calendar time than the ~2 days
  the model expects;
- **expired contracts are delisted** (§2.8 Finding D), so the month that just
  settled cannot be fetched from the venue at all.

**The splice rule is a calendar, and that was measured** (2026-08-23, re-verified
2026-09-01). All four RPT tapes — including `TXF_RPT_minute_night.csv`, the file
the pinned config was evaluated on — carry **exactly one contract per minute**, so
the highest-volume collapse never chose anything. Volume was therefore never the
convention.

**Which calendar instant changed on 2026-09-01** (`TAPE_REBUILD_PLAN.md` T2). The
tape used to switch **one day after each expiry** — a rule no deployment could
execute, since the roll must precede expiry to be tradeable. It now switches at
`roll_at(month, policy)`, i.e. at this module's own `front_month_at`. Tape and
live stream are one rule, and the §0.3 divergence of 1 d 8 h 30 m is closed.
It stays as the *tie-break*, because a minute the front month did not print is
better filled from the other month than dropped.

The rule passed in is [roll](roll.md)`.front_month_at` **bound to the running
deployment's policy** (`live/cli.py`), deliberately the same instant the live
stream switches on. It was passed *unbound* until 2026-09-02, which meant NIGHT's
15:30 for every deployment and put a DAY run an hour apart from its own roll
schedule on every roll day — `TAPE_REBUILD_PLAN.md` §5.3, and the reason
`front_month_at` now requires a policy at all. At the roll moment the expiring month
usually still has the larger volume, so a volume rule would keep feeding the
cache a contract we had already stopped trading — the warm-up context and the
tape would disagree for a day and a half every month, and nothing would detect it.

`fetch_bars_continuous` answers both: it merges the local cache, fetches every
contract it is given (a delisted one raises and is skipped, which is expected),
and collapses the result to one row per minute by **highest volume** — which is
`real_data.load_minute_bars`'s own rule, so the spliced warm-up is the same
front-month-continuous series the model was evaluated on, roll gaps and all. The
cache is the only reason the pre-roll side of the splice still exists after the
venue drops it; it is written on every backfill and capped to its tail.

The cache carries a **`contract`** column so the identity of each minute survives
— without it the collapsed output could never be re-derived under a different
rule, and live NIGHT data is the only place the real roll behaviour will ever show
up. A cache written before that column existed still loads, with the column blank.

The cache is an optimisation and never a dependency: a missing, truncated or
malformed file degrades to "what the venue can still serve" rather than stopping
a start-up. It is written by a process that can be killed at any moment.

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
