# [tools/build_minute_bycontract.py](../../tools/build_minute_bycontract.py)

**Role:** rebuild the evaluated tape's *source* from raw TAIFEX files, stopping **before** the front-month choice.
**Pipeline:** `dataset/RPT/extracted_data/Daily_*.rpt` (3 832 files, ~55 GB) → `TXF_RPT_minute_bycontract.csv` (4 551 359 bars, 0.27 GB) → `front_month_series(ts, policy)` → the three session tapes.
**Depends on:** nothing in the package (pandas only)   **Used by:** run by hand, once per rebuild — `TAPE_REBUILD_PLAN.md` T1

```powershell
uv run python -m forecast_eval.tools.build_minute_bycontract `
  --src D:\...\dataset\RPT\extracted_data `
  --out D:\...\dataset\RPT\TXF_RPT_minute_bycontract.csv
```

Offline and read-only with respect to the venue. ~28 minutes. Take a copy of the
existing CSVs first — a full rebuild cannot be undone with `truncate`.

## Responsibility

The shipped pipeline (`process_txf.ipynb` cell 7) picks the front month with
`min(contract)` per `trading_date`, *before* writing anything downstream. That
bakes **one** splice instant into the tick file and every tape derived from it.

`TAPE_REBUILD_PLAN.md` §2.3 needs **two** — day and the combined full-day tape
splice at 12:45, night at 15:30 — and none of the three tapes is a session-split
of another. A single front-filtered series cannot express that. So the expensive
pass stops one step earlier: it keeps the **two nearest listed months** and
aggregates to minute bars *per contract*, leaving the choice to
`live/roll.front_month_series`.

The payoff is that a fourth deployment later is a filter over 0.27 GB, not a
55 GB rebuild — which matters now that `TAPE_REBUILD_PLAN.md` D2 anticipates one.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `read_rpt(path)` | function | one raw file → TX prints with a wall-clock `datetime`, spreads dropped, two nearest months kept |
| `to_minute_bars(tx)` | function | minute OHLCV per `(minute, contract)`, left-labelled |
| `main(argv)` | function | CLI: walk the files, write, then dedupe and sort |
| `N_NEAREST` | constant | `2` — how many delivery months to carry |

## Four traps it exists to remember

- **`trading_date` is the calendar date of the print, not the session's trading
  day.** Verified across three consecutive files: `Daily_2026_08_18.rpt` carries
  Monday's night head as `20260817 15:00–23:00` and its tail as
  `20260818 00:00–05:00`. So `trading_date + time` is the correct wall-clock
  instant with no midnight adjustment — and grouping by `trading_date` is
  therefore a roll at the **00:00 calendar boundary**, which is the defect
  `dataset/NOTE.md` describes.
- **There is no sub-second precision anywhere.** Files before ~2015 are
  `HHMMSSCC` with `CC` always `00`; later ones are plain `HHMMSS`. Several
  prints share a key, so a minute's open/close are decided by row order alone.
  This module stable-sorts and preserves raw file order — the exchange's
  dissemination sequence, and the only ordering information that exists. The
  shipped tape used `sort_index()`, whose default `quicksort` is **not stable**,
  so its open/close are arbitrary and not reproducible (Defect C).
- **Some raw files carry a stale copy of an earlier session.**
  `Daily_2026_07_13.rpt` repeats 2026-07-09's night block byte-for-byte, and
  2026-07-10's night session is absent from every file as a result. The notebook
  summed both copies, so the shipped tape's volume is doubled — or tripled —
  across 12 dates back to 2019-09-27 (Defect D). The dedupe pass here asserts the
  duplicates are byte-identical *before* collapsing them, so two files
  disagreeing would surface rather than be resolved by `keep="first"`.
- **The two nearest months are enough, and far months are not.** Measured on a
  roll date: 86 903 of 87 007 TX prints (**99.88 %**) are the front and incoming
  months. Any splice rule between two adjacent months needs both present; nothing
  needs the rest.

## Verification

Applying the **old** `min(contract)` rule to the intermediate reproduces the
shipped tape's **2 933 058 bars exactly** — 0 missing, 0 extra — with `high` and
`low` differing on **0 %**. Everything that does differ is Defect C or D, and
both are corrections:

| field | differs | why |
|---|---|---|
| `high` / `low` | **0 %** | order-independent |
| `close` | 7.59 % | Defect C — 100 % of both values sit inside the bar's own high/low |
| `open` | 16.42 % | Defect C |
| `volume` | 0.172 % | Defect D — shipped/rebuilt ratio exactly 2.000–3.000 |
