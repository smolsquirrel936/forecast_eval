"""The deployment's fill history, carried across restarts.

A restart adopts the venue's *position* (§2.20) but starts with an empty fill
list, and `build_trades` walks fills from flat. So on a resumed run the first
fill — economically the *close* of an inherited position — was booked as an
open, and every pair after it was shifted: measured on a two-trade example, the
artifacts reported one SHORT of +100 where the truth was two LONGs of +145 and
+300, and the run's last fill vanished entirely. The trading was never affected
(the Portfolio is seeded on adoption, so realized PnL was always right) — only
the record derived from fills.

This module is that record. Each deployment appends its fills to a ledger and
replays them on `--resume`, so a run directory shows the soak's history rather
than one segment of it.

**Keyed by `policy.cache_slug()`, exactly like the bar cache.** DAY and NIGHT
must never read each other's fills: replaying a night trade into a day run would
put the wrong position in `trades.csv` and, through the reconciliation below,
argue with the venue about a lot that belongs to another deployment. C5 was the
same collision one file over, and it is avoided here the same way — the path is
derived from the policy rather than passed in, so a new deployment cannot forget
to change it.

**Scoped to the soak epoch.** Only rows carrying the live `soak_id` are
replayed, which is what makes `--fresh-start` mean a clean slate in the
artifacts as well as in the state file. Retired soaks stay in the file and stay
readable; they are simply not this run's history.

**A ledger is never a dependency.** It is read best-effort and its absence is
normal — the first resume after this module shipped has no ledger at all. Every
failure path falls back to the synthetic single fill in `cli.py`, which keeps
`trades.csv` correctly paired on the position actually held even when no history
can be recovered.
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, List, Optional, Sequence

import pandas as pd

from ..events import FillEvent

# Tech: the ledger's columns — a FillEvent, plus the soak it belongs to.
# Why:  the fill fields are spelled exactly as `logging_io.write_fills` spells
#       them, so a ledger row and an artifact row are the same shape and one can
#       be read with the reader written for the other. `soak_id` is the only
#       addition, and it is what scopes a replay.
LEDGER_COLUMNS = ["timestamp", "side", "fill_price", "quantity", "fee", "soak_id"]


def ledger_path(out_parent: Any, policy: Any) -> Path:
    """`fills_<slug>.csv`, beside the bar cache and keyed the same way."""
    # Tech: derive the filename from the policy's own slug.
    # Why:  C5 — one shared path let a DAY run write into the file a NIGHT run
    #       read back. `cache_slug()` is the existing answer to that, and reusing
    #       it means the two files cannot disagree about what a deployment is.
    return Path(out_parent) / f"fills_{policy.cache_slug()}.csv"


def net_position(fills: Sequence[FillEvent]) -> int:
    """The signed lot count a sequence of fills leaves behind."""
    return sum((1 if f.side == "BUY" else -1) * int(f.quantity) for f in fills)


class FillLedger:
    """Append this deployment's fills; replay the current soak's on resume."""

    def __init__(self, path: Any, *, soak_id: Optional[str] = None):
        self.path = Path(path)
        self.soak_id = soak_id
        self._written = 0
        self.replayed = 0
        self.appends = 0
        self.failures = 0

    def replay(self) -> List[FillEvent]:
        # Tech: read the ledger, keep rows whose `soak_id` matches this run's,
        #       and rebuild them as `FillEvent`s in file order.
        # Why:  file order is fill order — the ledger is append-only and
        #       `build_trades` pairs chronologically, so re-sorting could only
        #       introduce a difference. A missing, empty or unreadable ledger
        #       returns nothing: this is the bar cache's rule ("an optimisation,
        #       never a dependency"), and the first resume after this shipped has
        #       no ledger at all, which must be ordinary rather than an error.
        if not self.path.exists():
            return []
        try:
            df = pd.read_csv(self.path, dtype={"soak_id": str})
        except Exception:                                  # noqa: BLE001 — see Why
            self.failures += 1
            return []
        # Tech: the schema is checked BEFORE emptiness, and only a schema
        #       mismatch counts as a failure.
        # Why:  the order matters, and it was measured. An empty ledger is
        #       ordinary — it is what the first run of a soak leaves — so it must
        #       not raise the failure count. But corruption need not raise
        #       either: `read_csv` is lenient enough to parse a truncated write
        #       into a *header-only* frame with garbage column names, which is
        #       empty and would slip past an emptiness check first. Asking the
        #       more specific question first separates "nothing recorded yet"
        #       from "this is not our file", and both still replay nothing.
        if not set(LEDGER_COLUMNS) <= set(df.columns):
            self.failures += 1
            return []
        if df.empty:
            return []
        if self.soak_id is not None:
            df = df[df["soak_id"].astype(str) == str(self.soak_id)]

        fills = [
            FillEvent(timestamp=pd.Timestamp(r.timestamp), side=str(r.side),
                      fill_price=float(r.fill_price), quantity=int(r.quantity),
                      fee=float(r.fee))
            for r in df.itertuples()
        ]
        self.replayed = len(fills)
        self._written = len(fills)
        return fills

    def mark_written(self, count: int) -> None:
        # Tech: declare that the first `count` entries of the loop's fill list
        #       must never be appended by `sync`.
        # Why:  it covers the two seeding paths, which need the same treatment
        #       for opposite reasons. Replayed fills are *already* in the file,
        #       so re-appending would duplicate every one of them on the first
        #       sync of every resumed run. The synthetic opening fill is
        #       deliberately **not** a ledger row — it is a stand-in for a real
        #       execution this process never saw, and writing it would make the
        #       next resume replay a fill that never happened.
        self._written = max(self._written, int(count))

    def sync(self, fills: Sequence[FillEvent]) -> int:
        """Append every fill past the high-water mark; return how many were added."""
        # Tech: write the tail of `fills` and advance the mark, creating the file
        #       with a header when it does not yet exist.
        # Why:  the mark starts at whatever `replay` handed back, so replayed
        #       fills are not written a second time — the loop's list holds them
        #       and this would otherwise duplicate every one of them on the first
        #       sync of every resumed run. Appending (rather than rewriting) is
        #       what keeps the ledger's cost independent of a soak's length.
        new = list(fills)[self._written:]
        if not new:
            return 0
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            exists = self.path.exists() and self.path.stat().st_size > 0
            with self.path.open("a", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                if not exists:
                    w.writerow(LEDGER_COLUMNS)
                for f in new:
                    w.writerow([f.timestamp, f.side, f.fill_price, f.quantity,
                                f.fee, self.soak_id or ""])
        except Exception:                                  # noqa: BLE001 — see Why
            # Why:  the same rule as the artifacts — this records a live run and
            #       never feeds one, so a locked or full disk must not stop a
            #       process holding a position. The cost of failing is a resumed
            #       run with a shorter history, not a wrong one.
            self.failures += 1
            return 0

        self._written += len(new)
        self.appends += len(new)
        return len(new)

    def summary(self) -> dict:
        return {"fills_replayed": self.replayed,
                "fills_appended": self.appends,
                "ledger_failures": self.failures}
