# [live/bars.py](../../live/bars.py)

**Role:** Aggregates live trade prints into the 1-min bars the model expects.
**Pipeline:** Shioaji tick callback → **TickAggregator** → `MarketEvent` → [LiveLoop](loop.md).
**Depends on:** [events.py](../events.md), [live/sessions.py](sessions.md)   **Used by:** the live entry point (V3)

## Responsibility
The backtest is fed `TXF_RPT_minute_*.csv` via `real_data.load_minute_bars`; live
delivers raw ticks. This rebuilds the *same* bars from the tick stream, so the
model sees the input distribution it was evaluated on.

Emits the same `MarketEvent` shape [environment.py](../environment.md) does —
including session classification — so the live stream and an offline replay hand
`LiveLoop` identical sequences.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `TickAggregator(*, drop_simtrade=True, drop_non_session=True, policy=None, flush_grace=0)` | class | `policy` defaults to both sessions; `flush_grace` to zero |
| `add_tick(timestamp, price, volume, *, simtrade=False)` | method | returns 0 or 1 completed bars |
| `flush(now)` | method | closes a bar whose minute has ended (plus the grace), on the wall clock |
| `LIVE_FLUSH_GRACE` | constant | 2 s — what `LiveRuntime` passes; see below |
| `n_ticks` / `n_simtrade_dropped` / `n_late_dropped` / `n_bars_emitted` / `n_bars_dropped_off_session` | attribute | ingest counters |

## The RPT convention (measured, not assumed)
- **1 minute, left-labelled** — bar `T` covers `[T, T+1min)`. A day session
  (08:45–13:45) yields bars 08:45 … 13:44, exactly 300.
- `price` is the bar's **close**.
- **A minute with no trades produces no bar at all.** The RPT producer omits empty
  minutes rather than zero- or forward-filling them (194 of 3 756 day-session days
  carry fewer than 300 bars; none has volume 0).

## Live-only concerns the file cannot express
- **`simtrade`** — TAIFEX broadcasts trial-matching (試撮) prints before the open
  and Shioaji flags them. They are not trades; dropped at ingest so they can never
  reach a bar (SHIOAJI_PLAN §2.5).
- **Late prints** — a tick belonging to an already-emitted bar is **dropped, not
  back-applied**. The loop has traded on that bar; silently rewriting it would put
  the live series permanently out of step with an offline replay. The guard is
  `bucket <= _last_emitted`, kept *separately* from the open bucket because
  `_close()` clears `_bucket_start` — without that memory a straggler reopens a
  minute already emitted (§2.16; measured live as 20 bars for 15 minutes).
- **The two-clock race.** Bar boundaries come from *exchange* timestamps while
  `flush` fires on the *local* clock, so a tick stamped `:59.9` can still be in
  flight when the edge passes. `flush_grace` (2 s live, 0 offline) waits it out.
  It applies to `flush` **only**: once a tick for minute M+1 arrives, everything
  for M has arrived — delivery is in order (measured: 1 106 ticks, zero
  out-of-order) — so `add_tick` closes immediately and loses nothing.

## Key points / gotchas
- **`add_tick` only emits when time moves past a bucket**, so a quiet minute holds
  a completed bar hostage until the next trade — that is what `flush(now)` is for.
  It is the one place the live path may consult the local clock.
- **Several skipped minutes still emit only one bar**: the intervening minutes had
  no trades, and the convention omits them.
- **`policy` narrows further than `drop_non_session`.** The latter mirrors
  `Environment` (drop off-hours); the former drops sessions this deployment does
  not trade. A night-only deployment must not let day bars reach the model —
  they would be a regime it was never evaluated on, and the stride's bar counts
  would be wrong too.
- Dropped bars are **counted, not silently lost** — the counters are what the V1
  parity diff and the live health checks read.

## Related
[live/sessions.py](sessions.md) · [live/loop.py](loop.md) · [environment.py](../environment.md) · [real_data.py](../real_data.md) · [tests/test_live_bars.py](../tests.md)
