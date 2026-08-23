# Day-Session Port — Migration Plan

**DRAFT, written 2026-08-23. Nothing built, no decisions taken, no run made.**
Four decisions at the end are the user's and are not assumed anywhere above them.

Porting the pinned strategy from the **NIGHT** session (15:00–05:00) to the
**DAY** session (08:45–13:45). The deliverable this plan eventually produces is
the task-oriented porting guide that [docs/DOCS_PLAN.md](docs/DOCS_PLAN.md)
records as known debt ("write it after V5, as part of Phase INT") — that guide
says *how to run it*; this says *what has to become true first*.

Inherited ground rules, unchanged: **standalone first, validate, then integrate**;
**every run as short as it can be and still answer its question**; and the
ownership boundary from [SHIOAJI_PLAN.md §2.12](SHIOAJI_PLAN.md) — **autoresearch
owns what the strategy does, the live path owns doing it faithfully.**

---

## 0. The headline — this is a research task first and a wiring task second

[docs/live/sessions.md](docs/live/sessions.md) states that porting is
`SessionPolicy.day()` at the entry point and nothing else. **That is true of the
plumbing and false of the deployment.** The plumbing claim survives reading every
consumer (§1 below). What it does not cover:

1. **There is no tuned DAY configuration to deploy.** `PINNED` traces to a
   `results.tsv` row measured on `TXF_RPT_minute_night.csv`, and §2.12 says in
   terms that its parameters must not be assumed to transfer. A DAY deployment
   has nothing to pin, and `tests/test_live_config.py` will say so.
2. **There is no way to express a DAY deployment.** `cli.py:314` is `cfg = PINNED`
   — no flag, no override, and `LiveConfig` is frozen.
3. **Four constants and defaults were sized on the night tape** and are wrong by
   2.8× or worse on the day one (§2, C4–C7). None of them fails silently, which
   is the good news; three of them fail at start-up, which is a wasted market
   window each.
4. **The 85-day warm-up a DAY run needs cannot be fetched** — expired months are
   delisted, so most of that window does not exist at the venue (§2, C7). This is
   the largest practical obstacle and the one with no precedent in the night work.

So the port decomposes into: **what ports free** (§1), **what has to be built**
(§2), **what has to be measured** (§3), in that dependency order.

### 0.1 What this touches — it is not a Shioaji-only plan

**The backtest core needs no changes and gets none.** `run.py` never names a
session: it reads `market.session` off `classify_session` and tags realized PnL
with whatever that returns (`run.py:238`). `real_data.py`'s `DEFAULT_DATA` is
*already* `TXF_RPT_minute_day.csv` (`real_data.py:38`) — §2.12's finding was that
no tuning run ever *used* it, not that it does not work. So the research half of
this port is a `--data` argument, not a build, and SHIOAJI_PLAN's standing ground
rule holds unchanged: `run.py`, `execution.py`, `trader.py` and `portfolio.py`
stay unmodified.

| | Shioaji / `live/` | Offline harness | Untouched |
|---|---|---|---|
| C1 tuned DAY config | the pin lands in `live/config.py` | **the work is here** — a `real_data.py` run and a `results.tsv` row | |
| C2 config selection | `live/cli.py` | | |
| C3 rolled-leg session | `live/runtime.py` | | |
| C4 `ROLL_AT` | the constant is in `live/roll.py` | **the measurement is here** — RPT day file, offline | |
| C5 cache path | `live/cli.py` | | |
| C6 warm-up sizing | `live/runtime.py`, `live/cli.py` | | |
| C7 delisted months | **a venue constraint** | the fix (cache seeder) reads the RPT file | |
| C8 `forced_close` | | **a strategy decision** (SPEC §4.7), not a live one | |
| the event loop | | | `run.py` · `execution.py` · `trader.py` · `portfolio.py` |

Read down the middle column and the honest summary is: **the code changes are
almost all in `live/`, and the things that decide whether the port is worth doing
are almost all not.**

---

## 1. What ports free — verified by reading, not assumed

