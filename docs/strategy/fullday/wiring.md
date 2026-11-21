# [strategy/fullday/wiring.py](../../../strategy/fullday/wiring.py)

**Role:** FULL-DAY's strategy wiring — `PARAMS` plus `make_strategy()`.
**Pipeline:** the FULL-DAY entry in the [strategy seam](../wiring.md).
**Depends on:** [emitter.py](emitter.md), [forecaster.py](forecaster.md), [trader.py](trader.md)   **Used by:** [strategy/wiring.py](../wiring.md), [real_data.py](../../real_data.md)

## Responsibility
The entry point for FULL-DAY's strategy set: `PARAMS` declares the session's default
knobs, and `make_strategy()` builds its Forecaster, SignalEmitter and Trader
factory. This is the file a search for this session edits first.

> **simtrade_0929, since 2026-11-19: `PARAMS` are `fullday_split_iter14`**
> (tag `fullday_split_iter14`). Timestral-209M (`forecaster` `timestral`,
> context 2048, horizon/stride 5), band ±0.001, `min_prob` 0.65, `trend_filter`
> off, `entry_passive_ticks` 1, `entry_rest_forecasts` 3, `night_only_open`
> on, `time_exit_bars` 5. Every rule the iter34 stack added (stop, lock, both
> cooldowns, `entry_min_prob` 0.60) is at its control, so the exit rule is the
> bare `BarsInTradeExit`. A bare `--strategy fullday` replays the iter14 run
> folder byte for byte. The notes below describe the knobs; values quoted in
> them may be older ones. Settings and evidence: `strategy/README.md`.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `PARAMS` | dict | the twelve default knobs `--strategy fullday` resolves against |
| `make_strategy(*, warmup_bars, tick_size=1.0, **overrides)` | function | the session's strategy stack |

## Return contract
| key | type | meaning |
|---|---|---|
| `forecaster` | `Forecaster` | built by [forecaster.py](forecaster.md) |
| `emitter` | `SignalEmitter` | a fresh [emitter.py](emitter.md) instance |
| `make_trader` | `callable(Execution) -> Trader` | factory, not an instance — see [trader.py](trader.md) |
| `exit_rule` | `Optional[ExitRule]` | the `stop_pct` percent stop, composed with any protection rules set in [exits.py](exits.md) — `None` when no knob is set |
| `forced_close` | `bool` | force close at session boundaries |
| `params` | dict | the resolved knobs, for the run banner and `params.json` |

## Provenance
**UNTUNED** — DAY's numbers, inherited and not chosen here, band ±0.001 / `min_prob` 0.65 (inherited from DAY).

## Key points / gotchas
- **Starting at the pinned config is what makes the split checkable.**
  `--strategy fullday` must reproduce the recorded run exactly (gate G1), so
  any deviation is a bug in the split rather than a strategy change.
- An unknown `**overrides` key **raises** rather than being ignored. A silently
  dropped override would run the pinned value while the command line said
  otherwise — the class of mismatch `live/config.py` was written to end.
- `warmup_bars` and `tick_size` come from the run, not the strategy; everything
  else defaults from `PARAMS` and is beaten by an explicit CLI flag.
- Two emitter knobs are deliberately **not** in `PARAMS` — `short_window`,
  `min_trend_ticks`. `real_data.py` never exposed them, so every recorded run
  used the emitter's class defaults; leaving them there is what reproduces
  those runs. `trend_filter` and `path_momentum` are declared since the iter14
  port (iter14 turns the trend filter off).
- **`forecaster`, `time_exit_bars`, `entry_passive_ticks`,
  `entry_rest_forecasts`, `night_only_open`** (iter14 port, 2026-11-19): see
  [timestral.py](timestral.md), [time_exit.py](time_exit.md) and
  [trader.py](trader.md).
- **`entry_min_prob` (added `fullday_rev_iter02`)** is a package-declared knob
  beyond the twelve CLI knobs, like `stop_pct`: the Trader skips an OPEN from
  flat whose signal strength falls below the floor it implies. `make_strategy`
  raises `ValueError` unless
  `min_prob <= entry_min_prob < full_confidence_prob` — below `min_prob` the
  gate is vacuous, at/above `full_confidence_prob` unreachable (the strength
  ramp saturates at 1.0). Its `PARAMS` value 0.50 is the inert control.
