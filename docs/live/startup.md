# [live/startup.py](../../live/startup.py)

**Role:** The rails that must survive the process **dying**.
**Pipeline:** entry point → **RestartGuard.begin()** → login + backfill → `heartbeat` → `end()`.
**Depends on:** [live/sessions.py](sessions.md)   **Used by:** the live entry point (V3)

## Responsibility
[SafetyRails](state.md) protects a *running* loop and is deliberately pure.
These rails protect against the process being **restarted**, so they persist to a
file.

The failure mode: every process start performs a `kbars` backfill of the full
context, so an unsupervised crash-restart loop becomes a login + historical-query
loop. Shioaji answers repeated rate-limit violations with *"IP and ID access
suspended"*, and repeated `login()` during a suspension **extends the penalty**.
The outcome is not a slow system — it is a banned account that fights its own
recovery.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `RestartBlocked` | exception | a rail refused to let this process start |
| `RestartPolicy(state_file, ...)` | dataclass | the tunables |
| `RestartGuard(policy)` | class | all state lives in the file, not the instance |
| `begin(now)` | method | the single startup gate; **raises** rather than returning a status |
| `record_backfill(now)` | method | call *before* querying `kbars` |
| `heartbeat(now)` | method | stamp liveness (once per bar is ample) |
| `end(now)` | method | orderly shutdown; clears the failure count |
| `check_usage(usage)` | method | traffic headroom; duck-typed on `remaining_bytes` |
| `trading_day(now)` | staticmethod | delegates to [sessions.py](sessions.md) |

## The four rails
- **Exponential restart backoff**, keyed on consecutive *unclean* exits. A clean
  `end()` costs nothing, so operator restarts are instant.
- **Per-trading-day backfill cap** (default 10). The day rolls at **08:00**,
  matching Shioaji's own quota reset, so the cap and the limit it protects share
  one boundary.
- **`api.usage()` headroom check.** Over quota Shioaji returns **empty** history
  rather than erroring, which would silently yield an empty model context —
  refusing up front converts that into a clear stop.
- **No `login()` retry** — deliberately *absent* from the API. There is nothing
  here to reach for; a failed login must surface and stop. A test asserts no such
  method exists.

## Key points / gotchas
- **Failure scoring is latched (`scored`): once per dead run, not once per
  `begin()` call.** The first implementation charged a failure on every attempt,
  so each *rejected* restart doubled the wait it was already serving — a
  supervisor retrying on a fixed timer could never get back in. The penalty must
  track dead runs, not rejected attempts.
- **A crash after a healthy run** (`healthy_runtime_seconds`, default 600 s)
  resets the ladder to a single failure. One crash after eight good hours is not
  the fourth crash in two minutes.
- **A corrupt state file is charged as a failure**, not treated as a clean slate —
  otherwise a crash loop that damages the file bypasses the backoff entirely.
- The backoff is measured from the previous run's **last sign of life**, not from
  `now`, so repeatedly invoking `begin()` cannot compound or reset it.
- **Measured cost** for context: ~57 bytes/bar, so a 3 008-bar backfill is ~170 KB
  against a 500 MB daily quota. The cap is about runaway loops, not bandwidth.

## Related
[live/state.py](state.md) · [live/backfill.py](backfill.md) · [live/sessions.py](sessions.md) · [tests/test_live_startup.py](../tests.md) · [SHIOAJI_PLAN.md §2.8b](../../SHIOAJI_PLAN.md)