Every one of these was checked against the code on 2026-08-23. They are listed
because "it should just work" is exactly the claim this project keeps paying for,
and because the guide will need to say which of them were confirmed how.

| | Why it ports | Evidence |
|---|---|---|
| Bar filter | `TickAggregator` asks `policy.label(ts) in policy.sessions` | `live/bars.py:227-234` |
| Staleness rail | `same_session_instance` compares the trading day, not the label | `live/sessions.py:148` |
| FeedWatch gate | Gated on `policy.includes(now)` — a DAY policy simply falls quiet 13:45–08:45 | `live/cli.py:899` |
| Daily-loss anchor | Re-anchors on `policy.trading_day(bar.ts)` change; a DAY session sits wholly inside its own date | `live/state.py:145` |
| Weekend (§2.25) | `is_trading_day` tests the *trading* day; a DAY session never crosses midnight, so it is the easier case | `live/sessions.py:110` |
| **Re-login anchors (§2.24)** | Anchors are 08:40 and 14:55 for *every* deployment — the plan already re-derived this for a DAY policy: the anchors are the same two times, and the **night** pre-open then carries the 17 h 45 m interval | SHIOAJI_PLAN §2.24 |
| **`kbars` right-labelling** | The correction that shifts `08:46 … 13:45` to `08:45 … 13:44` was **derived and validated on the DAY session** — here the day path is the proven one and the night path the extrapolation | `live/backfill.py:138-146` |
| Roll *net* (`RollWatch`) | Watches the alias, not the clock | `live/roll.py` |
| Backfill traffic | 300 bars/day against 840 — a DAY window is 2.8× cheaper **per calendar day**, though C6 makes the window 2.8× longer, so the byte cost lands about level | §2.8b |

**Nothing in `live/` names a session** except the four sites in §2. That much of
the 2026-08-11 design held up.

---

## 2. What does not port — with severity and a proposed fix

### C1 — no tuned DAY configuration exists · **BLOCKING · research**

`live/config.py::PINNED` carries `source_dataset=…TXF_RPT_minute_night.csv` and
`policy=SessionPolicy.night()`. Every knob in it — the ±0.00045 band,
`min_prob=0.5`, `min_trend_ticks=3.0`, `path_momentum=True`, stride and horizon 30
— is the endpoint of a long night-session search recorded in `results.tsv`.

`tests/test_live_config.py::test_pinned_row_is_still_the_best_known_config`
asserts the pinned commit is the best row in that file. A DAY row cannot simply
be added: on Calmar the two are not comparable, and whichever won would silently
re-pin the *other* deployment. **`results.tsv` has no session column.**

**Proposed:** the DAY config is tuned on `TXF_RPT_minute_day.csv` and lands as a
second pinned value — `PINNED_DAY` — with its own provenance and its own drift
test keyed to a session-tagged row. See decision **D2** for how much searching
that means.

**By what mechanism, precisely.** There is no autoresearch entry point to invoke:
`live/config.py`'s own docstring records that `strategy/wiring.py` is that seam
and *"its entrypoint no longer exists"*, and that the winning night configuration
was produced through **`real_data.py` CLI flags** instead — which is what the
`edit:` / `keep:` commit pairs in the git log are. A DAY search is therefore the
same loop against `--data <day file>`, and the plan should say so rather than
gesture at a tool that is not there.

### C2 — the entry point cannot express a DAY deployment · build, small

`cli.py:314` is `cfg = PINNED`, and `LiveConfig` is frozen. There is deliberately
no `--session` flag, and adding a bare one would be the 2026-08-11 defect
returning by the front door: a flag that changes the policy without changing the
*strategy* would run night-tuned parameters on a day tape, and nothing would
detect it.

**Proposed:** select the whole config, not the session — `--config night|day`
resolving to `PINNED` / `PINNED_DAY`, with the policy travelling inside the value
it belongs to. One flag, no `dataclasses.replace`, and the banner stays honest
because provenance is a field on the config.

### C3 — `settle_rolled_leg(session="NIGHT")` · build, one line · **silent**

