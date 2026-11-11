"""The owner's own record of who holds what — `owner_positions.json`.

The owner books every deal it routes, so it always knows each deployment's
lots and the total it sizes every Cover against (`OrderBook`). Until 2026-11-11
that record lived only in memory and was rebuilt from the deployments' ledgers
at start-up. A fill the owner never heard (2026-10-30, `19EFD2`) could then be
corrected only by stopping everything, adding the ledger row and restarting,
and a restart after 05:00 left fullday's stop close waiting for Monday's open.

This file is that record on disk. The owner rewrites it whenever the book moves
and reads it back twice:

* at start-up, in place of the ledgers, which it must agree with, and
* while running, after a person has edited it: the correction is applied on the
  next pass, with no restart.

**The total is written beside the per-deployment lots and must equal their
sum, month by month.** It is redundant on purpose. A hand edit has to state the
change twice, so a typo in one place is refused instead of sizing the next
Cover.
"""
from __future__ import annotations

import json
import math
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

from .broker.base import HeldPosition

POSITIONS_NAME = "owner_positions.json"

# Tech: `{(slug, code): signed lots}` and `{slug: entry price}`, the two halves
#       of what the file records.
# Why:  the shapes `OrderBook.holdings` / `entries` answer with, so the book and
#       the file are compared without a conversion in between.
Holdings = Dict[Tuple[str, str], int]
Entries = Dict[str, float]


def positions_path(directory: Any) -> Path:
    """`owner_positions.json` in the owner's `--out`, beside the ledgers."""
    return Path(directory) / POSITIONS_NAME


def render(holdings: Holdings, entries: Entries, slugs: Iterable[str],
           now: Optional[datetime] = None) -> Dict[str, Any]:
    """The file's content for this book state; every served slug is listed."""
    # Tech: one entry per served deployment, flat ones with empty `lots`, and
    #       the total summed month by month from those lots.
    # Why:  listing the flat deployments gives a person editing the file the
    #       place to write a correction, rather than a key to remember.
    total: Dict[str, int] = {}
    deployments: Dict[str, Any] = {}
    for slug in slugs:
        lots = {code: n for (s, code), n in sorted(holdings.items())
                if s == slug and n}
        for code, n in lots.items():
            total[code] = total.get(code, 0) + n
        deployments[slug] = {"lots": lots,
                             "entry": float(entries.get(slug, 0.0)) if lots else 0.0}
    return {"updated_at": (now or datetime.now()).isoformat(timespec="seconds"),
            "total": {code: n for code, n in sorted(total.items()) if n},
            "deployments": deployments}


