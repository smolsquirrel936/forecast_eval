# Fullday percent-stop loop post-mortem (2026-10-12)

Branch `autoresearch_fullday_pctstop_1012` (forked from `simtrade_0929` tip
`cdab0c2`, where fullday is the `fullday_split_iter07` package), ledger
`results_fullday.tsv` iter01–iter04. Question: what `stop_pct` for the
FULLDAY strategy minimizes the performance degradation of applying the stop?

Baseline: `fullday_split_iter07` — Calmar 2.8213, 134 trades, net +9809.6712,
DD −3477.0220, **train 70 %** (split bar 1651591, boundary 2024-01-18 03:35,
warmup 5000), forecaster `d7acdb22299f`. iter01 reproduced it to 4dp with
`stop_pct=None [fullday]`.

**Answer: the only width with zero measured degradation is the inert 0.20**
(armed, never fires — the whole tape's max trade MAE is 11.98 %). Every
active width lands at or below the incumbent; like night, fullday's
worst-MAE trades recover before their signal exits, so a stop fires at max
pain.

## The evidence

### Pre-registration event study (no GPU, all 134 train trades)

MAE-in-%-of-entry on minute closes: median 1.55 %, p75 2.99 %, p90 4.81 %,
p95 6.22 %, max 11.46 % (train) / 11.98 % (whole tape, a 2026-04 SHORT).
The naive sweep (stop exit at the threshold vs the actual signal exit) is
**negative at every width**:

| stop_pct | stopped (W/L) | naive net | worst observation |
|---|---|---|---|
| 2 % | 53 (14/39) | −4 533 | forfeits 8 328 on winners |
| 3 % | 33 (6/27) | −210 | least-bad active width |
| 5 % | 12 (2/10) | −2 066 | stops losers deeper than their signal exits |
| 8 % | 2 (0/2) | −958 | the catastrophe-catchers fire at max pain |
| 10 % | 1 (0/1) | −559 | the 11.46 % trade recovered to −4 % of entry |
| 15–20 % | 0 | 0 | inert — max whole-tape MAE 11.98 % |

### Measured cells

| cell | Calmar | n | verdict |
|---|---|---|---|
| none (baseline) | **2.8213** | 134 | incumbent |
| 0.03 | 2.0740 | 158 | discard — churn, 4× the naive damage |
| 0.10 | 2.7895 | 135 | discard — 1 firing, −110 net, DD unchanged |
| 0.20 (control) | 2.8213 | 134 | inert, = baseline exactly |

## Why fullday is night's shape, not day's

Day adopted 0.10 because its 2011 worst-MAE pair **did not recover** — both
ran to their signal exits at full loss, so the stop trimmed them. Fullday's
worst train trade (LONG 9353, MAE 11.46 %) **recovered to −376.67 points**
(−4 % of entry) before its signal exit: a 10 % stop would have turned that
into −935. The V-bottom signature night documented, and the measured 0.10
cell confirms it — one firing, −110 net, the drawdown trough untouched.
Below 10 % the churn term dominates exactly as on day and night: 0.03 turns
134 trades into 158, costs −825 net (4× the naive −210), widens DD
−3477→−4333 and the whole-data DD −4248→−7549 (the stop's re-entries land
in the same 2026 crash cluster the naive study cannot model).

## The 2026 crash observation (unscored, not a decision input)

The held-out window's two worst trades (SHORT 2026-04, MAE 11.98 %,
−3800.9 pts; LONG 2026-02, MAE 11.52 %, −1867.7 pts) did **not** recover —
a 10 % stop catches both, and iter03's whole-data block improved on the
baseline (+1618 net, DD −4248→−3752). This is recorded as an observation
only: the test window is never scored for decisions, and picking 0.10 on
this evidence would be picking on lookahead. On the decision numbers 0.10
costs −110 net (Calmar −1.1 %, DD identical). It is the trade-off to name
if a firing catastrophe rail is ever preferred to an inert one: measured
small cost on train, demonstrated (only out-of-sample) benefit in a
2026-style crash.

## Consequences

- No active width beats the incumbent on the decision numbers — the curve
  is monotone toward inert:
  `0.03 → 2.0740   0.10 → 2.7895   0.20 → 2.8213 (= baseline, inert)`
- **The no-degradation width is 0.20**, with measured proof (iter04 =
  baseline to 4dp; never fires in 8.6 years of tape, 2018-01→2026-08). As
  a live knob it is pure catastrophic-regime insurance — an adverse
  excursion beyond anything recorded — at zero measured cost. It matches
  night's adopted width and day's control.
- Tighter inert widths exist by construction (anything ≥ 0.12 is inert on
  the whole tape — max MAE 11.98 %), but 0.20 is the width with measured
  proof and the family convention; the margin at 0.12 is two basis points
  of recorded data, one worse trade and it is an active width.
- Third session, same lesson: the percent-stop width is per-session
  evidence. Day keeps a firing rail (0.10, its one above-incumbent cell);
  night and fullday keep the inert 0.20.