`live/runtime.py:285` books a roll-settled leg into the NIGHT bucket by literal
default. On a DAY deployment every rolled round-trip would be attributed to a
session the run never traded, corrupting the `trading_day` / `trading_night`
split `compute_metrics` produces. **The only genuinely silent defect on this
list.**

**Proposed:** derive it from the fill's own timestamp (`classify_session`) or from
the policy, plus the regression test that fails on the literal.

### C4 — `ROLL_AT = 15:30` was measured for a night strategy · **measurement**

`live/roll.py:183-190` states the derivation outright: the expiring month's final
*night* session starts at 15:00 the day before its last trading day, and its
first half hour is the most liquid stretch either month sees. For a DAY
deployment 15:30 is **outside the traded session entirely** — the clock tick
still fires, so the ladder would run, but it would run unwatched by the strategy
and priced off a tape the run is not otherwise following.

The DAY analogue is presumably the first half hour after 08:45 on the same lead
day, for the same liquidity reason. **Presumably is not measured**, and §8 of the
main plan is a list of what that habit has cost.

**Proposed:** re-derive from the RPT day file the way the night figure was
derived — per-minute volume on both months across the last three rolls — and
carry `ROLL_AT` on the policy or the config rather than as a module constant, so
two deployments cannot share the wrong one. Offline; no market needed.

### C5 — `outputs/bars_cache.csv` is shared across deployments · build, small

`cli.py:502` hardcodes one cache path for every run. A DAY run writes DAY bars
into the file a NIGHT run reads. The result is **not** silent — `warm_up` raises
at `runtime.py:562` with "the backfill used a different SessionPolicy" — but it
would block whichever soak started second, and the night soak is the one
currently accruing V4's fill-gap number.

**Proposed:** key the cache by the policy (`bars_cache_day.csv` /
`bars_cache_night.csv`). Only matters if both run on one machine — decision **D1**.

### C6 — emitter priming: the 30-day default is a night-sized number · **build, quantified**

This is the one that costs a start-up. `_prime_emitter` replays up to
`emitter_prime_boundaries = 500` forecast boundaries out of the backfill; below
**3** accumulated samples `_check_primed` refuses to start, because
`_trend_confirms` fails **open** and would trade the signals the filter exists to
veto. K=500 was measured, not guessed: only **3.74 %** of the pinned run's 40 443
signals are non-HOLD, and the longest observed run of consecutive HOLDs is **385
boundaries**.

Bars required for a full 500-boundary prime: `3008 + 500 × 30` = **18 008**.

| | bars/session | trading days needed | `--warmup-days` needed | boundaries at the default 30 |
|---|---|---|---|---|
| NIGHT | 840 | 21.4 | ~30 — i.e. the current default **is** the design point | **487** |
| DAY | 300 | 60.0 | **~85 calendar days** | **109** — 22 % of the design |

At 109 boundaries and a 3.74 % non-HOLD rate the expected sample count is ~4,
against a floor of 3. **A DAY run on today's defaults sits one quiet fortnight
away from refusing to start**, and that tail is exactly what K=500 exists to
cover.

**Proposed:** derive the warm-up window from the policy's bars-per-session rather
than carrying a calendar-day default, so the number is right for any session by
construction. Whether 85 days of history is *reachable* is C7.

### C7 — an 85-day warm-up cannot be fetched · **the hard one**

Two constraints collide:

- **Expired months are delisted** (§2.8 Finding D). An 85-day window spans ~3
  delivery months, of which the older two no longer answer `kbars`.
- **`cli.py::_earlier_months` fetches `earlier[-2:]`** — the front month plus one.
  Also a night-sized number: it covers ~one roll back, which is what 30 days
  needs and a third of what 85 needs.

