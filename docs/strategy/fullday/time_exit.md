# time_exit.py — BarsInTradeExit (fullday's horizon exit)

`strategy/fullday/time_exit.py` closes a position after **N bars in the
trade** — the "hold for the forecast's horizon" semantics. Package-owned on
branch `forecaster_eval_1116` because the shared `exits/` are not edited in
place (STRATEGY_SPLIT_PLAN A3.1).

## Why it exists

The signal-level study of Timestral's gated 5-minute stream
(`model_eval --signal-study`) showed the edge is a **first-minute
mean-reversion bounce**. The strategy's default shape (no exit rule +
no-flip) holds such a signal until the next *opposite* gated signal — the
first no-exit run held trades for weeks (2018-03-20 → 2018-04-18) and drew
down 16 402 points. `BarsInTradeExit` matches the holding period to what
the forecast actually covers.

## Mechanics

- `should_exit` returns `bar_idx - position.entry_bar_idx >= bars` — a
  stateless read of the entry bar from the run loop's `PositionState`
  snapshot.
- **Adoptable as expired** (`adoptable = True`, simtrade_0929 port,
  2026-11-19, user decision). A live `--resume` that adopts a held lot cannot
  recover the entry bar, so `adopt_position` seeds `entry_bar_idx = -1`. The
  rule never times from that sentinel: it reads a negative `entry_bar_idx` as
  "the hold is over" and fires on the first bar, closing the adopted lot at
  once. A restart takes minutes, and the hold is 5 bars, so the backtest would
  have closed the lot anyway. Backtest and live-opened positions always carry
  a real bar index (≥ 0), so recorded runs are unchanged.
- Not restorable: it carries no state, so `exit_state_day_night.json` is not
  written or read while it is the only rule.
- Composes with `stop_pct` / the take-profit trail through the package's
  `AnyOf`.

## Knob

`time_exit_bars` (package-declared). On simtrade_0929 it is **5** in
`PARAMS` (fullday_split_iter14); `None` turns it off:

```powershell
--param time_exit_bars=5
```
