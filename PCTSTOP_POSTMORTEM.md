# Percent-stop loop post-mortem (2026-10-12)

Branch `autoresearch_day_pctstop_1012` (forked from local `main` de38848),
ledger `results_day.tsv` iter01–iter08. Question: can an **entry-anchored,
percent-of-entry stop** (`stop_pct`) limit losses without significantly
degrading the day incumbent — the mechanism requested for the live
deployment, where `max_daily_loss_points` is a fixed point budget that means
different fractions of the contract at different price levels?

Baseline: `day_split_iter02` — Calmar 5.1186, 79 trades, DD −1617.9028,
net +8281.3586 on train-70% (split bar 798498), forecaster 6587766b4391.
iter01 reproduced it to 4dp on this tree; iter08 (stop_pct 0.20, inert
control) reproduced it again with the rule armed but never firing — max train
MAE is 17.44%, so anything ≥ 0.18 is inert by construction.

## The sweep

| stop_pct | Calmar | n | net | max DD | verdict |
|---|---|---|---|---|---|
| none | **5.1186** | 79 | +8281.36 | −1617.90 | incumbent |
| 0.03 | 3.8049 | 94 | +7260.97 | −1908.62 | discard — churn |
| 0.05 | 2.0571 | 86 | +5814.87 | −2826.25 | discard — re-entry into the 2021 trough |
| 0.08 | 4.7908 | 81 | +8233.85 | −1718.46 | discard — near-neutral, still short |
| **0.10** | **5.3659** | 80 | +8012.02 | −1493.32 | **keep** — trims the 2011 cluster |
| 0.12 | 5.0858 | 79 | +8272.36 | −1626.90 | discard — flanks the pocket |
| 0.15 | 4.2660 | 79 | +8019.41 | −1879.85 | discard — exits the worst trade deeper |
| 0.20 | 5.1186 | 79 | +8281.36 | −1617.90 | control — inert, = baseline exactly |

Pre-registration event study (no GPU; `_pctstop_event_study.py` at the repo
root's parent): train-trade MAE median 1.7%, p90 5.7%, worst 17.4%. The naive
sweep predicted no catastrophic width (best +93 at 2%, worst −1885 at 6%) —
confirmed: this family, unlike the trail and the weakness exit, has no
cliff. The churn term the study cannot model is what separates the cells.

## Findings

1. **Below 0.10, the day_trail lesson repeats**: a stop that fires on normal
   adverse excursion converts multi-week holds into churn (0.03: n 79→94,
   net −1021) and its re-entries land in the same drawdown cluster the stop
   was exiting (0.05: DD −1618→−2826). Exit rules are only as good as their
   re-entry control, and this strategy has none at the pinned settings.
2. **The only cell above the incumbent is 0.10** (+0.25 Calmar, DD −125
   narrower, net −269). Its entire effect is two firings in 10.8 years, both
   on 2011 trades that ended among the worst losers (MAE 17.4% and 11.7%).
   Flanked by discards at 0.08 and 0.12 — a pocket, the jagged-surface
   signature the reentry loop documented. As a *Calmar claim* it is fragile;
   as a *catastrophe rail* it is exactly the requested shape: fires only
   where the loss is already extreme.
3. **The held-out window is untouched.** At 0.10 the whole-data run's test
   segment equals the incumbent's to 4dp (+16257.48, 73 trades) — the rule
   never fires there (2021-12→2026-08 max trade MAE < 10%). Unlike the
   reentry adoption, nothing out-of-sample was spent or depends on this.
4. **Inert control validates the machinery** end-to-end: 0.20 armed, never
   fired, byte-identical metrics.

## Recommendation for the live deployment

Wire the live rail at **stop_pct ≈ 0.10** with the flatten-and-continue
semantics already implemented in the backtest rule: on train it is Calmar
-neutral-to-positive; on the held-out window it is inert; its firings are
rare (twice per 10.8 years on recorded data) and land only on
already-catastrophic trades. Treat it as insurance against a regime worse
than anything in the tape, not as a performance knob: the 0.08–0.12 cells
bracketing it are within ±5 % of baseline Calmar, and the next-print fill
model means a gap through the stop exits at the print, not the threshold.

The live wiring itself (a per-position percent stop in the `live/` rails
that closes the position without halting the deployment) is **not built
here** — it is a change to the running simtrade's code path
(LIVE_STRATEGY_ROUTING_PLAN D2) and should be its own change, measured
against this ledger, after the user decides the width.

## The surviving config

`strategy/day` on this branch carries the knob default OFF
(`stop_pct: None`), so `simtrade_0929`'s incumbent is unchanged by this work;
the 0.10 operating point is recorded in the ledger row `day_pctstop_iter05`
and reachable by `--param stop_pct=0.10`. Adoption into `PARAMS` (which
would make Monday 08:45 trade it live) is a user decision, per the reentry
loop's adopt/unadopt precedent.
