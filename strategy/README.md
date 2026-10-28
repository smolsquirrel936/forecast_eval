# strategy/ — what `simtrade_0929` trades

The four packages (`day/`, `night/`, `fullday/`, `hybrid/`) as they stand on the
local branch `simtrade_0929`, forked from `main` @ `f830f7d` on 2026-09-29 for
the three-session simulation trade. The branch is **not** meant to merge into
`main`. The plan and every decision behind it: `../SIMTRADE_0929_PLAN.md`.

Live builds each deployment from its package's `wiring.py:make_strategy`
(LIVE_STRATEGY_ROUTING_PLAN D2), so **the `PARAMS` below are what the simtrade
places orders on.** `live/config.py` holds only the provenance record.

## Shared by all four

| | |
|---|---|
| Forecaster | Toto2-1B (`Datadog/Toto-2.0-1B`), context 3008 bars, horizon 30 bars |
| Emitter | `ThresholdEmitter`, trend filter + path momentum on, `full_confidence_prob` 0.9 (hybrid: one per half, behind a router) |
| Trader | limit orders at `aggression_ticks` 0, `max_position` 1, no-flip, no pyramiding |
| Exit rule | `EntryPercentStop` (`exits/pct_stop.py`) at each package's `stop_pct` — closes when the price moves `entry_price × stop_pct` against the position, checked every tick, filled at the next print. Otherwise positions close only on an opposite signal. Hybrid has none |
| `forced_close` | `False` — positions carry across sessions, nights and weekends |

## Day — `day_split_iter02`

| Knob | Value |
|---|---|
| band | buy +0.001 / sell −0.001 |
| `min_prob` | 0.65 |
| `stride_bars` | 30 |
| sides | long and short |
| `stop_pct` | **0.10** (since 2026-10-12, `aace5c7`) |

- **Source:** `autoresearch_split_0.7_day` @ `58e81b0`, the split loop's
  **baseline**. Its code is identical to `main` — iter01 and iter02 only added
  ledger rows. The only day change on this branch is the stop.
- **Score:** Calmar **5.1186**, 79 trades, on the **train 70 %** of the day eval
  window (split at 2021-12-02 13:23).
- **Rationale:** the band and gate are the W5b day search's cell
  (DAY_SESSION_PLAN), restored exactly by the 2026-09-14 reset
  (DAYNIGHT_TOTO2_RESET_PLAN). The loop's later keeps — `min_prob` 0.60
  (iter06, 5.3356) and a same-day loss cooldown (iter08, 5.6063) — are **not**
  taken.
- **Stop:** `stop_pct` 0.10 is `day_pctstop_iter05`
  (`autoresearch_day_pctstop_1012` @ `6aaa7bb`), the sweep's one cell above the
  incumbent: train Calmar **5.3659**, 80 trades, net +8 012, max drawdown
  −1 493. It fired twice, both in 2011, and never on the held-out tail. Every
  narrower width is much worse (stopcmp_20261018_145644, train Calmar: 0.03
  3.80, 0.05 2.06, 0.07 1.99); 0.20 and wider never fire (max MAE 17.44 %).
- **Live source record:** `PINNED_DAY` still reads `3c3ae38`, Calmar
  **1.6937**, and the banner prints that. It is accurate — the same settings —
  but measured on the **whole** eval window of a different research run. 1.6937
  and 5.1186 are the same strategy scored on different windows; neither is a
  forecast of the other.

## Night — `night_iter30`

| Knob | Value |
|---|---|
| band | buy +0.0004 / sell −0.0004 |
| `min_prob` | **0.6** |
| `min_trend_ticks` | **10** — the emitter's class default in this package (shared default 3) |
| `stride_bars` | **15** |
| sides | long and short |
| `stop_pct` | **0.05** (since 2026-10-18, `bb7a602`) |

- **Source:** `autoresearch_night_1004` @ `4cbc1e6`. `strategy/night/` is copied
  from that commit file for file; the branch forked from this branch at
  `887bc5d`. Replaced `night_iter10` (`autoresearch_night_0918` @ `6159a4c`,
  long-only, Calmar 6.5793) on 2026-10-05.
