# [live/v1_check.py](../../live/v1_check.py)

**Role:** V1's **live half** — do bars built from the tick stream match the venue's own `kbars`?
**Pipeline:** `ticks.csv` → [TickAggregator](bars.md) → bars ⟷ `api.kbars` → [kbars_to_bars](backfill.md) → bars.
**Depends on:** [live/bars.py](bars.md), [live/backfill.py](backfill.md), [live/capture.py](capture.md), [live/config.py](config.md), [live/contracts.py](contracts.md)   **Used by:** run by hand, once per validated session

> **Result 2026-08-11: PASS** — 39/39 closes, zero unmatched minutes, zero volume
> differences, against `outputs/live_20260811_205731`. V1's live half is closed.

```powershell
$env:SHIOAJI_API_KEY="..."; $env:SHIOAJI_SECRET_KEY="..."
uv run python -m forecast_eval.live.v1_check --capture outputs/live_<ts>/ticks.csv
```

Read-only: logs in, fetches history, prints a verdict. **Places no orders and
subscribes to nothing.** Exit `0` on PASS, `1` on FAIL, `2` on bad input.

## Responsibility
V1's offline half is already proven — replaying real RPT bars through
[bars.py](bars.md) reproduces `Environment` event for event. What that cannot
show is whether the *live* tick stream lands on the same bars the venue reports.
The two live sources had never been compared; agreement here closes the loop
transitively, and it tests the sources actually in use.

It has to be done this way because the historical route is impossible: expired
contracts are delisted, so every date `TXF_RPT_minute_day.csv` covers belongs to
a contract that no longer exists (§2.8, Finding D).

> **Not the same check as [v1c_check.py](v1c_check.md).** That one asks whether
> `kbars`' own convention is right — labelling, session span, empty minutes.
> This one takes that as settled and asks whether our tick aggregation agrees.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `replay_capture(ticks, *, policy)` | function | capture → bars, plus the aggregator for its counters |
| `compare_bars(tick_bars, kbar_bars)` | function | pure diff over the tick side's window |
| `format_report(result, *, agg, contract_note)` | function | the printed verdict; pure, so it is testable |
| `DiffResult` | dataclass | `matched`, `close_mismatch`, `volume_mismatch`, `only_in_*`, `passed` |
| `CLOSE_MATCH_REQUIRED` | constant | `0.995` — V1's stated criterion, named once |

## The verdict
**PASS requires both**: ≥99.5 % of minutes matching on `close` exactly, **and**
zero unmatched minutes on either side.

- **The denominator is the union, not the intersection.** A minute one source
  never produced is the most serious disagreement available; scoring against the
  intersection would hide it entirely.
- **An unmatched minute fails outright**, however good the ratio. It is a
  structural difference between the two bar streams, not a near-miss to average
  away — 399 perfect matches must not bury the 400th.
- **Volume is reported but never gates the verdict.** The model is univariate on
  close, so a volume gap cannot change a decision. It is still surfaced: a
  systematic factor (double-counted sides, say) would be a real finding about the
  feed rather than a rounding artefact.

## Key invariants / gotchas
- **Flush at the last captured tick, never past it.** Flushing later closes the
  final *partial* minute and emits a bar the live process never completed — which
  then shows up as a `kbars` mismatch and gets blamed on the venue. This is the
  gotcha the tool exists to make unrepeatable.
- **Replay in file order; do not sort.** The file records arrival order, which is
  what the live aggregator saw. Sorting would repair out-of-order delivery the
  live run had to cope with, so the replay would agree with `kbars` for a reason
  the live path never enjoyed.
- **`kbars` is clipped to the tick window.** It returns whole days while the
  capture is minutes; without clipping every minute outside the run counts as
  missing — true, and meaningless. A consequence worth knowing: `only_in_kbars`
  can only ever contain minutes *inside* the window.
- **The resolved contract is checked against the capture's `code` column**, via
  [contracts.py](contracts.md)'s `describe_contract_match`. `TXFR1` is an alias
  resolving to whatever is front month *now*, not necessarily what it resolved to
  when the capture was taken; diffing across a rollover would compare two
  different futures and blame the aggregator (§2.6 / V1b). It must go through
  that helper rather than comparing `code` directly — an alias reports
  `code='TXFR1'` while the venue stamps ticks `TXFH6`, and the first version of
  this check declared the contract different from *itself*, labelling a perfect
  39/39 diff "meaningless" (§2.18b).
- **The replay uses `flush_grace=0`.** Offline the whole stream is in hand and
  nothing is ever in flight; the 2 s grace is a live-only remedy (§2.16).
- The session comes from `PINNED.policy`, never from a flag — same reasoning as
  [backfill.py](backfill.md)'s missing default.

## Related
[live/bars.py](bars.md) · [live/backfill.py](backfill.md) · [live/capture.py](capture.md) · [live/v1c_check.py](v1c_check.md) · [tests/test_v1_check_helpers.py](../tests.md) · [SHIOAJI_PLAN.md V1](../../SHIOAJI_PLAN.md)
