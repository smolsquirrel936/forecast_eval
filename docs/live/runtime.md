# [live/runtime.py](../../live/runtime.py)

**Role:** Assembles a live session from one config and holds it together — everything *decidable*, nothing network-facing.
**Pipeline:** [cli.py](cli.md) → **LiveRuntime** → [bars.py](bars.md) / [loop.py](loop.md) / [state.py](state.md).
**Depends on:** [live/backfill.py](backfill.md), [live/bars.py](bars.md), [live/loop.py](loop.md), [live/state.py](state.md), [live/config.py](config.md), [strategy/&lt;session&gt;/trader.py](../strategy/night/trader.md), [portfolio.py](../portfolio.md)   **Used by:** [live/cli.py](cli.md), [tests/test_live_runtime.py](../tests.md)

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
| `LiveRuntime(config, *, broker=None, recorder=None, rails=None, rails_config=None, simulation=True, emitter_prime_boundaries=900, tick_capture=None)` | class | builds the whole stack; defaults to [DryRunBroker](broker/dryrun.md) |
| `adopt_position(*, timestamp, fallback=None)` | method | seed both mirrors from the venue's held position — or from `fallback` when the broker has no venue — or `None` when flat; raises when the state cannot be adopted |
| `venue_position` | attribute | what the venue answered at start-up; `None` when none was asked |
| `fetch_warmup_bars(api, contract, start, end, *, cache_path=None, front_month_at=None)` | method | [fetch_bars_continuous](backfill.md) with *this* runtime's policy — the caller never picks one; `contract` may be a list, spliced on volume |
| `settle_rolled_leg(*, price, timestamp, session="NIGHT")` | method | close the expiring month in both mirrors and arm the same exposure for re-entry (§2.6) |
| `request_roll(now=None)` / `resume_roll(now=None)` | methods | the scheduled roll's two halves — close on the old month, then re-open on the new once the caller has switched |
| `switch_contract(codes)` | method | from now on, only prints carrying `codes` build bars. It also discards the half-built bar and clears the ladder's reference bar, so the re-open is priced from the new month (2026-10-20). Prints with no code still pass, and before any roll nothing is filtered |
| `n_foreign_dropped` | attribute | prints of another month that arrived after a switch; captured but never aggregated |
| `roll_state` | property | `None` / `requested` / `closing` / `switch` / `reopening` |
| `advance_roll_ladder(now=None)` | method | re-price an unfilled roll leg one rung higher — **clock-driven**, 10 s apart |
| `ladder_ticks(attempt)` | function | 5 / 10 / 15 / 20 / 25, then capped |
| `ROLL_LADDER_*` | constants | start 5, step 5, max 25, interval 10 s |
| `roll_fills` | attribute | timestamps of fills that were roll legs — `build_trades(roll_fills=…)` reads this |
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
**The whole strategy comes from the session's package.** Since
LIVE_STRATEGY_ROUTING_PLAN D2 (2026-09-09) `__init__` calls
`strategy.wiring.make_strategy(config.strategy_package, warmup_bars=0,
tick_size=config.tick_size, **strategy_overrides)` **once**, and takes the
forecaster, emitter, trader factory, `exit_rule` and `forced_close` from the
returned stack. The resolved knob dict is kept as `self.strategy_params`, which
is where everything that is a strategy value but not a component now reads from —
`context_length` for the history window, `max_position` for the rails check.

This replaced three separate `config.build_*` calls. They are gone, along with
the strategy fields on `LiveConfig` that fed them: live and the backtest now
reach one factory, so what a live run trades **is** what
`real_data.py --strategy <session>` backtests, by construction rather than by two
declarations happening to agree. See [live/config.py](config.md).

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
| No venue at all (`has_venue = False`) and a `fallback` given | adopted from the **ledger**, `source="ledger"` |
| 1 lot | adopted at the **venue's** average entry price |
| Above `max_position` | raises — `Trader.on_fill` cannot average a second lot's entry (§2.10) |
| An `ExitRule` that is not `adoptable` (e.g. `TimeStop`) | raises — the entry *bar index* belongs to the dead process |
| An `adoptable` `ExitRule` (`EntryPercentStop`, `FixedStopLoss`) | adopted — the rule reads only side and entry price, both seeded from the venue |
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
- **`emitter_prime_boundaries` defaults to 900 because the tail, not the average,
  sets it** — raised from 500 on 2026-09-02, [DAY_SESSION_PLAN §2.5](../../DAY_SESSION_PLAN.md).
  Non-HOLD signals are 4.11 % of NIGHT's boundaries and 2.61 % of DAY's, so the
  ~80 the average rate suggests would prime nothing during a quiet stretch. The
  statistic that sets K is the **fewest non-HOLD signals in any K-boundary
  window**, *not* the longest consecutive HOLD run (385 NIGHT / 246 DAY) — samples
  are counted, not required to be adjacent. Measured at K=500 those worst windows
  were **1 for NIGHT** and 3 for DAY against a floor of 3; at K=900 they are 7 and
  10.
