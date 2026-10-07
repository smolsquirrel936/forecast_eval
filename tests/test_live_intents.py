"""Tests for live/intents.py — the wire between deployments and the owner (§18.2).

The failure this wire must never have is a dropped or half-read order, so most
of these are about partial writes, corruption and restart rather than about the
happy path.
"""
import json

import pytest

from forecast_eval.live.intents import JsonlStream, event_path, intent_path


def test_paths_are_keyed_by_slug(tmp_path):
    """Each deployment gets its own file, in each direction."""
    assert intent_path(tmp_path, "day").name == "intents_day.jsonl"
    assert event_path(tmp_path, "day").name == "events_day.jsonl"
    assert intent_path(tmp_path, "night") != intent_path(tmp_path, "day")
    assert intent_path(tmp_path, "day") != event_path(tmp_path, "day")


def test_append_returns_increasing_seq(tmp_path):
    s = JsonlStream(tmp_path / "w.jsonl")
    assert [s.append("submit", side="BUY"), s.append("cancel"),
            s.append("submit", side="SELL")] == [1, 2, 3]


def test_records_carry_kind_seq_ts_and_fields(tmp_path):
    s = JsonlStream(tmp_path / "w.jsonl")
    s.append("submit", side="BUY", limit_price=100.5, quantity=2)
    (record,) = JsonlStream(tmp_path / "w.jsonl").read_new()
    assert record["kind"] == "submit"
    assert record["seq"] == 1
    assert record["side"] == "BUY"
    assert record["limit_price"] == 100.5
    assert record["quantity"] == 2
    assert record["ts"] > 0


def test_read_new_returns_only_unseen_records(tmp_path):
    """A reader drains; it does not re-deliver what it already handed over."""
    path = tmp_path / "w.jsonl"
    writer, reader = JsonlStream(path), JsonlStream(path)
    writer.append("a")
    assert [r["kind"] for r in reader.read_new()] == ["a"]
    assert reader.read_new() == []
    writer.append("b")
    writer.append("c")
    assert [r["kind"] for r in reader.read_new()] == ["b", "c"]


def test_read_new_on_missing_file_is_empty_not_an_error(tmp_path):
    """The far end may not have started yet; that is normal, not a fault."""
    assert JsonlStream(tmp_path / "absent.jsonl").read_new() == []


def test_append_creates_the_directory(tmp_path):
    s = JsonlStream(tmp_path / "nested" / "deeper" / "w.jsonl")
    s.append("a")
    assert (tmp_path / "nested" / "deeper" / "w.jsonl").exists()


# ---- the partial-write rule, which is the point of the byte offset ---------

def test_a_trailing_partial_line_is_not_read_until_it_is_complete(tmp_path):
    """Half an order must never be visible. It becomes visible when whole."""
    path = tmp_path / "w.jsonl"
    path.write_text('{"seq":1,"ts":1,"kind":"submit"}\n{"seq":2,"ts":1,"ki',
                    encoding="utf-8")
    reader = JsonlStream(path)
    assert [r["seq"] for r in reader.read_new()] == [1]
    # The writer finishes the line it was in the middle of.
    with path.open("a", encoding="utf-8") as fh:
        fh.write('nd":"cancel"}\n')
    assert [r["seq"] for r in reader.read_new()] == [2]


def test_a_file_with_no_newline_at_all_reads_as_empty(tmp_path):
    path = tmp_path / "w.jsonl"
    path.write_text('{"seq":1,"ts":1,"kind":"su', encoding="utf-8")
    assert JsonlStream(path).read_new() == []


def test_a_complete_but_unparseable_line_raises(tmp_path):
    """Damage must be loud. Skipping it would drop an order silently."""
    path = tmp_path / "w.jsonl"
    path.write_text('{"seq":1,"ts":1,"kind":"a"}\nnot json at all\n',
                    encoding="utf-8")
    with pytest.raises(ValueError, match="corrupt record"):
        JsonlStream(path).read_new()


def test_a_json_scalar_line_is_rejected_too(tmp_path):
    """`123` parses as JSON but is not a record."""
    path = tmp_path / "w.jsonl"
    path.write_text("123\n", encoding="utf-8")
    with pytest.raises(ValueError, match="not an object"):
        JsonlStream(path).read_new()


def test_blank_lines_are_ignored(tmp_path):
    path = tmp_path / "w.jsonl"
    path.write_text('{"seq":1,"ts":1,"kind":"a"}\n\n{"seq":2,"ts":1,"kind":"b"}\n',
                    encoding="utf-8")
    assert [r["seq"] for r in JsonlStream(path).read_new()] == [1, 2]


# ---- restart ---------------------------------------------------------------

def test_resume_seq_continues_past_the_existing_file(tmp_path):
    """A restarted writer must not reissue sequence numbers already sent."""
    path = tmp_path / "w.jsonl"
    first = JsonlStream(path)
    first.append("a")
    first.append("b")
    restarted = JsonlStream(path)
    assert restarted.resume_seq() == 3
    assert restarted.append("c") == 3


