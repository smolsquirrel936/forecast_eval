# strategy/ — what `simtrade_0929` trades

The three session packages (`day/`, `night/`, `fullday/`) as they stand on the
local branch `simtrade_0929`, forked from `main` @ `f830f7d` on 2026-09-29 for
the three-session simulation trade. The branch is **not** meant to merge into
`main`. The plan and every decision behind it: `../SIMTRADE_0929_PLAN.md`.

Live builds each deployment from its package's `wiring.py:make_strategy`
(LIVE_STRATEGY_ROUTING_PLAN D2), so **the `PARAMS` below are what the simtrade
places orders on.** `live/config.py` holds only the provenance record.

## Shared by all three

| | |
|---|---|
| Forecaster | Toto2-1B (`Datadog/Toto-2.0-1B`), context 3008 bars, horizon 30 bars |
| Emitter | `ThresholdEmitter`, trend filter + path momentum on, `full_confidence_prob` 0.9 |
| Trader | limit orders at `aggression_ticks` 0, `max_position` 1, no-flip, no pyramiding |
| Exit rule | none — positions close only on an opposite signal |
| `forced_close` | `False` — positions carry across sessions, nights and weekends |

## Day — `day_split_iter02`

| Knob | Value |
|---|---|
| band | buy +0.001 / sell −0.001 |
| `min_prob` | 0.65 |
| `stride_bars` | 30 |
| sides | long and short |

- **Source:** `autoresearch_split_0.7_day` @ `58e81b0`, the split loop's
  **baseline**. Its code is identical to `main` — iter01 and iter02 only added
  ledger rows — so this branch carries no day change at all.
- **Score:** Calmar **5.1186**, 79 trades, on the **train 70 %** of the day eval
  window (split at 2021-12-02 13:23).
- **Rationale:** the band and gate are the W5b day search's cell
  (DAY_SESSION_PLAN), restored exactly by the 2026-09-14 reset
  (DAYNIGHT_TOTO2_RESET_PLAN). The loop's later keeps — `min_prob` 0.60
  (iter06, 5.3356) and a same-day loss cooldown (iter08, 5.6063) — are **not**
  taken.
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
- **Live source record:** `PINNED` has `source_commit`/`source_calmar` `None`
  (the row is in `autoresearch_night_1004`'s `results_night.tsv`, not
  `results.tsv`); the banner prints `UNTUNED calmar=nan` and `source_note` names
  the commit.
- **Mid-soak swap.** Night's soak (`soak_20260929_082008_180784`) ran
  `night_iter10` until this change. Its earlier run folders record the old
  `strategy_sha`, which `live/status_report.py` shows as differing from the
  package on disk.

## Fullday — `fullday_split_iter07`

| Knob | Value |
|---|---|
| band | buy +0.001 / sell −0.001 |
| `min_prob` | **0.50** |
| `stride_bars` | **15** |
| sides | long and short |

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
- **Live source record:** `PINNED_FULLDAY` stays `None`/`None`; its note and
  `wiring.py` docstring now name this commit instead of "UNTUNED, DAY's band".

## Cautions

1. **The three Calmars are not comparable.** Day and fullday are train-70 %
   figures; night's is whole-window. Different windows, different tapes.
2. **Day's and fullday's held-out 30 % has never been scored**
   (TRAIN_TEST_SPLIT_PLAN §4.4). This simtrade is their first out-of-sample
   exposure.
3. **Night was tuned and scored on the same bars** — every bar the Calmar covers
   was seen by the search that chose the settings. Five keeps out of thirty
   tries is how overfitting looks, and nothing here rules it out.
4. **Night trades both sides** since `night_iter30` (it was long-only under
   `night_iter10`). Its shorts lost points on their own in the search (−3 338 on
   iter13) but cutting them cost more drawdown than it saved.
5. **Fullday's probability gate is nearly idle at 0.50.** It still rejects a
   signal whose own direction has probability under one half (`emitter.py:180`),
   but moving 0.55 → 0.50 left the trade count at 134 — the loop's "gate
   vacuous". Fullday also has **no stop-loss** (that arrived at iter09, not taken).
6. **Positions are long-lived.** `forced_close=False` everywhere; W5b measured a
   median hold of **20 days** for day's settings. Positions ride through the
   other sessions and weekends unmanaged.
7. **The account can hold up to 3 lots in one direction** — fullday overlaps
   both day and night, and the owner nets them into one account book. No
   `--max-daily-loss-points` and no `--kill-file` are set (decided 2026-09-29).
8. **Three deployments under the owner is untested.** STRATEGY_SPLIT_PLAN §23
   lifted the one-instance rule for two deployments, one lot each, ten minutes.
   Three for full sessions has never run; this simtrade is that test.
9. **Data allowance.** Four logins subscribe the same tick stream; what that
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
12. **Re-logins and the 5-connection cap (accepted 2026-09-29).** Shioaji
    allows 5 connections per `person_id`; the owner plus three deployments hold
    4. Each process re-logs in before every open with `force_refresh=True`, which
    opens a new connection ~30 s before the old one drops. The owner goes first
    (08:37, 14:52); **the three deployments go in the same second (08:40,
    14:55)**, so up to 7 connections can exist briefly. What Shioaji does above 5
    has never been measured: a refused login retries 60 s later, but an evicted
    connection could be the owner's. Watch the consoles at 08:40–08:45 and
    14:55–15:00 for `VERIFY_FAILED` or `relogin` failures. Logins per day (~30)
    are far under the 1 000/day cap.
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

## How to run

Run everything from the repo root (`night/`), in **four separate terminals**.
Each process runs until Ctrl-C or a safety halt, and idles outside its own
session, so all four can start together before the 08:45 open — night simply
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
fits in what the broker serves.

```powershell
uv run --no-sync python -m forecast_eval.live.seed_cache --data "\\DESKTOP-SQUIRRE\dataset\RPT\TXF_RPT_minute_day.csv"   --out outputs/bars_cache_day.csv   --session day
uv run --no-sync python -m forecast_eval.live.seed_cache --data "\\DESKTOP-SQUIRRE\dataset\RPT\TXF_RPT_minute_night.csv" --out outputs/bars_cache_night.csv --session night
```

**1. The owner — start it FIRST.** It is the only process that places orders; a
client whose owner has never spoken halts.

```powershell
uv run --no-sync python -m forecast_eval.live.owner_cli --out outputs --i-understand-this-places-orders
```

**2–4. The three deployments**, each sending its orders through the owner:

```powershell
uv run --no-sync python -m forecast_eval.live.cli --strategy day     --broker client --fresh-start --i-understand-this-places-orders
uv run --no-sync python -m forecast_eval.live.cli --strategy night   --broker client --fresh-start --i-understand-this-places-orders
uv run --no-sync python -m forecast_eval.live.cli --strategy fullday --broker client --fresh-start --i-understand-this-places-orders
```

Each writes to `outputs/live_<slug>_<timestamp>/` (slugs `day`, `night`,
`day_night`), and its fill ledger `fills_<slug>.csv` to `outputs/` — the folder
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
