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
| `OrderEvent` | dataclass | a limit order: side, price, OPEN/CLOSE intent |
| `FillEvent` | dataclass | a realized execution: price, quantity, fee |

## Key points
- `Forecast.payload` is typed `Any` on purpose — the `SignalEmitter` API stays
  model-agnostic; only concrete emitters know the payload keys
  (e.g. `predicted_return`).
- `OrderEvent.intent` (OPEN vs CLOSE) lets logging/accounting distinguish entries
  from exits without re-deriving from position state.
- `FillEvent.fee` is stored *on the fill* (not recomputed downstream) so PnL
  booking is exact.

## Related
[execution.py](execution.md) · [trader.py](trader.md) · [portfolio.py](portfolio.md) · [SPEC §3](../SPEC.md)
