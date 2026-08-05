# [strategy/volmultitf.py](../../strategy/volmultitf.py)

**Role:** Volatility + multi-timeframe breakout emitter — the autoresearch exp227
winner. Only enters when return band, directional probability, short-window trend,
volatility, breakout extreme, and long-window trend all agree.
**Pipeline:** a concrete [SignalEmitter](base.md).
**Depends on:** [events.py](../events.md), [strategy/base.py](base.md)   **Used by:** not yet wired into `real_data.py` (construct `VolMultiTfEmitter` and pass it as the emitter, or add `--emitter {volmultitf,threshold}`)

## Responsibility
A six-filter cascade that neutralizes the counter-trend PnL leak a bare
return-threshold emitter suffers. Reads the same Toto2 payload fields
(`predicted_return`, `last_price`, `prob_up`) as [threshold.py](threshold.md), but
carries its own price memory in two rolling deques and derives every filter itself
(no SMA regime gate on a forecaster-computed field).

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `VolMultiTfEmitter(buy_threshold=0.0010, sell_threshold=-0.0010, min_prob=0.614, short_window=3, long_window=5, min_trend_ticks=2.5, breakout_buy=0.65, breakout_sell=0.35, min_vol=13.0)` | class | the six-filter cascade emitter |
| `emit(forecast)` | method | direction-only view (BUY/SELL/HOLD) for attribution |
| `emit_signal(forecast)` | method | `Signal(direction, strength)` — full decision |
| `_decide(forecast)` | method | shared decision core → `(direction, strength)`; both public methods route through it so they can never disagree |

## Key points / gotchas
- Construction **rejects** `sell_threshold >= buy_threshold` — same invariant as
  `ThresholdEmitter`. The exp227 defaults are baked in, so constructing with no args
  reproduces the winning strategy exactly.
- **Stateful:** two `deque`s (`_short_prices`, `_long_prices`, each `window + 1` slots
  so a window-length trend has an early and a late half). One instance per backtest
  run; like all stateful emitters it **must not be reused** across
  `fee_sensitivity_sweep` iterations — pass a factory, not an instance.
- Every filter after the base return-band can only turn a directional call into HOLD,
  never flip it — same one-way gate design as `threshold.py`.
- `min_prob` (ORDER_PLACEMENT_IDEAS #2) is a directional-probability gate: `prob_up`
  for BUY, `1 − prob_up` for SELL; inert when `prob_up` is absent or `min_prob is None`.
- Short-window filters need `>= 3` samples before they engage; before warm-up the
  emitter may emit on the base band alone.
- A signal that survives every gate is emitted at `strength = 1.0` (the winner ran
  `max_position = 1`) — the cascade is a hard gate, not a graded score.
- Returns `HOLD` when the forecast is `None` or has no usable `predicted_return`.

## Related
[strategy/base.py](base.md) · [strategy/threshold.py](threshold.md) · [forecaster/toto2.py](../forecaster/toto2.md) · [tests/test_volmultitf.py](../tests.md)