- **Score:** Calmar **7.2974**, 376 trades, net +29 107 points, max drawdown
  −3 989, on the **whole** night eval window (warmup 6000; the unsplit loop —
  there is no held-out tail).
- **Rationale (the keeps, in order):**

  | iter | change | Calmar |
  |---|---|---|
  | 01 | baseline — `PINNED`'s old settings (±0.00045, `min_prob` 0.5, stride 30) | 1.1731 (310 trades) |
  | 02 | `min_prob` 0.5 → 0.6 | 3.0996 |
  | 05 | `min_trend_ticks` 3 → 10 (emitter default) — the trend filter demands a real early→late move | 4.0294 |
  | 13 | stride 30 → 15 — a new forecast stream, not like-for-like | 5.5250 |
  | 30 | band ±0.00045 → ±0.0004 | 7.2974 |

  Discarded along the way: long-only (iter26, 4.0624 — dropping shorts raised
  the drawdown; two-sided shorts were acting as fast exits from longs), stride
  10, horizon 15 and 60, context 1504, a time stop and two fixed stop-losses,
  and a quantile fan gate.
- **Stop:** `stop_pct` 0.05, from the width comparison in
  `output_backup/outputs_20261027/stopcmp_20261018_145644` (whole window, warmup 6000): Calmar
  **9.1078**, 381 trades, net +30 271, max drawdown −3 324, 11 firings in the
  tape. The other widths: none 7.2974, 0.03 7.0854, 0.07 8.8814, 0.10 5.6650;
  0.20 and wider never fire (max MAE 16.83 %). 0.07 beside it also beats no
  stop, so 0.05 is not an isolated point. The stop's history on this branch:
  0.20 (`9a29ae4`, inert) → 0.10 (`260a033`, 2026-10-12) → 0.05 (`bb7a602`,
  2026-10-18). At 0.10 its one firing sold the 2024-08-05 gap at −15 % and
  widened the drawdown to −5 083.
- **Live source record:** `PINNED` has `source_commit`/`source_calmar` `None`
  (the row is in `autoresearch_night_1004`'s `results_night.tsv`, not
  `results.tsv`); the banner prints `UNTUNED calmar=nan` and `source_note` names
  the commit.
- **Mid-soak swap.** Night's soak (`soak_20260929_082008_180784`) ran
  `night_iter10` until this change. Its earlier run folders record the old
  `strategy_sha`, which `live/status_report.py` shows as differing from the
  package on disk. The stop adoptions changed `strategy_sha` again, for night
  and fullday on 2026-10-12 and 2026-10-18 and for day on 2026-10-12.

## Fullday — `fullday_rev_iter34` (since 2026-10-27)

