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
from typing import Any, List, Optional, Sequence, Tuple

import pandas as pd

from ..events import FillEvent
from .broker.base import HeldPosition

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


def open_position(fills: Sequence[FillEvent]) -> Optional[HeldPosition]:
    """What a sequence of fills leaves open, with the entry price it opened at.

    None when the sequence closes flat.
    """
    # Tech: walk the fills FIFO, keeping the lots that are still open, then
    #       size-weight their prices.
    # Why:  `net_position` answers "how many", which is all the reconciliation
    #       needs. Adopting from the ledger needs "at what price" as well — the
    #       entry is what the Portfolio realizes PnL against, so a run that
    #       invented one would book a fictional number on the closing fill. This
    #       is the ledger's answer to the same question `broker_position_detail`
    #       answers from the venue, and it returns the same type for that reason.
    #
    #       FIFO rather than average-everything: a sequence that crosses through
    #       flat (short, close, long) must forget the closed side entirely, which
    #       averaging over all rows would not. At `max_position=1` the residual
    #       is always a single lot and the weighting is a no-op — it is written
    #       generally so it does not become wrong if that ever changes.
    lots: List[Tuple[float, int]] = []          # (price, qty), all one side
    side = 0                                    # +1 long, -1 short, 0 flat
    for f in fills:
        direction = 1 if f.side == "BUY" else -1
        qty = int(f.quantity)
        if side == 0 or direction == side:
            side = direction
            lots.append((float(f.fill_price), qty))
            continue
        # Tech: opposite side — consume the open lots oldest-first.
        # Why:  this is the pairing `build_trades` does, and the two must agree
        #       about what is left open or the mirror and the record diverge.
        while qty and lots:
            price, held = lots[0]
            take = min(qty, held)
            qty -= take
            if take == held:
                lots.pop(0)
            else:
                lots[0] = (price, held - take)
        if qty:                                 # crossed through flat
            side = direction
            lots = [(float(f.fill_price), qty)]
        elif not lots:
            side = 0
    if not lots:
        return None
    gross = sum(q for _, q in lots)
    avg = sum(p * q for p, q in lots) / gross
    return HeldPosition(side * gross, avg)


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


def account_net_position(out_parent: Any) -> Optional[int]:
    """Signed lots EVERY deployment's ledger says this account holds.

    `None` when no ledger file exists at all, so a caller can fall back rather
    than read an absence as flat.
    """
    # Tech: sum the signed quantity of every row of every `fills_*.csv` beside
    #       `out_parent`, across all deployments and all soak epochs.
    # Why:  STRATEGY_SPLIT_PLAN §16. MEASURED 2026-09-02 (§14): a deployment's
    #       callback hears only ITS OWN fills, but `list_positions` returns the
    #       COMBINED book. `on_reconcile` compares those two, so two deployments
    #       holding at once halt each other on `mirror != venue` — not because
    #       anything is wrong, but because the operands describe different
    #       things. This is the other operand: what WE believe the account holds
    #       in total. Compared against the venue it is like-for-like again, and a
    #       disagreement means a genuinely missed or double-counted fill, which
    #       is what the rail was written to catch.
    #
    #       ALL soak epochs are counted, not just the live one. `replay()` scopes
    #       to `soak_id` because it is rebuilding one run's *history*; this is
    #       asking what the ACCOUNT holds, and a fill from a retired epoch moved
    #       the position just as much. Retired epochs net to zero in practice —
    #       `--fresh-start` refuses unless the account is already flat — so
    #       including them is both correct and, normally, free.
    #
    #       A fill placed outside this system (by hand, or by another tool) is in
    #       no ledger and will show up as a mismatch. That is intended: it is
    #       exactly the state the rail should refuse to trade through.
    parent = Path(out_parent)
    if not parent.is_dir():
        return None
    total, found = 0, False
    for path in sorted(parent.glob("fills_*.csv")):
        try:
            df = pd.read_csv(path)
        except Exception:                                  # noqa: BLE001
            # Why:  an unreadable ledger must not be silently counted as zero —
            #       that would under-report the position and manufacture the very
            #       mismatch this function exists to remove. Returning None makes
            #       the caller skip reconciliation for this bar instead, which is
            #       the same degradation `broker_position()` already uses when the
            #       venue cannot answer.
            return None
        if not {"side", "quantity"} <= set(df.columns):
            return None
        found = True
        for row in df.itertuples():
            total += (1 if str(row.side) == "BUY" else -1) * int(row.quantity)
    return total if found else None