The bar cache exists precisely because a delisted month has no other source — but
**the cache starts empty** and can only fill forward. For NIGHT that was
survivable (START HERE simply accepts "the cache starts empty and fills from this
run forward" over a 30-day window). For DAY it means the first run is short by
roughly two months of context and cannot recover except by waiting two months.

**Proposed:** seed `bars_cache_day.csv` offline from `TXF_RPT_minute_day.csv` —
the same file the DAY config would be tuned on — in the cache's own four-column
schema. ~40 lines, offline, testable, and it lets the first DAY run start with
the context the model was evaluated against instead of a truncated one. It also
raises a question the guide must answer: RPT and `kbars` are two bar sources, and
V1c compared them on **one** live session. Decision **D4**.

### C8 — `forced_close=False` inherits a night-measured risk profile · **research**

§2.12 settled `forced_close=False` on a measured fact: median holding period **4
days 11 hours**, 89 % of trades spanning more than one calendar day, so a
boundary close would truncate the strategy rather than protect it. That number is
a property of the *night* configuration; the DAY equivalent is unknown until C1
produces one. A DAY position held with `forced_close=False` rides the night
session and the weekend unmanaged — the mirror image of the night deployment's
day-session exposure, and not obviously the same trade.

The `HaltAction.FLATTEN` weakness is worse here and otherwise unchanged: a
flatten is `LMT`+`ROD`, ROD is cancelled at session end, and a DAY session hands
that failure a **13:45 close every single day** instead of 05:00. The aggressive
order type in V4's scope is what closes it, and here it is a prerequisite rather
than a nice-to-have.

---

## 3. Order of work

Offline throughout except where marked. Each step names what makes it done.

```
D0  Session-tag results.tsv + a DAY row schema             -> verify: test_live_config green with two pins
D1  C3 fix (rolled-leg session) + C5 (cache path)          -> verify: new tests fail against the old literals
D2  C4 - re-derive ROLL_AT from the RPT day file           -> verify: a measured table in this doc, like 2.6's
D3  C7 - cache seeder from TXF_RPT_minute_day.csv          -> verify: seeded cache passes warm_up's policy check
D4  C6 - policy-derived warm-up window                     -> verify: DAY asks ~85d, NIGHT still 30d
D5  C1 - tune on the day file, real_data.py (scope: D2)    -> verify: a pinned row + reproduction, 2.13-style
D6  C2 - `--config day` at the entry point                 -> verify: dryrun banner shows DAY provenance
D7  offline parity for DAY, V0-style                       -> verify: live loop = run_backtest, bit-identical
D8  5-minute dryrun inside a real DAY session   [MARKET]   -> verify: start-up, warm-up, banner, first bar
D9  soak                                        [MARKET]   -> verify: V5's terms, applied to DAY
```

D0–D4 are independent of the research and can proceed whenever. **D5 is the long
pole and everything downstream waits on it.**

---

## 4. Decisions needed before D0 — the user's, not assumed

- **D1 — replace or run alongside?** Two deployments on one machine share the
  cache (C5), the restart state file, the quota, and the ≤5 connections per
  `person_id`. One process trading `SessionPolicy.both()` is a third option and a
  different strategy again — neither config was tuned on the union.
- **D2 — how much re-tuning?** A full `edit:`/`keep:` search on the day file, or
  first a cheap **transfer baseline**: run the night-pinned parameters over
  `TXF_RPT_minute_day.csv` unchanged and read the Calmar. The baseline costs one
  ~4-minute run and answers whether the port is worth the search at all.
- **D3 — sequencing.** DOCS_PLAN's 2026-08-11 decision was "after V5", reasoning
  that V4/V5 could still change the procedure. V5 is not passed, and the next roll
  — the first live exercise of `roll.py` — is 2026-09-15. Does the offline half
  (D0–D4) start now in parallel, or does all of it wait?
- **D4 — RPT-seeded cache.** Seeding from `TXF_RPT_minute_day.csv` mixes two bar
  sources in one context window; V1c compared them on a single session and found
  the label convention differed. Acceptable, or does the port instead wait ~2
  months for a cache to fill from live?

---

## 5. Not covered here

The day-session **backtest result** itself (that is autoresearch's output, D5);
anything about real money (V4's fill gap is a hard gate for either session); and
the porting *guide*, which is this plan's eventual deliverable and cannot be
written as a procedure until D8 has been run at least once.
