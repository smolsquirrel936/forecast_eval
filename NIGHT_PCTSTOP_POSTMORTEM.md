# Night percent-stop loop post-mortem (2026-10-12)

Branch `autoresearch_night_pctstop_1012` (forked from `simtrade_0929` tip
`e977b75`, where night is the `night_iter30` package), ledger
`results_night.tsv` iter01–iter03. Question: what `stop_pct` is best for the
NIGHT strategy that simtrade actually runs?

Baseline: `night_iter30` — Calmar 7.2974, 376 trades, net +29107.2434, DD
−3988.7364, **full window** (the night loop's own convention — it never
adopted the 0.7 split), warmup 6000, forecaster `f09b7ef5fffb`. iter01
reproduced it to 4dp with `stop_pct=None [night]`, confirming the simtrade
night package is iter30 (band ±0.0004 / min_prob 0.6 / stride 15, two-sided),
not iter10.

**Answer: `None` (OFF). No active width beats the incumbent; the best
non-destructive width is inert (≥ ~0.18, which never fires on recorded
data).**

## The evidence

### Pre-registration event study (no GPU, all 376 trades)

MAE-in-%-of-entry on minute closes: median 0.93 %, p75 1.9 %, p90 3.2 %,
p95 4.2 %, max 16.8 %. The naive sweep (stop exit at the threshold vs the
actual signal exit) is **negative at every width**:

| stop_pct | stopped (W/L) | naive net | worst observation |
|---|---|---|---|
| 1 % | 179 (36/143) | −4 533 | forfeits 27 081 on winners |
| 2 % | 83 (10/73) | −1 321 | least-bad active width |
| 3 % | 46 (7/39) | −6 514 | |
| 5 % | 11 (0/11) | −1 694 | stops losers **deeper** than their signal exits |
| 8–15 % | 1–2 (0/–) | −1 036…−3 159 | the catastrophe-catchers fire at max pain |
| 20 % | 0 | 0 | inert — max MAE is 16.83 % |

### Measured cells

| cell | Calmar | n | verdict |
|---|---|---|---|
| none (baseline) | **7.2974** | 376 | incumbent |
| 0.02 | 5.1086 | 435 | discard — churn, 3× the naive damage |
| 0.10 | 5.6650 | 377 | discard — even 1 firing widens DD −3989→−5083 |
| 0.20 (control) | 7.2974 | 376 | inert, = baseline exactly |

## Why night is day's opposite

Day adopted 0.10 because its worst-MAE trades **did not recover** — the 2011
pair ran to their signal exits at full loss, so a stop trimmed them. Night's
worst-MAE trade (16.8 % adverse, entry 22 670) **recovered to −5.4 % before
its signal exit**: a 15 % stop would have turned a −1 230-point loss into
−3 400. On night, the band exit already cuts losers faster and shallower
than any stop width; a percent stop only adds two failure modes it cannot
pay for — V-bottom exits (fires at max pain, forfeits the recovery) and
re-entry churn (n 376→435 at 2 %, net −4 138, DD −900 wider).

## Consequences

- No active width beats the incumbent — the sweep's best *firing* cell
  (0.10) degrades, so nothing performance-relevant was found to adopt.
- If a hard live catastrophe rail is ever mandated for night, the only
  non-destructive width is one that never fires on recorded data (≥ 0.18);
  note that even the single ≥10 %-MAE trade recovered, so night has no
  recorded catastrophe profile for a stop to catch.
- Same rule, opposite verdicts across sessions — the width question is
  per-session evidence, which is why the shared `EntryPercentStop` ships
  with a per-package knob and no default.

## Addendum (same day): the no-degradation width, measured

iter04 closed the wide end: 0.10 — the best naive width (−1 036, exactly
one firing) — measures Calmar 5.6650, DD −5 082.56. Even a single firing
degrades: the post-stop re-entry clawed back most of the naive net damage
(−315 measured) but the stop-out + re-entry trajectory *deepened* the
drawdown trough by −1 094 points. The curve is monotone toward inert:

    0.02 → 5.1086   0.10 → 5.6650   0.20 → 7.2974 (= baseline, inert)

**The only stop_pct without performance degradation is an inert one**, and
0.20 is the width with the measured proof (iter03 = baseline to 4dp). It
never fired in 9+ years of night tape — as a live knob it is pure
catastrophic-regime insurance (an adverse excursion beyond anything
recorded), at zero measured cost. Adopted as night's `stop_pct` default on
`simtrade_0929` (2026-10-12) on exactly that understanding: a loss rail for
a regime the tape has never shown, not a performance mechanism — night's
active widths are all measured degradations.
