# Confidence-Aware Order Placement — Concept

Implements ideas **#1, #2, #7** from [ORDER_PLACEMENT_IDEAS.md](ORDER_PLACEMENT_IDEAS.md).
This document explains the *concept* behind the edit, not the line-by-line diff.

## The core problem

Before this edit, the only thing that survived the journey from a Toto2 forecast
to an actual order was a **3-way direction** (BUY / SELL / HOLD). Everything else
the model knew — how big the predicted move is, and *how sure* it is — was thrown
away at the emitter. Toto2 produces a full predictive distribution (9 quantiles);
the harness used only the median.

The whole edit is about putting the **confidence** back into the pipeline and
letting it shape *how* we trade, not just *which way*.

```
BEFORE:  Forecast(median) ──► direction ──► fixed-offset, 1-contract limit order
AFTER:   Forecast(quantile fan) ──► direction + strength ──► offset & size scale
                                                              with conviction
```

## The unifying concept: `strength`

A single scalar, **`strength ∈ [0, 1]`**, carries the forecast's conviction past
the direction bottleneck. It is the dial every feature turns on.

- It is derived from **directional probability** — the probability, read off the
  quantile distribution, that price moves the way the signal says. For a BUY that
  is `P(up)`; for a SELL it is `P(down) = 1 − P(up)`.
- `strength = 0` → the signal barely cleared the entry threshold; trade minimally.
- `strength = 1` → the model is as sure as its deciles allow; trade fully.

`strength` is transported in a new `Signal(direction, strength)` object. Emitters
that have no distribution (the dummy/naive paths) report `strength = 1.0`, so they
behave exactly as before.

## Where the probability comes from (#2)

Toto2 outputs nine quantiles at levels `[0.1, 0.2, …, 0.9]` — i.e. the inverse CDF
of where price will be at the horizon. Those nine points let us answer
"what is `P(next price > current price)`?" by reading the **CDF at the current
price** and taking `1 − F`. That number is `prob_up`.

`prob_up` (plus the raw quantile prices/returns and an 80% band width) is now
surfaced in the forecast payload. This is the raw material; everything downstream
is a policy choice on top of it.

The emitter can also **gate** on it: if directional probability is below a
configured floor (`min_prob`), the trade is suppressed to HOLD even though the
point forecast cleared the threshold. This is the "don't trade into uncertainty"
filter.

## The three policies that consume it

### #2 — Probability gate (emitter)
A minimum-conviction filter. Direction still comes from the return thresholds, but
a trade only fires if the distribution agrees strongly enough. Reduces trades in
noisy regimes.

### #1 — Confidence-scaled aggression (trader)
The limit-order **price offset** ramps from a base (`aggression_ticks`) to an
aggressive ceiling (`max_aggression_ticks`) as `strength` rises. Strong signals
cross more ticks to all but guarantee a fill; weak ones rest near the touch to
save cost. Closes are unaffected — they always use base aggression so risk-reducing
orders fill reliably.

### #7 — Confidence-scaled sizing (trader)
The **number of contracts** on an entry scales with `strength`, up to
`max_position`, with a floor of 1. Size with conviction. Closes always flatten the
*entire* position so the no-flip state machine returns cleanly to flat.

## How they compose

The order of operations is natural and the features stack without interfering:

```
prob_up ──► [#2 gate?] ──► strength ──► [#1 offset] + [#7 size] ──► order
            drop weak       conviction    how aggressively   how many
            trades          in [0,1]       to price it        contracts
```

The gate runs first, so any trade that survives already carries enough conviction
that its offset/size are never at the absolute minimum.

## Design principle: every feature is OFF by default

The defaults are chosen so the new code is **inert** unless explicitly enabled:

| Knob | Default | Effect of default |
|---|---|---|
| `max_position` | `1` | size is always 1 (sizing disabled) |
| `max_aggression_ticks` | `None` | offset is always the base (scaling disabled) |
| `min_prob` | `None` | no probability gate |
| emitter `strength` | `1.0` for non-distributional forecasters | inert |
| `OrderEvent.quantity` | `1` | every legacy order unchanged |

Consequence: **with defaults, every existing run and test is byte-identical.** The
features are opt-in via CLI flags / constructor args. Confidence only varies for a
forecaster that emits a distribution (Toto2); with naive/dummy models `strength`
stays a constant 1.0.

## Boundary it deliberately does NOT cross

`signal_attribution` (the frictionless "model edge" metric) stays **per-unit**: it
replays direction/price at one contract and ignores sizing. This is intentional —
it isolates the model's *directional* edge from *position-management* decisions. The
trade-off: when sizing is enabled, the gap between attribution PnL and realized PnL
is no longer a like-for-like contract count, so it can't be read as pure execution
cost.

## Touched components

| Concern | File |
|---|---|
| `Signal` object, `OrderEvent.quantity` | [events.py](events.py) |
| Quantile fan, `prob_up`, band in payload | [forecaster/toto2.py](forecaster/toto2.py) |
| `emit_signal` default | [strategy/base.py](strategy/base.py) |
| `strength`, `min_prob` gate | [strategy/threshold.py](strategy/threshold.py) |
| Aggression ramp (#1), sizing (#7), full-close | [trader.py](trader.py) |
| Fill `order.quantity`, fee × quantity | [execution.py](execution.py) |
| Thread `emit_signal`/`strength`, wire `max_aggression_ticks` | [run.py](run.py) |
| CLI knobs | [real_data.py](real_data.py) |
| Tests (no GPU) | [tests/test_confidence_placement.py](tests/test_confidence_placement.py) |
