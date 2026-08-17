# [live/broker/shioaji.py](../../../live/broker/shioaji.py)

**Role:** `Broker` adapter over Sinopac's Shioaji futures API.
**Pipeline:** LiveLoop → **ShioajiBroker** → `api.place_order` / order callbacks.
**Depends on:** [live/broker/base.py](base.md), [events.py](../../events.md), [live/state.py](../state.md), [live/contracts.py](../contracts.md)   **Used by:** the live entry point (V4)

## Responsibility
Translates `OrderEvent`s into Shioaji futures orders, and turns the venue's
asynchronous order/deal callbacks back into `FillEvent`s. Where
[SimBroker](sim.md) *infers* fills from prints, this one *receives* them — and
everything hard about it comes from that inversion.

`shioaji` is imported **lazily** and the `sj` namespace is injectable, so this
module and its tests run with the package absent.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `ShioajiBroker(api, contract, *, account, fee_rate, sj, rails, recorder, on_anomaly)` | class | registers its order callback at construction |
| `submit` / `cancel` / `has_pending` / `pending_order` / `poll_fills` | method | the `Broker` interface |
| `broker_position()` | method | signed position for this contract, `None` on query failure |
| `broker_position_detail()` | method | the same, plus the size-weighted entry price (§2.20 resume) |
| `anomalies` | attribute | every message it could not account for |

## Order translation
`OPEN → FuturesOCType.New`, `CLOSE → FuturesOCType.Cover` — **explicitly, never
`Auto`.** The no-flip invariant is built on meaning what we said; `Auto` would
mask a position-mirror desync instead of surfacing it (SHIOAJI_PLAN §2.3).
Orders are `LMT` + `ROD`, matching the backtest's resting-limit semantics; SPEC
§4.4's cancel-on-every-signal is what bounds their life.

## The four hard cases
- **Deals can arrive before the order event** (the docs say so). Correlation
  therefore cannot rely on knowing `ordno` first — deals carry their own `ordno`,
  and `trade_id` ↔ the `id` returned by `place_order` is the fallback.
- **Duplicate deals.** Dedup on `(ordno, exchange_seq)`; one venue deal produces
  exactly one `FillEvent` however often it is reported.
- **Cancellation is asynchronous.** `cancel()` moves the order to a `_retiring`
  list and frees the book *immediately*, because §4.4 has the Trader cancel and
  re-submit on the same signal — waiting for confirmation would stall every
  decision. The retired order stays tracked because it can still fill in the race
  window, and **that fill is real money**: discarding it would leave us holding
  contracts we do not know about.
- **Callbacks run on Shioaji's thread.** All mutable state is under a lock and
  fills are queued; nothing slow ever runs in a callback.

## Key invariants / gotchas
- **Three independent guards on every deal**, none sufficient alone: dedup key,
  correlation, and a running `filled` bound against the order's own quantity —
  the backstop that stops a stray report inventing contracts.
- **Contract codes are matched as a *set*.** Ordering through the `TXFR1` alias
  produces callbacks and positions stamped with the **resolved** code (`TXFH6`),
  while the alias object reports `code='TXFR1'`. Matching on the alias alone
  filtered out our own position, returned 0 against a mirror of 1, and would have
  halted the loop via the reconciliation rail on the first live trade. The set is
  seeded from [contracts.py](../contracts.md)'s `contract_codes` (`code` +
  `target_code`) and widened from what the venue reports. **Seeded from there, not
  inlined**, because the rule was independently rewritten — and got wrong — in
  `v1_check` thirty lines away from this fix (§2.18b); one function means the two
  cannot drift.
- **Dispatch upper-cases and tests DEAL before ORDER.** Verified literals:
  `FuturesOrder='FORDER'`, `FuturesDeal='FDEAL'` — neither contains a mixed-case
  `Order`/`Deal`, and `str(state)` for a deal is `'OrderState.FuturesDeal'`,
  which contains ORDER too. Testing ORDER first would route every fill into the
  order handler and silently lose it.
- **Anything unexplained is an anomaly that halts**, not a no-op: an unknown
  callback state, an unattributable deal, an overfill. Silently dropping a
  message here means silently losing a position.
- **`broker_position()` returns `None` on query failure**, so a transient error
  means "cannot compare" rather than a fabricated mismatch that halts a healthy
  loop.
- **Both position accessors share one parse** (`_held_lots`). The code filter
  especially must not be duplicated — it is what stops the `TXFR1` alias
  reporting flat while holding (§2.18b). `price` is the average entry, a field
  verified by introspecting `sj.FuturePosition` (2026-08-12) rather than read
  from the docs, which have been wrong four times.
- **Resume depends on `target_code`.** At start-up `_codes` holds only what
  [contract_codes](../contracts.md) returns, because no order callback has taught
  it the resolved month yet. `sj.Future` exposes `target_code`, so the alias
  covers `TXFH6` from the first query — without it, adoption would read flat
  while the account was holding, and the loop would open a second lot.

## Related
[live/broker/base.py](base.md) · [live/state.py](../state.md) · [tests/test_shioaji_adapter.py](../../tests.md) · [live/v72_probe.py](../v72_probe.md) · [SHIOAJI_PLAN.md](../../../SHIOAJI_PLAN.md)
