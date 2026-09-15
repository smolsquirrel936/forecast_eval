# [live/cli.py](../../live/cli.py)

**Role:** The live entry point — logs in, subscribes, and turns callbacks into a single-threaded event stream. **No orders are sent.**
**Pipeline:** Shioaji quote thread → queue → **cli worker** → [LiveRuntime](runtime.md).
**Depends on:** [live/runtime.py](runtime.md), [live/config.py](config.md), [live/startup.py](startup.md), [live/soak_epoch.py](soak_epoch.md), [live/recorder.py](recorder.md), [live/capture.py](capture.md), [live/broker/dryrun.py](broker/dryrun.md)   **Used by:** run by hand (V3)

```powershell
$env:SHIOAJI_API_KEY="..."; $env:SHIOAJI_SECRET_KEY="..."
uv run --no-sync python -m forecast_eval.live.cli --duration-minutes 60
```

> ## ⚠ ONE INSTANCE AT A TIME — WHATEVER THE SESSION
> With real money there must be exactly **one** running instance of this entry
> point at any moment: one deployment, not two, and not "day and night because
> their sessions do not overlap".
>
> **MEASURED 2026-09-02** (STRATEGY_SPLIT_PLAN §14), live, with separate API keys
> per deployment: a deployment's callback hears **only its own** fills, but
> `list_positions` returns the **combined** book to everyone. Separate API keys
> are an authentication boundary, not a position boundary — so two instances net
> against each other, and each one's mirror disagrees with the venue, halting
> **both** on `mirror != venue`.
>
> "The sessions do not overlap" is not a safe reason. DAY's median hold is **20
> calendar days**, NIGHT's is 4.5 — a position opened in one session is still
> open through the other. The sessions are disjoint; the positions are not.
>
> **This applies to `--broker shioaji`, which is what it was written for.**
> Lifting it needs the venue to give each deployment its own book — and MEASURED
> 2026-09-07 (§17.1), **`shioaji` 1.7.2 cannot address a sub-account at all**:
> `subaccount` is a read-only label on inbound deals, absent from `FuturesOrder`,
> `Account`, `list_positions` and `FuturePosition`. So that route is closed
> whatever the broker offers.
>
> The other route, the shared position broker (§7.1(b)), is **built**: run the
> [owner](owner_cli.md) and start each deployment with `--broker client`, and
> every order goes through one process holding one book. The rule stands until
> [G6](../tools/g6_live.md) passes against a real session — it lifts on evidence,
> not on a build — and then only for the shape G6 tested.

## Responsibility
The thin network shell around [LiveRuntime](runtime.md). Everything decidable
lives there and is testable offline; this module owns only what needs
credentials: login, contract resolution, subscription, the consumer loop, and
orderly teardown.

It is also the module that makes a session **reproducible after the fact** — it
opens the [Recorder](recorder.md) and the [TickCapture](capture.md) that V1's
live half and V3's shadow parity are both read back from.

