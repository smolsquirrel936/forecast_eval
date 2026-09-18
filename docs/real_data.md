# [real_data.py](../real_data.py)

**Role:** Full-dataset backtest variant with a time-based warmup/eval split.
**Entry point:** `python -m forecast_eval.real_data`   ·   needs Toto2
**Depends on:** [forecaster/toto2.py](forecaster/toto2.md), [run.py](run.md), [logging_io.py](logging_io.md), [metrics.py](metrics.md), [strategy/threshold.py](strategy/threshold.md)

## Responsibility
The full real-data backtest: runs Toto2 + ThresholdEmitter over the whole deduped
1-min file, splitting it so the first `lookback_frac` (30%) is silent warmup (no
trading) and the trailing 70% is the backtest window. Writes the artifact bundle
and prints the metric pack plus a buy-and-hold floor.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `load_minute_bars(path, n_bars)` | function | drop NaN closes; `n_bars=None` → full dataset |
| `main(argv)` | function | parse CLI, split, run (with progress bar), log, print metrics |

## The train/test split
`--train-frac` (default **1.0**) subdivides the *eval* window; warmup is
untouched. The leading `train_frac` of it is what the printed metric pack covers,
and the trailing remainder is **held out and never scored**:

```
|<---- warmup (no trades) ---->|<---- train ---->|<---- test (held out) ---->|
0                           warmup        warmup+T                    n_session
```

`--train-bars` names the same boundary in in-session bars and **overrides
`--train-frac` entirely**, exactly as `--warmup-bars` overrides `--lookback-frac`
— and for the same reason: a fraction moves when the tape grows, so a Calmar
recorded against one stops being re-derivable. Use the fraction interactively;
record the bar count.

**The HTML report always covers the WHOLE tape.** Only the terminal pack is
windowed. The artifact bundle written under the run dir is the whole run, so
every chart, the equity curve and the Result-summary tables are whole-data — the
train numbers exist to be *decided* on, the report exists to be *read*. When a
split is active the terminal prints the train block first, labelled
`THE DECISION NUMBERS`, and the whole-data block below it says it is the report
figure.

Held-out **test** metrics are computed nowhere and printed nowhere. A loop that
glances at the test Calmar every iteration has spent the holdout, which is the
whole point of having one.

Three derived fields reach `params.json` (and so the report's Settings table) next
to `train_frac`: `train_split_bars`, the resolved train length, and
`train_split_ts`, the last train bar. On a `--train-bars` run `train_frac` is
recorded as **`None`**, not `1.0` — argparse's untouched default would record a
value the run never consulted, and one that reads as "no split" on a run that has
one. See [TRAIN_TEST_SPLIT_PLAN](../TRAIN_TEST_SPLIT_PLAN.md).

## Key points
- **`--data` is required** — there is no default path. The old default named the
  DAY file while every tuning run used NIGHT, and a session picked by a default
  is one nothing can check (SHIOAJI_PLAN §2.12, the same reason `fetch_bars`
  takes a required policy).
- `--n-bars` defaults to **None** (use everything); `--lookback-frac` (0.30) sets
  the silent-warmup fraction.
- `warmup_bars` is the *data-driven split*, so trading is suppressed across the whole
  lookback region — but `context_length` (3008) still caps each forecast's window
  (Toto2 truncates anything longer, so a bigger context wouldn't help).
- Buy-and-hold is computed over the **eval window only**, so the comparison is fair
  (the strategy never traded the warmup region).
- Runs with a tqdm **progress bar** (the full file is millions of bars).
- `--precompute` batches every forecast on the GPU up front, then replays execution.

## Key gotchas
- Refuses to run if `warmup < context_length` (the model could never fire).
- `--lookback-frac` must be strictly in (0, 1).
- `--train-frac` must be in **(0, 1]** — 1.0 *is* admitted, unlike
  `--lookback-frac`, because it is the default and names "score the whole eval
  window", which is what every run recorded before the flag existed did.
- `--train-bars` is bounded by **`eval_bars`**, not the in-session count and not
  the raw row count: the train window is carved out of what is left after warmup,
  so a larger value would pass a whole-tape check and then silently mean no split.
- A round-trip open across the boundary is counted by neither side —
  `build_trades` books a trade only on its closing fill, which is also what a run
  that stopped at the split would have reported.

## The contract collapse is a no-op — measured
`load_minute_bars` keeps the highest-volume row per minute when a `contract`
column is present, on the assumption that several months print the same minute.
**They do not.** All four RPT tapes carry exactly one contract per minute —
measured 2026-08-23 and re-measured 2026-09-01 on the rebuilt vintage:

| file | rows | minutes with >1 contract |
|---|---|---|
| `TXF_RPT_minute_night.csv` | 1 849 007 | **0** |
| `TXF_RPT_minute_day.csv` | 1 146 712 | **0** |
| `TXF_RPT_minute.csv` | 2 995 727 | **0** |
| `TXF_RPT_minute_fullday.csv` | 2 364 417 | **0** |

The one file that *would* trip the collapse is
`TXF_RPT_minute_bycontract.csv` — the rebuild's intermediate, which carries two
delivery months per minute by design. It is not an evaluated tape and must never
be passed to this loader; see
[tools/build_session_tapes.md](tools/build_session_tapes.md).

So the files arrive already front-month-continuous, spliced by **calendar** — 106
of 108 seams land exactly one day after the expiring month's last trading day.
The rule stays as a guard against a future file that does mix months; it is not
the convention the model was evaluated on, and [live/roll.py](live/roll.md)
therefore splices live bars on the calendar rather than on volume
(SHIOAJI_PLAN §2.6).

The seam is worth knowing when reading model behaviour around a roll: its median
jump is **0.70 %** (95 points) against a median ordinary minute of 0.0095 %, and
95 % of seams exceed the strategy's ±0.00045 band. That is basis *plus* a whole
day session's move, because the last trading day's 15:00–23:59 block is absent
from the file.

## Related
[run.py](run.md) · [forecaster/toto2.py](forecaster/toto2.md) · [SPEC §4.8](../SPEC.md)
