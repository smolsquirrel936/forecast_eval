"""Recorder — the JSONL audit trail V3 and V4 are read back from.

The properties that matter are survivability and re-parseability: a process
killed mid-session must leave a file whose complete lines all parse, and every
timestamp must round-trip so an offline replay can be lined up against it.
"""
import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from forecast_eval.live.recorder import Recorder


def _lines(path: Path):
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]


def test_meta_header_is_written_first_with_the_simulation_flag(tmp_path):
    p = tmp_path / "run.jsonl"
    with Recorder(p, simulation=True, meta={"config": "wiring"}) as rec:
        rec.record("bar", price=100.0)
    recs = _lines(p)
    assert recs[0]["event"] == "meta"
    assert recs[0]["simulation"] is True
    assert recs[0]["config"] == "wiring"


def test_production_flag_is_recorded_verbatim(tmp_path):
    """A file must never be mistakable for a simulation run when read later."""
    p = tmp_path / "run.jsonl"
    Recorder(p, simulation=False).close()
    assert _lines(p)[0]["simulation"] is False


def test_each_record_is_one_line_with_a_monotonic_seq(tmp_path):
    p = tmp_path / "run.jsonl"
    with Recorder(p, simulation=True) as rec:
        for i in range(5):
            rec.record("bar", bar_idx=i)
    recs = _lines(p)
    assert [r["seq"] for r in recs] == [1, 2, 3, 4, 5, 6]   # meta is seq 1
    assert [r["event"] for r in recs] == ["meta"] + ["bar"] * 5
    assert [r["bar_idx"] for r in recs[1:]] == [0, 1, 2, 3, 4]


def test_flushed_per_line_so_a_kill_loses_at_most_the_last_event(tmp_path):
    """Read the file while the recorder is still open — everything written so
    far must already be on disk and parseable."""
    p = tmp_path / "run.jsonl"
    rec = Recorder(p, simulation=True)
    rec.record("bar", bar_idx=0)
    rec.record("bar", bar_idx=1)
    recs = _lines(p)          # no close() yet
    assert len(recs) == 3
    assert recs[-1]["bar_idx"] == 1
    rec.close()


def test_timestamps_and_numpy_scalars_serialize(tmp_path):
    p = tmp_path / "run.jsonl"
    with Recorder(p, simulation=True) as rec:
        rec.record(
            "fill",
            timestamp=pd.Timestamp("2024-03-05 09:00:00"),
            py_dt=datetime(2024, 3, 5, 9, 0, 0),
            price=np.float64(17500.5),
            qty=np.int64(3),
        )
    r = _lines(p)[-1]
    assert r["timestamp"] == "2024-03-05T09:00:00"
    assert r["py_dt"] == "2024-03-05T09:00:00"
    assert r["price"] == 17500.5
    assert r["qty"] == 3


def test_unserializable_field_is_stringified_not_raised(tmp_path):
    """A recorder that can kill the trading loop is worse than a lossy one."""
    class Weird:
        def __repr__(self):
            return "<weird>"

    p = tmp_path / "run.jsonl"
    with Recorder(p, simulation=True) as rec:
        rec.record("odd", thing=Weird())
    assert _lines(p)[-1]["thing"] == "<weird>"


def test_reopening_appends_rather_than_truncating(tmp_path):
    """A restart must add to the session trail, not destroy it."""
    p = tmp_path / "run.jsonl"
    Recorder(p, simulation=True).close()
    Recorder(p, simulation=True).close()
    recs = _lines(p)
    assert len(recs) == 2 and all(r["event"] == "meta" for r in recs)


def test_close_is_idempotent(tmp_path):
    rec = Recorder(tmp_path / "run.jsonl", simulation=True)
    rec.close()
    rec.close()


# ---- identifier redaction --------------------------------------------------

def test_a_national_id_never_reaches_the_trail(tmp_path):
    """The trail is the file that gets pasted into chats and tickets, and
    `cli.py` digests `person_id` so it never has to carry one. But the SDK
    embeds it **bare** inside session-event strings, where no field name exists
    to key on — and the §2.24 re-login emits exactly those events twice a day
    with the callback registered by then."""
    path = tmp_path / "t.jsonl"
    rec = Recorder(path, simulation=True)
    rec.record("session_event",
               info="#P2P/v:bcsolace01/PYAPI/L125551606/0817/050844/1/1.2.3.4/>")
    rec.close()
    text = path.read_text(encoding="utf-8")
    assert "L125551606" not in text
    assert "<redacted-id>" in text
    assert "bcsolace01" in text          # non-identifying context is preserved


def test_the_redaction_pattern_is_a_word_boundary_not_a_backspace():
    """REGRESSION — the pattern was once written through a tool that turned the
    `\b` escapes into literal 0x08 bytes. `grep` rendered it as correct because
    a backspace is invisible, and the regex silently matched nothing. Assert the
    *compiled* pattern, because reading the source is what hid this."""
    from forecast_eval.live.recorder import _ID_PATTERN
    assert _ID_PATTERN.pattern == r"\b[A-Z][0-9]{9}\b"
    assert "\x08" not in _ID_PATTERN.pattern
    assert _ID_PATTERN.search("PYAPI/L125551606/0817") is not None


def test_redaction_does_not_disturb_ordinary_records(tmp_path):
    path = tmp_path / "t.jsonl"
    rec = Recorder(path, simulation=True)
    rec.record("bar", bar_idx=3, price=46036.0, session="NIGHT")
    rec.close()
    import json
    rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["price"] == 46036.0 and rows[-1]["session"] == "NIGHT"
