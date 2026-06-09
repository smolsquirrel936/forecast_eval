# Order-Placement Improvement Ideas

Possibilities for improving the order-placing logic driven by
[real_data.py](real_data.py). Grounded in the current code paths.

## Current pipeline

```
Forecast
  -> ThresholdEmitter.emit          (direction only: BUY / SELL / HOLD)
  -> Trader.on_signal               (limit = price ± aggression_ticks·tick_size,
                                      qty always 1, no-flip state machine)
  -> Execution.submit               (marketable / passive fill model)
```

**Key observation:** the only thing that survives from the forecast to the order
is a 3-way direction. Magnitude, uncertainty, the whole Toto2 predictive
distribution, volatility, and spread are all discarded before an order is built.
Most of the improvements below are about putting that information back in.

Relevant files:
- [strategy/threshold.py](strategy/threshold.py) — `ThresholdEmitter`
- [trader.py](trader.py) — `Trader.on_signal` / `submit_exit` (limit-price + state machine)
- [execution.py](execution.py) — fill model
- [forecaster/toto2.py](forecaster/toto2.py) — what lands in `Forecast.payload`

---

## A. Use more of the forecast (emitter / signal layer)

1. **Confidence/magnitude-scaled aggression** — instead of a fixed
   `aggression_ticks`, make the offset a function of `predicted_return` size.
   Strong signal → cross more ticks (fill certainty); weak-but-passing → rest
   passively to save cost. Touches `strategy/threshold.py` + `trader.py:on_signal`.
2. **Use Toto2's distribution, not just the mean.** Toto2 emits quantiles; right
   now only the point return reaches the emitter. Gate trades on *probability*
   (e.g. P(return > 0) or quantile spread) rather than a single threshold — fewer
   trades into uncertain regimes. Needs the wrapper to surface quantiles in
   `Forecast.payload`.
3. **Quantile-anchored limit prices.** Place a BUY at the predicted lower quantile
   (buy the dip the model expects) instead of at last-price ± fixed offset —
   turns passive resting into an edge rather than a coin flip on whether it fills.

## B. Limit-price construction (trader layer)

4. **Volatility-scaled offset.** `aggression_ticks` in absolute ticks is brittle
   across regimes; scale the offset by recent realized vol (rolling std of
   returns) so "aggression" means the same thing in a calm tape vs. a fast one.
5. **Marketable vs. passive as a deliberate choice.** Today positive/zero
   aggression is always marketable. Expose a passive-entry mode (negative offset)
   to earn the spread, accepting non-fills — directly trades fill-rate for cost.
   The fill model already supports it (`execution.py` passive branch).
6. **Spread/tick-rounding awareness.** Limits aren't snapped to the tick grid and
   there's no spread proxy. Round limits to `tick_size` and optionally widen by an
   estimated spread so marketable classification is honest.

## C. Position sizing (currently hard-wired to 1)

7. **Confidence-weighted size** — scale contracts by signal strength / Kelly-lite,
   up to `max_position` (which already exists but is unused beyond 1).
8. **Volatility targeting** — size inversely to recent vol for roughly constant
   risk per trade. Requires the partial-fill / average-entry branch in
   `trader.py` (currently stubbed out) to be implemented.

## D. Order lifecycle / re-pricing

9. **Re-quote ("chase") unfilled orders intra-stride.** A passive limit currently
   sits at a stale price until the next stride boundary (could be 30 bars). Add a
   per-tick reprice toward the market, or convert to marketable after N ticks.
10. **Explicit order TTL** decoupled from stride — cancel/replace after K ticks
    rather than only on the next signal. Right now lifetime == stride, which
    couples two unrelated knobs.
11. **Fill-or-kill / immediate-or-cancel semantics** for entries you only want if
    they fill promptly.

## E. State machine (no-flip / pyramiding)

12. **Optional flip-on-opposite-signal.** Today a SELL while long only closes; you
    sit flat until the next stride to re-enter short, missing the move. A guarded
    close-then-reverse would capture it (at the cost of the PnL-attribution
    cleanliness SPEC §4.2 deliberately avoided — make it a flag).
13. **Pyramiding / scaling in** on confirming signals up to `max_position`.

## F. Execution realism (makes any of the above honest)

14. **Slippage / price-protection cap on marketable fills.** `execution.py` fills
    at the next print's price *however far it gapped* — a forced-close across a
    session gap can fill arbitrarily badly. Add a max adverse-move cap (reject or
    re-rest beyond X ticks).
15. **Latency model** — currently fill is exactly "next print." A configurable
    N-tick delay would stress-test whether the edge survives realistic latency.

---

## Recommended starting point

Highest signal-to-effort: **#2 + #1 + #7** (surface Toto2 quantiles →
confidence-scaled aggression and sizing). That's where the discarded information
actually lives, and it's a contained change across `toto2.py`, `threshold.py`,
and `trader.py`.

**#14** is a cheap, important realism fix worth doing regardless.

Before implementing any of these, check what Toto2's `forecast()` currently puts
in `payload` — several of these depend on it.