## Flags
| Flag | Default | Notes |
|---|---|---|
| `--broker {dryrun,sim,shioaji,client}` | `dryrun` | `dryrun` records intended orders; `sim` shares the fill model without the order log; **`shioaji` places simulated orders**; **`client` submits intents to the owning process**, which places them (§18). The last two need the flag below |
| `--i-understand-this-places-orders` | off | **Required** with `--broker shioaji` or `client` |
| `--fresh-start` | off | Start a new [soak epoch](soak_epoch.md). **Required with an order-placing broker** (with `--resume`, exactly one). Refuses at start-up unless *this deployment* is flat — never flattens itself |
| `--resume` | off | Continue the current [soak epoch](soak_epoch.md). **Required with an order-placing broker** (with `--fresh-start`, exactly one). Bootstraps the first epoch if none exists yet |
| `--flatten-aggression-ticks` | `5` | how far through the touch a FLATTEN close is priced before going IOC (`shioaji` only) |
| `--strategy {night,day,fullday}` | `night` | the deployment: its session policy and its `strategy/<session>/` package, which is where every strategy value now lives (D2). **`--config` is an accepted alias** for the same argument (D5). Since 2026-09-09 an **UNTUNED deployment is no longer refused** — see below |
| `--param KEY=VALUE` | — | override one strategy knob, repeatable, coerced against the package's `PARAMS` (D7). A run carrying one is not comparable to any `results.tsv` row; `params.json` records the resolved set |
| `--contract` | `TXFR1` | resolved by attribute, falling back to key lookup |
| `--duration-minutes` | none | stop after this long; otherwise runs until Ctrl-C |
| `--warmup-days` | `30` | calendar days requested for context + emitter priming (~21 night sessions) |
| `--out` | `outputs/live_<ts>` | **relative** — resolves against the CWD you launch from |
| `--state-file` | `<out>/../live_restart_<policy>.json` | [RestartGuard](startup.md) state. Keyed by policy; `soak_epoch` and `live_account` follow its directory |
| `--no-capture` | off | skips raw tick capture; V1's live half needs it |
| `--max-daily-loss-points` | none | [rails](state.md) `FLATTEN` threshold |
| `--kill-file` | none | presence flattens and exits at the next bar |
| `--staleness-seconds` | `900.0` | **backstop** — halt after this long unhealed. Detection is [FeedWatch](feedwatch.md)'s job (§2.22) |
| `--feed-check-seconds` | `60.0` | silence before asking the venue whether it traded without us. A polling interval, not a threshold |


## The untuned refusal is gone (LIVE_STRATEGY_ROUTING_PLAN D1)

`main` used to return **5** for any config whose `is_tuned` was False, before the
alerter, the credentials check or the run directory. It was removed on
2026-09-09.

The reason is narrow and worth stating so it is not read as a general loosening:
`api = sj.Shioaji(simulation=True)` in this file is **hardcoded, not a flag** — by
design, so this entry point has no reachable path to a production account. The
gate therefore protected an account that cannot lose money, while blocking the one
workflow simtrade exists for: running what the research loop just produced.

**Enforcement became a record.** `is_tuned` still exists, `describe()` still
renders `UNTUNED` and `calmar=nan`, and that line goes into the banner *and* the
recorder's `meta` header — so an unprovenanced run is self-identifying in its own
artifacts. What no longer happens is a refusal.

> **The gate's home is the eventual simulation→production boundary**, which does
> not exist yet. Whoever builds that boundary builds the gate with it.

## Exit codes
| Code | Meaning |
|---|---|
| `0` | clean |
| `1` | unhandled exception |
| `2` | credentials not in the environment |
| `3` | [RestartGuard](startup.md) refused to start (backoff, backfill cap, or quota headroom) |
| `4` | **flatten incomplete** — a position was still open after a `FLATTEN` halt |
| `5` | refused before the first bar — no acknowledgement flag, neither/both of `--fresh-start`/`--resume`, `--fresh-start` against a non-flat account, or a held position that cannot be adopted (above `max_position`, or an `ExitRule` is configured), *(the untuned-deployment refusal was removed 2026-09-09 — see below)* |

## Start-up order (each step gates the next)
1. **`RestartGuard.begin`** — *before* login, before any query. Every start
   performs a backfill, so a crash loop becomes a login+query loop, and Shioaji
   answers that with a suspension that further logins extend.
2. **VRAM floor** (5 GiB) — the model needs ~4.4 GiB, and a research run with
   `--batch-size auto` sizes itself to fill whatever is free.
3. **Login**, then `check_usage` — over quota, Shioaji returns *empty* history
   rather than erroring — and **read the traffic meter**, keeping the figure so
   the teardown can report what this run cost.
4. **Resolve the contract**, printing the resolved `code` and `delivery_month`,
   and the date the run plans to roll off it ([roll.md](roll.md)).
5. **`--fresh-start` refuses a non-flat account** ([soak_epoch.md](soak_epoch.md))
   — a check only, never a flatten; under `--broker client` "non-flat" means
   *this deployment*, not the account — then **adopt any held position** (§2.20),
   before the backfill, so an unadoptable one costs seconds instead of a full
   warm-up.