- **`exit_threshold` (added `fullday_rev_iter16`, reversal decision 8)** is a
  package-declared knob like the two above: while positioned, an opposite
  signal clears at `|predicted_return| >= exit_threshold`; entries keep the
  full band. `make_strategy` builds the emitter with it and `make_trader`
  binds the emitter to each trader it builds
  (`emitter.bind_position(lambda: trader.position)`), which is how the emitter
  knows the exit side without touching frozen `run.py`. The emitter's
  constructor raises `ValueError` unless the band is symmetric
  (`buy == -sell`) and `0 <= exit_threshold <= buy_threshold`. Its `PARAMS`
  value 0.001 equals the band — the inert control, byte-identical runs.
- **`lock_after_pct` / `giveback_frac` (added `fullday_rev_iter23`, reversal
  decisions 9/10, phase G)** are package-declared knobs for the protection
  exits in [exits.py](exits.md): a break-even lock and an armed give-back
  trail, composed with `stop_pct` through the any-of seam. Both `None` — the
  inert control: `make_strategy` returns the bare `EntryPercentStop`, so runs
  are byte-identical to the pre-knob package. The rules are stateful
  (`adoptable=False`) but restorable: a live `--resume` into a held lot
  restores their best price from `exit_state_<slug>.json` or a typed
  `--exit-state`, and refuses without one. A roll keeps their state (the
  Trader carries the original entry; [exits.md](exits.md)).
- **`channel_exit_bars` (added `fullday_rev_iter29`, reversal decision 11,
  phase H)** is a package-declared knob for the price-channel exit in
  [exits.py](exits.md): once the position is at least N bars old, exit on a
  break of the last N in-position bars' extreme — model-free, no forecast
  lag. `None` — the inert control; the rule composes through the same any-of
  seam. Stateful (`adoptable=False`).
- **`reentry_cooldown_signals` (added `fullday_rev_iter33`, reversal decision
  12, phase I)** is a package-declared knob for the Trader's re-entry
  cooldown: after a fill that flattens a position, an OPEN on the closed
  position's side is skipped for K signals (HOLDs count; the opposite side
  opens freely). `0` — the inert control, the cooldown never arms, runs are
  byte-identical to the pre-knob package. `make_strategy` raises `ValueError`
  unless it is an int >= 0.
- **`stop_cooldown_minutes` (strategy_test_1108 step 3; adopted at 4320 = 3
  days on 2026-11-08)** is passed to the [Trader](trader.md) with
  `stop_cooldown_loss = 0.98 * stop_pct`, so it arms on a stop-sized loss. With
  `stop_pct` None it is passed as 0 (no stop, nothing to key on). 0 is the
  control. Evidence and the live restart gap: [strategy/README.md](../../../strategy/README.md).

> **This session is UNTUNED.** The values in `PARAMS` are DAY's, inherited so the
> package is runnable and parity-checkable. They were not chosen for
> `TXF_RPT_minute_fullday.csv`, and there is no `results.tsv` row for full-day.
> The 2026-09-09 band sweep did measure this exact cell (97 trades, Calmar 1.3975
> at fee 0.00015, warmup 5000 — the harness default fee is 0.0002 now).
> DAYNIGHT_TOTO2_RESET_PLAN §8 used it as the restore check, not as a target, and
> **no Calmar carries into the loop as a bar to beat** (§6 Q4): `results_fullday.tsv`
> starts empty and the loop's `iter01` sets the first best. Live no longer refuses `--config fullday`
> (LIVE_STRATEGY_ROUTING_PLAN D1). The package was restored to this state on
> 2026-09-14 after the fullday loop (iter00–05, tip at commit
> `30f70d7`).

## Related
[strategy/fullday/wiring.md](wiring.md) · [strategy/wiring.py](../wiring.md) · [strategy/base.py](../base.md) · [STRATEGY_SPLIT_PLAN.md](../../../STRATEGY_SPLIT_PLAN.md)
