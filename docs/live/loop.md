# [live/loop.py](../../live/loop.py)

**Role:** The SPEC §4.1 per-tick body, restructured to be **pushed** instead of pulled.
**Pipeline:** TickAggregator → **LiveLoop.on_bar** → Trader / Broker / Portfolio.
**Depends on:** [live/broker/base.py](broker/base.md), [live/recorder.py](recorder.md), [live/state.py](state.md), [trader.py](../trader.md), [portfolio.py](../portfolio.md)   **Used by:** the live entry point (V3)

## Responsibility
[run.py](../run.md)'s `run_backtest` owns a `for market in env.stream()` loop — it
*pulls* events. A live feed pushes them, one callback at a time, and the process
must survive across days. `LiveLoop` holds exactly the state that loop kept in
local variables and exposes the body as `on_bar(market)`.

The four steps, their order, and the fresh-DataFrame look-ahead defense are
transcribed **unchanged**. That equivalence is the V0 gate.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `LiveLoop(forecaster, emitter, broker, trader, portfolio, *, exit_rule, forced_close_on_session_end, history_window, recorder, rails)` | class | collaborators are injected, not constructed |
| `on_bar(market)` | method | the whole per-tick body for one bar |
| `result()` | method | packs into the same `BacktestResult` the backtest returns |
| `fills` / `orders` / `forecasts` / `signals` | attribute | the audit trail as it accumulates |

## The four steps
1. **Fills** — drain `broker.poll_fills(market)`; apply to Portfolio and Trader.
   Runs first (§4.1) so a just-closed position is flat before new decisions.
2. **Exit rule** — queried every bar, not just on stride boundaries, so a stop can
   fire intra-stride.
3. **Forecast boundary** — past warm-up and on a stride boundary: build a **fresh**
   history frame, forecast, emit, act.
4. **Session-boundary forced close** — with the latch that books the resulting PnL
   to the session it was earned in (SPEC §4.7).

## Deliberately *not* carried over from `run_backtest`
- **The forecast precompute/cache path** — it batches over *future* bars, which do
  not exist live.
- **The model-less dummy-emitter path** — a live run always has a forecaster.

## Key invariants / gotchas
- **Not thread-safe; drive it from one thread.** Broker callbacks must *enqueue*,
  never call `on_bar` directly — a forecast is seconds of GPU work and would
  stall the venue's quote handler.
- **`history_window` is required in practice.** A live process runs for days and
  would otherwise grow one row per bar forever. Safe only because the forecaster
  consumes a fixed trailing context; set it ≥ `context_length`.
- **A halted loop makes no new decisions.** When [rails](state.md) latch, steps
  2–4 are skipped wholesale; only a `FLATTEN` halt submits anything, and that
  retry is gated on `has_pending()` so it cannot become a storm.
- **Fills are always booked, even while halted** — a fill is a fact.
- **`recorder` and `rails` are optional and inert when `None`.** Parity is
  asserted both without them and with a recorder attached, proving observation
  cannot perturb decisions.
- **A bar that does not advance the clock is recorded, not rejected.** A bar
  stamped at or before the newest one buffered writes a `bar_anomaly` record and
  is then processed normally. This is the detector for the backfill↔live seam
  duplicate (SHIOAJI_PLAN.md §2.14, cause removed in [backfill.py](backfill.md)).
  It does not raise — a data quirk must not kill a process holding a position —
  and it does not drop, because dropping would shift `bar_idx` and with it every
  stride boundary and holding-period count.
- It takes **no `SessionPolicy`** — it consumes whatever bars it is handed.
  Filtering belongs upstream in [bars.py](bars.md) and [backfill.py](backfill.md).

## Related
[run.py](../run.md) · [live/bars.py](bars.md) · [live/state.py](state.md) · [live/recorder.py](recorder.md) · [tests/test_live_parity.py](../tests.md) · SPEC §4.1