6. **Backfill → `warm_up`** — from *every listed month up to the front one*,
   merged with the local bar cache and spliced on the roll calendar, so a warm-up
   that spans a roll is the series the model was evaluated on (see
   [backfill.md](backfill.md)). Then context, model and emitter deques (see
   [runtime.md](runtime.md)).
7. **Subscribe**, then run the consumer loop.

## What the clock block runs, once a second
Four things, in this order, and the ordering is not arbitrary:

| | | |
|---|---|---|
| 1 | `rt.on_clock` + `guard.heartbeat` | close a quiet bar, prove we are alive |
| 2 | [`relogin.check`](relogin.md) | **never** gated on the session policy — its anchors sit in the pre-open quiet, which is by definition outside every session |
| 3 | `_check_schedule` | the [scheduled contract roll](roll.md); also ungated, for the same reason |
| 3b | `rt.advance_roll_ladder` | re-prices an unfilled roll leg 5 ticks higher, at most every 10 s |
| 4 | [`feed.check`](feedwatch.md) | *only* inside a session we trade — silence between 05:00 and 15:00 is expected |

A successful re-login also asks [RollWatch](roll.md) whether the front month
moved, and so does every `MISSED` verdict from the feed check. Those two are the
reactive net; step 3 is the plan.

## The contract roll (§2.6)
A soak died because the alias `TXFR1` is dereferenced **once**, at start-up, and
the object it returns is what every closure, the broker and the subscription hold
for the life of the run. When the month behind it expired, the run kept
re-subscribing a retired contract — 15 repairs, 0 ticks, then a staleness halt.

`_apply_roll` is the one place that moves, and it moves **four** holders together
plus the position:

```
api.unsubscribe(old)           # best effort; the month may already be gone
contract = rolled.contract     # the name every closure reads
live_broker.switch_contract()  # broker contract + accepted code set
_subscribe(drop_first=False)   # the new object, on a fresh subscription
ticks["at_relogin"] = ticks["n"]   # re-baseline the verify window's tick counter
```

Two paths reach it. `_check_schedule` is the **plan**: inside the roll window it
snapshots both months (recording their volumes, which is how the naive
third-Wednesday calendar gets validated from data), requires the incoming month
to be printing, and asks the runtime to close the old leg — then switches once
that close has actually filled, and arms the re-entry. `_check_roll` is the
**net**: if the plan was missed, the alias flip is caught and the position is
settled rather than traded, because the venue has already cash-settled it.

`_warmup_contracts` supplies the other half — the listed months up to the front
one — so the backfill can splice across the boundary. Any SDK-shape surprise
there degrades to `[front]`, which is exactly the old behaviour.

## Key invariants / gotchas
- **`--broker shioaji` takes two deliberate flags, not one.** Through V3 the
  protection was structural — the option was absent, so it "could not be reached
  by a mistake at 3 a.m." V4 needs it, and re-adding it as one word among three
  would spend that protection for nothing, so `--i-understand-this-places-orders`
  now carries it.
- **...and a third, orthogonal one: exactly one of `--fresh-start` /
  `--resume`.** Not a bigger acknowledgement gate — a separate question,
  "does this restart continue the soak or start a new one", that used to have
  no answer at all: [`adopt_position`](runtime.md) ran unconditionally either
  way, with no record of which the operator meant. See
  [soak_epoch.md](soak_epoch.md).
- **Simulation is hardcoded, not a flag.** §7.2 measured that `api.simulation`
  reads back as `None`: the SDK does not expose the mode, so it cannot be
  verified after the fact and the constructor argument is the only guarantee.
  Leaving it unparameterised means this entry point has **no reachable path to a
  production account**, whatever else is passed.
- **A pre-existing venue position is *adopted*, not refused (§2.20).** It used to
  be exit 5: the mirror started at zero, so an account already holding something
  made the reconciliation rail (§2.9) halt on the first bar. But the strategy
  holds a median 4.5 days, so a V5 soak restarts mid-trade far more often than it
  starts flat, and refusing made the soak impossible.
  [`rt.adopt_position`](runtime.md) now seeds both mirrors from the venue before
  warm-up — before, so a refusal costs seconds rather than a full backfill and
  model prime — and the run prints `RESUMING: adopted …`. What is still exit 5 is
  a position that *cannot* be adopted: above `max_position`, or with an
  `ExitRule` configured. A broker that cannot answer adopts nothing and does not
  block, which is "cannot compare", the same degradation the rail makes.