def write(path: Path, doc: Dict[str, Any]) -> str:
    """Write `doc` atomically and return the text written; raises OSError."""
    # Tech: a sibling temp file, then `os.replace` over the target.
    # Why:  an owner killed mid-write must leave the old record or the new one,
    #       never half of one — a torn file refuses the next start-up. The text
    #       is returned so the owner can tell its own write from a later edit.
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    text = json.dumps(doc, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with tmp.open("w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    os.replace(tmp, path)
    return text


def _lots(where: str, value: Any) -> int:
    # Tech: a whole number of lots, or ValueError naming where it was found.
    # Why:  JSON `true` is an int to Python, and `1.0` would be accepted by
    #       `int()`; a hand edit is exactly where either slips in.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{where}: {value!r} is not a whole number of lots")
    return value


def parse(doc: Any, slugs: Iterable[str]) -> Tuple[Holdings, Entries]:
    """Validate the file's content; ValueError says what is wrong."""
    # Tech: read every deployment's lots and entry, refuse a held deployment
    #       with no entry price or one this owner does not serve, then check
    #       the total against the lots month by month.
    # Why:  the owner applies this to the book that sizes every leg, so a file
    #       it cannot fully account for is refused whole. A held lot needs its
    #       entry because the deployment adopts it with that price, and the
    #       Portfolio realizes PnL against it (`broker/base.HeldPosition`).
    served = set(slugs)
    if not isinstance(doc, dict):
        raise ValueError("does not hold a JSON object")
    deployments, total = doc.get("deployments"), doc.get("total")
    if not isinstance(deployments, dict):
        raise ValueError('has no "deployments" object')
    if not isinstance(total, dict):
        raise ValueError('has no "total" object')
    holdings: Holdings = {}
    entries: Entries = {}
    for slug, body in deployments.items():
        if not isinstance(body, dict) or not isinstance(body.get("lots", {}), dict):
            raise ValueError(f'{slug}: wants {{"lots": {{"<month>": <lots>}}, '
                             f'"entry": <price>}}')
        lots = {}
        for code, n in body.get("lots", {}).items():
            if not code:
                raise ValueError(f"{slug}: a month code is empty")
            if _lots(f"{slug} {code}", n):
                lots[code] = n
        if slug not in served:
            if lots:
                raise ValueError(f"{slug} holds {lots}, but this owner does not "
                                 f"serve {slug}")
            continue
        entry = body.get("entry", 0.0)
        if (isinstance(entry, bool) or not isinstance(entry, (int, float))
                or not math.isfinite(entry) or entry < 0):
            raise ValueError(f"{slug}: entry {entry!r} is not a price")
        if lots and entry <= 0:
            raise ValueError(f"{slug} holds {lots} with no entry price")
        for code, n in lots.items():
            holdings[(slug, code)] = n
        if lots:
            entries[slug] = float(entry)
    totals = {code: _lots(f"total {code}", n) for code, n in total.items()}
    summed: Dict[str, int] = {}
    for (_, code), n in holdings.items():
        summed[code] = summed.get(code, 0) + n
    for code in sorted(set(totals) | set(summed)):
        if totals.get(code, 0) != summed.get(code, 0):
            raise ValueError(f"total {code} is {totals.get(code, 0):+d} but the "
                             f"deployments add up to {summed.get(code, 0):+d}")
    return holdings, entries


def parse_text(text: str, slugs: Iterable[str]) -> Tuple[Holdings, Entries]:
    """`parse` of the file's text; ValueError names the file and the problem."""
    try:
        doc = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"{POSITIONS_NAME} is not valid JSON ({exc})") from None
    try:
        return parse(doc, slugs)
    except ValueError as exc:
        raise ValueError(f"{POSITIONS_NAME}: {exc}") from None


def load(path: Path, slugs: Iterable[str]) -> Optional[Tuple[Holdings, Entries]]:
    """The validated record, None when there is no file; ValueError otherwise."""
    # Tech: a missing file is None; a file that does not parse, or parses into
    #       something `parse` refuses, raises.
    # Why:  no file is the ordinary first start, and the ledgers seed it. A
    #       file that is there but wrong is a mistake to show a person, not a
    #       reason to fall back to another source quietly.
    text = read_text(path)
    return None if text is None else parse_text(text, slugs)


def read_text(path: Path) -> Optional[str]:
    """The file's text, None when there is no file."""
    # Tech: decode as UTF-8, dropping a byte-order mark.
    # Why:  this file is edited by hand on Windows, and Notepad has saved
    #       UTF-8 with a BOM, which `json.loads` refuses.
    try:
        return Path(path).read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return None


def as_seed(holdings: Holdings, entries: Entries,
            slugs: Iterable[str]) -> Dict[str, Dict[str, HeldPosition]]:
    """`{slug: {code: HeldPosition}}` for `OwnerProcess.seed_from`; flat is `{}`."""
    # Tech: every served slug, each month's lots at the slug's entry.
    # Why:  a correction that takes a deployment flat must still name it, or
    #       `seed_from` would leave its old size in place.
    return {slug: {code: HeldPosition(n, entries.get(slug, 0.0))
                   for (s, code), n in sorted(holdings.items()) if s == slug}
            for slug in slugs}


def describe(holdings: Holdings, entries: Entries) -> str:
    """`day TXFK6 -1 @ 47025, night TXFK6 -1 @ 42571`, or `flat`."""
    return ", ".join(f"{slug} {code} {n:+d} @ {entries.get(slug, 0.0):g}"
                     for (slug, code), n in sorted(holdings.items())) or "flat"