- **The cap is deliberately wider than the fetch is sized for**, and the two are
  not meant to agree: [`warmup_days_default`](config.md) sizes the *request* for
  500 boundaries, while the bar cache routinely holds more (a
  [seeded](seed_cache.md) DAY cache is 30 000 bars = 899 boundaries) and this cap
  is what lets the replay spend them. The slice takes all available when the frame
  holds fewer, so a policy whose cache has not been seeded simply gets what it has
  — NIGHT's live-accumulated cache was 14 669 bars (388 boundaries) on 2026-09-02.
- **Priming runs at a fixed batch of 32 (`PRIME_BATCH_SIZE`), then releases the
  GPU cache.** `forecast_series_batch` defaults to `batch_size="auto"`, which
  sizes the batch to 80 % of the VRAM free at that moment; PyTorch then keeps
  that peak reserved for the life of the process. MEASURED 2026-10-03 (900
  boundaries, ctx 3008): auto picked 1024 and held 21.5 GiB, so three
  deployments together committed 63 GB on a 32 GB card. At 32 the replay takes
  5.0 s against auto's 5.2 s and peaks at 4.61 GiB. `_release_gpu_cache` then
  drops the process back to the model's ~4.1 GiB. Batch size changes outputs
  only at float noise (|Δ return| ≤ 1.8e-7, |Δ prob_up| ≤ 2.2e-5). The torch
  handle is read off the forecaster, so this module still never imports torch.
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

`LiveRuntime(..., ledger_dir=None)` is where the directory comes from: `cli.py` passes `out.parent`, the same directory `ledger_path` writes into. `None` disables the sum and keeps the previous operand, which is what every offline test and the V0 parity harness rely on.

## Reconciliation now compares the ACCOUNT, not one deployment (§16)
**MEASURED 2026-09-02** (STRATEGY_SPLIT_PLAN §14): a deployment's order callback
hears **only its own** fills, while `list_positions` returns the **combined**
book for the account. Comparing one deployment's mirror against the whole
account's size is not a desync check — it is a category error, and it halts every
deployment the first time two of them hold at once, with nothing actually wrong.

So the reconciliation mirror is now **what we recorded for the whole account**:
`ledger.account_net_position(dir)` sums the signed quantity of every row of every
`fills_*.csv` in the run's parent directory, across deployments and across soak
epochs. Both sides of the comparison then mean "the account", and a disagreement
means what the rail was written to catch: a fill missed or counted twice.

**Under `--broker client` the operand changes again, and improves.** With an
owner ([owner_process.py](owner_process.md)) the comparison splits in two, and
they are not the same check:

* the **owner** compares its own `account_net` against `list_positions` — the
  venue check, made in memory by the one process that can see every deployment,
  which is what retires §16's freshness race (the ledger sum is only as current
  as the other deployments' last flush);
* each **deployment** compares its `Trader.position` against the owner's
  `net_for(slug)`, delivered as a `position` event. Both sides are then "the
  fills this deployment had", so a disagreement means an event lost on the wire
  rather than a peer holding a position.

The ledger sum stays exactly as described above for a `--broker shioaji`
deployment, and becomes the owner's **restart seed** rather than a per-bar
operand.

**It degrades, it does not guess.** No ledger directory, no ledger files, or one
that cannot be parsed all return `None`, and the loop falls back to this
deployment's own position — the previous behaviour exactly. A partial sum would
be a confident wrong number, which is the failure mode this removes.

**It does not make concurrent deployments safe.** It stops the spurious halt; it
does not stop two deployments' orders netting into one book. See
STRATEGY_SPLIT_PLAN §15 — one instance at a time — which stands until the venue
gives each deployment its own book.

## Related
[live/cli.py](cli.md) · [live/loop.py](loop.md) · [live/backfill.py](backfill.md) · [live/bars.py](bars.md) · [live/state.py](state.md) · [live/config.py](config.md) · [SHIOAJI_PLAN.md §2.8](../../SHIOAJI_PLAN.md)

## Adopting from the ledger when there is no venue

`adopt_position` returns `None` for a flat account *and* for a broker that has no
account at all, and the ledger reconciliation in [cli.py](cli.md) has to tell
those apart — so the raw answer is kept on **`venue_position`**: the venue's
signed lots, or `None` when nobody was asked. `cli.py` reconciles only when it is
not `None`.

A `--broker dryrun --resume` run then has nothing to check its ledger against, and
the ledger is the only record of what the deployment holds. Passing
`ledger.open_position(inherited)` as `fallback` seeds both mirrors from it, and
the adoption reports `source="ledger"` rather than `"venue"` so an operator
reading `position_adopted` can tell which stood behind the numbers.

**The fallback is not a back door around the reconciliation.** It applies only
when `broker.has_venue` is `False`. A `shioaji` query that *raised* also returns
`None` here, and that case still adopts nothing — the answer is unknown, and
substituting the ledger would seed a history nothing checked against the real
account. Seeding the mirrors matters as much as seeding the fills: without it the
inherited fills net +1 while the Trader believes it is flat, and the next fill
pairs against a lot it does not know it holds.
