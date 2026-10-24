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
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd

from ..events import FillEvent
from .broker.base import HeldPosition

# Tech: the ledger's columns — a FillEvent, plus the soak it belongs to and the
#       dated month it traded (`TXFK6`).
# Why:  the fill fields are spelled exactly as `logging_io.write_fills` spells
#       them, so a ledger row and an artifact row are the same shape and one can
#       be read with the reader written for the other. `soak_id` scopes a replay.
#       `contract` is what lets a restarted owner seed each deployment on the
#       month it holds (2026-10-20, simtrade_operations §11): between a roll and
#       the old month's expiry the account holds two months, and the owner's own
#       `--contract` can name only one. It is the one optional column — ledgers
#       written before it have none (`stamp_months`).
LEDGER_COLUMNS = ["timestamp", "side", "fill_price", "quantity", "fee", "soak_id",
                  "contract"]
_REQUIRED_COLUMNS = LEDGER_COLUMNS[:-1]


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


def _fill(r: Any) -> FillEvent:
    # Tech: one ledger row, as read by `itertuples`, back into a `FillEvent`.
    # Why:  shared by both replays so they cannot rebuild a row two ways.
    return FillEvent(timestamp=pd.Timestamp(r.timestamp), side=str(r.side),
                     fill_price=float(r.fill_price), quantity=int(r.quantity),
                     fee=float(r.fee))


