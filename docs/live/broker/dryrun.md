# [live/broker/dryrun.py](../../../live/broker/dryrun.py)

**Role:** Decides and books exactly as live would, and sends nothing.
**Pipeline:** LiveLoop → **DryRunBroker** → [execution.py](../../execution.md) (fills) + [recorder.py](../recorder.md) (intent).
**Depends on:** [live/broker/base.py](base.md), [execution.py](../../execution.md)   **Used by:** [live/cli.py](../cli.md) (the default broker), [live/runtime.py](../runtime.md)

## Responsibility
The V3 dry run needs a session of *real decisions* against a *real feed* with no
order reaching the venue.

The naive way to get that — accept orders and never fill them — is useless: the
Trader would never open a position, the no-flip state machine would never
advance, and every signal after the first would be evaluated against a flat book
the live system would not have had. So this broker **keeps the offline fill
model** (the same `Execution` [SimBroker](sim.md) uses) and merely *records* what
it would have sent. Positions evolve, exits fire, the audit trail is complete,
and `place_order` is never called.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `DryRunBroker(execution, *, recorder=None)` | class | same composition as [SimBroker](sim.md), plus an intent log |
| `submit(order, *, current_price)` | method | append to `intended`, record `dryrun_order`, then fill offline |
| `poll_fills(market)` | method | `check_fill`, recorded as `dryrun_fill` with `simulated=True` |
| `cancel` / `has_pending` / `pending_order` | method | straight pass-through |
| `broker_position()` | method | `None` — no venue to ask |
| `intended` | attribute | the list of orders that would have been sent |

## Key invariants / gotchas
- **The recorded fields are exactly what `ShioajiBroker.submit` would translate** —
  side, limit, quantity, intent, and the price at placement. That is what lets a
  dry-run log be diffed against a later live log *order for order*.
- **Fills are labelled `simulated=True` in the recorder.** No downstream analysis
  can mistake them for venue fills, which matters once V4 logs both side by side.
- **`broker_position()` returns `None`, not `0`.** `None` makes the reconciliation
  rail skip — correct, because there is no real position to reconcile against and
  fabricating one would test nothing.
- **Shadow parity follows from the shared fill model.** Replaying a dry run's bars
  offline through [run_backtest](../../run.md) should reproduce it exactly, because
  both sides use the same `Execution`. Any divergence is therefore attributable to
  the *live path* — bar construction, timing, dropped ticks — and not to execution.

## Related
[live/broker/base.py](base.md) · [live/broker/sim.py](sim.md) · [live/broker/shioaji.py](shioaji.md) · [live/recorder.py](../recorder.md) · [live/cli.py](../cli.md)