| Knob | Value |
|---|---|
| band | buy +0.001 / sell −0.001 |
| `min_prob` | 0.50 (the emitter's gate; it gates exits too) |
| `entry_min_prob` | **0.60**: an OPEN from flat needs this directional probability; closes are never gated |
| `stride_bars` | **10** |
| sides | long and short |
| exits | `stop_pct` **0.05** + `lock_after_pct` **0.03**: once a trade is 3 % in profit, a return to its entry closes it (`strategy/fullday/exits.py`) |
| `reentry_cooldown_signals` | **16**: after a close, an open on the same side is skipped for 16 signals (~2.7 h); the other side opens freely |

- **Source:** `autoresearch_fullday_reversal_1026` →
  `candidate_fullday_iter34` (merged 2026-10-26). It is the loop's `iter34`;
  the loop's last kept run (`iter35`, cooldown 48) was not taken. Plan and
  results: `../../autoresearch_trading_plan_fullday_reversal.md`, phase 2.
- **Score:** train-70 % Calmar **4.4463**, 181 trades, net +10 789, max
  drawdown −2 427 (the replaced config: 3.2466). Its reversal ride is 0.3614
  against 0.4065, so it gets off the wrong side of a reversal sooner.
- **Held-out 30 %: optimistic.** Approximate Calmar **4.06** (net +19 513,
  drawdown −4 804) against the replaced config's 3.53, with a quicker ride
  (Δ −0.048 [−0.090, −0.007]). But it was read *after* iter35 failed there and
  six configs were compared on it, so the held-out window took part in the
  choice. This simtrade is its first clean out-of-sample test.
- **Live-only fixes it carries** (the backtest has no rolls):
  - the runtime's `rolling()` hook (`main` `825f069`), so the cooldown lets a
    roll's re-open through;
  - the roll's re-open keeps the original entry, so the stop and the lock keep
    measuring from it, as in the backtest.
- **Replaced:** `fullday_split_iter07` + `stop_pct` 0.05, below. Its soak ran
  from 2026-09-29 to 2026-10-23.

## Fullday until 2026-10-26 — `fullday_split_iter07`

| Knob | Value |
|---|---|
| band | buy +0.001 / sell −0.001 |
| `min_prob` | **0.50** |
| `stride_bars` | **15** |
| sides | long and short |
| `stop_pct` | **0.05** (since 2026-10-18, `bb7a602`) |

- **Source:** `autoresearch_split_0.7_fullday` @ `394ce85` (the later `620b338`
  only repaired a ledger row).
- **Score:** Calmar **2.8213**, 134 trades, on the **train 70 %** of the fullday
  eval window (split at 2024-01-18 03:35).
- **Rationale (the keeps, in order):**

  | iter | change | Calmar |
  |---|---|---|
  | 02 | baseline — DAY's band and gate, inherited at the split | −0.2061 (67 trades) |
  | 03 | stride 30 → 15 | 1.4452 |
  | 05 | `min_prob` 0.65 → 0.60 | 1.6797 |
  | 06 | `min_prob` → 0.55 | 2.8112 |
  | 07 | `min_prob` → 0.50 — "gate vacuous, walk ends" | 2.8213 |

  Not taken: the later keeps — buy band 0.0009 (iter08), a 500-tick stop-loss
  (iter09), buy band 0.0008 and sell band −0.0009 (iter12–13, 5.7392).
- **Stop:** `stop_pct` 0.05, from the width comparison in
  `output_backup/outputs_20261027/stopcmp_20261018_145644` (warmup 5000, `--train-bars 1651591`):
  train Calmar **3.2466**, 142 trades, net +9 674, max drawdown −2 980, 12
  train firings. The other widths (train): none 2.8213, 0.03 2.0734, 0.07
  2.6855, 0.10 2.7895; 0.20 and wider never fire (max MAE 11.98 %). Whole data,
  including the never-scored held-out 30 %: 0.05 7.6706, none 6.1216, 0.10
  7.3612, 0.07 4.5121. Replaced 0.10 (`c7e2178`, 2026-10-12).
- **Live source record:** `PINNED_FULLDAY` stays `None`/`None`; its note and
  `wiring.py` docstring now name this commit instead of "UNTUNED, DAY's band".

## Hybrid — `hybrid_split_iter22` (since 2026-10-27; not yet started)

The DAY half trades the DAY session and the NIGHT half the NIGHT session, one
position between them; a lot carries across the session change and the incoming
half manages it (`strategy/hybrid/router.py`).

| Knob | Day half | Night half |
|---|---|---|
| band | buy +0.0005 / sell −0.0005 | buy +0.0007 / sell −0.0007 |
| `min_prob` | 0.60 | 0.50 |
| opens | **none 09:30–12:59** (`day_open_block_start` 9.5 / `_end` 13) | **never** (`night_allow_open` False): closes only |
| `stop_pct` | none | none |
| shared | stride 30, Toto2-1B (one copy, both halves), context 3008 | |

- **Source:** `autoresearch_split_0.7_hybrid` @ `9c0741f` (`iter22`), placed on
  this branch as `58eb8b5` / `5382486` (no merge of the loop branch). Plan and
  results: `../../autoresearch_trading_plan_hybrid_split07.md`,
  `results_hybrid.tsv`.
- **Score:** Calmar **12.8738**, 86 trades, on the **train 70 %**. Held-out
  approximate Calmar **6.9773** against the baseline's 5.7634 and the day half
  alone's 0.371, read once after the search ended.
- **Live:** slug `hybrid` (its own ledger, bar cache and wire; never fullday's
  `day_night`). Registered by `4b82111` and the five commits before it on
  `main` (`autoresearch_trading_plan_hybrid_split07.md`, "Going live").
- **Live source record:** `PINNED_HYBRID` stays `None`/`None`; its note names
  iter22 (`fc27688`).

## Cautions

1. **The Calmars are not comparable.** Day, fullday and hybrid are train-70 %
   figures; night's is whole-window. Different windows, different tapes.
2. **Day's and fullday's held-out 30 % has never been scored**
   (TRAIN_TEST_SPLIT_PLAN §4.4). This simtrade is their first out-of-sample
   exposure. Hybrid's was read once, after its search ended (6.9773).
3. **Night was tuned and scored on the same bars** — every bar the Calmar covers
   was seen by the search that chose the settings. Five keeps out of thirty
   tries is how overfitting looks, and nothing here rules it out.
4. **Night trades both sides** since `night_iter30` (it was long-only under
   `night_iter10`). Its shorts lost points on their own in the search (−3 338 on
   iter13) but cutting them cost more drawdown than it saved.
5. **Fullday's probability gate is nearly idle at 0.50.** It still rejects a
   signal whose own direction has probability under one half (`emitter.py:180`),
   but moving 0.55 → 0.50 left the trade count at 134 — the loop's "gate
   vacuous". The loop's 500-tick stop-loss (iter09) is not taken; fullday's
   only stop is the 5 % `stop_pct` (caution 15).
6. **Positions are long-lived.** `forced_close=False` everywhere; W5b measured a
   median hold of **20 days** for day's settings. Positions ride through the
   other sessions and weekends, managed only by the percent stop.
7. **The account can hold up to 4 lots in one direction** — fullday and hybrid
   each overlap both day and night, and the owner nets them into one account
   book. No
   `--max-daily-loss-points` and no `--kill-file` are set (decided 2026-09-29).
8. **Four deployments under the owner is untested.** STRATEGY_SPLIT_PLAN §23
   lifted the one-instance rule for two deployments, one lot each, ten minutes.
   Three have run full sessions since 2026-09-29; hybrid's first start makes
   four.
9. **Data allowance.** Five logins subscribe the same tick stream; what that
   costs against the 500 MiB/day allowance is unmeasured (§18.13). The owner
   warns below 50 MB but does not stop.
10. **Night's banner is accurate again** (`night_iter30`). `LiveConfig.describe()`
    prints `trend=` from the emitter's signature default and one `band=±x`;
    under `night_iter10` that read `trend=3.0 band=±0.00065` against a stack
    trading 12 and +0.00065 / −0.00045. iter30 keeps `min_trend_ticks` in the
    emitter's default (10.0) and has a symmetric band, so the banner now matches
    what trades. `min_trend_ticks` is still absent from `params.json`.
11. **Fullday's and night's warm-ups are shorter.** At stride 15 the defaults
    are 13 calendar days for fullday and 18 for night (day 84; night was 30 at
    stride 30). Priming needs 3 non-HOLD signals in the window; if
    either stops with `emitter priming incomplete`, re-run it with
    `--warmup-days 30`. It refuses before trading, so a retry is safe.
12. **Re-logins and the 5-connection cap.** Shioaji allows 5 connections per
    `person_id`; the owner plus four deployments hold 5. Each process re-logs in
    before every open with `force_refresh=True`, which opens a new connection
    ~30 s before the old one drops. The owner goes first (08:37, 14:52); since
    2026-10-18 the deployments follow 40 s apart (day 08:40:00, night 08:40:40,
    fullday 08:41:20, hybrid 08:42:00; 14:55 likewise), so each spike makes 6.
    **6 was MEASURED on 2026-10-26** (`multi_deploy_probe --sessions 6`): all
    coexist and the first session stays usable. 7 or more is unmeasured. Watch
    the consoles at 08:40–08:45 and 14:55–15:00 for `VERIFY_FAILED` or
    `relogin` failures. Logins per day (~40) are far under the 1 000/day cap.
13. **Day and night warm up on tape bars spliced to broker bars.** The broker
    serves minute bars only for listed contracts (the front month and the one
    before), so a first run into an empty `outputs/` cannot reach the history
    priming needs. On 2026-09-29 day stopped with `emitter priming incomplete:
    2/3 samples from 5142 bars`: `202609` had expired on 09-16, and `202610`
    before the roll was the thinly traded back month. Both caches were therefore
    seeded from the RPT tapes (`live/seed_cache.py`, DAY_SESSION_PLAN C7; the
    mix was accepted in D4, 2026-08-24). Consequences, measured that morning:
    - day's context is RPT bars to **08-21**, then only `202610` back-month bars
      (~160 a session instead of ~300) until the 09-16 roll. Its 3 008-bar
      context (~10 day sessions) still reached into that thin stretch on 09-29
      and clears after about four more sessions;
    - night's and fullday's contexts (~3.6 and ~2.6 sessions) lie entirely
      after the roll;
    - all three primed with **4** samples, just over the floor of 3.
14. **Old run folders moved.** `outputs/` was renamed to
    `output_backup_260929/` on 2026-09-29; every `outputs/...` path in the plans
    and in AGENTS.md (the autoresearch run folders, `archive_*`) now lives there.
    On 2026-10-27 that folder became `output_backup/pre_simtrade_260929/`, and
    `outputs/` was cut down to live state only: finished loops and reference
    runs (e.g. `stopcmp_20261018_145644`) went to
    `output_backup/outputs_20261027/`. `output_backup/README.md` maps them.
15. **The stops.** Every stop width was chosen on bars it was scored on.
    Night's has no held-out tail at all. Fullday's 0.05 is an **isolated
    peak**: 0.07 falls below no stop on both windows, so a small shift in the
    market could put it on the wrong side. Day's 0.10 is flanked by worse
    cells on both sides (0.08 4.79, 0.12 5.09). Other points:
    - **Gaps:** a stop fills at the next print past its level, so a gap fills
      far beyond the width. Night's 2024-08-05 firing filled at −15 % against
      a 10 % stop.
    - **Re-entry:** a stopped position can be re-entered on the next signal.
      This is where narrow stops lose: day at 0.05 re-entered lower and lost
      about 4 000 points.
    - **Resume:** a position carried into a `--resume` start is judged against
      the current width at once. One already beyond it closes on the first
      tick.
16. **Fullday resumes a held lot only with its lock's best price** (since
    2026-10-29; from 10-27 to 10-29 it could not resume at all). The break-even
    lock tracks the best price since entry, which the venue cannot supply, so
    the lock is not `adoptable`. It is `restorable`: live writes the best price
    to `outputs/exit_state_day_night.json` on every change, and `--resume`
    restores it from there.
    - **No file for this lot** (opened before 2026-10-29, or the file is from
      another lot): `adopt_position` refuses with exit 5 and names the fix:
      `--exit-state best_price=<price>`, the best bar price since the entry
      fill (highest for a long, lowest for a short; the `bar` events in
      `session.jsonl` while `position` is non-zero).
    - **Flattening is no way out on the shared account:** `flatten` closes the
      account's net, which can be 0 while fullday holds (10-29: day −1, fullday
      +1).
    - **Day and night are unaffected:** their percent stops resume as before.
