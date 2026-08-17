"""Live-trading path: push-driven loop over the same strategy stack.

The backtest (`run.run_backtest`) pulls MarketEvents from an `Environment`
generator; a live feed pushes them. `LiveLoop` is the same four-step per-tick
body restructured to be driven one event at a time, so `Forecaster`,
`SignalEmitter`, `ExitRule`, `Trader` and `Portfolio` are reused unchanged.

Fills are the other difference: the backtest *infers* them from trade prints,
a broker *reports* them. The `Broker` interface (see `live.broker.base`) is the
seam; `SimBroker` wraps the existing `Execution` so the live loop can be run
offline and diffed against `run_backtest` (SHIOAJI_PLAN.md phase V0).
"""