- **The ledger is reconciled only when a venue actually answered.** `--resume`
  replays the deployment's fills and refuses them when their net disagrees with
  what the venue holds — that check is load-bearing against a real account, since
  a replay that does not add up means the history is wrong and seeding it would
  produce a confidently wrong `trades.csv`. It used to run against
  `adopted["position"] if adopted else 0`, which fabricated a flat venue for a
  broker that has none. MEASURED on `live_night_20260902_082640`: a `--broker
  dryrun` run's ledger replayed to +1 against a venue that was never asked, so it
  was refused on every start and the run wrote an empty `fills.csv` and
  `trades.csv`. It now reads [`rt.venue_position`](runtime.md) and skips when that
  is `None`, adopting the position from the ledger instead — while a `shioaji`
  query that *raised* still refuses, because there the answer is unknown rather
  than absent (see [broker/base](broker/base.md)). The skip is recorded as
  `fills_replay_unreconciled`.
- **The rails' config is built by `_rails_config`, not inline in `main`.** `main`
  needs a login, so anything assembled there is untestable — and a flag that is
  parsed but never reaches the rails looks exactly like one that works. That
  failure has happened here before (§2.19: the recorder logged an order type the
  venue never received), so the wiring has a seam and a mutation-checked test.
- **The rails are built here, not by [LiveRuntime](runtime.md)**, so the broker
  and the runtime share one instance. `ShioajiBroker` reports rejections straight
  to the rails, and a rejection the runtime's rails never saw would halt nothing.
- **A dropped feed is repaired, not halted on (§2.22).** [FeedWatch](feedwatch.md)
  runs on the clock tick, inside a traded session only, and asks the venue
  whether it traded without us. `on_session_down` and `on_event` are registered
  too, but they only **record** — their shape is unobserved (they have never
  fired in a healthy run), and writing recovery logic against an unobserved
  callback is how four earlier SDK assumptions went wrong. The repair acts on
  evidence instead.
- **The quote callback only enqueues.** A forecast is ~90 ms of GPU work; running
  it on Shioaji's thread would stall every quote behind it. One worker drains the
  queue and is the *sole* caller into the runtime, so [LiveLoop](loop.md)'s
  single-threaded requirement holds with no lock anywhere.
- **The 0.5 s queue timeout is load-bearing.** It is what lets clock work — bar
  flush, staleness, heartbeat — run *while the feed is silent*, which is exactly
  when it matters. `on_clock` fires at most once a second.
- **Subscription, not polling.** Pushes do not count against the daily traffic
  quota, and polling would sample the tape rather than see it — which cannot
  reconstruct minute bars faithfully.
- **Two SDK forms are deprecated and already corrected here:** the tick callback
  takes `(data)`, not `(exchange, data)`; and contracts are reached through
  `api.contracts`, not `api.Contracts`.
- **The roll's two orders are sequenced by observation, not by a timer.** The
  switch happens only after the closing fill is seen; a close that does not fill
  leaves the roll visibly stuck in `closing` rather than advancing into a state
  that would strand the old month's lot.
- **Teardown runs in `finally`** — unsubscribe, shutdown, summary, close capture
  and recorder, logout, `guard.end`. A crash still leaves a complete artifact set
  and a clean restart ladder.

## Related
[live/runtime.py](runtime.md) · [live/roll.py](roll.md) · [live/startup.py](startup.md) · [live/soak_epoch.py](soak_epoch.md) · [live/recorder.py](recorder.md) · [live/capture.py](capture.md) · [live/broker/dryrun.py](broker/dryrun.md) · [SHIOAJI_PLAN.md](../../SHIOAJI_PLAN.md)

## The run directory names its deployment

`outputs/live_<slug>_<timestamp>/` — `live_night_20260902_013204`,
`live_day_...`. It used to be `live_<timestamp>` alone, which says when a run
happened but not what ran; with two deployments a directory of DAY artifacts and
one of NIGHT artifacts were distinguishable only by opening `params.json`, and
they are the two things most likely to be compared side by side.

