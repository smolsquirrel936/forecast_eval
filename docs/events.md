# [events.py](../events.py)

**Role:** The event dataclasses every layer passes around (SPEC §3).
**Pipeline:** the shared vocabulary — not a stage itself.
**Used by:** essentially every module.

## Responsibility
Defines the immutable data contracts that flow through the system, plus the
string-literal type aliases (`Session`, `Direction`, `Side`, `Intent`) that
constrain their fields. It owns *only* the shapes — no behavior.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `MarketEvent` | dataclass | one trade print: timestamp, price, volume, session |
| `Forecast` | dataclass | a model output: timestamp, horizon, opaque `payload` |
| `SignalEvent` | dataclass | a BUY/SELL/HOLD decision at a time |
| `Signal` | dataclass | an emitter's decision: direction + `strength ∈ [0,1]` conviction (defaults 1.0) |
| `OrderEvent` | dataclass | a limit order: side, price, OPEN/CLOSE intent, `quantity` (defaults 1) |
| `FillEvent` | dataclass | a realized execution: price, quantity, fee |

## Key points
- `Forecast.payload` is typed `Any` on purpose — the `SignalEmitter` API stays
  model-agnostic; only concrete emitters know the payload keys
  (e.g. `predicted_return`, `prob_up`).
- `Signal` carries the forecast's *confidence* (`strength`) past the
  BUY/SELL/HOLD bottleneck so the Trader can scale aggression and size with
  conviction; non-distributional emitters report `strength = 1.0`, so legacy
  paths are unchanged.
- `OrderEvent.intent` (OPEN vs CLOSE) lets logging/accounting distinguish entries
  from exits without re-deriving from position state. `OrderEvent.quantity`
  defaults to 1 so every legacy order is byte-identical; confidence-scaled sizing
  sets it up to `max_position`.
- `FillEvent.fee` is stored *on the fill* (not recomputed downstream) and scales
  with `quantity` (`fill_price × fee_rate × quantity`) so PnL booking is exact.

## Related
[execution.py](execution.md) · [trader.py](trader.md) · [portfolio.py](portfolio.md) · [SPEC §3](../SPEC.md)