17. **A weekday market holiday can halt a running deployment.**
    `SessionPolicy.holidays` is empty, so a holiday's session reads as open.
    Once a deployment has seen ticks, 900 s of holiday silence halts it with
    `no market data`.
    - **Flat:** stop it before the holiday and restart after.
    - **Fullday holding:** see caution 16.
18. **Fullday's warm-up at stride 10.** The default fetch is 10 calendar days
    (500 boundaries); priming replays up to 900. `outputs/` already holds
    fullday's bar cache back to 09-15, which covers it. If fullday ever stops
    with `emitter priming incomplete`, re-run it with `--warmup-days 15`.
19. **Hybrid has never run live.** Everything it depends on is tested offline
    (`autoresearch_trading_plan_hybrid_split07.md`, "Going live"), not on a
    session.
    - **Credential:** `config/secrets.toml` needs a `[hybrid]` section; it may
      reuse another section's keys.
    - **Warm-up:** each half's trend filter must prime on its own (3 samples).
      The night half's band is 0.0007, so it primes more slowly; if hybrid
      stops with `emitter priming incomplete`, raise `--warmup-days`.
    - **The 12:45 roll** re-opens through the day half's open block (`rolling()`,
      `5fd2d3d`); it has never rolled live.
    - **`--resume`** adopts into both halves; with no exit rule, nothing blocks
      a restart into a held lot.
    - **VRAM:** four Toto2 copies are about 17–21 GiB of the 32 GiB card; check
      `nvidia-smi` after its first start.

