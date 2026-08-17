# [live/broker/sim.py](../../../live/broker/sim.py)

**Role:** The existing print-only fill simulator, behind the `Broker` API.
**Pipeline:** LiveLoop → **SimBroker** → [execution.py](../../execution.md).
**Depends on:** [live/broker/base.py](base.md), [execution.py](../../execution.md)   **Used by:** [tests/test_live_parity.py](../../tests.md)

## Responsibility
Lets the live loop be driven over historical ticks so its output can be diffed
against [run.py](../../run.md)'s `run_backtest` — the V0 parity gate. It adds
**no fill logic of its own**: every decision still comes from `Execution`,
untouched.

> If a parity test fails, the bug is in the loop restructuring — never in here.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `SimBroker(execution)` | class | wraps an `Execution` instance |
| `submit` / `cancel` / `has_pending` / `pending_order` | method | straight pass-through |
| `poll_fills(market)` | method | `check_fill(market)` boxed into a 0- or 1-element list |
| `broker_position()` | inherited | returns `None` — no venue to ask |

## Key points
- **Composition, not subclassing.** `Execution` predates the `Broker` interface
  and must keep working standalone inside `run_backtest`. Injecting the instance
  (rather than constructing one from a `fee_rate`) lets a parity test hand both
  paths an identically configured simulator.
- **`poll_fills` can never return two fills.** The print-only model holds at most
  one pending order and fills it whole, so boxing the single `Optional` into a
  list is the entire adaptation to the multi-fill signature — which keeps the
  loop's fill handling identical to `run_backtest`'s single-fill block.
- **`broker_position()` is `None`, not `0`.** The offline sim has nothing to
  compare against; `None` makes the reconciliation rail skip it rather than
  assert a false match.

## Related
[live/broker/base.py](base.md) · [execution.py](../../execution.md) · [live/loop.py](../loop.md) · [tests/test_live_broker_sim.py](../../tests.md)
