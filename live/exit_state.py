"""Exit-rule state on disk — what lets a stateful exit rule survive a restart.

`adopt_position` (`runtime.py`) recovers a held lot's side and average entry
from the venue, and nothing else. A rule that reads only those two
(`EntryPercentStop`) is `adoptable`. A rule that carries per-trade state — a
break-even lock's best price since entry — is not: that state lived in the
memory of the process that died, and arming the rule on an invented value is
worse than refusing to start.

This module closes the gap for rules that declare themselves `restorable`
(`exits/base.py`). While live, `ExitStateFile` writes the rule's state to
`exit_state_<slug>.json` beside the other per-deployment state files whenever
it changes. At the next `--resume`, `resolve` hands that state back — checked
against the adopted lot's side and entry, so a file left by a different trade
refuses instead of arming — or takes the operator's typed `--exit-state
KEY=VALUE` instead, for a lot opened before the file existed or a file that
cannot be trusted.
"""
from __future__ import annotations

import json
import math
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

# Tech: how far a saved entry price may sit from the adopted one and still
#       count as the same lot.
# Why:  both are prices on a 1-point tick grid that went through a JSON float
#       round trip; anything beyond rounding noise is a different entry.
ENTRY_TOLERANCE = 1e-6


def parse_injection(items: Iterable[str]) -> Dict[str, float]:
    """`["best_price=47376"]` -> `{"best_price": 47376.0}`; ValueError on a bad item."""
    # Tech: split each KEY=VALUE once, coerce the value to a finite float, and
    #       refuse a repeated key.
    # Why:  this is pure argument work, run before the restart guard like the
    #       CLI's other argument checks. A typed state is the operator
    #       overriding the record, so a malformed or ambiguous one must stop
    #       the launch rather than arm the rule on a guess.
    state: Dict[str, float] = {}
    for item in items:
        key, sep, raw = item.partition("=")
        key = key.strip()
        if not sep or not key:
            raise ValueError(f"--exit-state wants KEY=VALUE, got {item!r}")
        try:
            value = float(raw)
        except ValueError:
            raise ValueError(
                f"--exit-state {key}: {raw!r} is not a number") from None
        if not math.isfinite(value):
            raise ValueError(f"--exit-state {key}: {raw!r} is not finite")
        if key in state:
            raise ValueError(f"--exit-state {key} given twice")
        state[key] = value
    return state


def load(path: Path) -> Optional[Dict[str, Any]]:
    """The record on disk, None when there is no file; ValueError when unreadable."""
    # Tech: a missing file is None; a file that exists but does not parse as
    #       a JSON object raises.
    # Why:  "no file" is the ordinary case for a lot opened before this
    #       mechanism existed, and `resolve` says so. A corrupt file is
    #       different — something went wrong mid-write — and reading it as
    #       "no state" would hide that from the operator.
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        record = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"{path} is not valid JSON ({exc})") from None
    if not isinstance(record, dict):
        raise ValueError(f"{path} does not hold a JSON object")
    return record


def _hint(keys: Tuple[str, ...]) -> str:
    # Tech: the command-line form that would supply every key by hand.
    # Why:  every refusal below ends the same way — the operator's remedy is
    #       to type the state — so the message spells out exactly what to type.
    return "--exit-state " + " --exit-state ".join(f"{k}=<value>" for k in keys)