## How to run

Run everything from the repo root (`night/`), in **five separate terminals**.
Each process runs until Ctrl-C or a safety halt, and idles outside its own
session, so all of them can start together before the 08:45 open — night simply
waits for 15:00.

**0. The account must be flat.** `--fresh-start` and the owner both refuse
otherwise.

```powershell
uv run --no-sync python -m forecast_eval.live.flatten --dry-run   # read-only
uv run --no-sync python -m forecast_eval.live.flatten             # only if not flat, in session hours
```

**0b. Into a fresh `outputs/`, seed the day and night bar caches first.** Skip
this when `outputs/` already holds `bars_cache_day.csv` / `bars_cache_night.csv`
from an earlier run — every start-up merges the cache with the broker's bars and
rewrites it. There is no seeder for fullday (`day_night`); its 13-day window
fits in what the broker serves. Hybrid trades the same sessions, so a first
hybrid start can take fullday's history by copying
`outputs/bars_cache_day_night.csv` to `outputs/bars_cache_hybrid.csv`.

```powershell
uv run --no-sync python -m forecast_eval.live.seed_cache --data "\\DESKTOP-SQUIRRE\dataset\RPT\TXF_RPT_minute_day.csv"   --out outputs/bars_cache_day.csv   --session day
uv run --no-sync python -m forecast_eval.live.seed_cache --data "\\DESKTOP-SQUIRRE\dataset\RPT\TXF_RPT_minute_night.csv" --out outputs/bars_cache_night.csv --session night
```

