# [live/cli.py](../../live/cli.py)

**Role:** The live entry point — logs in, subscribes, and turns callbacks into a single-threaded event stream. **No orders are sent.**
**Pipeline:** Shioaji quote thread → queue → **cli worker** → [LiveRuntime](runtime.md).
**Depends on:** [live/runtime.py](runtime.md), [live/config.py](config.md), [live/startup.py](startup.md), [live/recorder.py](recorder.md), [live/capture.py](capture.md), [live/broker/dryrun.py](broker/dryrun.md)   **Used by:** run by hand (V3)

```powershell
$env:SHIOAJI_API_KEY="..."; $env:SHIOAJI_SECRET_KEY="..."
uv run python -m forecast_eval.live.cli --duration-minutes 60
```

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
| `--broker {dryrun,sim,shioaji}` | `dryrun` | `dryrun` records intended orders; `sim` shares the fill model without the order log; **`shioaji` places simulated orders** and needs the flag below |
| `--i-understand-this-places-orders` | off | **Required** with `--broker shioaji` |
| `--flatten-aggression-ticks` | `5` | how far through the touch a FLATTEN close is priced before going IOC (`shioaji` only) |
| `--contract` | `TXFR1` | resolved by attribute, falling back to key lookup |
| `--duration-minutes` | none | stop after this long; otherwise runs until Ctrl-C |
| `--warmup-days` | `30` | calendar days requested for context + emitter priming (~21 night sessions) |
| `--out` | `outputs/live_<ts>` | **relative** — resolves against the CWD you launch from |
| `--state-file` | `<out>/../live_restart.json` | [RestartGuard](startup.md) state |
| `--no-capture` | off | skips raw tick capture; V1's live half needs it |
| `--max-daily-loss-points` | none | [rails](state.md) `FLATTEN` threshold |
| `--kill-file` | none | presence flattens and exits at the next bar |
| `--staleness-seconds` | `900.0` | **backstop** — halt after this long unhealed. Detection is [FeedWatch](feedwatch.md)'s job (§2.22) |
| `--feed-check-seconds` | `60.0` | silence before asking the venue whether it traded without us. A polling interval, not a threshold |

## Exit codes
| Code | Meaning |
|---|---|
| `0` | clean |
| `1` | unhandled exception |
| `2` | credentials not in the environment |
| `3` | [RestartGuard](startup.md) refused to start (backoff, backfill cap, or quota headroom) |
| `4` | **flatten incomplete** — a position was still open after a `FLATTEN` halt |
| `5` | refused before the first bar — no acknowledgement flag, or a held position that cannot be adopted (above `max_position`, or an `ExitRule` is configured) |

## Start-up order (each step gates the next)
1. **`RestartGuard.begin`** — *before* login, before any query. Every start
   performs a backfill, so a crash loop becomes a login+query loop, and Shioaji
   answers that with a suspension that further logins extend.
2. **VRAM floor** (5 GiB) — the model needs ~4.4 GiB, and a research run with
   `--batch-size auto` sizes itself to fill whatever is free.
3. **Login**, then `check_usage` — over quota, Shioaji returns *empty* history
   rather than erroring.
4. **Resolve the contract**, printing the resolved `code` and `delivery_month`,
   and the date the run plans to roll off it ([roll.md](roll.md)).
5. **Adopt any held position** (§2.20) — before the backfill, so an unadoptable
   one costs seconds instead of a full warm-up.
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
[live/runtime.py](runtime.md) · [live/roll.py](roll.md) · [live/startup.py](startup.md) · [live/recorder.py](recorder.md) · [live/capture.py](capture.md) · [live/broker/dryrun.py](broker/dryrun.md) · [SHIOAJI_PLAN.md](../../SHIOAJI_PLAN.md)