def resolve(*, keys: Tuple[str, ...], side: str, entry_price: float,
            path: Path, injected: Optional[Dict[str, float]] = None,
            ) -> Tuple[Dict[str, float], str]:
    """The state to restore and where it came from (`"injected"` or `"file"`)."""
    # Tech: a typed state wins outright once its keys match the rule's.
    # Why:  the injection exists for exactly the cases the file cannot cover —
    #       a lot opened before the file was written, or a file the operator
    #       knows is wrong — so it must not be second-guessed by that file.
    #       Exact key equality: a missing key would leave part of the rule on
    #       an invented value, an extra one is most likely a typo.
    if injected:
        if set(injected) != set(keys):
            raise ValueError(
                f"--exit-state gave {sorted(injected)} but the exit rule needs "
                f"exactly {sorted(keys)}")
        return dict(injected), "injected"

    # Tech: otherwise the file — present, for this lot's side and entry, and
    #       carrying exactly the rule's keys.
    # Why:  the file is overwritten per trade, so a record from an earlier lot
    #       can still be on disk (a crash between an entry fill and the first
    #       write, or a roll that moved the venue's average entry). Side and
    #       entry are the lot's identity that both sides can see; a mismatch
    #       refuses rather than lend one trade's best price to another.
    record = load(path)
    if record is None:
        raise ValueError(
            f"the exit rule's state is not on disk ({path} does not exist), "
            f"so it cannot be resumed from it. Supply it by hand: "
            f"{_hint(keys)}")
    saved_side = record.get("side")
    saved_entry = record.get("entry_price")
    if (saved_side != side or not isinstance(saved_entry, (int, float))
            or abs(float(saved_entry) - float(entry_price)) > ENTRY_TOLERANCE):
        raise ValueError(
            f"{path} belongs to a different lot ({saved_side} @ {saved_entry}, "
            f"written {record.get('written_at')}) than the one adopted "
            f"({side} @ {entry_price}). Supply the state by hand: "
            f"{_hint(keys)}")
    state = record.get("state")
    if not isinstance(state, dict) or set(state) != set(keys):
        raise ValueError(
            f"{path} holds state {state!r} but the exit rule needs exactly "
            f"{sorted(keys)}. Supply it by hand: {_hint(keys)}")
    return {k: float(v) for k, v in state.items()}, "file"


class ExitStateFile:
    # Tech: the live writer — called once per bar after the exit check with the
    #       open position (or None), it rewrites `path` when the rule's state or
    #       the lot changes and deletes it when the deployment goes flat.
    # Why:  writing on change keeps it to a handful of writes per trade (a best
    #       price moves only on a new extreme), and deleting on flat means a
    #       file on disk always describes a lot that was open when it was
    #       written. A failed write is recorded, never raised: the loop is
    #       holding a live position and a full disk must not stop it trading.
    def __init__(self, path: Path, *, rule: Any, slug: str,
                 contract: Optional[str] = None,
                 soak_id: Optional[str] = None, recorder: Any = None):
        self.path = Path(path)
        self.rule = rule
        self.slug = slug
        self.contract = contract
        self.soak_id = soak_id
        self.recorder = recorder
        self.writes = 0
        self.failures = 0
        self._last: Optional[Tuple[Any, ...]] = None

    def update(self, position: Any, market: Any) -> None:
        """Bring the file in line with `position` and the rule's current state."""
        if position is None:
            self._clear()
            return
        state = self.rule.export_state()
        if state is None:
            return
        key = (position.side, float(position.entry_price),
               tuple(sorted(state.items())))
        if key == self._last:
            return
        record = {
            "slug": self.slug,
            "side": position.side,
            "entry_price": float(position.entry_price),
            "state": {k: float(v) for k, v in state.items()},
            "bar_timestamp": str(market.timestamp),
            "written_at": datetime.now().isoformat(),
            "contract": self.contract,
            "soak_id": self.soak_id,
        }
        # Tech: write a sibling temp file, then `os.replace` it over the target.
        # Why:  a process killed mid-write must leave either the old record or
        #       the new one, never half of one — `load` would refuse a torn file
        #       and the restart would need the state typed by hand.
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(record, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            self._failed("write", exc)
            return
        self._last = key
        self.writes += 1

    def _clear(self) -> None:
        # Tech: delete the file once, on the first flat bar after a position.
        # Why:  `_last` is None both at start-up and after a clear, so a flat
        #       deployment does not touch the disk every bar; the start-up case
        #       still removes a stale file a crashed run left behind.
        if self._last is None and not self.path.exists():
            return
        try:
            self.path.unlink(missing_ok=True)
        except OSError as exc:
            self._failed("clear", exc)
            return
        self._last = None

    def _failed(self, op: str, exc: OSError) -> None:
        self.failures += 1
        if self.recorder is not None:
            self.recorder.record("exit_state_write_failed", op=op,
                                 path=str(self.path), error=repr(exc))
