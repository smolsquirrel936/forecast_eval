"""The IOC probe's pure parts — everything testable without a venue.

The probe exists to answer one blocking question for the roll ladder: does an
expired IOC report anything the adapter can retire the order on? What is tested
here is that the probe *reads and reports* an answer faithfully — a probe whose
own summariser is wrong produces a measurement-shaped argument, which is worse
than no probe (SHIOAJI_PLAN §2.19's four wrong turns).
"""
from collections import UserDict

import pytest

from forecast_eval.live.ioc_probe import (
    message_facts,
    redact,
    summarise,
    verdict,
)


# ---- redaction: measured the hard way, twice -------------------------------

def test_a_person_id_field_is_blanked():
    assert "L125551606" not in redact("Account(person_id='L125551606')")


def test_a_bare_national_id_inside_free_text_is_blanked():
    # The leak route field-name redaction missed: the SDK embeds it inside an
    # event string where there is no `person_id=` to match on.
    text = "#P2P/v:bcsolace01/Oryfkqqm/PYAPI/L125551606/0817/054517/"
    assert "L125551606" not in redact(text)
    assert "<redacted-id>" in redact(text)


def test_redaction_leaves_everything_else_readable():
    assert redact("op_type='Cancel' op_code='00'") == "op_type='Cancel' op_code='00'"


# ---- reading a callback message --------------------------------------------

def test_the_nested_operation_block_is_where_the_fields_are_read_from():
    # The shape the venue actually sends, and the one `_handle_order` reads:
    # op_type / op_code / op_msg live under `operation`, not at the top level.
    # This case is here because the flat one below was invented rather than
    # copied from the adapter, so it passed while the live probe reported None
    # for every field of every message (measured 2026-08-24).
    msg = {"operation": {"op_type": "Cancel", "op_code": "00", "op_msg": ""},
           "order": {"order_type": "IOC", "price_type": "LMT", "quantity": 1},
           "status": {"status": "Cancelled"}}
    f = message_facts(msg)
    assert f["op_type"] == "Cancel"
    assert f["op_code"] == "00"
    assert f["order_type"] == "IOC"
    assert f["status"] == "Cancelled"


def test_a_flat_message_is_still_read():
    msg = {"op_type": "Cancel", "op_code": "00", "op_msg": "",
           "order": {"order_type": "IOC", "price_type": "LMT", "quantity": 1},
           "status": {"status": "Cancelled"}}
    f = message_facts(msg)
    assert f["op_type"] == "Cancel"
    assert f["op_code"] == "00"
    assert f["order_type"] == "IOC"
    assert f["status"] == "Cancelled"


def test_a_mapping_that_is_not_a_dict_is_still_read():
    # MEASURED 2026-08-24: the SDK hands the callback a mapping that fails
    # `isinstance(msg, dict)`. The probe fell back to raw-only and reported
    # op_type=None for three live IOCs and a cancelled ROD — indistinguishable
    # from the venue reporting nothing, which is the exact question being asked.
    msg = UserDict({"operation": {"op_type": "New", "op_code": "00"},
                    "order": UserDict({"order_type": "IOC"}),
                    "status": UserDict({"status": "PendingSubmit"})})
    assert not isinstance(msg, dict)
    f = message_facts(msg)
    assert f["op_type"] == "New"
    assert f["op_code"] == "00"
    assert f["order_type"] == "IOC"
    assert f["status"] == "PendingSubmit"


def test_a_message_of_an_unexpected_shape_still_reports_its_repr():
    # The SDK has produced a different shape than documented five times; a probe
    # that only reads known fields would report "nothing" for a message it simply
    # did not understand.
    f = message_facts("some string the SDK sent")
    assert "some string" in f["raw"]


def test_the_raw_repr_is_redacted_before_it_is_truncated():
    # Truncating first could leave a leaked prefix.
    f = message_facts({"op_msg": "L125551606 rejected"})
    assert "L125551606" not in f["raw"]


# ---- the verdict -----------------------------------------------------------

def test_silence_is_reported_as_silence():
    # The answer that blocks the ladder: no callback means nothing to retire on.
    v = verdict([], waited=10.0)
    assert v["reported"] is False
    assert v["n_events"] == 0
    assert "10s" in v["note"]


def test_an_expiry_report_carries_its_latency_and_op_types():
    events = [
        {"elapsed": 0.412, "state": "Cancelled",
         "facts": {"op_type": "Cancel", "op_code": "00", "status": "Cancelled"}},
    ]
    v = verdict(events, waited=10.0)
    assert v["reported"] is True
    assert v["first_after"] == 0.412
    assert v["op_types"] == ["Cancel"]
    assert v["statuses"] == ["Cancelled"]


def test_several_messages_are_all_reported_not_just_the_first():
    # Q4 is a consistency question, and an adapter keying on the *last* message
    # behaves differently from one keying on the first.
    events = [
        {"elapsed": 0.1, "facts": {"op_type": "New", "op_code": "00",
                                   "status": "Submitted"}, "state": "s"},
        {"elapsed": 0.4, "facts": {"op_type": "Cancel", "op_code": "00",
                                   "status": "Cancelled"}, "state": "s"},
    ]
    v = verdict(events, waited=10.0)
    assert v["n_events"] == 2
    assert v["op_types"] == ["New", "Cancel"]
    assert v["last_after"] == 0.4


# ---- the paste-back block --------------------------------------------------

def test_the_summary_warns_that_the_lines_above_it_are_not_redacted():
    # The probe cannot redact the SDK's own banner, so it must say so — the
    # operator pastes this into a chat.
    out = summarise({"contract": "TXFR1"})
    assert "national ID" in out
    assert "Paste only this block" in out


def test_every_finding_appears_in_the_block():
    out = summarise({"attempt_1": {"reported": False}, "probe_price": 44800.0})
    assert "attempt_1" in out and "probe_price" in out
