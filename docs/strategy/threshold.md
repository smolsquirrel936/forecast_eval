# [strategy/threshold.py](../../strategy/threshold.py)

**Role:** Threshold emitter — turns `predicted_return` into BUY/SELL/HOLD (SPEC §5).
**Pipeline:** a concrete [SignalEmitter](base.md).
**Depends on:** [events.py](../events.md), [strategy/base.py](base.md)   **Used by:** [run.py](../run.md), all Toto2 entry points

## Responsibility
Reads `payload["predicted_return"]` from the forecast and emits a direction when it
clears a configured band. The dead zone between the two thresholds filters weak/noisy
signals — the central tunable of the strategy.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `ThresholdEmitter(buy_threshold=0.001, sell_threshold=-0.001)` | class | the banded emitter |
| `emit(forecast)` | method | BUY above the upper band, SELL below the lower, else HOLD |

## Key points / gotchas
- Construction **rejects** `sell_threshold >= buy_threshold` — overlapping bands
  would make a return qualify as both BUY and SELL.
- Returns `HOLD` when the forecast is `None` or has no usable `predicted_return`
  (e.g. during warm-up) — never a blind trade.
- This emitter is **stateless** (unlike the dummy), so it's safe to reuse — though
  the *forecaster* paired with it usually isn't.

## Related
[strategy/base.py](base.md) · [forecaster/toto2.py](../forecaster/toto2.md) · [trader.py](../trader.md) · [SPEC §5](../../SPEC.md)