**1. The owner — start it FIRST.** It is the only process that places orders; a
client whose owner has never spoken halts.

```powershell
uv run --no-sync python -m forecast_eval.live.owner_cli --out outputs --i-understand-this-places-orders
```

**2–5. The four deployments**, each sending its orders through the owner:

```powershell
uv run --no-sync python -m forecast_eval.live.cli --strategy day     --broker client --fresh-start --i-understand-this-places-orders
uv run --no-sync python -m forecast_eval.live.cli --strategy night   --broker client --fresh-start --i-understand-this-places-orders
uv run --no-sync python -m forecast_eval.live.cli --strategy fullday --broker client --fresh-start --i-understand-this-places-orders
uv run --no-sync python -m forecast_eval.live.cli --strategy hybrid  --broker client --fresh-start --i-understand-this-places-orders
```

Each writes to `outputs/live_<slug>_<timestamp>/` (slugs `day`, `night`,
`day_night`, `hybrid`), and its fill ledger `fills_<slug>.csv` to `outputs/` — the folder
the owner's `--out` names. Every console prints the account's `person_id` (a
national ID); read before pasting output anywhere.

## How to stop

Ctrl-C each deployment, then the owner last. Then one of:

- **Flatten** — run `live/flatten.py` (in session hours). The next run starts
  again with `--fresh-start`.
- **Keep the positions** — leave them open and restart later with `--resume` in
  place of `--fresh-start`. This needs `outputs/` intact: the fill ledgers, soak
  epochs and per-deployment state files live there, and the owner reconciles the
  account against the ledgers' sum at start-up.

**Across a contract expiry** (October settles 10-21; the deployments roll on
10-20): the bar cache is written only at start-up, and an expired month can no
longer be fetched. Restart each deployment (`--resume` if it holds a position)
shortly before the expiry, so the cache takes the old month's bars while the
broker still serves them.