The slug is `policy.cache_slug()`, the same token the bar cache
(`bars_cache_night.csv`) and the fill ledger ([fills_night.csv](ledger.md)) carry,
so every file a deployment owns reads the same. A `both()` policy therefore reads
as `day_night` rather than "fullday" — that is a tape name, and a deployment with
two identities is how two names for one thing start.

`--out` still wins: a probe or a one-off run names its own directory.

### The start-up state carries the slug too — 2026-09-02

`_state_path(out_dir, policy, name)` keys the three files a deployment's start-up
owns: `live_restart_<slug>.json` ([RestartGuard](startup.md)),
`soak_epoch_<slug>.json` ([soak epoch](soak_epoch.md)) and
`live_account_<slug>.json` (the account fingerprint).

They were one path for every deployment until now, which C5 did not cover — it
keyed the *data* a deployment owns, not its *state*. The cost is concrete: the
RestartGuard caps backfills at **10 per trading day** to protect the venue quota,
and one file means **one budget shared by both**, so whichever deployment restarts
first can refuse the other's start-up for the rest of the day. Measured 2026-09-02,
the shared counter stood at 3 with a single DAY run pooled into two NIGHT ones.

**The legacy unkeyed files are not adopted automatically**, exactly as the pre-C5
`bars_cache.csv` was not: they hold NIGHT's state, and giving it to whichever
deployment starts first is the corruption the keying prevents. Rename by hand if
the state matters — `soak_epoch.json` is the one that does, since a later
`--resume` stitches through it.

`fatal.jsonl` stays **one file for both**, deliberately: "what has ever gone badly
wrong" should not be a search. Its records carry no deployment field, though, so
once two deployments run a `startup_refused` there cannot be attributed to one.

## `--broker client` — running under the owner (§18)

The deployment keeps its Forecaster, Trader, Portfolio, rails, ledger and PnL.
Only its `Broker` changes: [ClientBroker](broker/client.md) writes intents to
`intents_<slug>.jsonl` and reads fills back from `events_<slug>.jsonl`, and the
[owner](owner_cli.md) does the placing.

**Start [the owner](owner_cli.md) first.** A client whose owner has never spoken
halts rather than reading the silence as a flat account.

**This process still logs in.** It needs the market feed to build bars; only
*orders* are delegated. That means the owner plus three deployments is **four
sessions on one credential**, and §14b Q2 measured two coexisting, not four.

**Every `== "shioaji"` gate now reads `ORDER_PLACING_BROKERS`.** A mode that
sends orders through one more hop is still a mode that sends orders, and a gate
that forgets it is a gate that is off. That covers the preflight, the soak-epoch
resolution, and the `--fresh-start` flatness check.

**The `--fresh-start` gate means something different here, and better.** It calls
`broker_position_detail()`, which under a client is *this deployment's* size, not
the account's — so a flat deployment can start fresh while a peer holds, which
under `shioaji` would have been refused.

**No `ShadowBroker` wrapper.** V4's fill-gap measurement compares our own fill
model against the venue's, and the owner is neither.

## The traffic meter, at both ends (§18.13)

`check_usage` refuses to **start** below a 50 MB floor, because over quota
Shioaji returns *empty* history rather than erroring — which would silently
produce an empty model context. What it cannot do is say what a session
**costs**, and that is the number deciding whether the owner's
one-subscription-per-deployment shape fits the daily allowance.

So the meter is read twice — after login and again in teardown, before the
logout that would make it unanswerable — and the teardown prints:

```
  traffic used today    12,481,006 bytes of 524,288,000
  traffic this run      11,648,000 bytes
  four sessions would   46,592,000 bytes (8.9% of the allowance)
```

Both readings are recorded to the audit trail as `traffic_start` /
`traffic_end`. `usage_at_start` is bound *before* the `try`, like `rt`, so a
login failure does not turn into a `NameError` in teardown.

The allowance was MEASURED 2026-09-07 at **500 MiB/day**, and four logins alone
(no market) cost 833 KB. What a full session of TXF ticks costs is what these
two lines exist to find out.