class FillLedger:
    """Append this deployment's fills; replay the current soak's on resume."""

    def __init__(self, path: Any, *, soak_id: Optional[str] = None,
                 contract: Optional[str] = None):
        self.path = Path(path)
        self.soak_id = soak_id
        # Tech: the dated month stamped on every row `sync` appends.
        # Why:  the deployment sets it at start-up and moves it at a roll
        #       (`cli._apply_roll`), after flushing the old month's fills, so
        #       each row carries the month it traded.
        self.contract = contract
        self._written = 0
        self.replayed = 0
        self.appends = 0
        self.failures = 0

    def replay(self) -> List[FillEvent]:
        # Tech: read the ledger, keep rows whose `soak_id` matches this run's,
        #       and rebuild them as `FillEvent`s in file order.
        # Why:  file order is fill order — the ledger is append-only and
        #       `build_trades` pairs chronologically, so re-sorting could only
        #       introduce a difference.
        df = self._frame()
        if df is None:
            return []
        fills = [_fill(r) for r in df.itertuples()]
        self.replayed = len(fills)
        self._written = len(fills)
        return fills

    def replay_by_month(self) -> Dict[str, List[FillEvent]]:
        """`replay`, split by the dated month each row traded; `''` for none."""
        # Tech: the same rows as `replay`, grouped by `contract` in file order.
        # Why:  a lot on one month is never closed by a fill on another, so what
        #       a deployment holds has to be reduced month by month — the owner
        #       seeds a restart this way (`OwnerProcess.seed_from`). Rows from
        #       before the column existed group under `''`.
        df = self._frame()
        if df is None:
            return {}
        codes = (df["contract"].fillna("").astype(str) if "contract" in df
                 else pd.Series("", index=df.index))
        out: Dict[str, List[FillEvent]] = {}
        for code, r in zip(codes, df.itertuples()):
            out.setdefault(code, []).append(_fill(r))
        return out

    def _frame(self) -> Optional[pd.DataFrame]:
        # Tech: the ledger's rows for this soak as a frame, or None when there
        #       are none to replay.
        # Why:  a missing, empty or unreadable ledger returns nothing: this is
        #       the bar cache's rule ("an optimisation, never a dependency"),
        #       and the first resume after this shipped has no ledger at all,
        #       which must be ordinary rather than an error.
        if not self.path.exists():
            return None
        try:
            df = pd.read_csv(self.path, dtype={"soak_id": str, "contract": str})
        except Exception:                                  # noqa: BLE001 — see Why
            self.failures += 1
            return None
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
        if not set(_REQUIRED_COLUMNS) <= set(df.columns):
            self.failures += 1
            return None
        if df.empty:
            return None
        if self.soak_id is not None:
            df = df[df["soak_id"].astype(str) == str(self.soak_id)]
        return df

    def stamp_months(self, code: str) -> int:
        """Give every row with no month `code`; return how many were stamped."""
        # Tech: rewrite the file with a `contract` column, filling each empty
        #       cell with `code`; a ledger already stamped throughout is left
        #       alone. Written to a temporary file and swapped in.
        # Why:  ledgers from before the column have none, and leaving those rows
        #       unstamped breaks the per-month reduction the first time a lot
        #       opened in them is closed by a stamped row. The deployment calls
        #       this at start-up with the month it holds, which is the month of
        #       any lot still open in those rows; closed pairs net to zero on any
        #       one month. A failure is counted and left for the next start.
        if not self.path.exists() or not code:
            return 0
        try:
            with self.path.open(newline="", encoding="utf-8") as fh:
                rows = list(csv.reader(fh))
            if not rows:
                return 0
            header, body = rows[0], rows[1:]
            if "contract" not in header:
                header = header + ["contract"]
            at = header.index("contract")
            stamped = 0
            for row in body:
                row.extend([""] * (len(header) - len(row)))
                if not row[at]:
                    row[at] = code
                    stamped += 1
            if stamped == 0 and len(rows[0]) == len(header):
                return 0
            tmp = self.path.with_name(self.path.name + ".tmp")
            with tmp.open("w", newline="", encoding="utf-8") as fh:
                csv.writer(fh).writerows([header] + body)
            os.replace(tmp, self.path)
        except Exception:                                  # noqa: BLE001
            self.failures += 1
            return 0
        return stamped

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
            # Tech: a ledger whose header has no `contract` column is stamped
            #       first when this ledger knows its month, and otherwise kept
            #       to the old six columns.
            # Why:  a seven-field row under a six-field header is a file pandas
            #       refuses to read, which would cost every reader of it.
            months = True
            if exists and "contract" not in self._header():
                if self.contract:
                    self.stamp_months(self.contract)
                months = "contract" in self._header()
            with self.path.open("a", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                if not exists:
                    w.writerow(LEDGER_COLUMNS)
                for f in new:
                    row = [f.timestamp, f.side, f.fill_price, f.quantity,
                           f.fee, self.soak_id or ""]
                    w.writerow(row + [self.contract or ""] if months else row)
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

    def _header(self) -> List[str]:
        with self.path.open(newline="", encoding="utf-8") as fh:
            return next(csv.reader(fh), [])

    def summary(self) -> dict:
        return {"fills_replayed": self.replayed,
                "fills_appended": self.appends,
                "ledger_failures": self.failures}


def per_deployment_nets(out_parent: Any) -> Dict[str, int]:
    """`{slug: signed lots}` from each `fills_<slug>.csv`. Empty when unreadable.

    The account total is `account_net_position`; this is the same rows split by
    who owns them.
    """
    # Tech: read each ledger separately and key the result by the slug in its
    #       filename.
    # Why:  STRATEGY_SPLIT_PLAN §18.3 item 8. The venue can only say what the
    #       ACCOUNT holds; these files are the only record of WHO holds what
    #       (§16), which is what turns "closing 2 lots" into "closing day's 1 and
    #       night's 1" for an operator about to flatten. The CSV-reading rule
    #       lives here beside `account_net_position` rather than being spelled a
    #       second time at the call site, because two readers of one format are
    #       how they drift.
    #
    #       An unreadable or malformed ledger is SKIPPED rather than returning
    #       None for the whole set: this feeds a warning, and naming two of three
    #       deployments is more useful than naming none. `account_net_position`
    #       refuses instead, correctly — it feeds a safety rail, where a partial
    #       sum would be a confident wrong number.
    parent = Path(out_parent)
    if not parent.is_dir():
        return {}
    nets: Dict[str, int] = {}
    for path in sorted(parent.glob("fills_*.csv")):
        try:
            df = pd.read_csv(path)
        except Exception:                                  # noqa: BLE001
            continue
        if not {"side", "quantity"} <= set(df.columns):
            continue
        total = 0
        for row in df.itertuples():
            total += (1 if str(row.side) == "BUY" else -1) * int(row.quantity)
        nets[path.stem[len("fills_"):]] = total
    return nets


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
