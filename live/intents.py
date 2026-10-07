"""The wire between the three deployments and the owning process (§18.2).

Option (b) puts one process in front of the account: the deployments no longer
call `place_order`, they submit **intents**, and the owner sends back what
happened. This module is that wire and nothing else — it carries dicts, and the
protocol (what kinds exist, what fields they carry) lives with the two ends that
speak it.

**Line-delimited JSON files, one per direction per deployment.** Chosen over a
socket in STRATEGY_SPLIT_PLAN §18.4: it matches every existing idiom here (the
fill ledger, the bar cache, the run directory), adds no dependency, and leaves a
durable record that can be read after a crash — which a socket does not. The
loop runs on one-minute bars, so polling latency is irrelevant.

**What that choice costs, and what pays for it.** A dead writer's file looks
exactly like a quiet one. So every record carries a monotonic `seq` and a
timestamp, and `age_seconds()` exists for the reader to notice silence; a
deployment whose owner has gone quiet must halt rather than trade blind
(§18.3 item 6), and the owner must not act on a deployment it has stopped
hearing from.

**Reads are all-or-nothing per line.** Only complete lines (ending in a newline)
are returned, and a trailing partial write stays buffered for the next call — so
a reader can never see half an order. A line that IS complete but does not parse
means the file is damaged, and that raises: silently skipping it would drop an
order on the floor, which is the one thing this wire must never do.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# Tech: how much of a stream's end `age_seconds` reads to find the newest record.
# Why:  one record is ~50-200 bytes, so 64 KiB holds hundreds of them. Reading
#       the whole file instead grew without bound: the owner asks every 30 s per
#       deployment, each deployment asks about the owner's stream (38 MB and
#       437 000 lines by 2026-10-07), and deployments heartbeat every 30 s.
AGE_TAIL_BYTES = 64 * 1024


def intent_path(directory: Any, slug: str) -> Path:
    """`intents_<slug>.jsonl` — the deployment writes, the owner reads."""
    # Tech: derive both paths from the deployment slug, beside the ledgers.
    # Why:  `cache_slug()` already names the fill ledger (`ledger.ledger_path`)
    #       and the bar cache, and C5 is the record of what happens when two
    #       deployments share one path. Deriving rather than passing means a new
    #       deployment cannot forget to change it.
    return Path(directory) / f"intents_{slug}.jsonl"


def event_path(directory: Any, slug: str) -> Path:
    """`events_<slug>.jsonl` — the owner writes, the deployment reads."""
    # Tech: the return direction, same naming rule.
    # Why:  one file per direction rather than one shared file, so each has
    #       exactly one writer. A single writer is what makes an append atomic
    #       enough to rely on and what keeps `seq` meaningful.
    return Path(directory) / f"events_{slug}.jsonl"


class JsonlStream:
    """One direction of the wire: append records, read the ones you have not seen.

    A stream object is either a writer or a reader of a given path, never both —
    each file has exactly one writer by construction (see `event_path`). Both
    roles share this class because both need the same record shape.
    """

    def __init__(self, path: Any) -> None:
        # Tech: remember the path, the next sequence number to issue, and how far
        #       this reader has consumed.
        # Why:  `_offset` is a byte position rather than a line count so a partial
        #       trailing line can be re-read once it is complete; a line counter
        #       would have to re-parse the whole file to find its place.
        self.path = Path(path)
        self._next_seq = 1
        self._offset = 0

    # ---- writing -----------------------------------------------------------

    def append(self, kind: str, **fields: Any) -> int:
        """Append one record and return its sequence number."""
        # Tech: stamp the record with a monotonically increasing `seq` and a wall
        #       clock, serialise it as one line, and flush before returning.
        # Why:  `seq` is what lets the far end say "I have seen everything up to
        #       N" without trusting file offsets it cannot observe, and what makes
        #       a gap detectable — a dropped intent is an order that never
        #       happened, and it must not look like silence. The flush is not
        #       optional: the reader is a different process, and an intent sitting
        #       in this process's buffer is an intent the owner cannot act on.
        #       §16's freshness caveat is the same problem one file over.
        record: Dict[str, Any] = {
            "seq": self._next_seq,
            "ts": time.time(),
            "kind": kind,
        }
        record.update(fields)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")
            fh.flush()
        self._next_seq += 1
        return record["seq"]

    def resume_seq(self) -> int:
        """Set the next sequence number past whatever is already in the file."""
        # Tech: read the last complete record's `seq` and continue from it.
        # Why:  a restarted writer that began again at 1 would issue sequence
        #       numbers the far end has already seen, and the gap check would read
        #       the restart as a duplicate stream rather than a continuation. Call
        #       this before the first `append` on an existing file.
        last = 0
        for record in self._read_all():
            last = max(last, int(record.get("seq", 0)))
        self._next_seq = last + 1
        return self._next_seq

    # ---- reading -----------------------------------------------------------

    def read_new(self) -> List[Dict[str, Any]]:
        """Every complete record appended since the last call."""
        # Tech: read from the stored byte offset, keep only whole lines, advance
        #       the offset by exactly the bytes consumed.
        # Why:  a writer appending while this reads can leave a partial final
        #       line. Advancing past it would drop the record permanently; leaving
        #       it unconsumed means the next call sees it whole. This is why the
        #       offset moves by consumed bytes rather than to end-of-file.
        if not self.path.exists():
            return []
        with self.path.open("rb") as fh:
            fh.seek(self._offset)
            blob = fh.read()
        if not blob:
            return []
        # Tech: split off any trailing fragment that has no newline yet.
        # Why:  see above — the fragment is not a record until its newline
        #       arrives, and pretending otherwise is how half an order gets read.
        consumed = blob.rfind(b"\n") + 1
        if consumed == 0:
            return []
        self._offset += consumed
        return [self._parse(line)
                for line in blob[:consumed].decode("utf-8").splitlines()
                if line.strip()]

    def _read_all(self) -> List[Dict[str, Any]]:
        # Tech: every complete record in the file, ignoring the read offset.
        # Why:  `resume_seq` asks about the file's whole history, not about what
        #       this object has consumed. Kept private so the two questions cannot
        #       be confused at a call site.
        if not self.path.exists():
            return []
        blob = self.path.read_bytes()
        consumed = blob.rfind(b"\n") + 1
        return [self._parse(line)
                for line in blob[:consumed].decode("utf-8").splitlines()
                if line.strip()]

    def _parse(self, line: str) -> Dict[str, Any]:
        # Tech: decode one complete line, raising with the path and the text when
        #       it is not JSON.
        # Why:  a complete line that will not parse is a damaged file, not a
        #       partial write — the partial case is already handled by the newline
        #       rule above. Skipping it would drop an order silently, which is the
        #       one failure this wire cannot have; the caller halts instead. Same
        #       reasoning as `ledger.account_net_position` returning None rather
        #       than a partial sum.
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"corrupt record in {self.path}: {line[:200]!r}") from exc
        if not isinstance(record, dict):
            raise ValueError(
                f"record in {self.path} is not an object: {line[:200]!r}")
        return record

    # ---- liveness ----------------------------------------------------------

    def age_seconds(self) -> Optional[float]:
        """Seconds since the newest record was written; None if there are none."""
        # Tech: wall-clock now minus the last record's own `ts`, floored at zero.
        # Why:  the writer's own timestamp rather than the file's mtime, because
        #       mtime moves for reasons that are not a record (a touch, a copy)
        #       and the question being asked is "when did the far end last say
        #       something". None means it has never spoken, which is different
        #       from having gone quiet and must not read as age zero. Floored
        #       because two processes' clocks are not identical and a small
        #       negative age is noise, not a message from the future.
        #
        #       Tech: the newest record is the LAST complete line, read from the
        #             file's tail (`AGE_TAIL_BYTES`); the whole file is read only
        #             when the tail holds no complete line.
        #       Why:  each file has exactly one writer appending in order, so the
        #             last line is the newest — and the cost stays flat however
        #             long a soak runs.
        last = self._last_record()
        if last is None:
            return None
        return max(0.0, time.time() - float(last.get("ts", 0.0)))

    def _last_record(self) -> Optional[Dict[str, Any]]:
        # Tech: parse the last complete, non-blank line in the file's tail;
        #       fall back to `_read_all` when the tail starts mid-file and holds
        #       no whole line.
        # Why:  see `age_seconds`. The first line of a tail that starts mid-file
        #       may be cut, so it is never parsed; a trailing partial line has no
        #       newline yet and is not a record, the same rule as `read_new`.
        if not self.path.exists():
            return None
        with self.path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            start = max(0, size - AGE_TAIL_BYTES)
            fh.seek(start)
            blob = fh.read()
        complete = blob[:blob.rfind(b"\n") + 1]
        lines = complete.decode("utf-8", errors="replace").splitlines()
        if start > 0:
            lines = lines[1:]
        for line in reversed(lines):
            if line.strip():
                return self._parse(line)
        if start > 0:
            records = self._read_all()
            return records[-1] if records else None
        return None
