"""V1 (live half) — do bars built from the tick stream match `kbars`?

Read-only. Logs in, fetches history, prints a verdict. **Places no orders and
subscribes to nothing.**

The offline half of V1 is already proven: replaying real RPT bars through
[bars.py](../docs/live/bars.md) reproduces `Environment` event for event. What
that cannot show is whether the *live* tick stream aggregates to the same bars
the venue itself reports. The two live sources have never been compared:

    ticks  → TickAggregator → bars   (validated offline only)
    kbars  → kbars_to_bars  → bars   (validated by v1c_check)

Agreement here closes the loop transitively, and it tests the sources actually in
use rather than a three-month-old file — which matters because the historical
route is impossible: expired contracts are delisted, so every date
`TXF_RPT_minute_day.csv` covers belongs to a contract that no longer exists
(§2.8, Finding D).

**Not the same check as `v1c_check.py`.** That one asks whether `kbars`' own
convention is right (labelling, session span, empty minutes). This one takes that
as settled and asks whether our tick aggregation lands on the same bars.

Usage (credentials from the environment, never from a file in the repo):

    $env:SHIOAJI_API_KEY="..."; $env:SHIOAJI_SECRET_KEY="..."
    uv run python -m forecast_eval.live.v1_check --capture outputs/live_<ts>/ticks.csv
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional, Tuple

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from forecast_eval.live.backfill import kbars_to_bars              # noqa: E402
from forecast_eval.live.bars import TickAggregator                 # noqa: E402
from forecast_eval.live.capture import read_ticks                  # noqa: E402
from forecast_eval.live.config import PINNED                       # noqa: E402
from forecast_eval.live.contracts import (                         # noqa: E402
    describe_contract_match, resolve_txf,
)
from forecast_eval.live.sessions import SessionPolicy              # noqa: E402

# Tech: the fraction of minutes whose close must agree for V1's live half to pass.
# Why:  SHIOAJI_PLAN.md V1 — "≥99.5 % of minutes match on close exactly; every
#       mismatch explained". Kept as a named constant so the criterion is stated
#       once and the report and the exit code cannot drift apart.
CLOSE_MATCH_REQUIRED = 0.995


@dataclass(frozen=True)
class DiffResult:
    """What the two live bar sources agree and disagree about, over one window."""

    window: Tuple[Any, Any]
    matched: int = 0
    close_mismatch: List[tuple] = field(default_factory=list)
    volume_mismatch: List[tuple] = field(default_factory=list)
    only_in_ticks: List[Any] = field(default_factory=list)
    only_in_kbars: List[Any] = field(default_factory=list)

    @property
    def compared(self) -> int:
        # Tech: every minute either source claims inside the window.
        # Why:  the denominator has to be the union, not the intersection — a
        #       minute one side missed entirely is the most serious disagreement
        #       there is, and scoring against the intersection would hide it.
        return (self.matched + len(self.close_mismatch)
                + len(self.only_in_ticks) + len(self.only_in_kbars))

    @property
    def close_match_ratio(self) -> float:
        return self.matched / self.compared if self.compared else 0.0

    @property
    def passed(self) -> bool:
        # Tech: the ratio threshold AND zero unmatched minutes on either side.
        # Why:  a missing minute is not a near-miss to be averaged away. One
        #       source seeing a minute the other never produced means the bar
        #       streams are structurally different, however good the ratio looks.
        return (self.close_match_ratio >= CLOSE_MATCH_REQUIRED
                and not self.only_in_ticks and not self.only_in_kbars)


def replay_capture(ticks: pd.DataFrame, *,
                   policy: SessionPolicy) -> Tuple[pd.DataFrame, TickAggregator]:
    """Rebuild bars from a capture, exactly as the live loop would have."""
    # Tech: push every captured tick through a fresh aggregator in file order,
    #       then flush at the timestamp of the LAST tick — never beyond it.
    # Why:  two deliberate choices, both learned the hard way.
    #
    #       *File order, not sorted order.* The file records arrival order, which
    #       is what the live aggregator saw. Sorting would repair out-of-order
    #       delivery that the live run had to cope with, making the replay agree
    #       with `kbars` for a reason the live path never enjoyed.
    #
    #       *Flush at the last tick, not past it.* Flushing later closes the
    #       final, partial minute and emits a bar the live process never
    #       completed — a fabricated row that would then show up as a `kbars`
    #       mismatch and be blamed on the venue.
    #
    #       The aggregator keeps its default `flush_grace=0`: offline the whole
    #       stream is in hand and nothing is ever in flight (§2.16).
    agg = TickAggregator(policy=policy)
    bars: list = []
    for row in ticks.itertuples():
        bars += agg.add_tick(row.timestamp, row.price, row.volume,
                             simtrade=bool(row.simtrade))
    if len(ticks):
        bars += agg.flush(ticks["timestamp"].max())

    frame = pd.DataFrame({
        "timestamp": [b.timestamp for b in bars],
        "price": [float(b.price) for b in bars],
        "volume": [int(b.volume) for b in bars],
    })
    return frame, agg


def compare_bars(tick_bars: pd.DataFrame, kbar_bars: pd.DataFrame) -> DiffResult:
    """Diff two bar frames over the window the tick side covers."""
    if tick_bars.empty:
        return DiffResult(window=(None, None))

    # Tech: clip the kbars side to the closed interval the ticks cover.
    # Why:  `kbars` returns whole days; the capture is minutes. Comparing the
    #       full day would report every minute outside the capture as "missing
    #       from ticks", which is true and meaningless.
    lo, hi = tick_bars["timestamp"].min(), tick_bars["timestamp"].max()
    kb = kbar_bars[(kbar_bars["timestamp"] >= lo)
                   & (kbar_bars["timestamp"] <= hi)]

    t_by = {r.timestamp: r for r in tick_bars.itertuples()}
    k_by = {r.timestamp: r for r in kb.itertuples()}

    matched = 0
    close_mismatch: List[tuple] = []
    volume_mismatch: List[tuple] = []
    for ts in sorted(set(t_by) & set(k_by)):
        t, k = t_by[ts], k_by[ts]
        if float(t.price) == float(k.price):
            matched += 1
        else:
            close_mismatch.append((ts, float(t.price), float(k.price)))
        # Tech: volume is collected but never gates the verdict.
        # Why:  the model is univariate on close, so a volume difference cannot
        #       change a decision. It is still worth surfacing — a systematic
        #       factor (double-counted sides, say) would be a real finding about
        #       the feed rather than a rounding artefact.
        if int(t.volume) != int(k.volume):
            volume_mismatch.append((ts, int(t.volume), int(k.volume)))

    return DiffResult(
        window=(lo, hi),
        matched=matched,
        close_mismatch=close_mismatch,
        volume_mismatch=volume_mismatch,
        only_in_ticks=sorted(set(t_by) - set(k_by)),
        only_in_kbars=sorted(set(k_by) - set(t_by)),
    )


def format_report(result: DiffResult, *, agg: Optional[TickAggregator] = None,
                  contract_note: str = "") -> str:
    """The human-readable verdict; pure, so tests can assert on it."""
    lo, hi = result.window
    out = [
        "",
        "=" * 70,
        "V1 (live half) — ticks -> bars   vs   kbars -> bars",
        "=" * 70,
        f"window          {lo} .. {hi}",
        f"minutes compared {result.compared} (union of both sources)",
    ]
    if agg is not None:
        out.append(f"ingest           {agg.n_ticks} ticks, "
                   f"{agg.n_simtrade_dropped} simtrade dropped, "
                   f"{agg.n_late_dropped} late dropped, "
                   f"{agg.n_bars_dropped_off_session} bars off-session")
    if contract_note:
        out.append(f"contract         {contract_note}")

    pct = 100.0 * result.close_match_ratio
    out += [
        "",
        f"[{'PASS' if result.close_match_ratio >= CLOSE_MATCH_REQUIRED else 'FAIL'}] "
        f"close match {result.matched}/{result.compared} = {pct:.2f}% "
        f"(need {CLOSE_MATCH_REQUIRED * 100:.1f}%)",
        f"[{'PASS' if not result.only_in_ticks and not result.only_in_kbars else 'FAIL'}] "
        f"unmatched minutes: {len(result.only_in_ticks)} only in ticks, "
        f"{len(result.only_in_kbars)} only in kbars",
        f"[INFO] volume differs on {len(result.volume_mismatch)} minute(s) "
        f"— not part of the verdict; the model reads close only",
    ]

    for label, rows in (("close mismatches", result.close_mismatch),
                        ("volume mismatches", result.volume_mismatch)):
        if rows:
            out.append(f"\n  {label} (first 20) — ts, ticks, kbars:")
            out += [f"    {ts}  {a}  {b}" for ts, a, b in rows[:20]]
    for label, rows in (("only in ticks", result.only_in_ticks),
                        ("only in kbars", result.only_in_kbars)):
        if rows:
            out.append(f"\n  {label} (first 20):")
            out += [f"    {ts}" for ts in rows[:20]]

    out += ["", f"VERDICT: {'PASS' if result.passed else 'FAIL'}", ""]
    return "\n".join(out)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--capture", type=Path, required=True,
                    help="ticks.csv from a live run (outputs/live_<ts>/ticks.csv)")
    ap.add_argument("--contract", default="TXFR1")
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    policy = PINNED.policy

    ticks = read_ticks(args.capture)
    if ticks.empty:
        print("ERROR: capture is empty", file=sys.stderr)
        return 2
    tick_bars, agg = replay_capture(ticks, policy=policy)
    if tick_bars.empty:
        print("ERROR: the capture produced no bars in "
              f"{sorted(policy.sessions)} — wrong session or too short a run",
              file=sys.stderr)
        return 2
    print(f"replayed {len(ticks)} ticks -> {len(tick_bars)} bars "
          f"[{tick_bars['timestamp'].min()} .. {tick_bars['timestamp'].max()}]")

    api_key = os.environ.get("SHIOAJI_API_KEY")
    secret_key = os.environ.get("SHIOAJI_SECRET_KEY")
    if not api_key or not secret_key:
        print("ERROR: set SHIOAJI_API_KEY and SHIOAJI_SECRET_KEY.", file=sys.stderr)
        return 2

    import shioaji as sj
    api = sj.Shioaji(simulation=True)
    try:
        api.login(api_key=api_key, secret_key=secret_key)
        contract, resolved_note = resolve_txf(api, args.contract)

        # Tech: check the resolved contract against the codes the capture holds.
        # Why:  §2.6/V1b. `TXFR1` is an alias and resolves to whatever is front
        #       month *now* — not necessarily what it resolved to when the capture
        #       was taken. Diffing across a rollover would compare two different
        #       futures and blame the aggregator. The comparison goes through
        #       `describe_contract_match` because an alias reports `code='TXFR1'`
        #       while the venue stamps ticks `TXFH6`: matching on `code` alone
        #       flags a contract as different from itself.
        match_note, mismatched = describe_contract_match(
            contract, ticks["code"].unique())
        note = f"{resolved_note}; {match_note}"
        print(f"contract: {note}")

        # Tech: request whole days spanning the capture, then let `compare_bars`
        #       clip to the exact minute window.
        # Why:  `kbars` takes dates, not times; a night session crosses midnight,
        #       so both endpoint dates are needed.
        lo, hi = tick_bars["timestamp"].min(), tick_bars["timestamp"].max()
        kb_raw = api.kbars(contract, start=str(lo.date()), end=str(hi.date()))
        kbar_bars = kbars_to_bars(kb_raw, policy=policy)
        print(f"kbars: {len(kbar_bars)} bars in {sorted(policy.sessions)} "
              f"for {lo.date()}..{hi.date()}")
    finally:
        try:
            api.logout()
        except Exception:
            pass

    result = compare_bars(tick_bars, kbar_bars)
    print(format_report(result, agg=agg, contract_note=note))
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())
