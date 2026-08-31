"""Soak epoch — the operator's explicit choice for what a restart carries
forward, beyond the position `LiveRuntime.adopt_position` already recovers.

SHIOAJI_PLAN.md's "Open items" §3 (2026-08-24) named the gap this closes:
`adopt_position` runs unconditionally on every launch with no way to ask it
not to, and every launch got a fresh `session.jsonl` with nothing linking it
to the run before it. That left exactly two scenarios operators actually have
— "all bugs fixed, start clean" and "the process died mid-soak, keep going" —
indistinguishable at start-up.

**`--fresh-start` and `--resume` (`cli.py`) are the operator's explicit
choice, not an inference.** This module only tracks the *identity* of the
current soak — a `soak_id` that `--resume` carries forward and
`--fresh-start` replaces — so that `session.jsonl`'s `meta` record and any
future `list_profit_loss` reconciliation can tell which restart belongs to
which soak. It does not touch the position (that stays `adopt_position`'s
job, unconditionally, on every launch either way) and it never flattens
anything — `flatten.py` remains the only path that places a closing order.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class SoakEpoch:
    # Tech: the identity of one continuous soak, however many restarts it
    #       spans — a timestamp-derived id plus when it first began.
    # Why:  the id is derived from the clock, not a random uuid, so a human
    #       reading `session.jsonl`'s `meta` record can tell *when* a soak
    #       started without a second lookup.
    soak_id: str
    started_at: str


def _new(now: datetime) -> SoakEpoch:
    # Tech: microsecond, not second, resolution in the id.
    # Why:  MEASURED 2026-08-31 — two `resolve()` calls in the same wall-clock
    #       second (a manual sanity check, not even an automated one) minted
    #       identical ids, so a fresh-start could silently retire an epoch into
    #       one indistinguishable from itself. A human restart is seconds to
    #       minutes apart at the least, so microseconds cost nothing and close
    #       the collision the mechanism exists to prevent.
    return SoakEpoch(soak_id=f"soak_{now:%Y%m%d_%H%M%S_%f}",
                     started_at=now.isoformat())


def load(path: Path) -> Optional[SoakEpoch]:
    """The epoch currently on disk, or `None` if there isn't one (yet)."""
    # Tech: read the JSON file; a missing or corrupt file both read as "no
    #       epoch on disk" rather than raising.
    # Why:  a missing file is the ordinary first-ever-launch case. A corrupt
    #       one must not crash a restart that only wants to resume a
    #       position — `resolve` treats both the same way, by minting one.
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or "soak_id" not in data:
        return None
    return SoakEpoch(soak_id=data["soak_id"], started_at=data.get("started_at", ""))


def _write(path: Path, epoch: SoakEpoch, retired: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"soak_id": epoch.soak_id, "started_at": epoch.started_at,
              "retired": retired}
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def resolve(path: Path, *, fresh_start: bool, now: datetime) -> SoakEpoch:
    """The epoch this launch belongs to, given the operator's explicit choice.

    `fresh_start=True` always mints a new epoch, retiring whatever was on
    disk into the `retired` list rather than discarding it silently.
    `fresh_start=False` (`--resume`) carries the epoch on disk forward
    unchanged, or bootstraps the first-ever epoch if none exists yet — a
    `--resume` with nothing to resume is not an error, it is where every
    soak's history has to start.
    """
    # Tech: read whatever is on disk first; both branches need it, either to
    #       retire it (fresh start) or to return it (resume).
    # Why:  one read keeps `--fresh-start` and `--resume` from disagreeing
    #       about what "currently on disk" means if the file changes between
    #       two reads — there is only one.
    existing = load(path)
    if fresh_start:
        retired: List[Dict[str, Any]] = []
        try:
            retired = json.loads(path.read_text(encoding="utf-8")).get("retired", [])
        except (OSError, ValueError):
            pass
        if existing is not None:
            retired = retired + [{"soak_id": existing.soak_id,
                                  "started_at": existing.started_at,
                                  "retired_at": now.isoformat()}]
        epoch = _new(now)
        _write(path, epoch, retired)
        return epoch
    if existing is not None:
        return existing
    epoch = _new(now)
    _write(path, epoch, [])
    return epoch
