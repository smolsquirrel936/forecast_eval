# [tools/band_sweep.py](../../tools/band_sweep.py)

**Role:** the band × min-prob search, scored out-of-sample — one GPU precompute, every cell replayed off the cache.
**Pipeline:** a minute tape → `precompute_forecasts` (once) → `run_backtest(forecasts_cache=…)` per cell → a two-half Calmar table.
**Depends on:** [run.py](../run.md), [metrics.py](../metrics.md), [real_data.py](../real_data.md), [strategy/threshold.py](../strategy/threshold.md)   **Used by:** run by hand — `DAY_SESSION_PLAN.md` W5b, `TAPE_REBUILD_PLAN.md` T7

```powershell
uv run python -m forecast_eval.tools.band_sweep `
  --data \\DESKTOP-SQUIRRE\dataset\RPT\TXF_RPT_minute_day.csv `
  --warmup-bars 676123 `
  --out outputs\day_band_sweep.csv
```

~160 s for 33 cells: ~110 s of precompute, then ~2 s a cell.

## Responsibility

**The emitter never changes the forecasts.** It reads a `Forecast` and returns a
direction, so a sweep over emitter settings can reuse one set of model outputs —
~4 minutes a candidate becomes ~2 seconds, which is what makes a grid plus an
out-of-sample split affordable at all.

W5b's own harness was ad hoc and did not survive as a file, so T7 could not
simply re-run it. This is that harness written down; the plan's claim that W5b
"leaves behind a harness that could check it in minutes" is true as of now.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `as_frame(trades)` | function | `build_trades`' dataclasses → a frame with a real `entry_ts` |
| `calmar(trades)` | function | `(net/\|max dd\|, net, n)` over a set of round-trips |
| `sweep(ticks, forecaster, cache, *, fee_rate)` | function | every cell, both halves, one row each |
| `main(argv)` | function | CLI: load, precompute once, sweep, print both rankings |
| `BANDS`, `MIN_PROBS`, `HALF_SPLIT` | constants | the grid and the out-of-sample cut |

## Why selection is on the worse half

Ranking by full-window Calmar picks a fiction. On the pre-rebuild day tape the
top cell was `band=0.004` at Calmar **14.7697** — which reads as "day nearly
matches night" and is in fact 27 trades, 10 of them out-of-sample, with a
first-half Calmar of 2.2781. So every cell is scored on the two halves of the
eval window separately (split **2023-06-01**) and ranked on the worse one.

The evidence for the rule is the rank agreement: **spearman(h1, h2) = 0.45** on
the old tape, **0.39** on the rebuilt one. Full-window rank is weak evidence on
this tape, and the rebuild did not change that.

> **The worst-half rule alone is not sufficient, and the rebuilt tape is what
> exposed it.** On the old tape the degenerate corner only won the *full-window*
> ranking, so worst-half was enough to exclude it. On the rebuilt tape the top
> cell by **worst half** is `band=0.005, min_prob=0.8` — **9 trades**, 5 and 4
> across the halves, with an *infinite* h2 Calmar because a 4-trade half never
> drew down. A rule that ranks on worst half still needs an explicit
> minimum-trades floor; W5b supplied one by judgment and never wrote it down.
> The tables below therefore state their `n >= 40` filter rather than implying it.

## The grid

`BANDS` × `MIN_PROBS` = 11 × 3 = 33 cells, carried as module constants rather
than CLI arguments so a re-run cannot quietly use a different grid than a
recorded result came from. **W5b's exact grid was never recorded** — only five of
its cells and the count — so this one is reconstructed to contain every cell that
was reported, and spreads the rest around the ridge W5b identified.

## Verification

The harness is licensed by reproducing CLI runs, which is how W5b validated it
too. On the rebuilt day tape at `--warmup-bars 676123`, three cells were run both
ways and agree exactly:

| cell | via CLI | via sweep |
|---|---|---|
| `0.00045 / 0.50` (the W5a probe) | 230 trades, net +5 973.6154 | identical, Calmar 0.4129 |
| `0.001 / 0.65` (the old DAY pin) | 67 trades, net +9 748.0117 | identical, Calmar 1.6937 |
| `0.00125 / 0.65` | 56 trades, net +11 323.6816 | identical, Calmar 1.8859 |

## T7's table — the rebuilt day tape, 2026-09-01

Eval window 2020-03-11 08:49 → 2026-08-21 13:44 (470 589 bars), `n >= 40`, by
worst half:

| band | min_prob | n | h1 | h2 | worst | full |
|---|---|---|---|---|---|---|
| **0.00125** | **0.65** | 56 | 1.2056 | 1.1664 | **1.1664** | 1.8859 |
| 0.002 | 0.65 | 47 | 3.3869 | 1.0778 | 1.0778 | 2.1915 |
| 0.0025 | 0.50 | 41 | 1.0752 | 1.3648 | 1.0752 | 1.7644 |
| 0.001 | 0.65 | 67 | 1.1059 | 0.9967 | 0.9967 | 1.6937 |
| 0.0015 | 0.65 | 53 | 0.8786 | 0.9311 | 0.8786 | 1.5384 |
| 0.00045 | 0.50 | 230 | 0.0009 | 0.4124 | 0.0009 | 0.4129 |

**The ridge survives the rebuild; the level does not.** W5b's stable region
(`band 0.001–0.0015 × min_prob 0.5–0.65`) is still where the non-degenerate cells
cluster, and the two best are still adjacent to its old pick. But the numbers
fall by roughly a third: worst-half **3.1557 → 0.9967** for the previously pinned
cell, and the best non-degenerate cell now scores **1.1664**.
