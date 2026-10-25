# [tools/reversal_reaction.py](../../tools/reversal_reaction.py)

**Role:** a reaction-speed score for a backtest — how much of each trend reversal the strategy rode the wrong way — so a loop can rank on it next to Calmar.
**Pipeline:** a run folder (`params.json`, `trades.csv`, `forecasts.csv`) + its tape → 3 % zigzag pivots → per-leg ride → summary, optionally paired against a baseline run.
**Depends on:** [real_data.py](../real_data.md) (`load_minute_bars`)   **Used by:** run by hand and by the fullday reversal autoresearch loop after each backtest

```powershell
uv run --no-sync python -m forecast_eval.tools.reversal_reaction outputs\<run>
uv run --no-sync python -m forecast_eval.tools.reversal_reaction outputs\<run> --baseline outputs\<incumbent>
```

~3 s per call on the fullday tape (2.36 M bars). No GPU.

## Why it exists

Fullday's 2026-10 live losses were a reaction problem, not a sizing one: after a
turn it held the old position for a median ~32 h and gave back ~half its best
open profit, and the delay was the forecaster (~20 h to the first opposite-band
forecast), not the filters (~0.3 h after it). Calmar cannot see that directly —
a stop-width sweep moved Calmar from 2.07 to 3.25 and moved this score by at
most 0.012.

## Definitions

| Field | Meaning |
|---|---|
| swing leg | from one confirmed zigzag pivot to the next on the minute closes, `swing` = 3 % by default. Tape-only, so every run on the tape faces the same legs. |
| `reversal_ride` | **primary, lower = quicker.** Mean over legs of (points lost while positioned against the leg) ÷ (leg size). 0 = never against; 1 = rode the whole leg the wrong way. Charges both a late exit and a counter-trend entry mid-leg. |
| `exposed_at_turn` | legs where the position at the pivot already opposed the new leg |
| `ride_at_turn_median` | median ride over those legs |
| `react_hours_median` | session hours from the pivot until the position stops opposing the leg (exit or flip), over exposed legs |
| `censored_at_turn` | share of exposed legs it held to the leg's end |
| `delta_ride` | with `--baseline`: mean per-leg (run − baseline), with a fixed-seed bootstrap 95 % CI over legs |

Windows: `train` (first forecast → `train_split_ts`, legs ending after the split
excluded), `test` (split → end), `all`. Legs inside warmup are excluded — they
carry no position and would dilute the mean toward 0.

Every call writes `reversal_<window>.json` into the run folder.

## Reference values (fullday, train window, 2026-10-26)

| Run | `reversal_ride` | vs `fullday_pctstop_iter01` |
|---|---|---|
| `fullday_pctstop_iter01` (no stop) | 0.4094 | — |
| `fullday_pctstop_iter02` (stop 0.03) | 0.3978 | −0.0116 [−0.0268, +0.0036] |
| `fullday_pctstop_iter03` (stop 0.10) | 0.4084 | −0.0010 [−0.0031, +0.0000] |
| `fullday_pctstop_adopt005` (stop 0.05, live) | 0.4065 | −0.0030 [−0.0121, +0.0087] |

193 legs, 91 exposed at the turn, median reaction 38.2 session hours, 37 % ridden to the leg's end.

## Caveats

- Bar closes, not fill prices — a speed measure, not a P&L reconciliation.
- Pairing is by pivot bar index, so it refuses runs that differ in `data`, `warmup_bars` or `train_split_bars`.
- Being flat at a turn scores 0 on that leg. A strategy can improve the score by trading less; the Calmar and trade-count gates are what stop that.
- Roll splices in the tape can contribute to a swing; at 3 % that is rare.
