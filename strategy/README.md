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

## Night — `night_iter10`

| Knob | Value |
|---|---|
| band | buy **+0.00065** / sell −0.00045 |
| `min_prob` | **0.55** |
| `min_trend_ticks` | **12** (class default 3) |
| `long_only` | **True** — a SELL while flat opens nothing; a SELL while long still closes |
| `stride_bars` | 30 |

- **Source:** `autoresearch_night_0918` @ `6159a4c`, plus `4bf33063c` (fixes the
  iter08 knob test to pin the threading, not the value). The branch forked from
  `ff3a601`, six commits behind `main`; applied here as one commit whose code is
  identical to the branch apart from `main`'s docstring hash lines.
- **Score:** Calmar **6.5793**, 144 trades, on the **whole** night eval window
  (the unsplit loop — there is no held-out tail).
- **Rationale (the keeps, in order):**

  | iter | change | Calmar |
  |---|---|---|
  | 01 | baseline | 3.4962 (235 trades) |
  | 02 | `long_only` — the baseline's 88 shorts lost 3 248 pts while its 147 longs made 26 894 | 4.5025 |
  | 05 | buy band 0.00045 → 0.0006 — drops marginal entries whose re-entry paths were the drag | 5.3176 |
  | 06 | `min_prob` 0.5 → 0.55 — cuts entries with `dir_prob` in [0.5, 0.55) | 5.4405 |
  | 07 | buy band → 0.00065 | 5.9531 |
  | 08–10 | `min_trend_ticks` 3 → 20 → 15 → 12 — the trend filter demands a real early→late move | 6.2542 → 6.4202 → 6.5793 |

  Discarded along the way: stride 15 (iter03), conviction sizing to 3 lots
  (iter04), horizon 60 with √2 bands (iter11).
- **Live source record:** `PINNED` now has `source_commit`/`source_calmar`
  `None` (the row is in `results_night.tsv`, not `results.tsv`); the banner
  prints `UNTUNED calmar=nan` and `source_note` names the commit.

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
   was seen by the search that chose the settings. Eight hand-picked steps is how
   overfitting looks, and nothing here rules it out.
4. **Night is long-only.** It bets the tuning years' uptrend continues; in a
   sustained decline it can only sit flat or exit longs.
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
10. **Night's banner understates its settings.** `LiveConfig.describe()` prints
    `trend=` from the emitter's signature default and one `band=±x`, so night's
    console banner reads `trend=3.0 band=±0.00065` while the stack trades
    `min_trend_ticks` 12 and +0.00065 / −0.00045 (verified 2026-09-29 by building
    it). Live's `params.json` records the true values. A report rebuilt offline by
    `tools/session_artifacts.py` inherits the error (sell −0.00065).
11. **Fullday's warm-up is shorter.** At stride 15 the default is 13 calendar
    days (night 30, day 84). Priming needs 3 non-HOLD signals in the window; if
    fullday stops with `emitter priming incomplete`, re-run it with
    `--warmup-days 30`. It refuses before trading, so a retry is safe.
12. **Old run folders moved.** `outputs/` was renamed to
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
