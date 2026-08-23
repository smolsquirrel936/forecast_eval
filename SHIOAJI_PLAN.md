# Shioaji Integration Plan

Bringing the `forecast_eval` backtest stack to Sinopac **Shioaji** simulation
order placement (<https://sinotrade.github.io/zh/>).

Ground rule from the request: **standalone module first, validate, then
integrate.** Nothing under `run.py` / `execution.py` / `trader.py` changes until
Phase V5 passes. The live path is additive.

Second ground rule (added 2026-08-12): **make every test run as short as it can
be and still answer its question.** Derive the duration from what is being
tested, never from habit or a round number.

- Most live checks are **start-up** checks — contract resolution, position
  adoption, warm-up, the banner. All of that is finished before the first bar,
  so the run is over in minutes and the rest is idle loop time.
- Ask what the *next* minute would add. If the answer is "another identical bar",
  the run is already long enough.
- Long runs are not free: they are bound to market hours, they consume the daily
  traffic quota and restart budget (§2.8b), and a session spent on a run that
  proved nothing is a session that cannot be re-run.
- **The exception is the soak**, where duration *is* the measurement — V5 asks
  for 5 consecutive unattended days, and V4's fill gap needs orders that only
  time produces. Say so explicitly when a run is one of these; everything else
  should be defended in minutes.

This is a correction of a real habit: a 45-minute shakedown was proposed for the
§2.20 resume fix when every line it touches runs before the first bar. Ten
minutes covered it.

## ▶ NEXT SESSION — START HERE (written 2026-08-23)

**744 tests green. §2.24 is PROVEN LIVE. §2.6 — the contract roll — was found by
a dead soak and is now built, tested and undemonstrated.** Nothing offline is
blocking. What is left needs a market: one probe, one shakedown, one soak.

### 0. Do these three things, in this order

**1 — run the IOC probe (any open session, ~45 s).** It answers the one
assumption inside the roll ladder that is still ours rather than the venue's.

```powershell
uv run python -m forecast_eval.live.ioc_probe --i-understand-this-places-orders --control-rod
```

Unfillable orders by construction (a BUY 200 points below the market, one lot,
`simulation=True` asserted). It must run **inside a session** — off-hours the
venue rejects rather than expires, which is a different answer. ⚠️ The SDK's own
banner lines carry the national ID; only the redacted paste-back block is safe to
copy (§2.26).

*What it decides:* §2.19 saw an expired IOC leave `has_pending()` **True**. The
ladder submits only when nothing is pending, so if the venue never reports an IOC
expiry the ladder would be inert while logging beautifully. Today
`IOC_TTL_SECONDS = 3` covers that from our side. A clean report, or a report
shaped like a cancel, changes nothing; a **rejection** would change the order type.

**2 — shake the start-up down (5 minutes, any time).**

```powershell
uv run python -m forecast_eval.live.cli --broker dryrun --duration-minutes 5
```

The start-up path, the warm-up, the clock block and the order path have all
changed since the last live run. On this project the *first* live run of new code
is where things are found — §2.22b crashed a soak 42 bars in with green tests
behind it.

**3 — restart the soak** (weekday, ~14:45 so the 14:55/14:59 anchors fire inside
15 minutes):

```powershell
uv run python -m forecast_eval.live.cli --broker shioaji --i-understand-this-places-orders
```

The front month is now **202610** (`TXFI6` expired 2026-09-16 — check the banner).
The bar cache starts empty and fills from this run forward.

### 1. What the last soak proved, and what killed it

`outputs/live_20260817_134515`, 2026-08-17 13:45 → 2026-08-19 15:15 — **49 h**.

**§2.24 is settled.** It crossed the 24 h wall at 2026-08-18 13:45 and kept
answering REST afterwards: `api.usage()` at 14:59 (25 h 13 m after the first
token) and `snapshots()` a minute later. 3 re-logins, 3 verifies, 0 failures,
peak token age **17 h 45 m 00 s** — the designed ceiling to the second.
`force_refresh=True` resets the clock; it is not a cosmetic session swap.

**It died on the contract roll (§2.6), and the manner matters.** TXFH6 expired on
2026-08-19; the run held the object `TXFR1` resolved to at start-up. FeedWatch
read it right — "the venue traded without us", 15 times — and its repair
re-subscribed the same retired object: **15 attempts, 0 ticks**, then a staleness
halt at 15:15. Nothing was at risk (flat, simulation), but a soak cannot outlive
a roll without the fix below.

Also found: the verify window **caught it and talked itself out of it** — the
recovery re-login succeeded, so the window settled `RELOGGED`/INFO for a session
that was already dead. Fixed: the window now re-checks 20 s after the recovery.

### 2. Built since (2026-08-19 → 08-23), all offline-tested, none demonstrated

| | |
|---|---|
| `live/roll.py` | `RollSchedule` (the **plan** — 15:30 the day before the last trading day) and `RollWatch` (the **net** — the alias flip, which can only settle a position, never trade it) |
| the ladder | roll legs go out **LMT+IOC**, priced 5/10/15/20/25 ticks through, 10 s apart, clock-driven; capped because §2.19 says the venue matches on prints |
| the splice | `collapse_front_month` follows the **roll calendar**, not volume — measured: all three RPT files carry one contract per minute, so the evaluated series was always calendar-spliced |
| the cache | `outputs/bars_cache.csv`, with a `contract` column, because expired months are delisted and only a local copy holds the pre-roll side |
| accounting | `build_trades(roll_fills=…)` books a rolled position as **one** round-trip; `TradeRecord.rolled` counts boundaries |
| `live/ioc_probe.py` | written, never run — see step 0 |

**The next roll is 2026-09-16; the roll fires 2026-09-15 15:30.** That is the
first time any of this executes for real.

**Two facts worth keeping to hand** (they cost a session each to learn):

- `TokenError → AuthError → ShioajiError`; import from `sj`, since
  `shioaji.error.TokenError` is deprecated — the sixth wrong form the docs have
  offered. A logged-out session raises `AuthError: Not authenticated`; an expired
  token raises `TokenError: Token is expired`.
- The SDK **cannot be read.** `Shioaji` is a compiled `_core.pyd` with no source
  and no docstrings on `login` / `logout` / `subscribe`. Introspection is
  exhausted; a probe is the only remaining instrument, which is why there are
  four of them in `live/`.

### 3. Everything since `402c179` is uncommitted

26 modified files, 6 new (`live/roll.py`, `live/ioc_probe.py`, their docs and
tests) — ~2 000 lines. The same standing risk as before, back again.

### 4. What is still open, in rough priority

1. **V4's fill-gap number is still zero.** 49 h of soak produced 56 forecasts,
   56 signals, **0 orders** — every forecast fell inside the ±0.00045 band. At the
   pinned stride the rate is 0.18–0.37 orders/night, so this accrues in months,
   not weeks. It is a **hard gate before real money**.
2. **V5 is far from passed** — 5 unattended days, a kill-and-restart against a
   *strategy* position (never done), a network drop (never seen).
3. **The weekend has never been crossed live** (§2.25 is fixed offline).
4. **An SDK self-reconnect dropping callbacks is unmeasured** — the re-assertion
   closure is in FeedWatch's repair path as a precaution, untested against a real
   socket drop.
5. **No external watcher** for `kill -9` / power loss — deferred by decision.

### 5. Older V5 items, unchanged

- **A deliberate kill-and-restart against a *strategy* position.** §2.20 is
  verified live, but only against a lot a probe left behind.
- Fill data accrues in whatever soak runs next — do not start a separate one.

### 6. Built 2026-08-12/13 (all live-verified unless noted)

| § | What |
|---|---|
| §2.20 | **Position resume** — a restart adopts the venue's size *and* entry price into both mirrors. Verified live: adoption reported 45219.00, matching a probe fill from 75 minutes earlier, exactly |
| §2.21 | **The staleness watchdog was killing healthy runs.** A 151 s gap between prints tripped a 120 s window 31 s before the market traded again |
| §2.22 | **FeedWatch** — on 60 s of silence, ask the venue instead of guessing. The threshold became a *polling interval*; the rails' timer became a 900 s backstop. `state.py` untouched |
| §2.22b | **The first live run of FeedWatch crashed on a type.** `Snapshot.ts` is an `int` of epoch ns. Fixed, with the fakes rebuilt from measured types |
| §2.23 | **Alerts** — one `emit`, severities `FATAL`/`WARN`/`INFO`, sinks subscribe to the levels they want. Trades alert as INFO with the round trip's own PnL. **The user will attach Telegram** — that is one `add_sink` call and no other change |

### 7. Still gated, still true

**V4 is PARTIAL and is a hard gate before any non-simulation deployment.** Its
fill-model gap number was never collected. See the "V4 is PARTIAL" decision below
for the measured stride table and the three conditions.

**Do not use `v4_probe` for fill data.** Its shadow is fed synthetic prints; its
summaries record as `probe_fill_gap_summary` precisely so they cannot be swept
into the V4 aggregate.

**Deferred by decision, not forgotten:** an external watcher for `kill -9` /
power loss (nothing in-process can report its own instant death — `live_restart.json`'s
`last_heartbeat` is the hook); the day-session porting guide (→ Phase INT); the
seam losing one minute per start (§2.14 addendum).

### 8. How this project has actually made progress

Every hard question here was settled by **making an instrument answer it**, never
by reasoning from one sample — and the times that rule was skipped are the ones
that cost a market window (§2.18), three wrong diagnoses in a row (§2.19), and a
crashed soak (§2.22b). Two specific habits worth keeping:

- **Verify SDK shapes by introspecting the installed package.** The docs have
  been wrong five times now.
- **Build fakes from measured types, not from what the code expects.** A fake
  that shares the code's assumption tests the assumption, not the system —
  exactly how 14 green tests hid §2.22b.

---

## ▶ Previous START HERE (2026-08-11, after the session-open run)

**Where things stand:** three V3 dry runs are done and analysed — 10 min, 60 min
across the 05:00 close, and 20 min across the 15:00 open. **simtrade is
confirmed** (53 prints, all pre-open, all dropped — §2.15). The open run found a
defect the quiet ones could not: the aggregator emitted **20 bars for 15
minutes**. That, and two others it exposed, are fixed (§2.16, §2.17), along with
§2.14 and `api.Contracts` from the previous round. **336 tests green.**

**Nothing is blocked on analysis. The next step is a run.**

**Priority order:**

1. **A validation run in a BUSY window** — this is the gate for everything
   below. Five changes have landed since the last run and *none* has been
   exercised live: §2.14's backfill truncation and `bar_anomaly` detector,
   §2.16's `_last_emitted` guard and 2 s flush grace, §2.17's `read_ticks` parse,
   `api.contracts`, and the new `received` column.

   **Busy matters.** The 04:11 run at 6.8 ticks/min produced zero duplicates; the
   15:00 run at 68 ticks/min produced five. A quiet window cannot validate the
   §2.16 fix because it never reproduces the bug. Use a session open, or US
   hours (~21:00+).

   Checks on the artifacts afterwards, all cheap:
   - **zero `bar_anomaly` records**, and **bar count == distinct minutes**;
   - `warm_up`'s `last` strictly earlier than live `bar` `idx=0`;
   - no `api.Contracts` deprecation line on the console;
   - **`received - timestamp` distribution** — this is the measurement that turns
     `LIVE_FLUSH_GRACE = 2 s` from a judgement call into a number. Raise it if
     the tail runs long.
2. **The `kbars` ↔ ticks diff** — V1's true live half, and the last thing
   standing between here and V4. **The tool is built** (`live/v1_check.py`, 13
   tests), so this is now one command against the capture from step 1:

   ```powershell
   uv run python -m forecast_eval.live.v1_check --capture outputs/live_<ts>/ticks.csv
   ```

   Read-only — no orders, no subscription. Exit 0 on PASS. It replays the
   capture, fetches `kbars` for the same window, and prints a per-minute diff;
   PASS needs ≥99.5 % of closes matching **and** zero unmatched minutes on either
   side. The replay gotcha is now enforced in code rather than remembered: it
   flushes only as far as the last tick the live process saw, because flushing
   past it fabricates a bar live never completed.

   Use a *post-fix* capture. The pre-fix one still works for the replay side
   (already verified: `live_20260811_145506/ticks.csv` → 15 bars for 15 minutes,
   one per minute), but its **live** bars carry the §2.16 duplicates, so it
   cannot show that the live path and the venue now agree.
3. **V4** — wire `ShioajiBroker` into the CLI (deliberately absent today, see
   §7.2) and give `HaltAction.FLATTEN` an order type that actually closes.
   `forced_close=False` is **settled** (§2.12): live runs the tuned build, and if
   the day-session gap turns out to need managing, autoresearch produces that as
   a new config. The flatten work is unaffected — it serves the daily-loss and
   kill-file rails, which must close a position that may be days old.

   **Test the flatten deliberately with the kill file.** A daily-loss breach
   cannot be waited for on demand, but `--kill-file` triggers the same
   `FLATTEN` path on the next bar — the only practical way to exercise it, and
   the one rail whose failure mode (`flatten_incomplete`, exit 4) is silent
   without a real order behind it.

**Standing constraint:** live commands must be run by the user. Their Shioaji
credentials are session-scoped in their own shell and are not reachable from the
assistant's shell (verified: absent at process, user and machine scope). Run
*artifacts*, however, are readable directly — `night\outputs\` was granted
2026-08-11, so no round-trip is needed to analyse a completed run.

---

## Status

| Phase | State | Evidence |
|---|---|---|
| **A — Broker seam, SimBroker, LiveLoop, TickAggregator, Recorder, SafetyRails** | **COMPLETE** | `live/{loop,bars,recorder,state}.py`, `live/broker/{base,sim}.py`; 56 tests, no `shioaji` import anywhere |
| **V0 — offline parity** | **PASSED** | `tests/test_live_parity.py`, 4 configs × 39 725 in-session bars, bit-identical fills/signals/PnL; verified non-vacuous by two mutation checks |
| **V1 — bar aggregator** | **PASSED, both halves** (2026-08-11). Offline: event-for-event equality with `Environment`. Live: **39/39 closes match `kbars` exactly**, zero unmatched minutes, zero volume differences | `tests/test_live_bars.py` on 3 000 real RPT bars; `live/v1_check.py` on `outputs/live_20260811_205731` |
| V1b — contract/rollover | **no longer blocking** — `TXFR1` chosen outright (§7.4), so there is nothing to select between; reduces to logging + a pre-CLOSE contract assertion | — |
| **V2 — Shioaji adapter vs. a fake** | **PASSED**, then **corrected against reality** by the §7.2 probe — enum guess confirmed, alias-vs-resolved contract-code bug found and fixed | `live/broker/shioaji.py` + `tests/fakes/fake_shioaji.py`; 42 tests |
| **§7.2 — futures ordering in simulation** | **✅ CONFIRMED WORKING** (probe 2026-08-10) — V3/V4 unblocked | `live/v72_probe.py`; 6 safety tests + a live run |
| **V1c — `kbars` convention** | **PASSED** — and it found a real mismatch: `kbars` is **right-labelled** and carries the night session. Both corrected in `backfill.py`. Content diff moved to V3 (expired contracts are delisted). | `live/backfill.py` + `live/v1c_check.py`; 35 offline tests (`test_live_backfill{,_sessions}.py`, `test_v1c_check_helpers.py`) + 3 live runs 2026-08-10 |
| **Session scope** | **DECIDED** (§2.12) — **NIGHT** only, `forced_close=False`. The strategy holds a **median 4.5 days**; the three defects that followed from getting this backwards are all fixed. | `params.json` + 174 trades all tagged NIGHT |
| **V3 — live dry run** | **dry runs PASSED** (10 min 2026-08-11, then 60 min across the 05:00 close). Found and fixed the seam defect §2.14. Outstanding: simtrade + off-session filter (§2.15) and the `kbars`↔ticks diff | `outputs/live_20260811_034*`, `outputs/live_20260811_041125` |
| **V4 — sim orders + fill gap** | **PARTIAL** (2026-08-12) — code complete and proven against the venue; the fill-model **gap number was not collected** (deferred, see the decision). **Hard gate before real money.** A 49 h soak (2026-08-17) added **nothing**: 56 forecasts, 56 signals, **0 orders**, every one inside the ±0.00045 band. At 0.18–0.37 orders/night this accrues in months. | `live/broker/shadow.py`, `live/v4_probe.py`; §2.19 |
| V5 — soak | **ATTEMPTED 2026-08-17 → 08-19 (49 h), ended by §2.6.** It settled **§2.24**: the run crossed the 24 h wall and kept answering REST 25 h in, with peak token age 17 h 45 m — `force_refresh=True` genuinely resets the clock. It then died on the **contract roll**: the alias `TXFR1` is dereferenced once at start-up, FeedWatch re-subscribed the retired month 15 times for 0 ticks, staleness halt. Fixed by `live/roll.py` (a calendar **plan** plus the alias **net**, an IOC aggression ladder, a calendar splice and one-logical-trade accounting) — **none of it demonstrated; the next roll is 2026-09-16**. Still owed: 5 unattended days, a kill-restart against a *strategy* position, a weekend, and V4's fill gap accruing in the same run | `live/{relogin,roll,ioc_probe}.py`, `live/sessions.py`; 744 tests |
| INT | not started | — |

**All pre-V3 work is complete (2026-08-11).** Environment rebuilt and verified
(GPU detected, 275 tests pass, no skips); the live config is pinned with a drift
guard (`live/config.py`); latency measured (§2.7); the pinned backtest
**reproduces exactly** under the rebuilt environment (§2.13). **V3 is unblocked.**

### 2.13 Environment reproduction check (2026-08-11)

The venv was rebuilt from `UV_SETUP.md` and several packages resolved newer than
the documented table — torch 2.12.1→**2.13.0**, numpy 2.4.6→**2.5.2**, gluonts
0.16.3→**0.17.0**, lightning 2.4.0→**2.6.5**, scipy 1.15.3→**1.18.0**. Since
`live/config.py` pins the strategy config to a research run produced under the
*old* versions, the obvious question was whether the numbers still hold.

Re-ran the pinned config (`e44a047`) over the full night dataset — 1 733 348
bars, 40 443 forecasts, **244.5 s** with `--precompute`:

| | recorded (`results.tsv`) | reproduced | delta |
|---|---|---|---|
| trades | 185 | **185** | 0 |
| net PnL (pts) | 39 075.6312 | **39 075.631200** | **0.000000000** |
| max drawdown (pts) | −2 856.9506 | **−2 856.950550** | rounding only |

**Bit-identical.** The version drift changed nothing that reaches a trade
decision, so pinning drops from urgent to housekeeping — see the recommendation
in `UV_SETUP.md`.

> **Loose end — the recorded Calmar is not `net / |dd|`.** Those figures give
> **13.6774**, while `results.tsv` records **13.6742** (~0.02 % apart, 4th
> significant figure). Since net and drawdown reproduce exactly, the difference
> is in how the autoresearch loop derived the ratio, not in the strategy. Harmless
> for now — `live/config.py` pins the *recorded* value and its drift guard
> compares like with like — but the Calmar column should not be treated as
> re-derivable from the harness's own metrics until this is explained.

Cheap enough (4 minutes) to be the standard gate after any environment change —
and strictly more informative than pinning, since it verifies the numbers rather
than a proxy for them.

§7 fully answered 2026-08-10. **§7.2 is now confirmed empirically: futures
`place_order` WORKS under `simulation=True`** (probe: New `op_code='00'` →
Submitted → Cancelled, flat afterwards). V3 is no longer gated on it.

**§2.12 is settled (2026-08-11): NIGHT only, `forced_close=False`.** No decisions
are outstanding from the user. The next input needed is not a decision but a
*run* — see START HERE.

`run.py`, `execution.py`, `trader.py`, `portfolio.py` are **unmodified** — the
seam is duck-typed, as designed.

---

## 0. Executive summary

The harness already has the right seams. `Forecaster`, `SignalEmitter`,
`ExitRule` and the `Trader` state machine are pure decision logic and transfer to
live **unchanged**. Only three things are backtest-specific and must be replaced:

| Backtest component | Live replacement | Why it can't transfer |
|---|---|---|
| `Environment.stream()` | Shioaji tick callback → 1-min bar aggregator | Push, not pull; ticks not bars |
| `Execution.check_fill()` | `api.place_order` / `cancel_order` + deal callback | Fills are *reported*, not *inferred* |
| `Portfolio` | Same class, but reconciled against `api.list_positions` | Broker is the source of truth |

The plan introduces a `Broker` interface with **four** implementations as built —
`SimBroker` wrapping the existing `Execution` (offline parity), `DryRunBroker`
(real decisions, nothing sent), `ShioajiBroker` (the venue), and `ShadowBroker`
wrapping a venue broker so an offline `Execution` sees the same orders and the
two can be diffed (V4's fill-gap instrument) — plus a live loop mirroring
`run_backtest`'s four steps exactly.

**Three things the build added that the design did not foresee**, each because a
live run found it and none visible from the code: the process must survive its
own **session token expiring at 24 h** (§2.24), it must know that a **weekend is
not a trading day** (§2.25), and it must tell **a quiet market from a broken
feed** by asking the venue rather than by waiting (§2.22).

**The single most important validation** is V0: run the *live loop* over the
*historical tick DataFrame* through the *sim broker* and assert the fills and PnL
are byte-identical to `run_backtest`. That proves restructuring the loop from
pull to push changed no decision logic. Everything after that measures the gap
between the simulated fill model and reality.

---

## 1. Shioaji facts this plan relies on

Verified from the docs (2026-08):

- **Simulation mode**: `api = sj.Shioaji(simulation=True)`, then
  `api.login(api_key=..., secret_key=...)`. Available in sim: `place_order`,
  `update_order`, `cancel_order`, `update_status`, `list_trades`,
  `list_positions`, `list_profit_loss`, plus quote subscribe/unsubscribe, kbars,
  ticks, snapshots. The only documented exclusion is 興櫃 (emerging-stock board)
  and 零股 (odd lots). **The docs never state that futures ordering works in
  simulation** — the order APIs are listed without product qualification and the
  page never mentions 期貨/選擇權/TXF. Treat as unverified until the V3 probe
  script confirms it (see §7.2).
- **Futures order**: `sj.FuturesOrder(action=sj.Action.Buy|Sell, price=…,
  quantity=…, price_type=sj.FuturesPriceType.LMT|MKT|MKP,
  order_type=sj.OrderType.ROD|IOC|FOK, octype=sj.FuturesOCType.New|Cover|Auto|DayTrade,
  account=api.futopt_account)`, placed with
  `trade = api.place_order(contract, order)`. Returns a `Trade` with
  `status.status` in `PendingSubmit / Submitted / Filled / PartFilled / Cancelled / Failed`.
  `api.update_status(api.futopt_account)` populates `ordno`, which
  `cancel_order` / `update_order` need.
- **Quotes**: `api.subscribe(api.Contracts.Futures.TXF.TXFR1, quote_type=sj.QuoteType.Tick)`
  with `@api.on_tick_fop_v1()` receiving `(exchange: Exchange, tick: TickFOPv1)`.
  Tick fields include `code, datetime, close, volume, total_volume, tick_type,
  bid_side_total_vol, ask_side_total_vol, **simtrade**`.
- **Order/deal callbacks**: `api.set_order_callback(cb)`, `cb(state: OrderState, msg: dict)`.
  `OrderState.FuturesOrder` msg carries `operation{op_type, op_code, op_msg}`,
  `order{id, seqno, ordno, action, price, quantity, order_type, price_type,
  market_type, oc_type}`, `status{exchange_ts, modified_price, cancel_quantity,
  order_quantity}`, `contract{code, delivery_month, …}`.
  `OrderState.FuturesDeal` msg carries `trade_id, seqno, ordno, exchange_seq,
  action, code, price, quantity, ts, market_type`.
  **Docs warn deal events can arrive before the order event** — correlate order
  `id` ↔ deal `trade_id`.
- **Package**: `shioaji` 1.7.2 on PyPI, `requires_python >= 3.7`, classifiers
  through 3.14, **no mandatory dependencies**. Installing it cannot drag in or
  downgrade `torch`, so the cu130 pin in `UV_SETUP.md` is safe.

Sources are listed at the end.

---

## 2. Semantic gaps between the backtest and live

This is the part that decides whether the live numbers resemble the backtest.
Each gap gets a named validation below.

### 2.1 The fill model is optimistic — biggest risk

`strategy/wiring.py` currently runs `aggression_ticks: 0`. With offset 0 the
entry limit sits *exactly at the last print*, so `Execution` classifies **every**
order as marketable (`BUY: limit >= current`, `SELL: limit <= current`) and fills
it at the **next print's price**, guaranteed.

In a real book a BUY limit at the last trade price is *joining the bid queue*. It
fills only when a seller crosses to you — which is exactly the adverse-selection
case. The backtest's "always fills, next print" is a materially favourable
assumption at offset 0.

Implications:
- Live fill rate will be **below** 100 % and fills will be **adversely selected**.
- Options: (a) keep LMT at last price and accept misses (measure them);
  (b) raise `aggression_ticks` to ≥1 so the limit genuinely crosses;
  (c) use `FuturesPriceType.MKP` (range market). **Recommendation: (b) for live**,
  and re-run the backtest at that `aggression_ticks` so backtest and live agree
  on the same assumption. V4 measures the residual.
- Do **not** silently switch live to MKT — that breaks parity with every
  backtest number in `results.tsv`.

### 2.2 Order lifetime — §4.4 must be enforced explicitly

Backtest: *every* new signal, including HOLD, cancels the pending order. Live:
`OrderType.ROD` rests until end of session. So the live loop **must** call
`api.cancel_order(trade)` on every signal boundary, or a stale limit outlives its
forecast — violating §4.4 and silently changing strategy behaviour.

Cancellation is asynchronous: after `cancel_order` the order is not gone until
the `op_type="Cancel"` order callback with `op_code="00"` arrives. The live loop
needs a small pending-order state machine (`SUBMITTING → WORKING → CANCELLING →
DEAD`) rather than the backtest's synchronous `_pending = None`.

Using `IOC` instead would sidestep cancellation but changes semantics: the
backtest lets a limit rest across the whole 30-bar stride. Flag as a config knob
(`--order-type {ROD,IOC}`), default ROD to match the backtest.

### 2.3 `octype` mapping

`OrderEvent.intent` already carries `OPEN`/`CLOSE`. Map directly:
`OPEN → FuturesOCType.New`, `CLOSE → FuturesOCType.Cover`. Do **not** use `Auto`
— explicit intent is what the no-flip invariant is built on, and `Auto` would
mask a position-mirror desync instead of surfacing it.

### 2.4 Ticks → bars — **convention now established empirically**

Measured directly from `TXF_RPT_minute_day.csv` (1 124 790 rows,
2011-01-03 → 2026-05-15), so this is no longer an assumption:

- **1 minute, left-labelled** — bar `T` covers `[T, T+1min)`. The day session
  runs 08:45–13:45 and the file's bars run 08:45 … 13:44, exactly 300 per full
  day. Right-labelling is ruled out.
- **Empty minutes are omitted entirely.** 194 of 3 756 days carry fewer than 300
  bars (minimum 90) and **no bar has volume 0** — so the producer skips a
  tradeless minute rather than zero- or forward-filling it. The aggregator does
  the same.
- **`close` is the bar's price**, as `load_minute_bars` already assumes.

Two documented beliefs did **not** survive contact with the file, and both are
worth correcting elsewhere:

- **There are zero NaN closes in this file** — not ~17 %. `CLAUDE.md` and
  `real_data.py`'s docstring both claim ~17 % NaN "session gaps / halts"; that
  must describe a different file (the combined `TXF_RPT_minute.csv`, or the
  legacy tick schema). The `dropna` in `load_minute_bars` is a no-op here.
- **There are zero multi-contract minutes** — so the highest-volume front-month
  `groupby(...).idxmax()` collapse in `load_minute_bars` is *also* a no-op on
  this file; the producer already did that selection upstream. This matters for
  §2.6: the day file cannot be used to validate `TXFR1` against volume-max
  selection, because the file never exercises it.

One day carries 301 bars (one minute past the expected 300) — unexplained, not
blocking, flagged for V1.

Implemented in `live/bars.py`; the offline half of V1 is
`tests/test_live_bars.py::test_matches_offline_pipeline_on_real_rpt_bars`, which
replays 3 000 real bars through the aggregator and requires event-for-event
equality with `Environment`. The remaining half needs captured live ticks.

### 2.5 `simtrade` ticks must be dropped

TAIFEX broadcasts trial-matching (試撮) prints before session open; Shioaji flags
them `simtrade=True`. They are not real trades. If they reach the bar aggregator
they corrupt the model context; if they reach a fill model they'd fabricate
fills. **Hard filter at ingest.**

### 2.6 Contract selection and rollover

Backtest: highest-volume contract per minute (front month, auto-rollover).
Live: `api.Contracts.Futures.TXF.TXFR1` (nearest-month continuous alias).
These are *usually* the same but diverge around rollover when volume migrates
before the alias does. Log both the resolved `code` and `delivery_month` on every
bar and every order; assert they match the position's contract before submitting
a CLOSE. → **V1b**.

#### The alias is resolved once, at start-up — FIXED 2026-08-19, unproven live

`resolve_txf` is called from a single site (`cli.py:405`), before the loop. The
alias is dereferenced there and the resulting **contract object is what the run
holds for its whole life** — every `subscribe`, every order, every reconciliation.
`TXFR1` tracking the front month is a property of *reading* the alias, and a
long-running process reads it exactly once.

So a soak spanning a roll keeps trading a contract the venue has retired.
`capture.py:57` already notes the code can change at rollover; nothing acts on it.

#### MEASURED 2026-08-19 — the roll killed the soak, and the repair was inert

`live_20260817_134515` started 2026-08-17 and resolved `TXFR1` →
`delivery_month=202608`; all 96 624 captured ticks are stamped `TXFH6`. That
contract's last trading day was the third Wednesday, **2026-08-19**, and it
traded a normal day session to 13:44:59. Then:

```
04:59:00  last bar of the night session          (bar 1657, the last bar ever)
13:44:59  TXFH6's final print
14:55:00  re-login, ok — re-subscribes the same expired contract
14:59:00  verify_failed: "no tick since the re-login (count still 96624)"
14:59:05  recovery re-login succeeded → reported RELOGGED, INFO, window settled
15:00:00  feed_check MISSED — and 15 more, once a minute
15:15:00  HALT: no market data for 900.2 s
```

**The mechanism: one contract object, two different resolutions.** The venue kept
answering `api.snapshots()` with a *live* front month — `venue_ts` advancing and
volume climbing 575 → 1 796 through the new session — while the subscription
delivered nothing at all. So `subscribe` binds to the month the alias resolved to
at start-up (`target_code=TXFH6`, now expired), whereas `snapshots` re-resolves
`TXFR1` at call time and follows the roll. FeedWatch read that gap exactly right
— "the venue traded without us", 15 times — and its repair could not close it:
**re-subscribing the old object is inert, 15 attempts for 0 ticks.** A roll is
repaired only by re-reading the alias from `api.Contracts` and subscribing the
*new* object.

**The verify window caught it and then talked itself out of it — FIXED
2026-08-19.** The tick half fired correctly at 14:59, one minute before the open,
exactly its design case. But `_run_verify` took one recovery re-login and settled
on *that call* succeeding, without re-running the check — so a session still
receiving no ticks reported `RELOGGED`/INFO instead of `VERIFY_FAILED`/FATAL. The
loudest alert in the system stayed silent while the run bled to a staleness halt
sixteen minutes later.

The window now stays open after the recovery and **re-checks 20 s later**, which
decides it: `VERIFIED` or `VERIFY_FAILED`, either way ~40 s before the open. The
grace is load-bearing rather than padding — the check's tick half asserts a print
since the last re-login and the recovery re-login *resets that baseline*, so an
immediate re-check would fail by construction and make every recovery a FATAL. A
re-check still owed when the window closes is a `VERIFY_FAILED` too; nothing may
leave the window unsettled. 6 new tests, `tests/test_live_relogin.py` now 54.

Nothing was at risk — flat account, simulation-only — but **V5's
five-unattended-day criterion cannot be met across a roll** without either a
restart or code that re-resolves.

The cheap operational answer is a restart after settlement, which re-reads the
alias. The design question — re-resolve in-process on a schedule, or on an
expiry check, and how a *held position* migrates when the contract under it
retires — is untouched, and the second half of it is the hard part: the strategy
holds a median 4.5 days, which is longer than the gap between a roll and the
position that straddles it.

#### The fix — `live/roll.py`, built 2026-08-19

**Detection.** `RollWatch` re-reads the alias and compares `delivery_month`,
driven from two triggers the CLI already has: after a successful re-login (in the
pre-open quiet), and on a FeedWatch `MISSED` — the verdict the dead soak produced
fifteen times. A 5-minute throttle keeps the reactive trigger from turning a
genuine feed outage into a venue call a minute. Nothing raises out of `check`: it
runs inside a liveness path, so a failed read is recorded and retried rather than
ending a healthy run.

`refresh` (`api.fetch_contracts`) is injected **separately** from `resolve`,
because whether a `force_refresh` re-login re-downloads the contract table is
unmeasured and unmeasurable from a compiled `_core.pyd`. Calling it explicitly
means the fix does not rest on that assumption.

**Application.** `cli.py:_apply_roll` moves all four holders of the expired
object together — the name the closures read, the broker's contract *and* its
accepted-code set (`switch_contract`, which rebuilds rather than adds, so a
settled leg cannot keep counting toward `broker_position()`), the subscription,
and the verify window's tick baseline. Forgetting any one of them is what makes a
roll look repaired while staying broken.

**The position.** The alias moves only *after* the old month is dead, so by the
time a roll is detectable the venue has already cash-settled anything held —
there is no order that could close it. `settle_rolled_leg` closes the leg in both
mirrors through the same two calls a venue fill uses, at the last price this
process saw, recorded with `price_is_estimated: true` because the real number
comes from a settlement auction we never see. The size is then re-opened on the
first bar of the new month through the ordinary signal path, which is what
rolling the exposure means once the old leg is beyond reach.

**The warm-up.** `fetch_bars_continuous` splices: cache + every listed month,
collapsed to one row per minute by highest volume — `real_data.load_minute_bars`'s
own rule, so a warm-up spanning a roll is the same front-month-continuous series
the model was evaluated on. The local cache exists because **expired contracts
are delisted** (§2.8 Finding D): after the settlement the pre-roll bars are
unreachable from the venue, and only a local copy still has them. Without this a
restart near a roll warms up on the new month's thin back-month history, where
3 008 bars span weeks rather than ~2 days.

**37 tests** — 28 in `tests/test_live_roll.py`, 3 on the broker's code set in `tests/test_shioaji_adapter.py`, and 6 on the verify window's re-check in `tests/test_live_relogin.py`; suite **691 green**. The scheduled roll that followed is below. All of it is
offline — **none of it has run against a real roll.** The next settlement is the
third Wednesday of September 2026 (2026-09-16); until then the evidence is that
the September contract resolves and the run stays alive, not that the roll path
works.

#### The scheduled roll — BUILT 2026-08-23

The reactive path above is a net, not a plan: the alias only flips **after** the
old month is dead, so it can never roll a position, only settle one. The plan
moves while both months still trade.

| | `RollSchedule` — the plan | `RollWatch` — the net |
|---|---|---|
| Driven by | the calendar | the alias's `delivery_month` |
| Fires | 15:30 the day before the last trading day | after the expiry |
| Position | **closed on the old month, re-opened on the new** | settled; the venue got there first |

**Why 15:30 that evening.** For a NIGHT strategy the expiring month's final night
session starts 15:00 the day *before* its last trading day — the 15:00 session on
the last trading day already belongs to the next month. 15:30 is the most liquid
half hour either contract sees (the incoming month is thin, so the roll wants the
busiest moment, not the calmest), and it is ~13 h clear of the contract's end, so
a failed roll has the whole session behind it. 04:00 also satisfies "an hour
before the close" and is the worst hour of the tape: 8–12 lot bars on both
contracts at once. The window stays open an hour because the roll is two orders
that both have to fill.

**No holiday table, deliberately.** `last_trading_day` computes the naive third
Wednesday. If a holiday moves the real one *later* we roll early — harmless. If
*earlier*, we roll late and `RollWatch` catches the expiry, which is the path that
already existed. A table would replace two survivable errors with one silent
dependency maintained for ever. What is checked instead is the tape: before
rolling, both months are snapshotted and the incoming one must be printing. The
gate is "is the month we are about to trade alive", **not** "has volume crossed
over" — at the roll instant the expiring month usually still has the larger
volume, so a crossover test would refuse every correct roll. Both volumes are
recorded, which is how the calendar gets validated from data later.

**The sequence is driven by observation, never a timer.** `request_roll` →
`submit_exit` on the old month → the close *fills* → `switch` → the CLI moves the
contract → `resume_roll` → entry on the new month → that fills. A close that does
not fill stays visible as `closing` instead of advancing into a switch that would
strand the old lot.

#### The roll ladder — BUILT 2026-08-23, and one assumption inside it

**Market orders were asked for and rejected on evidence.** The request was that
the roll be guaranteed to execute; `MKT` does not deliver that here. §2.19
measured that Shioaji's simulator **matches on prints, not on a resting book** —
in a print-less minute no price fills, so aggression is irrelevant and a market
order buys nothing the ladder does not. In production it would fill, but it gives
up the bound: the worst fill stops being knowable before the order is sent, in a
night-session back month. `MKP` is the right shape and its band semantics are
still unverified.

**What was built instead: an escalating-aggression ladder, IOC.**

| attempt | 1 | 2 | 3 | 4 | 5 | 6+ |
|---|---|---|---|---|---|---|
| ticks through | 5 | 10 | 15 | 20 | 25 | 25 |

- Equal **5-tick steps** (the operator's call, over doubling) — every rung's cost
  is knowable in advance.
- **Capped at 25**, because past a point aggression stops helping for the §2.19
  reason and only spends the bound. Beyond it the ladder retries at the cap.
- **IOC, not ROD** — and this inverts §2.19's flatten conclusion *only* because a
  retry now exists. Nothing rests, so re-pricing needs no cancel and §2.2's
  asynchronous cancel/replace race never happens: **the retry is the re-price.**
- **10 s between rungs, not the 5 s first proposed.** `max_orders_per_minute` is
  10 and a breach is a **STOP halt** — halted mid-roll while still holding the
  expiring contract is the worst state reachable here. 10 s is 6/min for one leg,
  inside the cap with room for the other, and longer than `IOC_TTL_SECONDS` so
  attempt N+1 cannot race attempt N's expiry.
- **Clock-driven**, because a bar is a minute wide and the point is to retry
  inside one. Only submission moved off the bar path; fill collection stays the
  loop's, so the single-consumer invariant is untouched.

**The assumption, stated because it is load-bearing.** The ladder submits only
when nothing is pending, and §2.19 saw **an expired IOC leave `has_pending()`
True**. Whether the venue reports an IOC expiry is UNMEASURED —
`live/ioc_probe.py` answers it and needs a live session, which was not available.
So the broker does not depend on the answer: an IOC unresolved after
`IOC_TTL_SECONDS` (3 s) stops counting as pending on our side, **retired rather
than dropped** so a late fill is still booked. If the probe shows the venue does
report, this becomes a backstop and nothing else changes. If it shows the venue
reports something *else* — a rejection, a different `op_type` — that is the one
finding that could still change the design.

**Simultaneous legs were rejected.** Closing the old month and opening the new one
at the same instant is unrepresentable in the current state model: one
`self.contract` per broker, one signed int per mirror, one month's code set for
reconciliation. A buy that fills against a sell that does not leaves two lots
across two contracts, `max_position=1` violated and the reconciliation rail
halting into that state. The cost of sequencing is ~1 point of tracking error (a
median night minute moves 0.0095 %, ~4 points at 45 000, and the gap is one bar);
the cost of double exposure is hundreds.

**15 further tests** — 7 on the ladder, 8 on the broker's IOC path and local
deadline; suite **744 green**. `tests/test_ioc_probe.py` (11) covers the probe's
own reading and redaction.

#### MEASURED 2026-08-23 — the splice rule is a calendar, and volume never was

The question was whether the bar splice should follow volume (as
`load_minute_bars` appears to) or the calendar. It is settled, from the data:

| file | rows | minutes carrying >1 contract |
|---|---|---|
| `TXF_RPT_minute_night.csv` | 1 733 348 | **0** |
| `TXF_RPT_minute_day.csv` | 1 124 790 | **0** |
| `TXF_RPT_minute.csv` | 2 858 138 | **0** |

**`load_minute_bars`'s highest-volume collapse is a no-op on every dataset this
project has evaluated on** — there was never anything to choose between. The
evaluated series is spliced by **calendar**: across 108 seams in the night file,
106 land exactly one day after the expiring month's last trading day, all stamped
00:00, each preceded by a ~19 h gap (the last trading day's 15:00–23:59 block is
absent from the file entirely).

**The seam is large.** Median jump **95 points / 0.70 %**, mean 155 pts, max 760
pts (8.0 %); ordinary 1-min |return| runs median 0.0095 %, p99 0.099 %. So **95 %
of seams exceed the ±0.00045 band**, and the model was evaluated on a series
carrying one ~0.7 % discontinuity a month — basis *plus* a whole day session's
move, because of the 19 h hole. Calmar 13.6742 already includes whatever that
does.

Three things follow, and all three are now implemented:

- `collapse_front_month` takes `front_month_at` and follows the roll calendar,
  with volume kept only as the tie-break for a minute the front month did not
  print. At the roll instant the expiring month usually still has more volume, so
  a volume rule would keep feeding the cache a contract we had stopped trading.
- **No revalidation is needed** — the offline series does not change. It was
  already calendar-spliced; only live moves to match it.
- **Splice raw, no back-adjustment.** Live switches mid-session, so its seam is
  pure basis (~0.05 %) — roughly 14× *smaller* than the evaluated one. Adjusting
  would move away from the evaluated convention, not toward it. Live's seam also
  lands ~1.5 days earlier, which is unavoidable: an expired contract cannot be
  traded and the marks must sit in the same contract as the position.

The bar cache now carries a `contract` column so the rule stays re-derivable —
live NIGHT data is the only place the real roll behaviour will ever show up.

#### One logical trade across a roll

`build_trades(roll_fills=…)` banks a roll's closing leg instead of ending the
round-trip, and resumes at the re-entry's price; `TradeRecord.rolled` counts the
boundaries crossed. Two trades would split a median 4.5-day hold in half and
inflate the trade count, so per-trade metrics would stop being comparable with the
backtest, where a held position simply crosses a splice. Opt-in: omit it and
behaviour is byte-identical. A roll whose re-entry never arrives is still booked,
at the price the position actually left at. `LiveRuntime.roll_fills` collects the
legs and the trail records them, so a session can be re-scored months later.

**26 further tests** — 7 on the schedule and calendar, 7 on the roll sequence, 5
on the calendar splice and the cache's contract column, 7 on the trade
accounting; suite **715 green**. Still entirely offline: **none of this has run
against a real roll.** The next is 2026-09-16, and the roll fires 2026-09-15
15:30.

### 2.7 Latency

Backtest: an order placed at tick *t* is checked against tick *t+1* — an implied
one-bar (1-min) latency. Live budget:

```
bar close → aggregator emits → Toto2 forecast → emitter → trader → place_order → ack
```

Stride is 30 bars = one decision per 30 minutes, so there is enormous headroom —
but the *forecast itself* is now a single un-batched call (the `--precompute`
GPU-batching path does not exist live).

#### MEASURED 2026-08-11 — RTX 5090, PINNED config (Toto-2.0-1B, ctx 3008)

| | |
|---|---|
| Warm single forecast | **p50 87.2 ms · p90 88.8 ms · max 89.3 ms** (n=12, min 86.7) |
| Cold first forecast | **18.8 s** — disk→GPU load + first inference, checkpoint already cached |
| VRAM: model + context | **4.33 GiB** (peak torch allocation 4.14 GiB) |
| VRAM free afterwards | 25.87 of 31.84 GiB |
| Budget | 89 ms against an 1 800 s stride → **~20 000× headroom**, 0.005 % utilisation |

**Latency is a non-issue and §2.7 is closed.** The distribution is also
remarkably tight (3 % spread min-to-max), so there is no tail risk to design
around.

Two things the measurement *did* settle, neither of them about latency:

- **Pre-warm the model at startup anyway** — not for headroom, but because the
  first forecast costs 18.8 s while every later one costs 0.09 s. Issue a
  throwaway forecast during warm-up so the first *real* signal is not delayed.
  (18.8 s assumes a populated HF cache; a genuinely first-ever run adds the
  3.9 GB download on top.)
- **§7.6 VRAM contention now has a number.** Live needs **~4.4 GiB**, leaving
  ~26 GiB free. The hazard is directional: a research run with
  `--precompute --batch-size auto` probes *free* VRAM and sizes its batch to fill
  it, so starting research **first** can leave live unable to load at all.
  The startup check should refuse to start below ~5 GiB free.

### 2.8 Warm-up and restart — **nothing waits in real time**

Stating this unambiguously, because the earlier draft invited the opposite
reading: **the process never waits for live bars to accumulate.** All context is
*backfilled from history* at startup. Startup cost is one API call plus one
batched GPU pass — seconds to a minute — after which the loop is immediately
live and fully armed.

Two pieces of state must be warm before the first live decision:

**1. Forecaster context — 3 008 bars, fetched not waited.**
`Toto2Forecaster` reads a trailing `context_length=3008` 1-min bars. These come
from `api.kbars(contract, start, end)` in a single historical request and are
pushed straight into `LiveLoop`'s history buffers. Real-time waiting never
enters into it.

**How long accumulating them live would actually take**, since it sets the cost
of getting the backfill wrong:

| Subscription | Bars/day | Time to reach 3 008 |
|---|---|---|
| **Day only** (08:45–13:45) — matches the current dataset | ~300 | **~10 trading days ≈ 2 calendar weeks** |
| Day + night (adds 15:00–05:00) | ~1 140 | ~2.6 trading days |

Measured: `TXF_RPT_minute_day.csv` holds 1 124 790 rows over 3 756 days ≈ 299.5
bars/day (median exactly 300). *(An earlier draft of this section said "≈2.5
trading days" — that silently assumed day+night accrual and is wrong for the
day-only configuration actually in use.)*

Note the model does not strictly *require* 3 008 bars to run — `_prepare_context`
truncates to a multiple of `patch_size`, so it would happily forecast off a
shorter window. That is the trap: it would produce plausible numbers from a
context the strategy was never evaluated on. Matching the backtest, not
satisfying the model, is what forces the full 3 008.

**2. Emitter deques — small, and reconstructible.**
This is the *only* thing question 5 was ever about, and it is much smaller than
the context. `ThresholdEmitter._short_prices` is `deque(maxlen=short_window+1)`
= **4 entries**, and `_trend_confirms` fails **open** (returns `True`, i.e.
trades) while `len < 3`. So an un-warmed restart runs with the trend filter
disabled for at most ~3 signals — at `stride=30` that is ~90 minutes, not 3 008
bars.

The two emitters accumulate differently, which matters:

- `ThresholdEmitter` appends only inside `_trend_confirms`, which `_decide`
  reaches **only for BUY/SELL** — a HOLD returns early and appends nothing.
- `VolMultiTfEmitter` appends to both deques unconditionally at the top of
  `_decide`, for every forecast.

**Chosen approach — replay, don't hand-build.** Do not reconstruct the deques by
poking at them; replay historical forecasts through `emitter.emit_signal(...)`
and let the emitter fill itself. That is correct for both emitters by
construction, and immune to the HOLD-skip asymmetry above. Concretely at startup:

1. `api.kbars` for `context_length + K*stride` bars. **K = 500, not the ~100
   estimated here originally** — see the measurement below. That is ~18 008 bars
   ≈ 21 night sessions ≈ 4 calendar weeks; confirm `kbars` returns that span in
   one call (V1c only measured a 10-day request).
2. Run the K forecast boundaries through `forecaster.forecast_series_batch(...)`
   — the same batched GPU path `run.precompute_forecasts` already uses.
3. Feed those forecasts through `emitter.emit_signal(...)`, **discarding the
   results** — they must not reach the Trader.
4. Assert the deque is full (`len(_short_prices) >= 3`). If it is not, **refuse
   to start** — below 3 samples `_trend_confirms` returns `True`, i.e. it fails
   **open** and trades signals the filter exists to veto. Refusing is
   recoverable (widen the window); trading unfiltered is not.

#### Why K = 500 — measured, not estimated (2026-08-11)

The original K≈100 was a guess, and it was wrong for the reason that matters.
`ThresholdEmitter` appends to its deque **only on non-HOLD signals**, so priming
depends entirely on how often replayed forecasts act. From the 40 443 signals of
the reproduction run:

| | |
|---|---|
| non-HOLD | **1 513 of 40 443 = 3.74 %** (1 223 BUY, 290 SELL) |
| boundaries for ~3 non-HOLD *on average* | ~80 |
| **longest observed run of consecutive HOLDs** | **385 boundaries** (~11 550 bars, ~14 night sessions) |

So K=100 tracks the *average* and fails the *tail*: a restart during a quiet
stretch would have primed **nothing** and started with the filter open, silently.
K=500 clears the worst observed run with ~115 boundaries of margin. The cost is
negligible — batched forecasts run ~6 ms each (40 443 in 244 s), so 500 is ~3 s.

Implemented in `live/runtime.py` (`_prime_emitter` / `_check_primed`), with the
check skipped when the emitter has no rolling window or its trend filter is off.

Zero real-time dead period, and the filter is correctly primed from bar one.
This is strictly better than either option originally offered.

#### Open gap this creates — `kbars` is an unvalidated second bar source

Warm-up and live now draw bars from **different places**:

| Path | Source | Validated by |
|---|---|---|
| Live bars | Shioaji ticks → `TickAggregator` | V1 (§2.4) |
| Warm-up bars | `api.kbars` | **nothing** |

So the 3 008-bar context feeding the *first* forecasts after every start and
every restart comes from a source whose convention has never been checked. If
`kbars` right-labels, forward-fills empty minutes, or resolves a different
contract, the model's input is subtly wrong exactly when it is least observable.
This is §2.4 again for a source V1 does not touch.

**→ V1c — code READY, verdict PENDING (needs you to run it).**

- `live/backfill.py` — `kbars_to_bars()` / `fetch_bars()`, the production
  converter. Written to the RPT convention: left labels, close as price, NaN
  closes dropped, and **zero-volume rows dropped** (the RPT file provably has
  none, so any filler `kbars` returns is not an observation the model ever saw).
  11 offline tests, including a real-data equivalence test that wraps 3 000 real
  RPT bars in a kbars-shaped payload and requires byte-identical output to
  `load_minute_bars`.
- `live/v1c_check.py` — read-only runner. **Places no orders, subscribes to
  nothing.** Simulation login only.

```powershell
$env:SHIOAJI_API_KEY="..."; $env:SHIOAJI_SECRET_KEY="..."
uv run python -m forecast_eval.live.v1c_check --date <a past trading date>
```

It answers four questions and prints a PASS/FAIL for each:

1. **Span** — does one request return ≥3 008 bars, or is it capped (→ paging)?
2. **Label** — left or right edge? Day session 08:45–13:45 ends at **13:44** if
   left-labelled (RPT), **13:45** if right. A right-labelled result means every
   backfilled bar must be shifted back one minute before use.
3. **Empty minutes** — omitted (RPT behaviour) or zero-volume filler?
4. **Content** — closes vs `TXF_RPT_minute_day.csv`, needing ≥99.5 % and zero
   unmatched minutes on either side.

It also brackets the fetch with `api.usage()`, which **replaces the ~1 MB
estimate in §2.8b with a measured number** and sizes the backfill cap honestly.

#### V1c results — run 2026-08-10, `--date 2026-05-14`, TXFR1

| # | Question | Result |
|---|---|---|
| 1 | Span | **PASS** — 9 902 bars for a 10-calendar-day request. ≥3 008 in one call; **no paging needed.** |
| 1b | Traffic cost | **MEASURED** — 561 468 bytes for 9 902 bars ≈ **57 bytes/bar**. A 3 008-bar backfill ≈ **170 KB**. `limit_bytes=524 288 000` confirms the **500 MB floor tier**, so one backfill is ~0.03 % of the daily quota. The 10/day cap is hugely conservative; leave it. |
| 2 | Label | **RIGHT-LABELLED — MISMATCH FOUND AND FIXED.** See below. |
| 3 | Empty minutes | **PASS** — zero zero-volume rows; omitted like RPT. |
| 4 | Closes vs RPT | **FAIL, but not a data problem** — contract mismatch, see below. |

**Finding A — `kbars` returns day *and* night; the evaluated data is day-only.**
The returned bars span 00:00–23:59, because TXF trades a night session
(15:00–05:00). `TXF_RPT_minute_day.csv` is **08:45–13:45 only** — verified across
all 1 124 790 rows (`distinct hours: [8,9,10,11,12,13]`). Unfiltered backfill
would make 3 008 bars mean ~2.6 mixed-session days instead of the ~10 day-session
days the model was evaluated on. **Fixed:** `fetch_bars(..., sessions=("DAY",))`
is now the production default, with 7 tests. *This also invalidated my own label
test, which read the edges of a 24-hour range (00:00/23:59) and so could not see
the day session at all.*

**Finding B — `TXFR1` is not a historical continuous series.** It resolved to
`delivery_month=202608` — the contract that is front month *now* (run date
2026-08-10). The RPT file's contract on 2026-05-14 was **202605**. The diff
therefore compared two different futures, which is why only 3 of 300 closes
matched. This is not evidence about labelling or data quality. It does confirm
§2.6 concretely: **a backfill window that crosses a rollover gets the new
contract's thin pre-roll history, not the front-month series the model expects.**
Operationally the exposure is small — a 5–10 day backfill from *today* normally
sits inside one contract — but it is real around roll dates and must be handled.

**Finding C — the 301-bar day is explained.** 2026-05-14 runs 08:45…**13:45**
inclusive. A 13:45 bar is the **closing call auction**, present on ~1 200 of
1 124 790 rows (~32 % of days). It is harmless by construction:
`classify_session` defines DAY as `08:45 <= t < 13:45`, so `Environment` already
drops it in the backtest — and `TickAggregator` and the backfill filter drop it
identically. Consistent on all three paths; no action needed.

**Finding D — expired contracts are delisted, so the historical diff is
impossible via this route.** `--list-contracts` (2026-08-10) returns only live
months:

| code | delivery_month | | code | delivery_month |
|---|---|---|---|---|
| `TXFH6` | 202608 | | `TXFL6` | 202612 |
| `TXFI6` | 202609 | | `TXFC7` | 202703 |
| `TXFJ6` | 202610 | | `TXFF7` | 202706 |
| `TXFR1` | 202608 (alias → `TXFH6`) | | `TXFR2` | 202609 (alias → `TXFI6`) |

Three nearest months plus three quarterly — exactly TAIFEX's listing rule, so the
set is complete rather than truncated. **202605 is gone**, and with it any
possibility of diffing `kbars` against `TXF_RPT_minute_day.csv`: every date the
file covers (it ends 2026-05-15) belongs to a contract that no longer exists.

Also decoded: **TXF month letters are plain alphabetical, A=Jan … L=Dec** — `H`
is August, `I` September, `L` December, `C` March. This is *not* the CME
F/G/H/J/K/M/N/Q/U/V/X/Z convention, so May 2026 would have been `TXFE6`, not the
`TXFK6` a standard-convention guess produces. Recorded because it will matter
whenever a contract is named explicitly.

**Consequence — the content check moves into V3.** Rather than matching a
three-month-old file, cross-validate the two *live* sources against each other:
capture a session's ticks during the V3 dry run, aggregate them with
`TickAggregator`, and diff against `kbars` for the same session. V1 already
established offline that `TickAggregator` reproduces the RPT convention exactly,
so agreement there closes the loop transitively — and it tests the sources
actually in use rather than a historical artefact.

**Finding E — `kbars` is RIGHT-labelled. This was the bug V1c existed to find.**
Re-run 2026-08-07 with the day-session window applied:

```
bars on 2026-08-07: 1134 total, 300 in the 08:45-13:46 window
first=08:46 last=13:45
```

300 bars either way, but stamped one minute later than RPT's 08:45…13:44. So
`kbars` bar `T` covers `(T-1min, T]` while RPT's covers `[T, T+1min)` — the same
intervals under different names.

**Fixed:** `kbars_to_bars(source_label="right")` (the default) subtracts one
minute. The shift renames bars; it moves no prices.

**The ordering is load-bearing.** The shift runs *before* session filtering,
because DAY is `08:45 <= t < 13:45`: an unshifted `13:45` bar — the session's
genuine final minute — classifies as `None` and would be silently dropped, losing
one real bar every single day while the other 299 were each misattributed by a
minute. Covered by `test_shift_happens_before_session_filtering` and
`test_a_full_day_session_survives_the_shift_intact` (08:46…13:45 → exactly 300
bars, 08:45…13:44).

`v1c_check.py`'s label section now passes `source_label="left"` so it keeps
measuring raw stamps — applying the corrective shift there would make a
right-labelled feed report itself as left-labelled and hide the very thing being
checked. Its verdicts are inverted accordingly: RIGHT is now the expected result,
and LEFT would mean the API changed and the shift has become wrong.

**Had this gone unmeasured**, every backfilled bar would have carried a
one-minute-wrong label and each day would have lost its closing minute — feeding
Toto2 a context misaligned against the series it was evaluated on, on every
process start, with nothing downstream able to detect it. `v1c_check.py` is fixed — it now restricts to the 08:45–13:46
window before reading the edges, warns explicitly on contract mismatch, and
accepts `--contract TXF202605` for a like-for-like historical diff. **Re-run
needed**, ideally with a *recent* date so TXFR1 is the same contract the RPT file
used:

```powershell
uv run python -m forecast_eval.live.v1c_check --date <recent trading date>
```

### 2.8b API quotas — the backfill is cheap, the restart loop is not

Documented limits (futures):

| Limit | Value |
|---|---|
| Daily traffic | 500 MB (no trading) / 2 GB / 10 GB by 30-day volume tier; resets 08:00 on trading days |
| Market-data queries | ≤50 per 10 s across `ticks` / `kbars` / `snapshots` / `credit_enquires` / `short_stock_sources` |
| Intraday `kbars` | ≤270 queries |
| Orders | ≤250 per 10 s (`place_order`, `update_status`, `cancel_order`, …) |
| Subscriptions | ≤200 active |
| Connections | ≤5 per `person_id` |
| Logins | ≤1 000/day |

**The startup backfill is negligible.** ~6 000 1-min bars is at worst ~20 `kbars`
calls and on the order of ~1 MB — ~0.2 % of even the 500 MB floor tier. Assume
the floor tier applies; simulated trades almost certainly do not count toward
the volume thresholds. Our order rate (≈1 per 30 min) is three orders of
magnitude under the order cap, and we need exactly 1 of 200 subscriptions.

The design already matches the documented best practice — *query history after
hours, cache it, and take intraday data from subscriptions, whose pushes do not
count toward traffic* — because the loop does exactly one historical fetch at
startup and everything else arrives on the tick callback.

**The hazard is an unsupervised crash-restart loop.** Each restart is a login
plus a backfill. Per the docs, repeated rate-limit violations lead to *"IP and ID
access suspended"*, and repeated `login()` attempts during a suspension **extend
the penalty**. So the failure mode is not degradation — it is a banned account
that fights its own recovery.

**→ Rails — IMPLEMENTED** in `live/startup.py` (`RestartGuard`), 20 tests:

- **Restart backoff** — exponential (60 s → cap), keyed on consecutive *unclean*
  exits, persisted to a JSON state file since the process is what died.
- **Backfill cap per trading day** — refuse to start after N backfills (default
  10); the day rolls at **08:00**, matching Shioaji's own quota reset, so the cap
  and the limit it protects share one boundary.
- **`api.usage()` headroom check** — duck-typed on `remaining_bytes` so this
  module never imports `shioaji`. Refuses to start below a margin, because over
  quota Shioaji returns **empty** history rather than erroring — which would
  silently yield an empty model context instead of failing loudly.
- **No `login()` retry** — deliberately *absent* from the API. A test asserts no
  method containing "login"/"retry" exists, so there is nothing to reach for.

Not folded into `SafetyRails`: that class is deliberately pure (in-memory,
clock-free, no I/O) and protects a *running* loop. These protect against the
process being restarted and must own a file. Different lifecycle, separate class.

Two design points worth recording, both found by the tests:

- **Failure scoring is latched (`scored`) so it happens once per dead run, not
  once per `begin()` call.** The first implementation charged a failure on every
  attempt, so each *rejected* restart doubled the wait it was already serving —
  meaning a supervisor retrying on a fixed timer could never get back in. The
  penalty must track dead runs, not rejected attempts.
- **A crash after a healthy run (`healthy_runtime_seconds`, default 600 s) resets
  the ladder to a single failure** rather than inheriting the previous streak's
  backoff. One crash after eight good hours is not the fourth crash in two
  minutes.
- **A corrupt state file is charged as a failure, not treated as a clean slate** —
  otherwise a crash loop that damages the file would bypass the backoff entirely.

**→ V1c addition:** call `api.usage()` immediately before and after the first
backfill and record the delta. That replaces the ~1 MB estimate above with a
measured number, and sizes the backfill cap honestly.

### 2.9 Position reconciliation

`Trader.position` is a mirror. Live it must be checked against
`api.list_positions(api.futopt_account)` after every fill and on a timer.
On mismatch: **halt and alert**. Never auto-correct — a desync means a fill was
missed or double-counted, and trading on a wrong mirror is how the no-flip
invariant turns into an unbounded position.

### 2.10 Partial fills

`Execution` documents "no partial fills in this print-only sim". `Trader.on_fill`
accumulates signed quantity correctly for the *first* partial, but the
`prev == 0 and new != 0` entry-stamp branch means a second partial would need the
average-entry branch that is currently a comment
(`trader.py:209` — "v1 uses max_position=1 so this branch is unreachable").

With `max_position: 1` (current wiring) this is moot. **Constraint for live v1:
`max_position == 1`, asserted at startup.** Lifting it requires implementing
average-entry in `Trader.on_fill` first.

### 2.11 Sessions and the overnight process

`classify_session` is reusable as-is. Live additions: the process must survive
across midnight (NIGHT runs to 05:00 next day), and the Shioaji order
`market_type` is `"Day"`/`"Night"` — worth asserting it matches
`classify_session` on every order callback as a cheap clock/session sanity check.
`forced_close: False` in current wiring, so the boundary-close latch is not
exercised live in v1; keep the code path but leave it off.

---

### 2.12 Session scope — **the strategy is NIGHT-session, and not intraday**

> **Correction (2026-08-10).** An earlier draft of this section asserted the
> opposite — that the strategy was evaluated on the day session. That was wrong,
> and every conclusion drawn from it was wrong with it. Checked against the run
> artifacts:
>
> * `outputs/iter53_night_20260808_173343/params.json` →
>   `"data": "…\\TXF_RPT_minute_night.csv"`;
> * all 174 trades in that run are tagged `session=NIGHT`;
> * `results.tsv` line 1 reads "night-session ThresholdEmitter baseline".
>
> `real_data.py`'s *default* is the day file, but no tuning run used it.

**Established facts.**

| | |
|---|---|
| Evaluated session | **NIGHT only** (15:00–05:00) |
| `TXF_RPT_minute_night.csv` | 1 733 348 rows, 2017-05-15 → 2026-05-15, hours 15–23 and 0–5; `classify_session` → NIGHT for 1 733 269 of them |
| Night vs day size | 840 min/night vs 300 min/day — night is **2.8× larger** |
| 3 008-bar context | ≈ **3.6 night-session trading days** (not the ~10 quoted earlier for day bars) |

**The reframe that matters: this is not an intraday strategy.** From the 174
trades of the latest run:

* **median holding period 4 days 11 hours**; maximum **164 days**;
* **155 of 174 (89 %) span more than one calendar day.**

So with `forced_close=False`, positions already ride straight through the daily
05:00–15:00 gap — *including the entire day session* — plus weekends. The
backtest does capture that P&L (the exit is a real later night-session price, so
gap moves are realized), but it models the strategy as **unable to act** while
the day session runs. That is a faithful account of a night-only feed, not an
oversight.

#### Three defects — ALL FIXED, and the session is now a parameter

Fixed by introducing `live/sessions.py::SessionPolicy` — one immutable value
naming the sessions a deployment trades and its trading-day boundary — and
threading it through the backfill, the tick aggregator and the rails. **No module
names a session any more.** Porting to the day session is
`SessionPolicy.day()` at the entry point and nothing else.

1. **`fetch_bars` defaulted to `("DAY",)`.** Now takes a **required** `policy`;
   there is no default at all, because a wrong one is undetectable once it ships.
   `kbars_to_bars` keeps `policy=None` (no filter) as the low-level converter.
2. **Staleness watchdog.** Now skips when the last tick and `now` are not in the
   same *occurrence* of a traded session, and refreshes its marker so it re-arms
   cleanly at the next open. `same_session_instance` compares the trading day as
   well as the label — 04:59 tonight and 15:01 tomorrow are both "NIGHT" but ten
   hours apart, and comparing labels alone would still halt every morning.
3. **Daily-loss anchor.** Now re-anchors whenever `policy.trading_day(bar.ts)`
   changes, driven by exchange time rather than the wall clock. `RestartGuard`
   delegates to the same rule, so the loss limit and the backfill cap share one
   boundary definition.

All three verified by restoring the old behaviour and watching the tests fail
(5 tests catch defects 2 and 3; 8 catch defect 1). Suite: **252 passed**.

1. **`fetch_bars` defaults to `sessions=("DAY",)` — wrong.** I set that default
   from the false premise above. For the evaluated configuration it must be
   `("NIGHT",)`, or the backfilled context is the wrong session entirely.
2. **The staleness watchdog will halt the loop every single day.**
   `SafetyRails.check_staleness` has no session awareness, so the 05:00→15:00
   silence — ten hours with no night ticks — trips it on the first morning.
   It must treat "outside our trading session" as expected silence.
3. **The daily-loss anchor never rolls in a night-only run.**
   `SafetyRails.on_bar` re-anchors on *entry into the DAY session*; a night-only
   feed never produces a DAY bar, so the anchor is set once on the first bar and
   the "daily" loss limit silently becomes a since-inception limit. It should use
   the same 08:00 trading-day boundary `RestartGuard.trading_day` already
   defines, which correctly keeps a 15:00→05:00 night inside one trading day.

#### The decision

- **(a) Night-only live** — matches what was actually measured. Requires flipping
  the backfill default to NIGHT and fixing defects 2 and 3. Positions will hold
  through day sessions unmanaged, exactly as backtested.
- **(b) Night + day live** — adds the day session, on which this configuration
  has never been evaluated. `TXF_RPT_minute_day.csv` exists, so it *could* be
  backtested first — but note the tuning history in `results.tsv` is a long
  night-session search, and its parameters should not be assumed to transfer.

**Recommendation: (a).** Trade the session that was measured. The genuinely open
question is not which session to trade but **whether to keep `forced_close=False`
given a median 4.5-day hold** — that is a risk decision the backtest quantifies
but does not make, and it is independent of the live/backtest split.

#### DECIDED 2026-08-11 — `forced_close=False` stands for V4

**Live executes the build that was tuned; it does not invent risk behaviour the
research loop never produced.** Turning the boundary close on would make the live
strategy differ from every number in `results.tsv`, and the gap would be
invisible — the live run would simply produce different trades with no baseline
to compare against. If holding through the day session turns out to be wrong,
**that is a finding for autoresearch to act on**, and it arrives as a new pinned
config through `live/config.py`'s drift guard like any other strategy change.

This keeps the ownership boundary clean: **autoresearch owns what the strategy
does; the live path owns doing it faithfully.**

> **This does not remove the need for a working flatten — do not conflate the
> two.** `forced_close_on_session_end` is a *strategy* behaviour (SPEC §4.7) and
> is now settled as off. `HaltAction.FLATTEN` is a *risk control* (§5) and is
> unaffected: the daily-loss limit and the kill file both close the position and
> stop, and both must work on a position that may be days old.
>
> That path has a known weakness, already reported by `shutdown()` as
> `flatten_incomplete` and by the CLI as exit code 4: a flatten order is
> `LMT`+`ROD`, and ROD is cancelled at session end, so a flatten fired near the
> close can silently fail and leave the position on with the loop blind until the
> next session. **The "aggressive order type" in V4's scope is for this, not for
> the boundary close**, and it survives this decision intact.

---

### 2.14 The backfill↔live seam duplicated one minute — FIXED

**Found 2026-08-11 in the 60-minute run.** Two records say it outright:

```
warm_up  last = 2026-08-11T04:11:00      (last backfilled bar)
bar      idx=0  2026-08-11T04:11:00      (first live bar)
```

The same minute enters the model context **twice**, and neither copy is a whole
minute:

- The backfill ran at ~04:11:29. `kbars` returns the **in-progress** minute, so
  its last raw bar was labelled 04:12 — which `kbars_to_bars(source_label="right")`
  correctly shifts to 04:11 — covering only 04:11:00–04:11:29.
- Subscription completed at ~04:11:41 (warm-up costs 12.3 s). The first captured
  tick is 04:11:43.195, so the live 04:11 bar covers 04:11:43–04:11:59.

Nothing dedupes them. `seed_history` loads the backfill into `_history_ts`
(`loop.py:134`) and `on_bar` appends unconditionally (`loop.py:199`); `_trim_history`
then evicts the *oldest* bar to hold the window at 3 008. So the context is
3 007 distinct minutes plus one duplicated, doubly-partial one — and the first
live forecast (bar_idx 0, 04:11) ran on exactly that.

**Why it matters more than its size suggests.** Numerically it is one bar in
3 008, and the two closes here were a tick apart. But it is silent, systematic,
and fires on **every process start** — and §7 answer 5 makes a full refetch the
policy on every restart, so there is no start that avoids it. It is the same
class of defect as the right-labelling bug V1c existed to find (§2.4, Finding E):
a convention mismatch at a source boundary that nothing downstream can detect.

#### The fix (2026-08-11) — remove the cause, then detect any recurrence

**Cause removed — `fetch_bars` keeps only completed minutes.** Bar `T` covers
`[T, T+1min)`, so it is finished exactly when `T + 1min <= now`; trailing bars
failing that are dropped. `now` is an injectable parameter (defaulting to
`datetime.now()`) so the boundary is pinned in tests rather than raced against
the clock — the same convention `state.py` already uses for the rails.

Comparing against the bar's *right edge* rather than flooring `now` is what makes
the boundary exact: at 04:12:00 the 04:11 bar genuinely is complete and is kept.
Flooring would have discarded a good bar on every fetch that landed on a minute
edge — trading one silent defect for another.

**Detector added — a non-monotonic bar is recorded, not raised on.** `LiveLoop.on_bar`
writes a `bar_anomaly` record when a bar arrives stamped at or before the newest
bar already buffered. Deliberately *not* a halt: a data quirk must not kill a
process that may be holding a position, and the audit trail is where V3/V4 read
from anyway. Deliberately *not* a drop either — dropping would shift `bar_idx`
and with it every stride boundary and holding-period count.

**Verified:** 7 new tests (4 on the truncation incl. both sides of the minute
boundary and the index reset, 3 on the detector), suite **323 passed**. Both
halves were mutation-checked by restoring the old behaviour — the truncation
tests fail 3/4 and the detector test fails 1/1. V0 parity still passes, which is
the load-bearing check: `on_bar` is the parity-critical path, and the anomaly
branch touches only the recorder, so no decision can move.

**Confirmed live 2026-08-11.** The backfill ran at 20:57:35 and its last bar was
**20:56** — the last complete minute, with the in-progress 20:57 correctly
dropped. Pre-fix it would have returned 20:57 and collided with the live stream.

#### Addendum — the seam now loses one minute per start, and that is new

Truncation removed the duplicate, but the run exposed the other side of the
trade. The first live tick arrived at **20:58:03**, so the seam reads
`…20:55, 20:56, [nothing], 20:58…`: the backfill would not claim 20:57 because it
was in progress, and the subscription came up after that minute's last print.

**20:57 traded — we simply were not listening.** That is a different thing from
the RPT convention's omitted minutes, which are genuinely tradeless, so the
context now carries a gap the offline data would never produce.

Bounded and small: one minute in a 3 008-bar context, once per process start, and
strictly better than the previous duplicate-with-a-wrong-value. Not a defect —
a faithful record of what was observed. Closable two ways if it ever matters:
**subscribe before backfilling** (so the live stream covers the fetch window), or
**top up** with a second short `kbars` call for the gap once the feed is live.
Neither is urgent; the first is nearly free and belongs in V4, which touches this
start-up path anyway.

### 2.18 `api.contracts` is not `sj.Contracts` — a lost market window

**Found 2026-08-11 at 20:51, live.** The V3 startup died immediately:

```
ERROR: AttributeError("'builtins.ContractsApi' object has no attribute 'Futures'")
```

`api.Contracts` is deprecated in favour of `api.contracts`, and all three entry
points were switched after "verifying" that `sj.Contracts` exposes `Futures`. It
does. **But `api.contracts` does not return a `sj.Contracts`** — it returns a
`ContractsApi`, a different type whose surface is
`futures / futures_by_underlying / futures_roots / get / list / options / …`,
with no `Futures` at all. The verification checked a class that merely *shared a
name* with the object that would actually be called; `api.contracts` raises
`AuthError` before login, and a lookalike was substituted for the real check
without that substitution being stated.

**Cost: a market window that could not be re-run.** This is the sixth Shioaji
assumption to prove wrong on this project (see the list in §1 and §7.2) and the
first to cost a session.

**Fixed** by `live/contracts.py`, now the single place that knows how to reach a
TXF contract. It tries routes in order and fails only if all do:

| # | Route | Status |
|---|---|---|
| 1 | `api.Contracts.Futures.TXF` | **PROVEN** — five live runs |
| 2 | `api.contracts.futures.TXF` | unverified fallback |
| 3 | `api.contracts.futures['TXF']` | unverified fallback |

**The deprecated route goes first, deliberately inverting "prefer the new API".**
It is the only one with evidence behind it, and these runs are bound to market
hours: a deprecation warning is noise, a failed start-up is a lost session. The
`api.contracts` routes exist so that the day `Contracts` is removed the code
degrades instead of dying — their inner layout is still a guess, and is labelled
as one. If every route fails the error names each and what it raised, so the next
fix needs no live run. The route actually used is printed at start-up; the
2026-08-11 21:00 run reported `via api.Contracts.Futures.TXF`.

> **The deprecation warning has disappeared from the console, and that means
> nothing.** Python only shows `DeprecationWarning` by default when it fires in
> `__main__`; the call now lives in `contracts.py`. We are still on the
> deprecated route — do not read the silence as evidence of v2.

#### 2.18b The same alias trap, caught twice — and written twice

`v1_check`'s contract guard compared the resolved `code` against the codes in the
capture and reported:

```
** MISMATCH — different contract, diff is meaningless **
```

on a diff that was in fact perfect. `TXFR1` reports `code='TXFR1'` while the
venue stamps every tick `TXFH6`, so comparing on `code` alone declares a contract
**different from itself**.

This is the *same* trap §7.2 found in `ShioajiBroker`, where it would have halted
the loop on the first real fill — and the fix there (match a *set* seeded from
`code` **and** `target_code`) was already in the tree, thirty lines from where
the bug was rewritten.

A false mismatch is worse than no check: it labels a valid result meaningless and
teaches the reader to ignore the warning for the time it is real. Now both call
sites seed from one function, `live.contracts.contract_codes`, so they cannot
drift; 4 tests, including a genuine-rollover case to keep the check non-vacuous.

### 2.15 The off-session bar filter is unreachable in normal operation — ~~CLOSED~~ **CORRECTED 2026-08-15**

> **This section was wrong, and the 24-hour soak proved it.** It generalised from
> runs that never spanned a *day* session. `off_session_dropped` = **300** in
> `outputs/live_20260813_191326` — exactly the 300 minutes of the day session,
> and the counter counts *bars*. The branch that fired is
> `session not in self.policy.sessions` (`bars.py:233`), not the
> `session is None` branch the text below reasons about. That branch is live,
> load-bearing, and runs 300 times a day for a night-only policy — it is what
> keeps the day session out of a NIGHT deployment's context.
>
> Everything below remains true **of the `session is None` branch only** (the
> 13:45–15:00 and 05:00–08:45 dead zones), which is still unreachable in normal
> operation for the reason given. The claim "no further live run will exercise
> it" was false and is withdrawn.

`off_session_dropped` stayed **0** across the 05:00 close *and* across the 15:00
open. Both runs measured, and the reason differs at each end:

- **At a session end** the feed simply stops. There are no post-close ticks to
  drop, so the counter cannot rise.
- **At a session open** there *are* pre-open ticks — but every one of them is a
  trial print, and `add_tick` drops `simtrade` at the very top and returns before
  a bucket is ever opened. Measured 2026-08-11: **all 53 pre-open ticks were
  `simtrade=True`, and zero genuine ticks arrived before 15:00:00.**

So the off-session *bar* filter is dead code under normal operation — not because
it is wrong, but because the simtrade filter upstream of it catches everything
that would reach it. It remains correct as a backstop (a policy change, or a feed
that ever sent genuine out-of-session prints, would need it) and is covered by
offline tests. **No further live run will exercise it; stop waiting for one.**

### 2.16 Duplicate bars from the clock-flush race — FIXED 2026-08-11 (V1 retired the residual)

**Found 2026-08-11 in the 14:55→15:15 session-open run.** The run emitted **20
bars for 15 distinct minutes**. Five minutes — 15:00, 15:03, 15:04, 15:05,
15:07 — were each emitted **twice**: a full bar, then a second bar for the same
minute carrying 1–7 lots.

**Mechanism.** `_close()` sets `_bucket_start = None`. The late-print guard in
`add_tick` is `bucket < self._bucket_start`, which is **unreachable once the
bucket has been cleared** — so the tick falls through to the
`if self._bucket_start is None` branch and *opens a fresh bucket for a minute
already emitted*. The aggregator keeps no memory of what it last closed.

**The trigger is a two-clock race.** Bar boundaries come from *exchange*
timestamps, but `flush(now)` fires on *local wall-clock*. When `on_clock` runs at
local 15:01:00.x it closes the 15:00 bucket; a tick stamped 15:00:59.965 that is
still in flight then arrives and reopens 15:00. Every duplicated minute here had
its last tick within ~1.2 s of the boundary (15:00:59.965, 15:03:59.717,
15:04:59.864, 15:05:59.978, 15:07:58.823) and no other minute did. Any local
clock skew *ahead* of the exchange makes this systematically worse.

**Proof it is live-only, and that the merged bar is the correct one.** Replaying
the captured `ticks.csv` through `TickAggregator` offline — where no wall-clock
flush intervenes — yields **15 bars for 15:00–15:14, one per minute**, and its
15:00 bar is `close=44959, volume=570` — exactly the two live bars (44958/568 and
44959/2) merged. So **neither** live bar is the true minute: the first is missing
its last prints, the second is a fragment.

**Why this outranks §2.14.**

| | §2.14 (fixed) | §2.16 |
|---|---|---|
| When | once per process start | **repeatedly mid-session** — 5 of 15 minutes here |
| Effect on `bar_idx` | +1 | **+33 %** on this sample |
| Detected by | — | the §2.14 `bar_anomaly` detector would have caught all 5 |

`bar_idx` inflation is the part that changes strategy behaviour rather than just
context quality: `forecast_stride_bars=30` counts *bars*, so at this rate
forecasts fire every ~22 real minutes instead of 30, and `ExitRule` holding
periods — measured in the same units — shrink to match. It also breaks the V1
live-half diff by construction, since live and replay no longer agree.

#### Fixed 2026-08-11 — the aggregator now remembers the last minute it closed

`_close()` records the bucket in `_last_emitted` **before** the session filter (a
dropped bar still closed that minute), and `add_tick` tests
`bucket <= _last_emitted` *ahead of* the `_bucket_start is None` branch. That is
the behaviour `add_tick`'s late-print comment already claimed; it simply did not
survive a flush.

**Verified against the real capture.** Replaying
`outputs/live_20260811_145506/ticks.csv` through the fixed aggregator gives
**15 bars for 15 distinct minutes**, 53 simtrade dropped — against the 20 bars
the live run emitted. 3 new tests, mutation-checked (2 fail with the guard
disabled). Suite 327 passed; V0 parity intact.

#### Residual — closed 2026-08-11 by a 2 s flush grace

The fix removes the duplicate bar. It does **not** make live match an offline
replay of the same ticks, because live genuinely closed the bar before the
straggler arrived: for 15:00 live would now report `close=44958, volume=568`
while the replay reports `close=44959, volume=570`. Dropping the straggler is
consistent with the documented late-print rule — the loop already acted on that
bar and back-applying would rewrite history — but the two sources still disagree
on 5 of 15 minutes here.

**That matters because V1's live half requires ≥99.5 % of minutes to match on
`close` exactly.** As it stands the session-open sample would score ~67 %.

**Proposed remedy: a small flush grace.** Close a bucket only when
`now >= bucket_start + BAR_LENGTH + grace` rather than at the exact edge. With
`grace ≈ 2 s`, a tick stamped 15:00:59.965 that arrives at local 15:01:00.5 still
lands in its own minute. The cost is bars — and therefore decisions — emitted up
to `grace` later, which against a 1 800 s stride and an 89 ms forecast (§2.7,
~20 000× headroom) is not measurable. It also absorbs local-clock skew *ahead* of
the exchange, which is the direction that makes this defect systematically worse.

**Implemented.** `TickAggregator(flush_grace=...)`, defaulting to **zero** — an
offline replay has the whole stream in hand and nothing is ever in flight, so the
aggregator stays a pure function of its ticks. Only `LiveRuntime` opts in, via
`LIVE_FLUSH_GRACE = 2 s`, and it is the single construction site for the live bar
stream. 6 new tests, mutation-checked; suite **333 passed**.

**The grace applies to `flush` only, not to `add_tick`'s close-on-new-minute
path** — and that asymmetry is load-bearing. Once a tick for minute M+1 arrives,
everything for M has arrived too, so the tick path can close immediately and lose
nothing; delaying it as well would push *every* bar 2 s late for no gain. This
rests on in-order delivery, which the capture supports directly: **1 106 ticks,
zero out-of-order arrivals.**

> **The one number here that is estimated, not measured — and the artifact cannot
> check it.** 2 s was chosen because every duplicated minute's last tick fell
> within ~1.2 s of the boundary. But that is the gap between a tick's *exchange*
> timestamp and the bar edge — **not** the delivery lag that actually causes the
> race, which is the gap between exchange timestamp and *local arrival*.
> `TickCapture` records only the exchange timestamp, so the lag distribution is
> unobservable in `ticks.csv`.
>
> **Closed the same day: `TickCapture` now records a `received` column** — local
> time stamped at *write*, which is the meaningful instant because `write` runs
> from `on_tick` on the same consumer thread as `on_clock`, so it is what
> actually races the flush (and it captures queue backlog as well as network
> lag; an enqueue-time stamp would miss half of it). Optional on read, so the
> four pre-existing captures — including the only one holding observed simtrade
> prints — still load. 3 tests; suite **336 passed**.
>
> **The next live run therefore measures the grace instead of assuming it.** The
> quantity to check is `received - timestamp`: its distribution, and specifically
> how often it exceeds 2 s. If the tail runs long, raise `LIVE_FLUSH_GRACE`; if
> it is comfortably short, 2 s stops being a guess and becomes a measured
> setting.

### 2.16b Delivery lag — MEASURED 2026-08-11, and 2 s is confirmed

The one estimated number in the §2.16 fix is now measured. From 1 603 ticks in
the 21:00 run, `received - timestamp`:

| | seconds |
|---|---|
| p50 | 0.249 |
| p90 | 0.304 |
| p99 | 0.335 |
| **max** | **0.877** |
| ticks > 1 s | **0 of 1 603** |

`LIVE_FLUSH_GRACE = 2 s` sits at **2.3× the observed maximum** and ~6× p99. The
measurement also shows **1 s would have been tight**: a tick stamped `:59.99`
plus the 877 ms tail lands at `:00.87`, inside a 1 s window by 130 ms. Keep 2 s.

**The race was genuinely exercised, which is what makes the clean result mean
something.** 12 of 40 minutes had their last tick within 2 s of the boundary and
8 within 0.5 s — the same profile that duplicated 5 of 15 minutes at the 15:00
open under the old code. Result: 39 bars, 39 distinct minutes, zero
`bar_anomaly` records, and `late_dropped = 0` (the grace *absorbs* stragglers
rather than discarding them, so zero is the correct reading, not merely a low
one).

> **Caveat on the sample.** This window ran at ~41 ticks/min, where the consumer
> queue never backs up — and `received` is stamped at write time, so it includes
> queue wait by design. The 15:00 open ran at 68 ticks/min. This is a
> moderately-busy measurement, not a worst case; a future session open is the
> stress test for this number.

### 2.17 `read_ticks` could not read a real capture — FIXED

`capture._iso` uses `ts.isoformat()`, which **omits the fractional part when
microseconds are zero**. A real session therefore produces mixed-format
timestamps, and `read_ticks`' bare `pd.to_datetime(df["timestamp"])` raises on
the first whole-second tick:

```
ValueError: unconverted data remains when parsing with format
"%Y-%m-%dT%H:%M:%S": ".024000", at position 53
```

Verified against `outputs/live_20260811_145506/ticks.csv`. The offline tests miss
it because their fixtures are uniform — every timestamp either has microseconds
or none does, so no fixture mixes the two.

**Fixed 2026-08-11:** `format="ISO8601"`, plus a test whose fixture deliberately
mixes the forms and puts the **whole-second tick first** — that ordering is what
pins the strict format and makes the sub-second row the one that raises.
Verified by reading the real capture back (1 106 ticks) and mutation-checked.

### 2.20 A restart could not resume into its own position — FIXED (2026-08-12)

**The gap.** §0 says the broker is the source of truth for position, and §2.9
reconciles the mirror against it every bar. But nothing ever *initialised* the
mirror: `Trader.position` and `Portfolio.position` both start at zero, which is
only true of a process that starts flat. The strategy holds a **median 4.5 days**
(§2.12), so a restart during a multi-day soak normally lands mid-position — and
V5's pass criterion is precisely "restarts recover to correct position".

Three layers each said no, for three different reasons:

| Layer | What it did | Why that was wrong for a soak |
|---|---|---|
| CLI preflight | exit 5, "flatten the account, then restart" | flattening to restart *is* abandoning the trade the soak is testing |
| Reconciliation rail | mirror 0 vs venue 1 → `STOP` on bar 1 | correct behaviour given a wrong mirror; the mirror was the bug |
| Nothing at all | no seed | worse than the halt — with the rail disabled, the no-flip machine reads "flat" and opens a **second lot** |

The third is the dangerous one. The refusal and the halt are loud; a silent
second lot is how `max_position=1` stops being true.

**The fix.** `Broker.broker_position_detail() -> HeldPosition | None` — the
venue's signed size *and* its average entry price — read **once**, at start-up.
The per-bar path stays on `broker_position()` and gains no extra query.
`LiveRuntime.adopt_position()` seeds both mirrors from it before the first bar;
the CLI calls it before the backfill, so a refusal costs seconds rather than a
full warm-up, and prints `RESUMING: adopted +1 lot(s) @ 17480.00`.

**The entry price is the venue's, not the current market.** That is a real
decision, not a detail: an inherited loss then lands on the day the position
closes, and a restart's first close can go straight through the daily loss limit
(the rail is realized-PnL and anchors at the trading-day roll). It is the money
that was actually lost, it is what the backtest books, and the alternative —
resetting entry to the market — would flatter every restart of a losing trade
with nothing downstream able to tell. Erring toward halting sooner is also the
safe direction for the rail.

**What is still refused (exit 5):**

- **more lots than `max_position`** — `Trader.on_fill` has no average-entry
  branch (§2.10), the same reason `check_startup` raises. Flatten first;
- **an `ExitRule` configured** — the entry *bar index* belongs to the dead
  process and is not recoverable, so a bars-held rule would measure from a
  sentinel. Live runs configure none today; this fails loudly the day one is
  added rather than firing a stop at the wrong moment;
- a second `adopt_position` call on an already-seeded mirror.

A broker that cannot answer adopts nothing and does not block — `None` is
"cannot compare", the same degradation §2.9 already makes.

**One dependency worth naming.** Adoption happens *before* any order, so
`ShioajiBroker._codes` holds only what `contract_codes(contract)` returned — it
has not yet learned the resolved month from an order callback (§2.18b). Verified
by introspection (2026-08-12) that `sj.Future` exposes `target_code`, so the
`TXFR1` alias covers `TXFH6` from the first query. Without that field, adoption
would read flat while holding — the §2.18b failure, in the one place where its
consequence is a second lot rather than a halt. `sj.FuturePosition` was checked
the same way: `code / direction / quantity / price / last_price / pnl / id`.

**Tests:** `tests/test_live_resume.py` (19). The pair that matters is
`test_the_reconciliation_rail_halts_a_restart_that_did_not_adopt` and
`test_after_adopting_the_same_restart_runs` — the same restart, the same bar, one
without the seed and one with, so the fix cannot become vacuous. **517 green.**

#### VERIFIED LIVE 2026-08-12 — and `price` really is the entry

The shakedown run intended to prove nothing (`--duration-minutes 10`, expected
"account is flat") instead landed on the real case: a lot left open at 02:10 by
`v4probe_20260812_021012`, whose urgent IOC close never filled (§2.19 again).

| | |
|---|---|
| Probe fill, 02:10:20 | **45219.0** (`broker_deal`) |
| Adoption, 03:25:36 | **45219.00** (`position_adopted`) |
| Market across the run | 45202 – 45244 |

**`FuturePosition.price` is the average entry, not a mark.** That is the one
thing the fake could not answer, and this settles it against a position aged 75
minutes while the market moved around it — a stronger test than a freshly opened
lot, where entry and market coincide and the two hypotheses are indistinguishable.
Had `price` been the mark, adoption would have reported something in the 452xx
band *of that minute* and looked perfectly plausible.

It also confirms the §2.18b dependency: `contract_codes` covered the resolved
`TXFH6` from the alias's `target_code` at start-up, before any order had taught
`_codes` anything — the position was found, not missed.

Ran **three times**: `live_20260812_031655` (aborted in warm-up),
`_031836` (adopted, 4 bars, ended holding, no halt) and `_032534` (adopted the
same lot again, 9 bars, no halt). The middle two are a restart-into-position
cycle, which is V5's pass criterion exercised in miniature.

**Not yet covered:** adoption of a **short**, and the refusal paths, live. Both
are unit-tested; neither is worth a market window to stage.

### 2.21 The staleness watchdog halted a healthy soak — MEASURED 2026-08-13

**The event.** The V5 soak ran 22h54m (2026-08-12 03:43 → 08-13 02:37) and ended
on `no market data for 121.0s (limit 120.0s)`, action `STOP`. It ended cleanly —
position 0, no pending order, `flatten_incomplete` false — so the rail did
exactly what it was told. The question was whether it was told the right thing.

**Settled by tick-level query, not inference.** Two diagnostics were needed
because the first was wrong:

1. Minute bars for 02:25–02:50 appeared to show 14 traded minutes after our last
   print → read as a feed fault. **That reading was invalid**: it compared
   against our last print (02:35:05) instead of the moment the loop stopped
   consuming (02:37:06). Everything after the halt is missing because the run
   ended.
2. `api.ticks(..., RangeTime)` resolved it. Our last print was **02:35:05.893**;
   the market's next print was **02:37:36.991** — a **151 s** gap. The watchdog
   fired at 121 s, **31 s before the market spoke again**.

Corroboration: our capture's final four ticks match the exchange's tick-for-tick
to the millisecond. The feed missed nothing.

**Why the earlier statistics understated it.** Inter-print gaps computed from the
run's own capture gave a worst in-session gap of 118.2 s — because the 151 s gap
*never completed in the capture*: we halted inside it. A distribution measured
from a run that a threshold terminated is truncated by that threshold. Any
recalibration from such a sample is biased low, by construction.

For the record, worst gap per hour from that (truncated) night: 19:00 → 105.5 s,
01:00 → 79.7 s, 02:00 → 118.2 s, 04:00 → 73.0 s. The thin hours routinely run
80–120 s between prints. **A 120 s limit sits inside normal behaviour.**

**Why raising the number is the weaker fix.** It stays blind — silence cannot
distinguish a quiet market from a dead socket, and a bigger number just makes the
same guess more slowly, from a sample that is biased low anyway.

**The fix that answers the question: ask the venue.** On staleness, take one
snapshot before halting.

| Snapshot says | Meaning | Action |
|---|---|---|
| last trade is also old | market is quiet | reset the timer, carry on |
| trades we never received | our feed is broken | **halt** |
| query fails / times out | network is down | **halt** — fail closed |

This lets the threshold stay *tight* (fast detection of a real fault) without
false alarms, which raising it cannot. Cost is one query per staleness event — a
handful a night against the 50-per-10 s cap. Shape: `SafetyRails` takes an
optional `quiet_check` callable, default `None`, so offline tests and V0 parity
are untouched; the CLI wires it to a snapshot probe on `ShioajiBroker`.

**Blocked on one measurement.** `sj.Snapshot` exposes `ts`, `total_volume`,
`close`, `volume` (introspected 2026-08-13). Whether `ts` is the *last trade's*
time or the snapshot's *generation* time is unverified — and if it is generation
time, a rail built on it halts every time. `total_volume` (day-cumulative) is the
safer signal but needs a baseline. **Pin the semantics with a read-only probe
before writing the rail**, per the standing rule that the docs have been wrong
four times.

**Also exposed: there is no reconnect path.** Whatever the cause, the loop stops
at the first `STOP` and never re-subscribes. A soak that dies permanently on one
event cannot do 5 unattended days at any threshold. V5 needs this regardless of
how the staleness rail is settled. → resolved in §2.22.

### 2.22 FeedWatch — the timer asks instead of deciding — BUILT 2026-08-13

**The user's correction, and it was the right one.** The fix first proposed here
was to raise the staleness window. The objection: *"how long to wait on a
staleness is undefined"* — any number is a guess, and this one would be
calibrated against a distribution truncated by the very threshold that ended the
run (§2.21). The better shape is to **detect** rather than wait: on silence, find
out whether it is the market or us, and if it is us, unsubscribe and re-subscribe.

That reframes the number from a **decision threshold** into a **polling
interval**. Getting it wrong now costs one query instead of the run — so it moves
*down* (60 s) rather than up. This is the second time this project's answer came
from making the instrument able to answer the question rather than tuning a
guess (cf. §2.19's probe).

**Measured 2026-08-13 (day session, 9 samples over 3 min) — which query can
answer it:**

| Candidate | Result |
|---|---|
| **`snapshot.ts`** | **the last *trade's* time.** Matched our newest tick exactly 8/9; once 0.8 s *behind*; **never ahead** |
| `snapshot.total_volume` | rises monotonically with prints (+81, +74, +31 …) — a clean independent cross-check |
| `api.ticks` newest | lags 0–4.4 s, latency spikes to 580 ms |
| Latency | snapshots **32–82 ms** vs. ticks 65–580 ms |

Snapshots win on every axis, and the one imperfect row errs in the safe
direction: a snapshot *behind* us yields QUIET, so the error mode is
under-detecting for a second — never a spurious repair.

`on_event` also fired with a 4-argument shape `(resp_code, event_code, info,
event)`; `on_session_down` stayed silent through a healthy run.

**Built:** `live/feedwatch.py` + 19 tests (`test_live_feedwatch.py`).

```
silence for 60s → snapshot (5 s timeout, not the SDK's 30 s default)
   ├─ venue's last trade is one we already have   → QUIET.  do nothing.
   ├─ venue traded since our newest print (+1 s)  → MISSED. unsub + resub.
   └─ the query itself fails                      → UNREACHABLE. back off.
```

**Two layers, and `state.py` was not touched.** FeedWatch heals and never halts;
the rails' staleness window becomes the backstop that halts only if healing did
not work (CLI default 120 s → **900 s**; `RailsConfig`'s own 120 s stays, because
V0 parity and the offline tests depend on it). Keeping halt semantics untouched
is deliberate — a recoverable condition and a human-attention condition are
different things, and merging them is what made a transient gap terminal.

**The failure mode this could have introduced.** Re-subscribing on suspicion
risks a **doubled tick stream**; duplicate prints inflate bar volume and would
silently break the bar equality V1 proved. Hence: repair only on positive
evidence, always unsubscribe-then-subscribe, capped at 3 per silence (an
uncapped loop is a submit storm against the quote service), and any print refills
the budget.

**Session-down callbacks are registered but only *record*.** Their shape and
timing are unobserved — they have never fired — and writing recovery logic
against an unobserved callback is precisely how the four earlier SDK assumptions
went wrong (§2.18 cost a market window). When a real drop is captured in the
trail, `session_down` becomes the faster trigger for the same repair.

**Known limit, stated rather than buried.** This assumes a broken feed is
detectable from outside — that the venue will tell us about trades our socket
missed. A fault that drops prints *and* the query path leaves only silence, and
the backstop halt is the last line. That is why the escalation stays.

#### 2.22b The first live run crashed on a type — FIXED 2026-08-13

`TypeError: '>' not supported between instances of 'int' and 'datetime.datetime'`,
42 bars into the soak, on the **first** liveness check.

**`Snapshot.ts` is an `int` of epoch nanoseconds.** A tick's `datetime` is a
`datetime`. The comparison at the heart of the check had mixed types.

**The evidence was already in hand.** A throwaway liveness probe — written the same day
to pin what `ts` *meant* — converted it with `pd.Timestamp(snap.ts)` in order to
print it. That conversion never reached `feedwatch.py`. The probe answered
"what does this field mean" and was not read for "what type is it", though it
showed both.

**The tests agreed with the assumption, not the venue.** The fake `_Venue`
returned `ts=datetime(...)`, so 14 tests passed against a shape the SDK never
sends. **A fake built from the same belief as the code tests the belief, not the
system** — the fakes now use `int(pd.Timestamp(...).value)`, and the regression
mutation-fails with the identical `TypeError`.

**A worse failure was closed at the same time.** `pd.Timestamp` of an integer
reads it as *nanoseconds*; had the SDK sent seconds, the result would be a
**1970** date — older than our newest tick, so every check answers QUIET, and the
rail is disarmed permanently and silently. `SANE_AFTER` makes an implausible date
UNREACHABLE. The crash was loud and cost 40 minutes; that one would have cost
nothing visible and left a dead rail reporting success.

**Worth noting:** §2.23's alerting seam worked on its first real outing — the
crash announced itself as `[FATAL] crashed: TypeError(...)` and landed in
`fatal.jsonl`, which is exactly the case it was built for.

### 2.23 Alerts — one seam for everything that needs a human — BUILT 2026-08-13

**Why now.** A five-day unattended soak has no console anyone is watching. Every
"needs a human" condition already existed — halts, `flatten_incomplete`, refused
starts, crashes — but each ended at stderr and a JSONL read the next morning.
Requested with a transport (Telegram) in mind, to be attached later.

**Shape, per the decision taken 2026-08-13:** *fatal + notable + info, routed to
different channels, tagged rather than split into separate methods.*

`live/alerts.py` — one `emit`, three entry points (`fatal` / `warn` / `info`),
every alert carrying a `severity` and a machine-readable `kind`. **Sinks
subscribe to the levels they want**, which is what lets one chat take fatals and
another take everything without a single call site knowing a channel exists.
Attaching Telegram later is one `add_sink` call and no other change.

| Level | Raised by |
|---|---|
| `FATAL` | any rails halt, `flatten_incomplete`, startup refused (guard / preflight / adoption), unhandled crash, missing credentials |
| `WARN` | feed missed → resubscribed, feed unreachable, session down, account fingerprint changed |
| `INFO` | **position opened / closed (with the trade's PnL)**, resumed into a position, feed recovered |

**Trades are alerts too, and they are the interesting ones.** At 0.37
orders/night a fill is roughly one message every three days — it cannot cause
fatigue, and it is the single event most worth reading on a phone
(`position_opened: LONG 1 @ 46200` / `position_closed: closed @ 46350 — +150
points`). It fires on the **fill**, not the order, because an order can be
cancelled unfilled at the next boundary (§4.4) and a notification for a trade
that never happened is worse than none. The closing alert carries **this round
trip's** PnL, taken as a delta across `apply_fill` — the Portfolio only exposes a
running total, and an alert reading it afterwards would report every trade of the
night (mutation-checked).

**Delivery is best-effort, decided rather than defaulted.** The alert most worth
sending is often *about* a network fault — precisely when the transport fails.
Losing that message is acceptable; losing the run to the notifier is not. So a
sink that raises is swallowed and **counted**: `alert_sink_failures` is what
distinguishes a quiet night from a Telegram sink that silently died, and without
it the two are identical.

**Two write paths, per the same decision.** Everything goes to `session.jsonl` as
an `alert` event; **fatals additionally append to a standalone journal** beside
`live_restart.json`, outside the run directory, so "what has ever gone badly
wrong" is one file rather than a search across every run.

**Halts notify from inside the latch** (`state.py`), so a rail added later cannot
forget to notify, and a latched halt buzzes **once** rather than once per bar.
`alerter=None` is the default everywhere — V0 parity and the offline tests are
untouched.

**Out of scope by decision:** `kill -9`, power loss, a blue screen. Nothing
in-process can report its own instant death; that needs an external watcher over
`live_restart.json`'s `last_heartbeat`, which is a separate piece of work.

**Tests:** `tests/test_live_alerts.py` (19).

### 2.24 The session token expires at 24 h — MEASURED 2026-08-14, FIXED 2026-08-18

**The soak died at 24 h 0 m 36 s.** Started 2026-08-13 19:13:26, halted
2026-08-14 19:14:02, artifacts `outputs/live_20260813_191326`:

```
halt: broker anomaly: list_positions raised:
      TokenError('StatusCode: 401, Detail: Token is expired')
```

**Nothing was wrong with the run.** It shut down clean — `halt_action=stop`,
`position=0`, `pending_order=false`, `flatten_incomplete=false` — and nothing was
left at the venue. 826 bars, 28 forecasts, 28 signals, 0 orders, 54 218 ticks.
The halt is the *only* alert in 24 hours.

**It also paid for itself on the way down.** `feed_quiet` = **36**: thirty-six
occasions the old 120 s rail would have killed a healthy run. §2.21 is now a
measured number rather than an argument, and `feed_missed` / `feed_unreachable` /
`feed_resubscribes` were all 0.

**The failure is in the REST token, not the connection.** Genuine prints were
still arriving at 19:13:57, five seconds before the halt; the quote session never
dropped and `on_session_down` never fired. Only `list_positions` — a REST call on
the reconciliation path — saw the 401. The two have different lifetimes, and a
five-day soak cannot happen until the token one is managed.

#### The quiet windows, measured from the same capture

`simtrade` is **two short pre-open windows, one per session** — not the single
long regime a first read of the hourly counts suggests:

| Window | Prints | Opens |
|---|---|---|
| 08:30:10 .. 08:44:55 | 178 | DAY at 08:45:00 |
| 14:50:15 .. 14:59:55 | 117 | NIGHT at 15:00:00 |

Genuine ticks run 08:45–13:45 and 15:00–05:00, with a 225-minute silence from
04:59:58 to 08:45:00. So there are exactly two moments a day when the feed is
live but carries nothing real.

#### The rule: two windows in the quiet before every session open

**Anchors are 08:40 and 14:55, and they do not depend on which session we
trade.** `SessionPolicy` says what we *trade*; re-login anchors on *all* session
opens, because **the non-traded session's anchor is the one that breaks the
24-hour cadence.** A single daily anchor re-logins every exactly 24 h — a dead
heat with the wall, every day.

| | |
|---|---|
| Anchors | `DAY_OPEN`/`NIGHT_OPEN` − 5 min (`environment.py:13-16`, no new constants) |
| Intervals | 6 h 15 m and 17 h 45 m |
| **Max token age** | **17 h 45 m** against a 24 h wall — 6 h 15 m of margin |
| Cost | 2 logins/day against a documented cap of 1 000/day (§2.8b) |

Both anchors sit inside a measured simtrade-only window with 5 minutes of
clearance before the first genuine print. That is the whole reason for choosing
them: `login()` blocks the consumer thread for seconds, and at 08:40 and 14:55
there is nothing real to block. The same rule re-derives correctly for the
day-session port with no special case — for a DAY policy the anchors are the
same two times, and it is the *night* pre-open that carries the long interval.

##### DECIDED 2026-08-16 — a verify window, not an age backstop

The re-login window is followed by a **health check one minute before the open**,
and the age-based backstop this section originally deferred is **rejected
outright**. Each open therefore carries two adjacent, non-overlapping windows:

```
open − 5 min ............ open − 1 min ...... open
└── re-login, retried ──┘ └── verify ──┘
    08:40 → 08:44           08:44 → 08:45
    14:55 → 14:59           14:59 → 15:00
```

The check calls an **authenticated REST call — the same kind that actually
failed** — and if it raises, takes one recovery re-login right there. One shot at
each, not a ladder: a check still retrying when the session opens has failed at
its only job.

**Why this and not the backstop.** A backstop fires on a clock reading,
mid-session, on suspicion. This fires on **evidence**, in the quiet, at the last
moment it is still free — and it is the same instinct that produced FeedWatch
(§2.22), where a timer that had to guess was replaced by asking the venue. It
also chose itself on the arithmetic: the gap the backstop was meant to close was
"every attempt in one window fails", and the verify window turns that from one
failure into **two independent ones**.

**A feed-side check would not have caught this.** Genuine prints were arriving
five seconds before the halt. Only the REST path knew.

**DECIDED — the check is `api.usage()`**, not `list_positions`. Both are REST
calls on the token that dies; `usage()` is the cheaper one, carries no position
semantics, and is already the call `RestartGuard` duck-types for its headroom
check (§2.8b).

**CONFIRMED EMPIRICALLY 2026-08-16** (probe run 1): on a dead token `usage()`
raises `AuthError: Not authenticated`. It does *not* return quietly, so the
verify window is live rather than inert. The concern below is closed.

**The trap, recorded before it is wired.** `cli.py` already has a `_usage(api)`
helper that **swallows every exception and returns `None`**. Passing that as
`verify` would make the check pass forever and disarm the window silently — the
same failure shape as `SANE_AFTER` in `feedwatch.py`, where a malformed answer
read as "healthy" would have been worse than the crash it replaced. The closure
must call `api.usage()` **directly**.

~~**And one thing we do not know:** that `usage()` actually raises on an expired
token.~~ **Settled 2026-08-16 — it raises.** See the run-1 table above.

#### Step 1 — the probe — WRITTEN 2026-08-16, RUN FIVE TIMES 2026-08-16/17

`live/relogin_probe.py`, with `tests/test_relogin_probe.py` (25) and
`docs/live/relogin_probe.md`. **Two modes**, because five of the seven questions
need a live tape and two do not:

| Mode | Answers | Needs | Cost |
|---|---|---|---|
| `--mode offhours` | **Q6, Q7** (+Q1 partially, Q4, Q5) | nothing — any time, weekend included | ~10 s |
| `--mode session` | all seven | live ticks | ~6 min |

```powershell
uv run python -m forecast_eval.live.relogin_probe --mode offhours
uv run python -m forecast_eval.live.relogin_probe --mode session
```

**Q7 is destructive and runs last** — it calls `logout()` to manufacture a dead
token in seconds rather than waiting out a 24-hour expiry, and nothing can run
after it. The sequence is pinned by test.

**An exception is an answer, not a probe failure.** Every venue call goes through
a `call()` wrapper that catches, prints and records; letting one propagate would
abandon the remaining questions and waste the session, which is exactly how
§2.18 cost a market window.

**Off-hours mode touches nothing** — no subscription, no snapshot, no order —
asserted against a fake that raises if `place_order` is reached. Session mode's
order safety argument is `v72_probe`'s, reused rather than restated, and it uses
**the adapter's own order form** (`sj.FuturesOrder` with the top-level enums,
verified to resolve on shioaji 1.7.2) so it cannot measure a path the live system
never takes.

##### RUN 1 — off-hours, 2026-08-16 00:45 Sunday (shioaji 1.7.2)

| Q | Answer |
|---|---|
| **Q6** | **GRANTED.** A login on a Sunday at 00:45 — outside every session — succeeded and returned the futures account. The wall-clock anchors can fire on Saturday and Sunday without special-casing |
| **Q7** | **`usage()` raises on a dead token** — `AuthError: Not authenticated`. **`api.usage()` is a valid verify call**, confirmed. `list_positions` raises the same way |
| Q4 | `list_positions()` works before and after a re-login, same answer (`[]`, flat) |
| Q5 | fingerprint **unchanged** across re-login (`a685deb6b5b8` both sides) — a re-login stays on the same account |
| Q1 (partial) | plain `login()` and `login(force_refresh=True)` on a live session **both succeed**, no exception |

**The exception type is `AuthError`, not `TokenError`.** A *logged-out* session
gives `AuthError: Not authenticated`; the *expired* token in the soak gave
`TokenError: Token is expired`. `TokenError` subclasses `AuthError`, so catching
`AuthError` covers both — but `ReloginScheduler` catches bare `Exception` by
design, so nothing changes.

##### DEFECT FOUND IN THE PROBE ITSELF — a leaked national ID

Run 1 printed `person_id='L125551606'` **in full**, straight to a console whose
output is meant to be pasted into a chat. `cli.py:107` digests this field and
deliberately does not even mask it, on the grounds that a partial national ID is
more than an audit trail needs to carry — and the probe defeated that in one
line, via `_brief()` dumping an SDK repr.

Fixed: redaction now lives in the **renderer** (`_redact`, applied before
truncation and on the exception path too), so no future question can reintroduce
it. Three regression tests. The instrument was leakier than the system it
measures — worth remembering when writing the next one.

##### RUN 2 — off-hours, 2026-08-16 00:56 Sunday — Q8 ANSWERED

**`connections` from `usage()`, sampled after each login form:**

| Step | connections | Δ | Reading |
|---|---|---|---|
| login #1 | 1 | — | baseline |
| login #2 — **plain** | 1 | **+0** | **a plain re-login is a NO-OP.** It returns a cached session |
| login #3 — **`force_refresh=True`** | 2 | **+1** | opens a real connection — **this is the form to use** |
| `logout()` | 2 | +0 | **logout releases nothing** |
| login #4 — after logout | 3 | +1 | another connection, on top of the un-released one |

**Finding 1 — `login()` alone does nothing, and would have made the fix inert.**
This is exactly the failure the question was invented for: `ReloginScheduler`
would have called plain `login()` twice a day, recorded `relogin` events, printed
healthy counters, and the run would still have died at 24 h. The scheduler is
correct; the closure would have been hollow. **`relogin` must call
`login(..., force_refresh=True)`.**

**Finding 2 — connections spike, then come back.** Each effective re-login adds
one, briefly. §2.8b caps connections at **5 per person_id**, which looked like a
two-day budget until Q9 measured the other half: **the count returns to 1 within
~30 s** (run 3 below). Transient, not cumulative. **Concern closed.**

##### RUN 3 — off-hours, 2026-08-16 01:02 Sunday — Q9 ANSWERED, concern CLOSED

**Leaked connections are reclaimed in under a minute.**

```
01:03:09  connections=3      (immediately after the third login)
01:03:39  connections=1      <- back to steady state
01:04:09 .. 01:17:39         1, held flat for the remaining 14 minutes
```

Thirty samples over 15 minutes, and the count sat at **1** for twenty-nine of
them. So the "leak" of run 2 is **lag in the venue's connection accounting**, not
an accumulation: a superseded connection is torn down within ~30 s, and the
steady state is one connection — the live session.

**This closes the §2.8b concern outright.** Two re-logins a day, six hours apart,
produce a transient peak of 2–3 against a cap of 5, clearing long before the next
one. The cap is never approached. **Re-login at the anchors is safe.**

**Run 2's "logout releases nothing" was over-read, and run 3 says why.**
`Q8_after_logout_only` came back **`None`** — because `_connections` reads
`usage()`, and `usage()` needs authentication, so after a logout the count is not
*askable* at all. Logout's effect on connections is **not observable by this
instrument**, and the run-2 inference from 2 → 3 was exactly the kind of
reasoning-from-one-reading §2.19 punished three times. The verdict logic now
omits the claim rather than making it, and Q9 answers the question that actually
matters from the authenticated side.

##### RUN 4 — DAY session, 2026-08-17 11:56 Monday — Q2 and Q3 ANSWERED

**A re-login tears the client down completely.** Both callbacks and the
subscription are lost.

| | Before | After | |
|---|---|---|---|
| `Q2_relogin_was_real` | connections 1 → 2 | | the re-login genuinely happened — everything below is about a real one |
| **Q2** ticks / 60 s | **172** | **0** | **the tick subscription does NOT survive** |
| **Q3** order callbacks | **2** | **0** | **the order callback is DROPPED** |

172 ticks then zero, with two live orders placed and cancelled either side, is
not a marginal reading.

**Q3 is the good outcome of the two available.** DROPPED is recoverable by
re-registering; DUPLICATED would have double-counted every fill, corrupting the
position mirror and the PnL together and looking like a venue bug rather than
ours.

**What this means: `force_refresh=True` rebuilds the session object, so
everything the CLI registers at start-up must be re-registered.** Five things:

| Re-assert | Registered today at |
|---|---|
| `api.subscribe(contract, QuoteType.Tick)` | `cli.py:468` |
| `@api.on_tick_fop_v1()` | `cli.py:446` |
| `api.set_order_callback(...)` | `broker/shioaji.py:155` |
| `@api.on_session_down` | `cli.py:480` |
| `@api.on_event` | `cli.py:485` |

**One nuance the data does not settle:** zero ticks could mean the *subscription*
was dropped or the *tick callback* was dropped — the probe cannot separate them,
and does not need to. The closure re-does both.

**Do not reuse `cli.py`'s `_resubscribe`.** It unsubscribes first, which is right
when repairing a *live* subscription (§2.22 — subscribing on top of one risks a
doubled tick stream and inflated bar volume). After a re-login there is nothing
to unsubscribe from, and the call would at best be a no-op against a session that
no longer exists.

##### RUN 5 — the control, DAY session 2026-08-17 12:22 — MECHANISM CONFIRMED

Same experiment as run 4, one variable changed: `--relogin-form plain`.

| Form | connections | ticks | order callbacks |
|---|---|---|---|
| `plain` | **1 → 1** (+0) | 12 → **50** | 2 → **2** |
| `force_refresh=True` | 1 → 2 (+1) | 172 → **0** | 2 → **0** |

**A plain `login()` disturbs nothing.** The correlation is exact: the form that
opens no connection breaks nothing, and the form that opens one breaks
everything registered against the old one.

**So callbacks are bound to the connection object, not to the `api` handle.**
That was a hypothesis inferred from a counter; it is now a controlled result.
The practical consequences:

- **Re-assertion is needed after session-replacing logins, and only those** —
  the scope of the fix is settled rather than assumed.
- **There is no cheap safe re-login.** A plain `login()` is harmless *and*
  useless: it neither refreshes the token nor disturbs anything.

*(The 12-tick baseline is thin — 12:22 is the lunchtime lull, against 172/60 s
at 11:56. The load-bearing number is the **50 after**, which is unambiguous
against run 4's zero.)*

##### This sharpens the reconnect risk considerably

If callbacks die with the connection, then **any** replacement of it drops them —
including one the SDK performs itself after a dropped socket. A reconnect
creates a new connection by definition, so unless the SDK re-attaches handlers
internally, a network blip leaves the process **alive, subscribed, and deaf**.

That is no longer speculation about a mechanism; it is a prediction of a
confirmed one. It means the re-assertion closure is not a re-login helper —
**it is the correct repair for any session replacement**, and FeedWatch's repair
path should call it rather than the subscription-only `_resubscribe`, which
cannot fix a dropped callback no matter how many times it retries.

Still unmeasured (it needs a real socket drop): whether the SDK re-attaches on
its own reconnect. The 24 h soak never exercised it — `on_session_down` never
fired and `feed_missed` stayed 0.

##### The gap this opens in the verify window

**`usage()` proves the token, not the feed.** If a re-subscribe silently fails at
14:55, `usage()` at 14:59 still passes, and the first evidence of a dead feed is
FeedWatch ~60 s into the session — because FeedWatch is gated on
`policy.includes()` and 14:59 is outside it. The healing path would then work,
but a session would open blind for a minute or so.

The anchors sit inside the `simtrade` window (14:50–15:00, measured), so **there
are prints available to confirm against** — the verify step could assert that at
least one tick has arrived since the re-login, simtrade included. **Not yet
decided**; recorded here so it is a choice rather than an oversight.

##### The probe leaked the ID a second time, by a second route

Run 2 still printed `L125551606` — this time bare inside an SDK **session-event
string** (`#P2P/v:…/PYAPI/L1255…/0815/…`), where there is no `person_id=` for the
field-name redaction to match. Redaction is now **shape-based** as well: any
`[A-Z][0-9]{9}` token is blanked wherever it appears. Two regression tests.

Note the SDK **also prints that line itself**, on its own stdout, outside our
code entirely — no redaction we write can reach it. Treat raw probe consoles as
sensitive regardless.

##### Q8 — the question run 1 did not ask, and the one that matters most

**Q1 established that a second `login()` *succeeds*. It did not establish that it
does anything.** The parameter is named `force_refresh` and defaults to `False`,
which is exactly the shape of an SDK that returns a cached session when it can.
Run 1 is consistent with that reading: **no second "Session up" event appeared**
after logins #2 and #3, only after #1.

If a plain re-login is a no-op, `ReloginScheduler` will call it faithfully twice a
day, record `relogin` events, print healthy counters — and the run will still die
at 24 h. **The fix would be inert while looking like it was working**, which is
the worst outcome available and precisely the §2.22b failure shape.

Two instruments were added, both answerable off-hours:

| Instrument | What it distinguishes |
|---|---|
| `usage().connections` sampled after each login | a login that opens a *new* connection did something. It also catches a **leak**: §2.8b caps connections at **5 per person_id**, and twice-daily re-logins would breach that in three days — the fix killing the run it exists to protect |
| `on_event` "Session up" counts | the only externally visible sign that a session was established rather than reused |

The third Q1 form — **`logout()` then `login()`** — was specified and never
actually run; it is now exercised, and it is the unambiguous fallback if the
other two turn out to be no-ops.

##### The questions

New `live/relogin_probe.py`, ~6 minutes inside **any** session — it tests SDK
mechanics, not strategy, so the day session (08:45–13:45) serves as well as the
night one and comes sooner. Seven questions, none of which the docs can be trusted
for (§2.18, §2.22b), and none of which introspection can reach: `Shioaji` is a
compiled `_core.pyd` and `login` / `logout` / `subscribe` carry no source and no
docstrings.

1. Does `login()` on an already-logged-in object succeed? Compare `login(...)`,
   `login(..., force_refresh=True)` and `logout()` + `login()`. Signature
   confirmed on shioaji 1.7.2:
   `login(api_key, secret_key, subscribe_trade=True, receive_window=30000, force_refresh=False)`.
   There is **no** token-refresh method — re-login is the only lever.
2. **Does the tick subscription survive?** 60 s of ticks before, 60 s after, with
   no re-subscribe in between.
3. **Does the order callback survive, or duplicate?** The dangerous one.
   `ShioajiBroker.__init__` sets `api.set_order_callback(...)` once
   (`broker/shioaji.py:155`) and `login(subscribe_trade=True)` re-establishes
   trade subscription. Place one passive limit far from the touch so it cannot
   fill, count invocations, cancel. A duplicated callback double-counts fills; a
   dropped one loses them in silence.
4. Does `list_positions()` work on the new token, and report the same position?
5. Does the account fingerprint change across a re-login?
6. **Does Shioaji grant a session on a non-trading day?** (§2.25) The 08:40 and
   14:55 anchors are wall-clock and will fire on Saturday and Sunday. If a
   weekend `login()` is refused, the scheduler's backoff would burn its whole
   attempt budget twice a day against a closed venue and WARN each time — noise
   that needs designing out rather than discovering in a soak. **This is the one
   question answerable off-hours**, and it needs only a bare login, no
   subscription and no orders.
7. **Does `api.usage()` raise on a dead token?** The verify window calls it to
   prove the session is usable, so a `usage()` that returns quietly instead of
   raising would leave the window **inert** — passing forever while the token is
   already gone. `list_positions` was *observed* raising 401; `usage()` was not.
   Testable in seconds with no 24-hour wait: `login()` → `logout()` → `usage()`,
   and see what comes back. If it does not raise, the verify call reverts to
   `list_positions`. **Answerable off-hours too**, alongside Q6.

**Verify:** a written answer to all seven. Steps 2–3 are designed against those
answers, not against a guess — which is the rule that the last five SDK
surprises were all found by breaking.

#### Step 2 — `live/relogin.py` — BUILT 2026-08-16 (608 tests green)

`ReloginScheduler` is written, with `tests/test_live_relogin.py` (48) and
`docs/live/relogin.md`. Pure clock logic behind two injected callables, no
`shioaji` import — the probe determines only what goes *inside* them.

**Three things the tests found, all of which changed the design:**

| Found | Was | Now |
|---|---|---|
| **Retry state leaked across windows.** A window closing mid-retry never settled, so the next day's window inherited a spent budget and a deadline set yesterday | anchors settled only on success or exhaustion | every window ends settled, abandonment included |
| **The attempt counter was redundant.** 60 s exponential backoff inside the window fits attempts at +0/+60/+180 s; the fourth lands past the window | `max_attempts_per_anchor = 5` | **removed** — the window *is* the budget, a structural bound rather than a tuned one |
| **A token-age floor cost margin for nothing.** It stranded the first anchor for any process started 13:00–15:00 | 2 h floor, to avoid a "wasted" login | **removed** — a login costs 2 of 1 000/day; the floor pushed the ceiling from 17 h 45 m to **18 h 36 m** |

The last one was caught by the week-long property test replayed from **all 24
start hours** — a case no hand-written scenario would have covered, and the
direct argument for writing the invariant as a property rather than an example.

##### The shape as built

A `ReloginScheduler` in the `FeedWatch` mould: injected callables, no `shioaji`
import, driven from the existing 1 Hz clock tick.

```
note_login(now)          record a successful login
token_age(now)           how old the current token is
check(now) -> Outcome    act if a window is live; else None
summary()                counters for the shutdown block

Outcome: RELOGGED | FAILED | ABANDONED | VERIFIED | VERIFY_FAILED
```

- Each window fires **at most once per day**, and no window survives its own
  closing time — abandonment is an outcome, not a leak.
- **Failure → WARN, then retry on exponential backoff** (60 s base, 900 s cap),
  bounded by the window itself. That keeps it on the right side of §2.8b: the
  hazard recorded there is an *unbounded* login loop during a suspension, and
  six attempts a day against a 1 000/day cap is not that.
  `test_no_login_retry_helper_is_exposed` stays green — it asserts over
  `dir(RestartGuard)` only (`tests/test_live_startup.py:238`), and this is a
  different module with a different lifecycle, the same reasoning that kept
  `RestartGuard` out of `SafetyRails`.
- **No token-age floor**, and no age backstop. See the two decisions above.
- On success, re-run position reconciliation once against the new token.

#### Step 3 — wire into `cli.py` — WIRED 2026-08-17

One call beside `feed.check(stamp)` in the clock block (`cli.py:702`), gated on
nothing: the anchors sit in the pre-open quiet, which is by definition outside
every session, so the policy gate the feed check carries would have stopped this
firing at all.

The closures are what Step 1 measured (`cli.py:586-626`). `_relogin` is
`login(force_refresh=True)` followed by `_register_callbacks()` and
`_subscribe(drop_first=False)` — a refreshing login replaces the session object
and takes every registration with it. `_verify` is `api.usage()` plus an
assertion that a tick has arrived since the re-login, skipped on non-trading days
(§2.25). INFO on success, WARN on a failed attempt or an abandoned window, FATAL
on a session that failed its pre-open check and could not be recovered.

#### Step 4 — `tests/test_live_relogin.py` — DONE (48 tests)

Offline and clock-driven. The re-login half: anchors derived rather than written,
firing once per window per day, the window closing before real prints, backoff
spacing measured at +0/+60/+180 s, and an unresolved window abandoning its
attempt so nothing leaks into the next day. The verify half: the check running
once a minute before each open, `verify=None` disabling it, a failed check taking
exactly one recovery re-login, and a failed check with a failed recovery
reporting `VERIFY_FAILED`.

**The property that actually matters** — a simulated week replayed from **all 24
start hours**, asserting token age never reaches 24 h. This is the test that
found the age floor, and no hand-picked scenario would have.

Still to do when the probe lands: **fakes built from probe-measured types**
(§2.22b). Today's fakes are bare callables that raise or return `None`, which is
honest — they stand in for nothing whose shape we have observed yet.

#### Step 5 — verify in the soak, not in a separate run — PASSED 2026-08-18

Success was the next soak crossing **24 h** with `relogin` records at 08:40 and
14:55, `verified` records at 08:44 and 14:59, and no 401. All three happened.

`outputs/live_20260817_134515` started 2026-08-17 13:45:15, **passed the wall at
2026-08-18 13:45:15 and was still running at 2026-08-19 01:29 (35 h 44 m)** —
where the soak above died at 24 h 0 m 36 s.

| Anchor | re-login | verify |
|---|---|---|
| 2026-08-17 night | 14:55:00, `attempts=1` | 14:59:00 `verified` |
| 2026-08-18 day | 08:40:00, `attempts=1` | 08:44:00 `verified` |
| 2026-08-18 night | 14:55:00, `attempts=1` | 14:59:00 `verified` |

Every window settled on its first attempt: 0 failures, 0 abandoned, 0 verify
failures. The three INFO re-login alerts are the only alerts of any severity in
the file, and all 54 `feed_check` verdicts were `quiet`.

**What closes the question is the REST calls made *after* the wall, not the
uptime.** `api.usage()` answered at 2026-08-18 14:59:00 — 25 h 13 m 45 s after
the login that issued the first token — and the feed check's `api.snapshots()`
answered a minute later. `usage()` is an authenticated call on the same token
that raised `TokenError` here, so a run that crosses 24 h *and* keeps answering
it is exactly the branch this step named: `force_refresh=True` resets the clock,
rather than firing on schedule and printing healthy counters while the token
expires anyway.

**Peak token age was 17 h 45 m 00 s** (2026-08-17 14:55 → 2026-08-18 08:40) — the
designed ceiling to the second, on the overnight leg between the night anchor and
the next day's.

#### The open risk, stated (§2.24)

**The 24 h lifetime rests on a single data point.** The 6 h 15 m margin absorbs a
real lifetime as short as ~18 h; below that the anchors alone fail — though the
verify window would then catch it at the next open rather than mid-session.

**The remaining gap needs two independent failures.** Every re-login attempt in
one window must fail *and* the health check a minute before the open must fail
too; only then does token age reach 24 h. That is the state after rejecting the
age backstop, and it is asserted rather than assumed by
`test_a_lost_window_still_reaches_the_wall_when_verify_fails_too`.

### 2.25 Nothing in this system knew what a weekend was — FIXED 2026-08-17

**A soak halts on Saturday at 15:16.** Measured offline by stepping a one-minute
clock across a weekend through the real `SafetyRails` and `SessionPolicy`:

```
halted at : 2026-08-15 15:16:00   (Saturday)
reason    : no market data for 960.0s (limit 900.0s)
action    : HaltAction.STOP
```

**The cause.** `classify_session` (`environment.py:19-34`) reduces a timestamp to
its **time of day** and nothing else — a grep for `weekday` / `isoweekday` /
`holiday` / `dayofweek` across every `.py` in the repo returns **zero** hits. So
Saturday 15:00 classifies as `NIGHT`, `policy.includes()` says True, and the
staleness backstop starts counting against a market that closed at 05:00 that
morning and will not reopen until Monday. Sixteen minutes later it halts.

The trading-day test inside `same_session_instance` is what delays it — it
refreshes the marker once at 15:00, because Saturday 05:00 belongs to *Friday's*
trading day — but that buys exactly one refresh, and then the gap accrues
normally against the 900 s backstop.

**This makes V5 unreachable as written.** "Five consecutive unattended days"
*must* contain a weekend, and the run dies on the first Saturday afternoon. It
was never seen because neither soak crossed one: the first ran Wed→Thu
(`live_20260812_034336`), the second Thu→Fri (`live_20260813_191326`).

**The two blockers are sequential, and §2.24 comes first.** A run today dies at
24 h and never reaches a Saturday, so fixing §2.25 alone changes nothing. Fixing
§2.24 alone moves the wall from 24 h to the first Saturday 15:16 — real progress,
but still short of five days. **Both are required for V5, in that order**, and
§2.25 is the one that specifically owns the "5 consecutive days" criterion.

**Second-order cost, if the staleness rail alone were relaxed.** FeedWatch is
gated on the same `policy.includes()` test, so it would poll the venue **1 680
times** between Saturday 05:00 and Monday 09:00 — one a minute through two fake
14-hour "night" windows — to confirm a market that is closed. Any fix has to
teach *both* the same calendar, not just the rail that halts first.

**What re-login (§2.24) would do meanwhile.** The 08:40 and 14:55 anchors are
wall-clock and would fire on Saturday and Sunday like any other day. Whether
Shioaji grants a session on a non-trading day is **unmeasured** — it is **Q6 of
the §2.24 Step 1 probe**, and the only one of the six answerable off-hours.
Note the ordering: Saturday's 14:55 anchor fires 21 minutes **before**
the 15:16 halt, so a weekend re-login would be attempted, and the run would then
die of staleness regardless.

#### FIXED 2026-08-17 — a weekday test in `SessionPolicy`, and nothing more

`SessionPolicy.is_trading_day()` (Mon–Fri, minus an optional holiday set), tested
inside `includes()`. That one place fixes all three consumers at once: the
staleness rail stops accruing a gap against a closed market, FeedWatch stops
polling, and the bar filter stops classifying Saturday 15:00 as a session.

The original instrument now answers cleanly — **no halt, 0 in-session minutes,
0 venue polls** across the same Friday-close-to-Monday sweep that produced
`halted at 2026-08-15 15:16:00`. Pinned as a regression test.

**The trading-day test is on the *trading day*, not the raw date.** A raw
`ts.weekday()` gets Friday's night session backwards: 02:00 on Saturday belongs
to Friday's session and must read as trading, or the loop halts every Saturday
morning mid-session. Covered by its own test.

**A cleverer design was considered and rejected.** Since FeedWatch already
*asks the venue*, its QUIET verdict is empirical evidence the market is closed,
and feeding that into the staleness rail would have handled weekends, holidays
and make-up Saturdays with no calendar at all. It was dropped for a better
reason than complexity: **it depends on how `snapshots()` behaves across a
two-day closure, which has never been observed.** Fixing an unmeasured problem
with unmeasured behaviour is the trade this project keeps losing (§2.18, §2.19,
§2.22b). The weekday test is deterministic and fully testable offline.

**The residual, stated:** a weekday market holiday still reads as a trading day
and would halt a soak the same way — roughly 10–15 days a year against the ~104
this fixes. `SessionPolicy.holidays` carries them as **data**, empty by default,
so closing that gap is a list rather than a redesign. Empty rather than
pre-populated on purpose: a stale holiday list would halt a run on a day the
market was open, which is worse than the gap it closes.

**Make-up trading Saturdays (補班日) would be skipped**, costing a session's
trading rather than breaking anything. Rare, and the failure is benign.

##### One consequence for §2.24

The re-login anchors are wall-clock and **still fire on weekends** — the token
expires 24 h after login whether or not the market opens, so a run spanning a
weekend must refresh through it. Only the *verify* step's tick half is gated on
`is_trading_day`: on a closed day there is no pre-open trial print to confirm
against, and without the gate the check would fail twice a day, spend a
redundant recovery re-login, and write a `verify_failed` record each time —
training a reader to ignore the one record that matters.

### 2.26 The trail was about to start leaking a national ID — FIXED 2026-08-17

Found by the 5-minute dry run that shook down the §2.24/§2.25 wiring
(`outputs/live_20260817_130842`). Everything else in that run was clean: 287
ticks, the anchors banner, `token_age_at_close 0:05:17`, and `bars 0` /
`off_session_dropped 4` / `feed_checks 0` — all correct for a NIGHT policy
running inside the day session.

**The SDK embeds `person_id` bare in its session-event strings:**

```
#P2P/v:bcsolace01/Oryfkqqm/PYAPI/L1255…/0817/050844/…/49.213.252.147/>
```

`cli.py`'s `_on_session_event` records `info` verbatim, so those go into
`session.jsonl` — the file most likely to be pasted into a chat or a ticket, and
the one `account_fingerprint` digests `person_id` specifically so it never has to
carry (`cli.py:109`).

**The dry run's trail was clean only by an accident of ordering.** The login
events fire before `_register_callbacks()` runs, so nothing was registered to
receive them — `session_event` count was **0**. But §2.24's re-login emits
exactly those events at 08:40 and 14:55, *with the callback registered by then*.
The first soak would have written the ID into the trail twice a day.

**Fixed in `Recorder.record()`**, scrubbing national-ID-shaped tokens from the
serialised line before it reaches disk. In the recorder rather than at the call
site, for the same reason the probe's redaction lives in its renderer: no future
`record()` can reintroduce it. Non-identifying context is preserved.

**Out of reach:** the SDK also prints that string on its own stdout. No
redaction here touches it — treat raw live consoles as sensitive.

#### The fix was broken in a way reading could not see

The first attempt compiled to `'\\x08[A-Z][0-9]{9}\\x08'`. The `\\b` word
boundaries had become literal **backspace bytes**, because `re.sub` interprets
escapes in its *replacement* string. The regex then matched nothing.

```
grep / print .pattern :  [A-Z][0-9]{9}          <- looks right
repr(.pattern)        :  '\\x08[A-Z][0-9]{9}\\x08'  <- what was compiled
```

A silently disarmed redaction is worse than none: it looks protective while
writing the identifier verbatim. **This is the same shape as §2.22b** (14 green
tests against a fake that shared the code's assumption) and as `SANE_AFTER` in
`feedwatch.py` (a malformed answer reading as "healthy"). The regression test
asserts the **compiled** pattern, not the source text, because the source text is
exactly what hid it.

---

## 3. Module layout

**As built, 2026-08-17.** Entirely additive — nothing under `run.py`,
`execution.py`, `trader.py` or `portfolio.py` was modified (the V0 parity harness
is what holds that claim honest).

```
forecast_eval/live/                        (~7 000 lines)
  broker/
    base.py          Broker ABC — the seam Trader drives unchanged
    sim.py           wraps the existing Execution (V0 parity harness)
    dryrun.py        real decisions, real bookkeeping, nothing sent
    shadow.py        wraps a venue broker so an offline Execution sees the
                     same orders — V4's fill-gap instrument
    shioaji.py       the venue adapter: orders, fills, positions, callbacks
  cli.py             the entry point: login, subscribe, clock loop, teardown
  runtime.py         LiveRuntime — everything decidable, testable offline
  loop.py            LiveLoop — SPEC §4.1's four steps, restructured to be pushed
  bars.py            TickAggregator: ticks -> 1-min bars (RPT convention)
  backfill.py        kbars -> warm-up bars, with the three corrections kbars needs
  sessions.py        SessionPolicy: which sessions we trade, which days are
                     trading days (§2.25), where a trading day begins
  state.py           SafetyRails — what stops a running loop, STOP vs FLATTEN
  startup.py         RestartGuard — the rails that survive the process dying
  relogin.py         ReloginScheduler — the 24 h token wall (§2.24)
  feedwatch.py       quiet market vs. broken feed, by asking the venue (§2.22)
  alerts.py          Alerter — the one door for anything needing a human (§2.23)
  flatten.py         standalone position-flattening tool
  contracts.py       TXF contract resolution and the alias trap (§2.18b)
  capture.py         TickCapture — the raw feed, before any filter
  recorder.py        structured JSONL audit trail
  config.py          LiveConfig, pinned to the results.tsv row

  # probes and checkers — instruments, not runtime
  relogin_probe.py   §2.24's seven questions about what a second login does
  v1_check.py        V1's live half: replayed bars vs. kbars
  v1c_check.py       V1c: the kbars labelling convention
  v4_probe.py        V4: the simulation venue's fill behaviour
  v72_probe.py       §7.2: does place_order work under simulation=True
  prod_audit.py      read-only production-account audit
```

**Tests: 653 collected, 37 s** — 29 files under `forecast_eval/tests/` covering
the live path, plus `fakes/fake_shioaji.py`. The largest, by what they defend:

| File | n | Defends |
|---|---|---|
| `test_live_relogin.py` | 48 | the 24 h token invariant, replayed from all 24 start hours |
| `test_shioaji_adapter.py` | 42 | the venue adapter, incl. callback re-attachment |
| `test_live_state.py` | 33 | the rails — incl. the weekend regression (§2.25) |
| `test_live_runtime.py` | 26 | the offline-decidable half of a live session |
| `test_relogin_probe.py` | 25 | the probe's own correctness, and its ID redaction |
| `test_live_sessions.py` | 21 | SessionPolicy, trading days, the night-crossing case |
| `test_live_parity.py` | 5 | **V0** — live and backtest bit-identical over 39 725 bars |
| `test_docs_coverage.py` | 53 | the 1:1 docs rule, enforced rather than remembered |

Every module above has a page under `docs/live/`, enforced by
`test_docs_coverage.py` rather than by anyone remembering.

### 3.1 The `Broker` interface

Deliberately narrow — exactly what `Trader` already needs, nothing more:

```python
class Broker(ABC):
    def submit(self, order: OrderEvent, *, current_price: float) -> None: ...
    def cancel(self) -> None: ...
    def has_pending(self) -> bool: ...
    def pending_order(self) -> Optional[OrderEvent]: ...
    def poll_fills(self, market: MarketEvent) -> List[FillEvent]: ...

    # added after the original design, each for a measured reason:
    @contextmanager
    def urgent(self) -> Iterator[None]: ...          # §5 FLATTEN pricing
    def broker_position(self) -> Optional[int]: ...  # §2.9, None if unsupported
    def broker_position_detail(self) -> Optional[HeldPosition]: ...  # §2.20
```

`poll_fills` takes the bar: `SimBroker` genuinely needs it to resolve a fill
against the incoming print, while the venue adapter ignores it because fills are
reported rather than derived. `urgent()` is a scope, not a flag, so the offline
brokers can ignore it entirely — which is what keeps the V0 comparison unchanged.
`broker_position_detail` returns the entry price as well as the size, which is
what lets a restart adopt a position it did not open (§2.20).

`Trader` already talks only to `submit` / `cancel` — so `Execution` satisfies the
first four **as-is**. `SimBroker` adds `poll_fills` by calling
`Execution.check_fill(market)` and returning `[fill] or []`. That is what makes
V0 possible without touching `execution.py`.

`Trader.__init__(execution=…)` keeps its name; it is duck-typed against the ABC.
No change to `trader.py`.

### 3.2 `LiveLoop` — same four steps

```
on_bar(bar):                       # driven by TickAggregator, not a for-loop
  1. for fill in broker.poll_fills():  portfolio.apply_fill; trader.on_fill
  2. exit_rule.should_exit(...)     -> trader.submit_exit
  3. if bar_idx >= warmup and stride boundary:
         forecaster.forecast(history_df)  # fresh slice, look-ahead guard intact
         emitter.emit_signal -> trader.on_signal
  4. session boundary -> forced close (off in v1)
  reconcile(); record()
```

Step ordering and the fresh-DataFrame look-ahead defense are preserved verbatim
— that is what V0 asserts.

**Threading**: Shioaji callbacks arrive on its own thread(s). The loop must be
single-threaded w.r.t. strategy state. Design: callbacks only *enqueue* onto a
`queue.Queue`; one worker thread drains it and runs the four steps. Never call
`forecaster.forecast()` (GPU, seconds) from a Shioaji callback thread — it would
stall the quote handler.

---

## 4. Phased delivery

Each phase has an explicit pass criterion. Do not start the next until the
current one passes.

### Phase A — Broker seam + SimBroker (no Shioaji dependency)

1. `live/broker/base.py`, `live/broker/sim.py`, `live/loop.py`, `live/recorder.py`.
2. `live/bars.py` with the RPT convention.
3. Tests: `test_live_broker_sim.py`.

**Verify:** `uv run python -m pytest forecast_eval/tests/ -q` — existing 98 tests
still pass, new ones green. No import of `shioaji` anywhere yet.

### Phase V0 — Offline parity *(the gate)*

Feed `LiveLoop` + `SimBroker` the same `ticks` DataFrame `run_backtest` consumes,
by pushing rows through it one at a time.

**Pass criterion:** for at least 3 configs (current `wiring.make_strategy()`; a
stop-loss config; a forced-close config), over ≥50 000 bars:

- identical `len(fills)` and, element-wise, identical
  `(timestamp, side, fill_price, quantity, fee)`;
- identical `portfolio.net_pnl()` to full float precision;
- identical signal list (`timestamp, direction, strength`).

Any divergence is a bug in the restructuring, not in Shioaji. Use a real Toto2
forecast cache (or a cheap deterministic forecaster) so the test is CI-fast.

### Phase V1 — Bar aggregator parity

Capture one full session of live ticks (`--broker dryrun`, quotes only, no
login-order scope needed beyond market data). Aggregate to 1-min. Diff against
`TXF_RPT_minute_day.csv` for the same date.

**Pass criterion:** ≥99.5 % of minutes match on `close` exactly; every mismatch
explained (rollover minute, halt, late print). Also confirm every `simtrade=True`
tick was dropped, and (V1b) that the resolved `TXFR1` code equals the RPT
volume-max contract for that day.

### Phase V2 — Shioaji adapter against a fake

`fakes/fake_shioaji.py` implements `place_order`, `cancel_order`,
`update_status`, `list_trades`, `list_positions`, `set_order_callback` and can be
scripted to emit: normal fill, partial fill, rejection (`op_code != "00"`),
**deal-before-order** ordering, duplicate deal, cancel-race (fill arrives after
cancel was sent).

**Pass criterion:** adapter produces exactly one `FillEvent` per real deal
(dedup on `(ordno, exchange_seq)`), surfaces rejections as a halt rather than a
silent no-op, and never emits a fill for a confirmed-cancelled order. No network,
no credentials — runs in CI.

**PASSED** — `live/broker/shioaji.py`, 24 tests at the time, **42 today** (`test_shioaji_adapter.py`; the growth is §2.20 resume, §5 urgent pricing and §2.24 callback re-attachment). Design points worth recording:

- **Cancel does not discard the order.** `cancel()` moves it to a `_retiring`
  list and frees the book immediately, because §4.4 has the Trader cancel *and
  re-submit* on the same signal — waiting for the venue's confirmation would
  stall every decision. The retired order stays tracked because it can still fill
  in the race window, and **that fill is real money**: discarding it would leave
  us holding contracts we do not know about. This is the sharpest divergence from
  `SimBroker`, where cancellation is synchronous and total.
- **Three independent guards on deals**, because any one alone is insufficient:
  dedup on `(ordno, exchange_seq)`; correlation by `trade_id`↔`id` *or* `ordno`
  (needed because deals can precede order events, so `ordno` may not be known
  yet); and a running `filled` bound against the order's own quantity as the
  backstop that stops any stray report inventing contracts.
- **Unknown callback states are anomalies, not no-ops.** The `OrderState` enum
  literals are matched on *name* (`FuturesOrder`/`FuturesDeal`) as the docs
  display them; the real values are unverified until first contact. A wrong guess
  therefore halts loudly on day one instead of silently discarding every fill.
- **`broker_position()` returns `None` on query failure**, so a transient error
  degrades to "cannot compare" rather than fabricating a mismatch that halts a
  healthy loop.

Not yet covered (needs a real session, deferred to V3/V4): the actual
`OrderState` literals, real `Trade`/position object shapes, and whether
`place_order` is even accepted for futures under `simulation=True` (§7.2).

### Phase V3 — first live dry run: PASSED (2026-08-11, 10 min)

`uv run python -m forecast_eval.live.cli --duration-minutes 10`, night session:

| | |
|---|---|
| Backfill | **17 458 night bars in 0.7 s** (2026-07-13 → 2026-08-11), 30-day request |
| Warm-up | context 3008, **emitter primed to 4/4 samples**, 12.7 s total |
| Live | 103 ticks → **10 bars**, 1 forecast, 1 signal (HOLD), 0 orders |
| Drops | 0 simtrade, 0 late, 0 off-session |
| Halted | no |

**Questions this answered:**

- **`kbars` does not truncate a 30-day request.** 17 458 bars in one call closed
  the open item from §2.8 — no paging needed for the K=500 priming window.
- **Priming works on real data.** 4/4 deque samples, so the trend filter was
  armed from the first live bar — the failure mode §2.8 was written to prevent.
- **The alias resolution fix is confirmed in production.** `TXFR1` was subscribed
  as `TXFH6` (`TIC/v1/FOP/*/TFE/TXFH6`), and every captured tick carries
  `code=TXFH6` — exactly the alias-vs-resolved mismatch that would have halted
  the loop via reconciliation (§7.2).
- **Capture fidelity holds on real ticks.** Replaying `ticks.csv` through
  `TickAggregator` reproduces the live bars **exactly** — 10/10 on timestamp,
  price, volume and session.

**Two SDK deprecations the run surfaced, both now fixed:**
`set_on_tick_fop_v1_callback`'s `(exchange, data)` signature is deprecated in
favour of `(data)`; and `api.Contracts` is deprecated in favour of
`api.contracts` — moved 2026-08-11 in `cli.py`, `v1c_check.py` and
`v72_probe.py`. The swap was checked by introspection first, since the warning
says the old name is "backed by contracts v2" and a v2 container could have had
a different shape: `sj.Contracts` exposes **both** `Futures` and `futures`, and
both `api.Contracts` and `api.contracts` gate on the same pre-login `AuthError`,
so `Contracts` is a shim over `contracts` rather than a different object. The
post-login shape is still unverified — a first-contact failure would be a loud
`AttributeError` at startup, before login-time trading.

**Still unobserved** (a quiet 03:44–03:54 window cannot exercise them):
simtrade prints, which only appear around a session open; the 05:00 session
boundary; reconnects; and any non-HOLD signal.

#### Phase V3 — 60-minute boundary run, 2026-08-11 04:11:25 → 05:11:40

`--duration-minutes 60`, deliberately spanning the 05:00 night-session close.
Artifacts: `outputs/live_20260811_041125`.

| | |
|---|---|
| Backfill | **17 486 night bars in 0.1 s** [2026-07-13 15:00 .. 2026-08-11 04:11] |
| Warm-up | context 3008, emitter primed **4/4**, 12.3 s |
| Live | 332 ticks → **47 bars** (04:11 … 04:59), 2 forecasts, 2 signals (both HOLD), 0 orders |
| Drops | 0 simtrade, 0 late, 0 off-session |
| Halted | **no** — `halt_reason` null, `position_at_close` 0, `pending_order` false |

**① No spurious staleness halt after 05:00 — PASSED, and this was the run's
purpose.** `cli.py`'s consumer calls `rt.on_clock()` every second, which flushes
the aggregator and *then* runs `rails.check_staleness` (`runtime.py:283-285`), so
the watchdog was evaluated ~660 times against a completely silent feed — 11
minutes against a 120 s window. It never fired. The §2.12 defect-2 fix
(`same_session_instance`, comparing trading day as well as label) is confirmed in
production; without it the loop would have halted around 05:02.

**② The final 04:59 bar was flushed — PASSED, and by the clock path
specifically.** The last tick of the session is 04:59:58.506 and no later tick
ever arrived, so `add_tick` could not have closed that bar; only
`aggregator.flush(now)` could. Bar `idx=46` is stamped 04:59 with volume 37.

**③ Off-session drops — not exercised, and cannot be here.** See §2.15.

**Convention check passed incidentally:** 47 bars over a 49-minute span, with
**04:23 and 04:44 omitted entirely** rather than zero-filled. That is §2.4's
"empty minutes are absent" rule holding on live ticks, which the 10-minute run's
unbroken 10-bar stretch could not show.

**One defect found — see §2.14**: the backfill's last bar and the first live bar
are both stamped 04:11, so one minute enters the model context twice.

### Phase V1 — live half: PASSED (2026-08-11)

`uv run python -m forecast_eval.live.v1_check --capture outputs/live_20260811_205731/ticks.csv`

| | |
|---|---|
| Source A | 1 603 captured ticks → `TickAggregator` → **39 bars** (20:58 … 21:36) |
| Source B | `api.kbars` → `kbars_to_bars` → 700 NIGHT bars, clipped to the same window |
| **Closes** | **39/39 = 100.00 %** (criterion: ≥99.5 %) |
| **Unmatched minutes** | **0** on either side |
| Volume | 0 differences (informational; the model reads close only) |
| Ingest | 0 simtrade, 0 late, 0 off-session |

**What this establishes.** The two live sources are now cross-validated against
each other:

```
ticks → TickAggregator → bars   ═══ 39/39 ═══   kbars → kbars_to_bars → bars
```

Combined with the offline half — the aggregator reproducing `Environment` event
for event on 3 000 real RPT bars — the chain closes transitively: **live bars
land on the convention the model was evaluated on.** And it tested the sources
actually in use rather than a historical file, which was the only option left
once §2.8 Finding D established that expired contracts are delisted.

**It also retires the §2.16 residual outright.** Before the flush grace, live and
replay disagreed on ~⅓ of busy minutes. Now three independent views — live bars,
the replayed capture, and the venue's own `kbars` — agree on all 39. The verdict
being *exact* rather than merely above 99.5 % is what makes that claim strong.

*(The run also printed a contract MISMATCH warning. It was a bug in the check,
not a finding — see §2.18b. The 39/39 agreement is itself proof the contract was
right: when V1c compared two genuinely different months, only 3 of 300 closes
matched.)*

### Phase V3 — original plan

`sj.Shioaji(simulation=True)` + real TXF quotes + `DryRunBroker`. Run a full
session; place nothing.

**Pass criterion — shadow parity:** afterwards, replay that session's bars
offline through `run_backtest` with the same config. The *decisions* (signal
timestamps, directions, strengths, and the limit price each order would have
used) must match the live log exactly. Divergence isolates to one of: bar
mismatch (→V1), `simtrade` leakage (→2.5), or latency/timestamping.

Also record and report: `decision_latency_ms` distribution, quote gap
distribution, reconnect count.

### Phase V4 — Simulation mode, **real simulated orders**

Same, with `ShioajiBroker` actually calling `place_order`. This is where the fill
model gets measured.

**Instrumentation (the deliverable of this phase):** for every live order, log
side-by-side —
- what `Execution` *would* have filled (price and whether at all), and
- what Shioaji actually reported (filled / not filled by cancel time, price).

Derive: **fill rate**, **mean slippage vs. sim fill price**, and **PnL gap**.
This slots directly into the SPEC §7 attribution story — it is the same
"signal PnL − realized PnL" decomposition, extended one layer to
"sim-realized − live-realized".

**Pass criterion:** ≥5 trading sessions, zero position-mirror desyncs, zero
unhandled exceptions, kill switches never falsely triggered, and a written
fill-model gap number **carrying the §2.19 caveat** — the figure describes our
model against *Shioaji's simulator*, which fills on prints rather than against a
book, and the distance from that to a real venue is not estimable from this side.

> **Expect this to take more than five nights.** At `stride=30` a full night
> session is ~28 boundaries and only 3.74 % of signals are non-HOLD, so the
> arithmetic gives **~1 order per night**. Five sessions is therefore ~5 orders:
> enough for a crude fill rate, not for a slippage distribution. Plan the number
> of nights around how much confidence the figure needs, not around the "≥5".

#### V4 progress (2026-08-11) — order path wired, instrumentation still to come

**Done — the flatten order type.** `Broker.urgent()` is a scoped marker,
defaulting to a **no-op**, that the loop opens around a `FLATTEN` close. The
constraint that shaped it: the close is submitted by `Trader.submit_exit`, and
`trader.py` is not ours to modify, so the intent could not travel as an argument
without touching code the backtest shares. A context manager rather than a flag
so it cannot leak — an exception mid-submit still restores normal pricing.
`SimBroker`/`DryRunBroker` inherit the no-op, which is what keeps V0 parity
unchanged by construction.

`ShioajiBroker` prices an urgent order **through the touch** (5 ticks, directional
— a BUY closing a short pays *more*) and sends it **IOC**. Rejected: **MKT**
(unbounded in a thin book; a flatten that fills at any price is its own kind of
accident) and **MKP** (band semantics unverified — §7.2's history says do not
ship an unverified Shioaji behaviour into the risk path). A bounded limit either
fills now or expires, and the loop's per-bar retry picks it up next bar. All four
enums verified by introspection first: `OrderType.IOC`,
`FuturesPriceType.LMT/MKP/MKT`, `FuturesOCType.Cover`, `Action.Buy/Sell`.

**Done — `--broker shioaji` wired, behind two flags.** Through V3 the protection
was structural: the option was *absent*, so it could not be reached by a mistyped
word. Re-adding it as one word among three would spend that for nothing, so it
now also requires `--i-understand-this-places-orders`. Further guards:

- **`simulation=True` stays hardcoded** — §7.2 measured `api.simulation` as
  `None`, so the mode cannot be verified after the fact and the constructor
  argument is the only guarantee. This entry point has no reachable path to a
  production account.
- **A pre-existing venue position refuses the start (exit 5).** The mirror starts
  at zero, so an account already holding something makes the reconciliation rail
  halt on the first bar; a clear message beats a confusing mid-session halt. A
  broker that *cannot answer* does not block — that is "cannot compare".
- **The rails are built in the CLI and shared** with the broker, so a rejection
  reported by `ShioajiBroker` reaches the same rails the runtime reads.

Suite **372 passed**; V0 parity intact.

**Done — the fill-gap instrumentation.** `live/broker/shadow.py` wraps the venue
broker so a private `Execution` sees the same orders, and diffs them. Only venue
fills leave `poll_fills`, so measuring cannot change what is measured — the same
rule the recorder follows. 14 tests; suite **386 passed**.

The unit is an **order episode**, submit → cancel/replacement, scored on *close*
rather than on fill. That is what makes "neither side filled" and "only one side
filled" representable at all: a log written only when something fills cannot
compute a fill *rate*, which is the headline number §2.1 asks for.

Reported per session, to console and as a `fill_gap_summary` record:

| | |
|---|---|
| fill rate | live and shadow, over identical orders |
| slippage | on orders **both** filled — signed so positive always means live did worse |
| divergences | `live_only` / `shadow_only` / `neither` |

**Deliberately not reported: a full sim-versus-live PnL.** Once one side fills and
the other does not, the two positions differ, and every later order — generated
from the *live* position by `Trader` — is meaningless for the shadow, which would
be "closing" something it never opened. A PnL number from that is a fabricated
counterfactual. What is given instead is the **slippage total**, the part of the
gap attributable to execution price, plus the divergence counts, which are the
part that cannot be priced without assuming a strategy the shadow never ran.
*(This is a narrowing of the original V4 wording, which asked for a "PnL gap".)*

Two design points worth keeping:

- **Slippage is signed by side.** Paying 2 points more on a BUY and receiving 2
  less on a SELL are the same outcome; unsigned, they cancel to zero.
- **Slippage statistics cover matched episodes only.** Averaging over unmatched
  ones would treat "did not fill" as "filled at no cost" — the exact inversion of
  the adverse-selection effect being hunted.

**Remaining for V4:** ≥5 sessions with `--broker shioaji`, zero mirror desyncs,
zero unhandled exceptions, no false rail triggers, and the written gap number.

#### §2.19 The simulation venue fills on PRINTS — and four wrong turns to get there

**The finding, stated first.** Across nine probe orders on 2026-08-12, whether a
close filled tracked one thing and one thing only: **whether the market printed
during the window.**

| market during the 20 s window | result |
|---|---|
| moving (45214 → 45216) | filled |
| moving (45261 → 45262) | filled |
| **stationary at 45272** | **no fill** |
| **stationary at 45216** | **no fill** |

The last two were priced 5 ticks *through* a stationary market, with the limit
reachable throughout. **Shioaji's simulator matches against trade prints, not
against a resting book.** In a quiet minute there is nothing to match, so an
order's aggression is irrelevant — and at 02:10 TXF is thin enough for 20 seconds
to pass with no trade at all.

**Consequence for the flatten: ROD, and the reasoning that survives.** Neither
time-in-force fills a stationary market. But ROD *rests* and fills on the next
print whenever it comes, while IOC expires with nothing — and an expired IOC left
`has_pending()` **True**, which is what `LiveLoop._on_halted` gates the flatten
retry on. A `FLATTEN` halt would have submitted one IOC, watched it expire, and
never retried: position open, loop believing an order is working. Implemented as
LMT+ROD differing from an ordinary order *only* in price.

**Consequence for V4's headline number — record this with the result.** A fill
rate measured here is a property of **Shioaji's matching engine**, not of a real
book. A live venue fills a marketable order instantly against resting quotes;
this one waits for a print. The V4 gap is therefore "our model vs. the
simulator", which is not "our model vs. the market", and the difference is not
estimable from this side. It is still worth measuring — the simulator is
print-driven and so is `Execution`, which makes them more comparable than they
might have been — but the number must not be reported without this sentence
attached.

#### The four wrong turns, because the instrument was the lesson

1. **"IOC is unsupported in simulation."** Inferred from one non-fill with no
   record of the market. Wrong: the close had been priced off a snapshot taken
   before the *open* had filled, ~6 s stale, so an "aggressive" SELL arrived
   passive. A probe bug read as a venue property.
2. **"So IOC is fine, keep it."** The next run filled, apparently confirming it.
   Also wrong, same blind spot, opposite luck.
3. **"ROD will fill where IOC did not."** Stated as an expectation after the
   `has_pending` evidence. Wrong — ROD did not fill either, because nothing
   printed.
4. **"The ROD change did not take effect."** The trail said `order_type: IOC`
   after the switch. It had gone out as ROD; the *log* was wrong — see below.

What ended the sequence was not a better guess but **making the probe able to
answer the question**: sample the market during the wait window, report whether
the limit was ever reachable, and print `has_pending()` afterwards. The very next
run was unambiguous and needed no interpretation.

#### Two defects the sequence exposed

**A log line that lied.** `broker_submit` recorded its time-in-force from a
*second* expression — `"IOC" if urgent else "ROD"` — which was not updated when
the sent value changed. The artifact described an order the venue never received,
and that record was then read as evidence (wrong turn 4). Fixed by reading the
value back off the order object actually sent, with a regression test asserting
record-matches-sent for every submit. **A trail is the input to every later
diagnosis; a wrong one is worse than none.**

**The probe's gap numbers are artifacts, and would have contaminated V4.** The
shadow is fed a synthetic `MarketEvent` built from one fixed price four times a
second, so it fills trivially and its 100 % rate is meaningless; the live side is
a deliberately marketable order where the strategy's sit *at* the touch. Both
sides wrong, in opposite directions. They now record as
**`probe_fill_gap_summary`**, never `fill_gap_summary` — aggregating the ≥5
sessions by scanning for the latter would otherwise have swept these in and
produced a wrong headline with nothing to flag it.

**The probe has done its job and should now be retired from use.** Every V4
component has run correctly against the venue; it cannot produce meaningful fill
data by construction.

#### V4 session 1 — 2026-08-11 23:36 → 00:16 (`outputs/live_20260811_233651`)

First run with `--broker shioaji`. Everything new held; **0 orders**, as the
arithmetic predicted (2 boundaries × 3.74 % ≈ 0.07 expected).

| | |
|---|---|
| Bars | **40 bars, 40 distinct minutes**, 0 `bar_anomaly`, 0 empty minutes |
| Seam | warm_up last **23:35**, first live bar **23:37** — truncation holding |
| Account | fingerprint recorded (`19***25`, digest `a685deb6b5b8`), no drift warning |
| Banner | `[ORDERS: SIMULATED-LIVE] … flatten=5 ticks IOC` |
| Rails | no halt over 40 bars with a live `broker_position()` behind reconciliation |
| Gap report | `no orders this session`, and `fill_gap_summary` still recorded |

**First midnight crossing — §2.11 validated.** The process ran 23:37 → 00:16
unbroken: 23 bars on the 11th, 17 on the 12th, `23:59 → 00:00` continuous, every
bar tagged NIGHT, no halt. The session-instance and trading-day logic that
`same_session_instance` and the loss anchor depend on now has live evidence
across the date boundary, not just tests.

**Delivery lag, second sample** (n=1 508, ~38 ticks/min):

| | run 1 (21:00) | run 2 (23:36) |
|---|---|---|
| p50 | 0.249 | **0.430** |
| p90 | 0.304 | 0.480 |
| p99 | 0.335 | 0.508 |
| **max** | **0.877** | **0.887** |
| > 1 s | 0 | 0 |

The median nearly doubled while the **maximum barely moved** — 0.877 vs 0.887
across 3 111 ticks in two windows at different tick rates. A tail that stable
looks like a ceiling (a batching or heartbeat interval) rather than random
jitter, which is the better shape to be sizing a timeout against.
`LIVE_FLUSH_GRACE = 2 s` keeps ~2.25× headroom over both.

> **What this session did *not* exercise: the order path.** `ShioajiBroker` was
> constructed, registered its callback and answered preflight, but `submit`,
> the deal callbacks, the fill dedup, the urgent flatten and the whole
> `ShadowBroker` comparison have still never run against the venue. V4's
> instrumentation remains untested end to end, and one order — whenever it
> comes — is what tests it.

#### DECIDED 2026-08-12 — V4 is **PARTIAL**; proceed to V5 and collect the gap passively

**Status: PARTIAL, not passed.** Every V4 component is built and has run correctly
against the venue (§2.19), but the pass criterion includes *a written fill-model
gap number* and there is none. Recording this as PASSED would leave a later
reader — including the author in three months — believing the gap was measured.

**Why deferring is acceptable.** The account is simulation-only, so an unmeasured
fill gap costs nothing today. It becomes load-bearing exactly once: when a
decision rests on the backtest's numbers being right.

**Why it is deferral rather than abandonment.** `ShadowBroker` records
`fill_gap_order` per order automatically, so **a V5 soak collects the data for
free** — at 0.18–0.37 orders/night a 30-day soak yields ~10 orders. What is given
up is *speed*: the measurement config (below) would have produced a distribution
in ~1 week instead of months.

**Measured 2026-08-12 — orders/night vs. stride** (pinned config, 30 nights,
2026-03-24 → 2026-05-15, batch pinned at 256):

| stride | signals | orders | orders/night | nights for 20 |
|---:|---:|---:|---:|---:|
| **30** (pinned) | 840 | 11 | **0.37** | 55 |
| 10 | 2 520 | 33 | 1.10 | 18 |
| 3 | 8 400 | 53 | 1.77 | 11 |
| 1 | 25 200 | 113 | **3.77** | **5** |

**Sub-linear, and that is structural**: 30× the signals gives only 10× the
orders, because `max_position=1` plus no-flip bounds orders by *round trips* — a
BUY while already long is discarded however often it arrives. An earlier
first-principles estimate said 5.4/night at stride 1; the measurement says 3.77.
Note also this 30-night window runs ~2× hotter than the full history (0.37 vs
0.18 orders/night at the pinned stride), so the right-hand column is optimistic.

`orders == fills` at every stride — the harness stating its own optimism, which
is precisely the assumption V4 exists to test.

**Conditions attached to proceeding:**

1. **The resume fix is V5 work, not leftover V4 work.** The strategy holds a
   median 4.5 days, so any restart during a soak lands mid-position — a state
   that currently refuses to start (CLI preflight) and would halt reconciliation.
   V5 cannot pass without it.
2. **Soak with `--broker shioaji`, not `dryrun`.** Same run, one flag, and the
   fill data accrues instead of being lost.
3. **Gate for real money:** the gap number must be collected *before* any
   non-simulation deployment, not after. At ~211 points/trade average a 20 %
   execution drag decides whether the strategy is worth running — and the
   missed-fill component changes *which* trades occur, not just the mean.

### Phase V5 — Soak & recovery

Multi-day continuous run across DAY↔NIGHT boundaries and midnight. Test: kill
`-9` mid-session and restart (warm-up path §2.8), network drop and reconnect,
market-data staleness watchdog.

**Pass criterion:** 5 consecutive days unattended, restarts recover to correct
position and context, no stale orders left working.

### Phase INT — Integration

Only now:
- document the live path in `SPEC.md` (new §11) and `CLAUDE.md`;
- **write the day-session porting guide** — decided 2026-08-11 to land here
  rather than earlier. `live/sessions.py` made the session a parameter precisely
  so this port is possible, and `live/runtime.py` now makes a policy mismatch
  unrepresentable, so the guide is writable today. It waits because V4 still
  decides `aggression_ticks` from a measured fill rate and V5 is what actually
  exercises restart and recovery — after both, the guide can describe a procedure
  that has been run rather than one that should work. Tracked in
  [docs/DOCS_PLAN.md](docs/DOCS_PLAN.md); it is a task page next to `usage.md`,
  not a mirror-tree module page, so the docs' 1:1 rule will not flag it missing;
- add `shioaji` to `UV_SETUP.md` install steps (safe — no deps, cannot disturb
  the torch cu130 pin);
- decide whether `real_data.py` gains a `--live` flag or the live CLI stays
  separate. **Recommend separate** — `real_data.py` is a research entrypoint and
  mixing a network/broker path into it invites accidents.

---

## 5. Safety rails (implemented — `live/state.py`)

### Two halt actions, not one

Building this surfaced a decision the original plan glossed over: **flatten is
not universally the safe response.** A rail must choose between

- **`FLATTEN`** — close the position, then stop. Correct when the *position* is
  the problem and prices are still trustworthy: daily loss limit, kill file.
- **`STOP`** — send no orders at all. Correct when the *inputs* are
  untrustworthy: a frozen feed (a limit priced off a stale print is a blind
  order — including the closing one), a disputed position mirror (§2.9 — "close
  the position" is undefined when the size is in dispute), a rejection, or an
  order-rate breach (a submit storm means the order path itself is misbehaving,
  so pushing one more order down it is the wrong move).

Collapsing these into a single action would either fire blind orders into a book
we cannot see, or leave a losing position open past its limit.

Rail → action mapping as implemented:

| Rail | Action |
|---|---|
| `max_position > 1` at startup | raises — misconfiguration, never reaches a bar |
| Daily loss limit | `FLATTEN` |
| Kill file | `FLATTEN` |
| Quote staleness (900 s backstop) | `STOP` |
| Position-mirror mismatch | `STOP` |
| Order rejection (`op_code != "00"`) | `STOP` |
| Order rate breach | `STOP` |

Two further implementation notes:

- **"Daily" is anchored on `SessionPolicy.trading_day`**, whose boundary is
  08:00 — before either session opens and after the night session closes, and
  the same hour as Shioaji's own quota reset (§2.8b). A calendar-date reset would
  cut a night session in half and hand the second half a fresh loss budget. The
  rail is session-agnostic as a result, which is what lets the day-session port
  reuse it unchanged.
- **The flatten retry is gated on `has_pending()`** — at most one close per bar,
  and none while one is working. A one-shot submit would silently leave risk on
  if that single order never filled; an ungated retry would be the very storm the
  rate limiter exists to catch.

Non-negotiable, all in `live/state.py`:

- `max_position == 1` assertion at startup (§2.10).
- **Daily loss limit** — halt and flatten at a configured point loss.
- **Order rate limit** — max N `place_order` per minute; breach → halt.
- **Quote staleness watchdog** — no non-simtrade tick for N seconds during a
  session we trade → stop trading (do not keep forecasting on a frozen book).
  Now a **900 s backstop** rather than the detector: `FeedWatch` (§2.22) does the
  detecting by asking the venue, and this fires only if the healing did not work.
  Gated on `policy.includes`, which since §2.25 also means **a trading day** — a
  weekend accrues no gap, so the rail no longer halts a soak on Saturday.
- **Reconciliation halt** (§2.9).
- **Rejection halt** — any `op_code != "00"` stops the loop for human review.
- **Explicit `simulation` flag in every log line and on the console banner**, so
  a production run can never be mistaken for a sim run.
- **Kill file** — presence of `live/STOP` flattens and exits at the next bar.

Guard against the worst failure mode: a live process that keeps trading on a
stale mirror or a frozen feed. Every rail above defaults to *halt*, never
*continue*.

---

## 6. Effort estimate

| Phase | Scope | Est. |
|---|---|---|
| A | Broker seam, SimBroker, LiveLoop, bars, recorder, rails | 1–2 d |
| V0 | Parity harness + tests | 0.5–1 d |
| V1 | Aggregator parity (needs one captured session) | 0.5 d + 1 session |
| V2 | FakeShioaji + adapter | 1–1.5 d |
| V3 | Dry-run session + shadow parity report | 0.5 d + 1 session |
| V4 | Sim orders + fill-gap instrumentation | 0.5 d + 5 sessions |
| V5 | Soak | 5 sessions, mostly waiting |
| INT | Docs, wiring | 0.5 d |

Coding ≈ 5–7 days; calendar dominated by needing ~11 live sessions.

**Actual, as of 2026-08-17.** A–V4 landed roughly to estimate. **V5 did not**, and
the reason is worth keeping: the soak phase was estimated as "5 sessions, mostly
waiting", but the waiting is what *found* the work — the 23 h staleness kill
(§2.21), the `Snapshot.ts` type crash (§2.22b), the 24 h token wall (§2.24) and
the weekend halt (§2.25) were all discovered by running long, not by building.
Each cost a fix that was not in any estimate, and none was visible from the code.

The transferable lesson for the day-session port: **budget the soak as
discovery, not as waiting.**

---

## 7. Answers (settled 2026-08-10)

1. **Credentials — available.** API key/secret issued with a futures
   sub-account. V3 is unblocked on this axis.
2. **Simulation + futures — ✅ CONFIRMED WORKING (probe run 2026-08-10).**
   `place_order` → `op_code='00'`, `PendingSubmit` → `Submitted`;
   `cancel_order` → `op_code='00'`, `Cancelled`; positions flat afterwards.
   **V3/V4 are unblocked.** The probe also settled every V2 guess — see
   "§7.2 probe results" below. *(Original analysis retained for context:)* The supplied doc page does not
   settle it. It lists the sim-available order APIs generically
   (`place_order`, `update_order`, `cancel_order`, `update_status`,
   `list_trades`) with **no product qualification**, and the page contains no
   occurrence of 期貨, 選擇權 or TXF at all. Its only stated exclusion is
   「模擬環境的下單不支援興櫃與零股交易」 — **興櫃 is the emerging-stock (pre-IPO)
   board, not OTC/上櫃**; that and 零股 (odd lots) are both equity-specific, which
   is weak positive evidence for futures but not a confirmation.
   **Resolution: empirical — probe WRITTEN, verdict PENDING.**
   `live/v72_probe.py` (6 safety tests). Sim login → resolve `TXFR1` → snapshot
   for a reference price → `place_order` **1 lot, 500 pts below the market** →
   `update_status` → `cancel_order` → verify flat.

   ```powershell
   uv run python -m forecast_eval.live.v72_probe --dry-run   # places nothing
   uv run python -m forecast_eval.live.v72_probe             # places + cancels
   ```

   Safety is structural, not procedural: `simulation=True` is *asserted* (it
   aborts if `api.simulation` is False), quantity is fixed at 1, the limit is
   **passive by construction** (a BUY strictly below the touch cannot fill on
   submission — `probe_limit_price` floors rather than rounds so the tick snap
   can only move it further away), it cancels in a `finally` with a second
   attempt on any failure path, and it prints the closing position.

   The 500-point default is bounded on both sides: far enough that it will not
   fill, near enough to stay inside TAIFEX's ±10 % daily band — an order outside
   that band is rejected, which would masquerade as "futures unsupported in
   simulation". Both bounds are tested.

   **Must be run during a trading session.** Outside one, a rejection is
   ambiguous between "unsupported" and "market closed", and those imply very
   different plans. The probe prints `op_msg` verbatim so the two can be told
   apart.

   #### §7.2 probe results (2026-08-10, TXFR1 @ 44 949, BUY limit 44 449)

   **Verdict: futures ordering works in simulation.** New and Cancel both
   returned `op_code='00'`; status went `PendingSubmit` → `Submitted` →
   `Cancelled`; `list_positions` was empty at the end.

   **Enum literals — V2's dispatch guess was right.** `OrderState.FuturesOrder`
   has `name='FuturesOrder'`, `value='FORDER'`; the SDK also defines
   `FuturesDeal='FDEAL'`. Verified offline that `FuturesOCType` really does
   expose `New`, `Cover`, `Auto`, `DayTrade` — so the `OPEN→New` / `CLOSE→Cover`
   mapping (§2.3) is correct as written. *Note the wire form differs from the
   SDK form:* the callback reports `oc_type='NewPosition'` while the enum we send
   is `New`. Harmless — nothing parses `oc_type` — but do not assume they match.

   **Dispatch hardened.** Values are `FORDER`/`FDEAL`, which contain no
   mixed-case `Order`/`Deal`; and `str(state)` for a deal is
   `'OrderState.FuturesDeal'`, which contains **ORDER as well**. Matching is now
   upper-cased and tests DEAL first — testing ORDER first would route every fill
   into the order handler and silently lose it.

   **BUG FOUND AND FIXED — alias vs. resolved contract code.** Ordering through
   `TXFR1` produced a `Trade`, callbacks and (by implication) positions stamped
   with the **resolved** code `TXFH6`, while the alias object reports
   `code='TXFR1'`. `broker_position()` filtered on the alias code, so it would
   have excluded our own position, returned 0 against a mirror of 1, and **halted
   the loop via the reconciliation rail the moment anything was held** — on the
   very first live trade. Now matched against a *set* of codes seeded from
   `code`/`target_code` and widened from what the venue reports. Three regression
   tests; verified by restoring the old behaviour and watching them fail.

   **Safety gap to record: `api.simulation` is `None`.** The SDK does not expose
   the flag, so simulation mode **cannot be verified programmatically** — the
   constructor argument is the only guarantee. The probe's abort-if-False check
   therefore cannot fire. Consequence for §5: the "simulation" banner and every
   log line must come from *our own* config, never from asking the SDK.

   > **RESOLVED 2026-08-11 — from the other side, and better than expected.**
   > `live/prod_audit.py` attempted `simulation=False` with the same credential
   > and the venue refused it:
   >
   > ```
   > BadRequestError: StatusCode: 400, Detail: Token doesn't have production permission.
   > ```
   >
   > **The API key is scoped to simulation server-side.** No order placed with it
   > can reach a real account regardless of what the client passes — which is a
   > stronger guarantee than the constructor argument was ever going to be, and
   > it holds for *every* session rather than for one tested window. The gap
   > above is closed, though not in the way §7.2 expected: not by verifying the
   > flag, but by making the flag irrelevant.
   >
   > **The guarantee now lives in the credential, not in the code**, and that is
   > where the remaining risk sits: issuing a production-capable key into the
   > same `SHIOAJI_API_KEY` would silently remove it, with no code change and
   > nothing in the suite to catch it. Re-run `prod_audit` whenever the key is
   > reissued or its permissions change.
3. **`aggression_ticks` — option (c).** Leave the decision until V4 measures the
   real fill rate. V4 runs at `aggression_ticks=0` purely as instrumentation;
   the choice between keeping 0 and moving to ≥1 is made from that number.
4. **Contract — `TXFR1`.** Settles §2.6 as a *decision*, so V1b is no longer
   blocking: there is nothing to choose between. Still log the resolved `code`
   and `delivery_month` on every bar and order, and assert contract consistency
   before any CLOSE — rollover is where a continuous alias and an open position
   can disagree.
5. **Restart warm-up — DECIDED: refetch the full 3 008-bar context on every new
   process start.** No incremental/persisted context cache, no resume-from-disk.
   Startup is one `kbars` backfill plus one batched GPU pass to prime the emitter
   deques (§2.8). Simple, always correct, and quota-cheap (§2.8b).

   Two consequences that follow from this and are now load-bearing:
   - Every restart hits `kbars`, so the **restart-backoff and per-day backfill
     cap rails in §2.8b are required, not optional** — an unsupervised crash loop
     is the one path to a suspended account.
   - Every restart depends on `kbars` being bar-for-bar correct, so **V1c gates
     V3**. A silent convention mismatch there poisons the context on every start.
6. **Same 5090 box.** Consequence to plan for: **VRAM contention.** A research
   backtest with `--precompute --batch-size auto` probes *free* VRAM and sizes
   its batch to fill it, which can starve or OOM a resident live process (and
   vice versa). Mitigations, in order of preference: (a) do not run research
   backtests during live sessions; (b) pin an explicit `--batch-size` for
   research instead of `auto`; (c) cap the research process with
   `CUDA_VISIBLE_DEVICES` / a memory fraction. Add a startup check that logs free
   VRAM and refuses to start live if another process already holds most of it.

---

## Sources

- [Shioaji 總覽](https://sinotrade.github.io/zh/)
- [Simulation Mode](https://sinotrade.github.io/zh/tutor/simulation/)
- [期貨/選擇權下單](https://sinotrade.github.io/zh/tutor/order/FutureOption/)
- [期貨即時行情](https://sinotrade.github.io/zh/tutor/market_data/streaming/futures/)
- [Order & Deal Event — Futures](https://sinotrade.github.io/tutor/order/order_deal_event/futures/)
- [登入](https://sinotrade.github.io/zh/tutor/login/)
- [shioaji on PyPI](https://pypi.org/project/shioaji/)