def test_resume_seq_on_an_empty_or_missing_file_starts_at_one(tmp_path):
    assert JsonlStream(tmp_path / "absent.jsonl").resume_seq() == 1
    (tmp_path / "empty.jsonl").write_text("", encoding="utf-8")
    assert JsonlStream(tmp_path / "empty.jsonl").resume_seq() == 1


def test_resume_seq_ignores_a_trailing_partial_record(tmp_path):
    path = tmp_path / "w.jsonl"
    path.write_text('{"seq":1,"ts":1,"kind":"a"}\n{"seq":2,"ts":1,"ki',
                    encoding="utf-8")
    assert JsonlStream(path).resume_seq() == 2


def test_a_writer_and_a_reader_on_one_path_do_not_share_state(tmp_path):
    """Reading does not consume the writer's sequence, or vice versa."""
    path = tmp_path / "w.jsonl"
    writer, reader = JsonlStream(path), JsonlStream(path)
    writer.append("a")
    reader.read_new()
    assert writer.append("b") == 2


# ---- liveness --------------------------------------------------------------

def test_age_seconds_is_none_when_nothing_has_been_said(tmp_path):
    """Never spoken is not the same as gone quiet, and must not read as fresh."""
    assert JsonlStream(tmp_path / "absent.jsonl").age_seconds() is None
    (tmp_path / "empty.jsonl").write_text("", encoding="utf-8")
    assert JsonlStream(tmp_path / "empty.jsonl").age_seconds() is None


def test_age_seconds_is_small_right_after_a_write(tmp_path):
    s = JsonlStream(tmp_path / "w.jsonl")
    s.append("heartbeat")
    age = s.age_seconds()
    assert age is not None and 0.0 <= age < 5.0


def test_age_seconds_reports_a_stale_stream(tmp_path):
    """A writer that stopped an hour ago must be visibly stale."""
    path = tmp_path / "w.jsonl"
    import time as _t
    path.write_text(
        json.dumps({"seq": 1, "ts": _t.time() - 3600, "kind": "heartbeat"}) + "\n",
        encoding="utf-8")
    age = JsonlStream(path).age_seconds()
    assert age is not None and age > 3000


def test_age_seconds_uses_the_newest_record_not_the_first(tmp_path):
    path = tmp_path / "w.jsonl"
    import time as _t
    now = _t.time()
    path.write_text(
        json.dumps({"seq": 1, "ts": now - 3600, "kind": "a"}) + "\n"
        + json.dumps({"seq": 2, "ts": now, "kind": "b"}) + "\n",
        encoding="utf-8")
    age = JsonlStream(path).age_seconds()
    assert age is not None and age < 5.0


def test_age_seconds_floors_a_clock_skewed_future_record(tmp_path):
    """Two processes' clocks differ; a small negative age is noise, not a message."""
    path = tmp_path / "w.jsonl"
    import time as _t
    path.write_text(
        json.dumps({"seq": 1, "ts": _t.time() + 30, "kind": "a"}) + "\n",
        encoding="utf-8")
    assert JsonlStream(path).age_seconds() == 0.0


def test_age_seconds_reads_a_stream_longer_than_its_tail(tmp_path):
    """The owner's events stream was 38 MB by 2026-10-07; only the tail is read,
    and the newest record is still the one that counts."""
    from forecast_eval.live.intents import AGE_TAIL_BYTES
    import time as _t
    path = tmp_path / "w.jsonl"
    old = json.dumps({"seq": 1, "ts": _t.time() - 3600, "kind": "position",
                      "pad": "x" * 200}) + "\n"
    newest = json.dumps({"seq": 2, "ts": _t.time(), "kind": "position"}) + "\n"
    path.write_text(old * (2 * AGE_TAIL_BYTES // len(old)) + newest,
                    encoding="utf-8")
    age = JsonlStream(path).age_seconds()
    assert age is not None and age < 5.0


def test_age_seconds_never_parses_a_line_the_tail_cut(tmp_path):
    """A tail that starts mid-line must not be read as a corrupt record."""
    from forecast_eval.live.intents import AGE_TAIL_BYTES
    import time as _t
    path = tmp_path / "w.jsonl"
    line = json.dumps({"seq": 1, "ts": _t.time() - 3600, "kind": "a",
                       "pad": "y" * 97}) + "\n"
    body = line * (AGE_TAIL_BYTES // len(line) + 3)
    path.write_text(body, encoding="utf-8")
    age = JsonlStream(path).age_seconds()
    assert age is not None and age > 3000


def test_age_seconds_ignores_a_trailing_partial_line(tmp_path):
    import time as _t
    path = tmp_path / "w.jsonl"
    path.write_text(
        json.dumps({"seq": 1, "ts": _t.time() - 3600, "kind": "a"}) + "\n"
        + '{"seq": 2, "ts": ', encoding="utf-8")
    age = JsonlStream(path).age_seconds()
    assert age is not None and age > 3000
