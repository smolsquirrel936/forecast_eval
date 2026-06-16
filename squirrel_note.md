Result of autoresearch

### New Best Result: Calmar 0.591

**Key Discovery:** Adding `min_prob=0.61` (probability gating) was the biggest improvement:
- Filters low-confidence forecasts from Toto2
- Reduces trades from 401 to 363 (-43%)
- Increases net PnL from +561 to +1,341 points
- Improves Calmar from 0.217 to **0.591** (+172% improvement)

### Final Configuration
```python
ThresholdEmitter(
    buy_threshold=0.0010,
    sell_threshold=-0.0010,
    min_prob=0.61,  # NEW: probability gating
)
FixedStopLoss(stop_loss_ticks=35)
```

### Files Updated
- `forecast_eval/experiment.py` - Final strategy
- `forecast_eval/results.tsv` - Experiment log (7 entries)
- `autoresearch_report.md` - Full report


This folder (D:\Users\squirrel\Documents\paper\myPaper\automatic\test-autoresearch) have some the result of
  autoresearch, most related files are forecast_eval/expriment.py

————————————————————————————————————————————————————————————————————————————————————————————————————————————————————————————

Look at what forecast() actually returns (toto2.py:251–266): a Forecast with a payload dict — predicted_return, prob_up,
quantile_prices, etc. That's just the model's view of the future. No order, no signal.

The decision-making happens in separate files, each with one responsibility:

┌───────────────────────────────────────────┬───────────────────────┬───────────────────────────────┐
│                   Stage                   │         File          │            Output             │
├───────────────────────────────────────────┼───────────────────────┼───────────────────────────────┤
│ Predict                                   │ forecaster/toto2.py   │ Forecast (numbers only)       │
├───────────────────────────────────────────┼───────────────────────┼───────────────────────────────┤
│ Decide (forecast → BUY/SELL/HOLD)         │ strategy/threshold.py │ Signal (direction + strength) │
├───────────────────────────────────────────┼───────────────────────┼───────────────────────────────┤
│ Place order (signal → limit order)        │ trader.py             │ OrderEvent                    │
├───────────────────────────────────────────┼───────────────────────┼───────────────────────────────┤
│ Orchestrate (calls all three in sequence) │ run.py:283–350        │ wires them together           │
└───────────────────────────────────────────┴───────────────────────┴───────────────────────────────┘

The chain in run.py:
forecast = forecaster.forecast(hist_df)          # toto2.py     → Forecast
sig      = emitter.emit_signal(forecast)          # threshold.py → Signal
order    = trader.on_signal(sig.direction, ...)   # trader.py    → OrderEvent

So the accurate statement is:

▎ forecaster/toto2.py is the core file that produces the forecast. The forecast is then converted to a signal by
▎ strategy/threshold.py and turned into an order by trader.py.


——————————————————————————————————————————————————————————————————————————————————————————————————————————————————————————————————

strategy/threshold.py — yes, exactly right. That's where the rule "forecast → BUY/SELL/HOLD" lives. Edit it to change
how/when a signal is generated from a forecast (e.g. add a new gate, change the strength formula, use band_return_80
instead of prob_up).

forecaster/toto2.py — depends on what you mean by "parameters":

- If you want to change parameter values (checkpoint, horizon_bars, stride_bars, context_length, signal_step, bar_freq,
device) — you usually don't edit toto2.py. Those are constructor arguments, and the real run sets them from CLI flags in
real_data.py (real_data.py:200–209). So you'd run:
python -m forecast_eval.real_data --checkpoint Datadog/Toto-2.0-1B --horizon-bars 60 --context-length 3008
- Same for the emitter's thresholds — --buy-threshold, --sell-threshold, --min-prob, --full-confidence-prob
(real_data.py:210–215). You don't edit threshold.py to change those numbers either.
- If you want to change model behavior/logic — how the context is built, switching univariate→multivariate OHLC, how the
quantile fan is reduced into predicted_return/prob_up — then you edit toto2.py (the _prepare_context, forecast,
_make_forecast, _prob_up methods).

So the clean rule of thumb:

┌───────────────────────────────────────────────────────────────────┬──────────────────────────────────────────┐
│                               Goal                                │                  Where                   │
├───────────────────────────────────────────────────────────────────┼──────────────────────────────────────────┤
│ Change a knob's value (checkpoint, horizon, thresholds, min_prob) │ CLI flags in real_data.py — no code edit │
├───────────────────────────────────────────────────────────────────┼──────────────────────────────────────────┤
│ Change how the model computes a forecast                          │ forecaster/toto2.py                      │
├───────────────────────────────────────────────────────────────────┼──────────────────────────────────────────┤
│ Change how a forecast becomes a signal (the decision rule)        │ strategy/threshold.py                    │
├───────────────────────────────────────────────────────────────────┼──────────────────────────────────────────┤
│ Change how a signal becomes an order (offset, sizing, no-flip)    │ trader.py                                │
└───────────────────────────────────────────────────────────────────┴──────────────────────────────────────────┘

The thing to internalize: editing toto2.py or threshold.py changes the algorithm; for routine experiments you're just
changing values, which are exposed as CLI args in real_data.py.