# [live/runtime.py](../../live/runtime.py)

**Role:** Assembles a live session from one config and holds it together — everything *decidable*, nothing network-facing.
**Pipeline:** [cli.py](cli.md) → **LiveRuntime** → [bars.py](bars.md) / [loop.py](loop.md) / [state.py](state.md).
**Depends on:** [live/backfill.py](backfill.md), [live/bars.py](bars.md), [live/loop.py](loop.md), [live/state.py](state.md), [live/config.py](config.md), [trader.py](../trader.md), [portfolio.py](../portfolio.md)   **Used by:** [live/cli.py](cli.md), [tests/test_live_runtime.py](../tests.md)

## Responsibility
The split with [cli.py](cli.md) is the point: credentials, sockets and callbacks
live there, so **everything here is testable without a login, a feed, or a GPU**.

It owns two jobs that were previously nobody's.

**1. One policy, injected everywhere.** `SessionPolicy` used to be handed
separately to the aggregator, the backfill and the rails, with nothing checking
they agreed — and a mismatch splices the model's context from two regimes without
raising. The runtime takes the policy from the pinned config, builds the
aggregator with it, fetches warm-up through it, and **validates** any bars handed
to it. It deliberately does *not* accept a policy argument; doing so would
reintroduce exactly the mismatch it exists to prevent.

**2. A warm-up that leaves nothing cold.** Three things must be warm before the
first live decision, and only the first is obvious.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `LiveRuntime(config, *, broker=None, recorder=None, rails=None, rails_config=None, simulation=True, emitter_prime_boundaries=500, tick_capture=None)` | class | builds the whole stack; defaults to [DryRunBroker](broker/dryrun.md) |
| `adopt_position(*, timestamp)` | method | seed both mirrors from the venue's held position, or `None` when flat; raises when the state cannot be adopted |
| `fetch_warmup_bars(api, contract, start, end)` | method | [fetch_bars](backfill.md) with *this* runtime's policy — the caller never picks one |
| `warm_up(bars)` | method | validate → prime emitter → pre-warm model → seed context |
| `on_tick(ts, price, volume, *, simtrade, code)` | method | capture → filter → aggregate → `loop.on_bar` per completed bar |
| `on_clock(now)` | method | flush a quiet bar, then run the staleness watchdog |
| `shutdown(now)` | method | close out; returns the end-state dict and records `session_end` |
| `result()` / `summary()` | method | the `BacktestResult`, and the counters the CLI prints |

## The three cold things `warm_up` fixes
| What | Why it must be warm | How |
|---|---|---|
| Forecaster **context** | `context_length` bars; a short context still produces plausible numbers from a distribution the strategy was never evaluated on | backfilled, never waited for (§7.5) |
| The **model** | first forecast ~18.8 s vs ~0.09 s warm (§2.7) | one throwaway forecast at start-up |
| Emitter **deques** | `_trend_confirms` fails **open** below 3 samples — an un-primed restart trades with a risk filter silently disabled | replay historical forecasts through `emit_signal`, discarding results (§2.8) |

## Resuming into an open position (§2.20)
Both mirrors — the Trader's and the Portfolio's — start at zero, which is only
true of a process that starts flat. The strategy holds a **median 4.5 days**, so
a restart during a soak normally lands mid-position, and a zero mirror there is
not merely stale: the reconciliation rail compares 0 against the venue's N and
halts on the first bar, and the no-flip machine reads "flat" and opens a second
lot. `adopt_position` seeds both from `broker_position_detail()` before the first
bar. Held once, at start-up — the per-bar path stays on `broker_position()`.

| Situation | Result |
|---|---|
| Account flat, or broker cannot answer | `None` — nothing adopted, nothing refused |
| 1 lot | adopted at the **venue's** average entry price |
| Above `max_position` | raises — `Trader.on_fill` cannot average a second lot's entry (§2.10) |
| An `ExitRule` is configured | raises — the entry *bar index* belongs to the dead process |
| Called twice | raises — adoption seeds a fresh mirror, once |

## Key invariants / gotchas
- **The venue's entry price goes in, not the current market.** So an inherited
  loss lands on the day the position closes, and a restart's first close can go
  straight through the daily loss limit. That is the money that was actually
  lost, and what the backtest books too; resetting entry to the market would
  flatter every restart of a losing trade and nothing downstream could tell.
- **Priming replays; it never pokes the deques.** The accumulation lives in the
  emitter's own code — `ThresholdEmitter` appends only on non-HOLD while
  `VolMultiTfEmitter` appends on every forecast — so replay is correct for either
  by construction, and immune to that asymmetry.
- **`emitter_prime_boundaries` defaults to 500 because the tail, not the average,
  sets it.** Only 3.74 % of signals are non-HOLD, and the longest observed run of
  consecutive HOLDs is **385 boundaries**. The ~80 the average rate suggests would
  prime nothing at all during a quiet stretch.
- **An under-primed emitter refuses to start.** Below 3 samples the filter fails
  open; refusing is recoverable (widen the window), trading unfiltered is not.
- **`warm_up` rejects bars from outside the policy**, and requires at least
  `context_length` of them. Both are startup errors, not warnings.
- **`on_tick` raises before `warm_up`.** Trading on a partial context is the exact
  failure the start-up sequence exists to prevent.
- **Capture happens before every filter.** simtrade and out-of-session ticks are
  written too — a file holding only what survived cannot audit the filter
  ([capture.md](capture.md)).
- **Only genuine prints refresh the staleness marker.** A simtrade tick is not
  data, so it must not keep a frozen feed looking alive.
- **`shutdown` reports `flatten_incomplete` as its own failure.** "Submitted a
  close" is not "flat": orders are `LMT`+`ROD` and ROD is cancelled at session
  end, so a flatten fired near the close can silently fail. The CLI turns this
  into a non-zero exit rather than letting it read as a clean run.

## Related
[live/cli.py](cli.md) · [live/loop.py](loop.md) · [live/backfill.py](backfill.md) · [live/bars.py](bars.md) · [live/state.py](state.md) · [live/config.py](config.md) · [SHIOAJI_PLAN.md §2.8](../../SHIOAJI_PLAN.md)
