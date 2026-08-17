"""Recorder — append-only JSONL audit trail of everything the live loop saw.

Two downstream phases depend entirely on this file existing and being complete:

  * **V3 shadow parity** replays a live session's bars offline through
    `run_backtest` and requires the *decisions* to match. That diff is only
    possible if every bar, forecast, signal and order was written down with the
    timestamp it carried at the time.
  * **V4 fill-gap** compares, per order, what `Execution` would have filled
    against what the venue actually reported. Both sides are recorded here.

Design constraints that follow from "the process may be killed at any moment":

  * one JSON object per line, flushed after every write — a truncated tail loses
    at most the final event, and every complete line stays parseable;
  * no cross-record state, so the file can be read with a plain `for line in f`;
  * a `meta` header record first, carrying the `simulation` flag, so a file can
    never be mistaken for a production run when it is read back months later.

This is a sink, not a decision-maker: nothing it does may influence trading.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Optional


def _json_default(obj: Any) -> Any:
    # Tech: coerce the non-JSON-native types the loop actually carries —
    #       pandas/py datetimes, numpy scalars, Path — and stringify anything
    #       else rather than raising.
    # Why:  a recorder that can crash the trading loop by failing to serialize an
    #       unexpected field is worse than one that writes an imperfect string.
    #       Timestamps go out as ISO-8601 so the file is diffable and re-parses
    #       losslessly for the V3 comparison.
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if hasattr(obj, "isoformat"):          # pandas Timestamp
        return obj.isoformat()
    if hasattr(obj, "item"):               # numpy scalar
        try:
            return obj.item()
        except Exception:
            pass
    if isinstance(obj, Path):
        return str(obj)
    return str(obj)


# Tech: the shape of a Taiwanese national ID — one letter then nine digits.
# Why:  MEASURED — the SDK embeds it bare inside session-event strings, where
#       there is no field name to redact on. Matching by *shape* is what catches
#       it wherever it appears, and over-redaction is the safe direction in an
#       audit trail.
_ID_PATTERN = re.compile(r"\b[A-Z][0-9]{9}\b")


class Recorder:
    def __init__(self, path: "str | Path", *, simulation: bool,
                 meta: Optional[Dict[str, Any]] = None):
        # Tech: open the JSONL file for append, then immediately write a `meta`
        #       record stamping the simulation flag and any caller-supplied config.
        # Why:  append (not truncate) means a restart adds to the session's trail
        #       instead of destroying it. The header is written eagerly so even a
        #       process that dies during warm-up leaves a file that identifies
        #       itself — including, non-negotiably, whether orders were real
        #       (SHIOAJI_PLAN.md §5).
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.simulation = simulation
        self._fh = self.path.open("a", encoding="utf-8")
        self._seq = 0
        self.record("meta", simulation=simulation, **(meta or {}))

    def record(self, event: str, **fields: Any) -> None:
        # Tech: write one JSON object — monotonic seq, local wall-clock, event
        #       name, then the caller's fields — and flush.
        # Why:  `seq` orders events even when several share a bar timestamp, and
        #       `wall` is the only place local time enters the record (bar/market
        #       times come from the feed). Flushing per line is what makes the
        #       trail survive a kill -9; the cost is irrelevant next to a forecast.
        self._seq += 1
        rec = {
            "seq": self._seq,
            "wall": datetime.now().isoformat(),
            "event": event,
        }
        rec.update(fields)
        # Tech: scrub national-ID-shaped tokens from the serialised line before
        #       it reaches disk.
        # Why:  the trail is the file that gets pasted into chats and tickets, and
        #       `cli.py` digests `person_id` precisely so it never has to carry
        #       one. But the SDK embeds it **bare** inside session-event strings
        #       (`#P2P/v:.../PYAPI/L1255…/0817/…`), where no field name exists to
        #       key on — so a `session_event` record would carry it verbatim. The
        #       §2.24 re-login emits exactly those events twice a day, with the
        #       callback registered by then, so this would have started leaking
        #       on the first soak. Scrubbing the rendered line rather than each
        #       call site means no future `record()` can reintroduce it.
        line = _ID_PATTERN.sub("<redacted-id>",
                               json.dumps(rec, default=_json_default))
        self._fh.write(line + "\n")
        self._fh.flush()

    def close(self) -> None:
        # Tech: close the handle if it is still open; safe to call twice.
        # Why:  idempotent so both an orderly shutdown and an exception handler
        #       can call it without guarding.
        if not self._fh.closed:
            self._fh.close()

    def __enter__(self) -> "Recorder":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
