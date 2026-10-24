"""Tests for live/broker/client.py — the deployment's end of the owner wire (§18.2).

The two things that must hold: `Trader` cannot tell this from any other Broker,
and a silent owner halts rather than being treated as a flat account.
"""
import json

import pandas as pd
import pytest

from forecast_eval.events import MarketEvent, OrderEvent
from forecast_eval.live.broker.base import Broker
from forecast_eval.live.broker.client import ClientBroker
from forecast_eval.live.intents import JsonlStream, event_path, intent_path

TS = pd.Timestamp("2026-09-07 10:00:00")


def _order(side="BUY", price=100.0, qty=1, intent="OPEN"):
    return OrderEvent(timestamp=TS, side=side, limit_price=price,
                      intent=intent, quantity=qty)


def _market(price=100.0):
    return MarketEvent(timestamp=TS, price=price, volume=1, session="DAY")


class _Rails:
    """Minimal stand-in that records halts the way SafetyRails would."""

    def __init__(self):
        self.halts = []

    def halt(self, reason, action):
        self.halts.append((reason, action))


def _owner(tmp_path, slug="day"):
    """A stream standing in for the owning process's outbound side."""
    return JsonlStream(event_path(tmp_path, slug))


def _client(tmp_path, slug="day", **kw):
    return ClientBroker(tmp_path, slug, **kw)


# ---- interface conformance -------------------------------------------------

def test_is_a_broker(tmp_path):
    assert isinstance(_client(tmp_path), Broker)


def test_has_venue_is_true(tmp_path):
    """None from broker_position means 'query failed', not 'no venue' (base.py)."""
    assert _client(tmp_path).has_venue is True


# ---- submitting ------------------------------------------------------------

def test_submit_writes_an_intent_carrying_the_order(tmp_path):
    c = _client(tmp_path)
    c.submit(_order(side="SELL", price=101.5, qty=2, intent="CLOSE"),
             current_price=99.0)
    (record,) = JsonlStream(intent_path(tmp_path, "day")).read_new()
    assert record["kind"] == "submit"
    assert record["side"] == "SELL"
    assert record["limit_price"] == 101.5
    assert record["quantity"] == 2
    assert record["intent"] == "CLOSE"
    assert record["current_price"] == 99.0
    assert record["urgent"] is False


def test_submit_carries_intent_not_octype(tmp_path):
    """OPEN/CLOSE is what the deployment means; octype is the owner's call (§18.9)."""
    c = _client(tmp_path)
    c.submit(_order(), current_price=100.0)
    (record,) = JsonlStream(intent_path(tmp_path, "day")).read_new()
    assert "octype" not in record
    assert record["intent"] == "OPEN"


def test_submit_sets_pending(tmp_path):
    c = _client(tmp_path)
    assert c.has_pending() is False and c.pending_order() is None
    order = _order()
    c.submit(order, current_price=100.0)
    assert c.has_pending() is True
    assert c.pending_order() is order


def test_urgent_marks_the_intent_and_does_not_leak(tmp_path):
    c = _client(tmp_path)
    with c.urgent():
        c.submit(_order(), current_price=100.0)
    c.submit(_order(), current_price=100.0)
    first, second = JsonlStream(intent_path(tmp_path, "day")).read_new()
    assert first["urgent"] is True
    assert second["urgent"] is False


def test_urgent_resets_even_when_the_block_raises(tmp_path):
    c = _client(tmp_path)
    with pytest.raises(RuntimeError):
        with c.urgent():
            raise RuntimeError("boom")
    c.submit(_order(), current_price=100.0)
    (record,) = JsonlStream(intent_path(tmp_path, "day")).read_new()
    assert record["urgent"] is False


# ---- cancelling ------------------------------------------------------------

def test_cancel_writes_an_intent_but_keeps_pending(tmp_path):
    """A live cancel is asynchronous: pending stays until the owner says done."""
    c = _client(tmp_path)
    c.submit(_order(), current_price=100.0)
    c.cancel()
    kinds = [r["kind"]
             for r in JsonlStream(intent_path(tmp_path, "day")).read_new()]
    assert kinds == ["submit", "cancel"]
    assert c.has_pending() is True


def test_cancel_with_nothing_working_writes_nothing(tmp_path):
    c = _client(tmp_path)
    c.cancel()
    assert JsonlStream(intent_path(tmp_path, "day")).read_new() == []


def test_only_done_clears_pending(tmp_path):
    c = _client(tmp_path)
    owner = _owner(tmp_path)
    c.submit(_order(), current_price=100.0)
    owner.append("fill", timestamp=TS.isoformat(), side="BUY",
                 fill_price=100.0, quantity=1, fee=0.5)
    c.poll_fills(_market())
    assert c.has_pending() is True, "a fill does not end the order; partials exist"
    owner.append("done")
    c.poll_fills(_market())
    assert c.has_pending() is False


# ---- fills -----------------------------------------------------------------

def test_poll_fills_returns_and_drains(tmp_path):
    c = _client(tmp_path)
    owner = _owner(tmp_path)
    owner.append("fill", timestamp=TS.isoformat(), side="BUY",
                 fill_price=100.5, quantity=1, fee=0.25)
    (fill,) = c.poll_fills(_market())
    assert fill.side == "BUY"
    assert fill.fill_price == 100.5
    assert fill.quantity == 1
    assert fill.fee == 0.25
    assert c.poll_fills(_market()) == []


def test_partial_fills_accumulate_as_separate_events(tmp_path):
    """MEASURED §18.9: a 2-lot order filled as two 1-lot deals 9s apart."""
    c = _client(tmp_path)
    owner = _owner(tmp_path)
    owner.append("fill", timestamp=TS.isoformat(), side="SELL",
                 fill_price=101.0, quantity=1, fee=0.1)
    owner.append("fill", timestamp=TS.isoformat(), side="SELL",
                 fill_price=100.0, quantity=1, fee=0.1)
    fills = c.poll_fills(_market())
    assert [f.quantity for f in fills] == [1, 1]
    assert [f.fill_price for f in fills] == [101.0, 100.0]


def test_a_malformed_fill_halts_rather_than_being_booked(tmp_path):
    rails = _Rails()
    c = _client(tmp_path, rails=rails)
    _owner(tmp_path).append("fill", side="BUY")          # no price, no timestamp
    assert c.poll_fills(_market()) == []
    assert rails.halts and "malformed fill" in rails.halts[0][0]


# ---- position --------------------------------------------------------------

def test_position_comes_from_the_owner(tmp_path):
    c = _client(tmp_path)
    _owner(tmp_path).append("position", signed_qty=2, avg_price=100.25)
    assert c.broker_position() == 2
    detail = c.broker_position_detail()
    assert detail.signed_qty == 2 and detail.avg_price == 100.25


def test_a_flat_report_is_zero_not_none(tmp_path):
    """Zero is an answer; None means 'cannot see the book'."""
    c = _client(tmp_path)
    _owner(tmp_path).append("position", signed_qty=0)
    assert c.broker_position() == 0
    assert c.broker_position_detail() is None


def test_a_malformed_position_halts(tmp_path):
    rails = _Rails()
    c = _client(tmp_path, rails=rails)
    _owner(tmp_path).append("position", signed_qty="three")
    assert c.broker_position() is None
    assert rails.halts and "malformed position" in rails.halts[0][0]


# ---- the owner going quiet -------------------------------------------------

def test_an_owner_that_never_spoke_is_gone_not_flat(tmp_path):
    rails = _Rails()
    c = _client(tmp_path, rails=rails)
    assert c.broker_position() is None
    assert c.broker_position_detail() is None
    assert rails.halts and "not spoken" in rails.halts[0][0]


def test_a_silent_owner_halts_and_reports_none(tmp_path):
    rails = _Rails()
    path = event_path(tmp_path, "day")
    import time
    path.write_text(
        json.dumps({"seq": 1, "ts": time.time() - 600, "kind": "position",
                    "signed_qty": 1}) + "\n", encoding="utf-8")
    c = _client(tmp_path, rails=rails, owner_timeout=90.0)
    assert c.broker_position() is None, "a stale number must not be compared"
    assert rails.halts and "silent for" in rails.halts[0][0]


def test_a_fresh_owner_does_not_halt(tmp_path):
    rails = _Rails()
    c = _client(tmp_path, rails=rails)
    _owner(tmp_path).append("position", signed_qty=1)
    assert c.broker_position() == 1
    assert rails.halts == []


def test_a_corrupt_event_line_halts_instead_of_raising(tmp_path):
    """The wire raises so nothing is dropped; the loop needs a halt, not a traceback."""
    rails = _Rails()
    path = event_path(tmp_path, "day")
    path.write_text("this is not json\n", encoding="utf-8")
    c = _client(tmp_path, rails=rails)
    assert c.poll_fills(_market()) == []
    assert rails.halts and "corrupt record" in rails.halts[0][0]


def test_an_owner_rejection_clears_pending_and_halts(tmp_path):
    rails = _Rails()
    c = _client(tmp_path, rails=rails)
    c.submit(_order(), current_price=100.0)
    _owner(tmp_path).append("rejected", reason="account net would exceed limit")
    c.poll_fills(_market())
    assert c.has_pending() is False
    assert rails.halts and "owner rejected" in rails.halts[0][0]


def test_an_unknown_event_kind_is_ignored_not_fatal(tmp_path):
    """The owner may add a message type before the deployments are redeployed."""
    rails = _Rails()
    c = _client(tmp_path, rails=rails)
    _owner(tmp_path).append("something_new", detail=1)
    assert c.poll_fills(_market()) == []
    assert rails.halts == []


# ---- restart ---------------------------------------------------------------

def test_a_restarted_client_does_not_rebook_old_events(tmp_path):
    """A fresh stream reads from byte 0: a `--resume` would re-book every fill
    the owner ever sent, on top of the ledger replay, and halt on any old
    rejection. Only the newest position is kept, for `adopt_position`."""
    owner = _owner(tmp_path)
    owner.append("fill", timestamp=TS.isoformat(), side="BUY",
                 fill_price=100.0, quantity=1, fee=0.1)
    owner.append("done")
    owner.append("rejected", reason="old")
    owner.append("position", signed_qty=0, avg_price=0.0)
    owner.append("position", signed_qty=1, avg_price=100.0)
    rails = _Rails()
    c = _client(tmp_path, rails=rails)
    assert c.skipped_events == 5
    assert c.poll_fills(_market()) == []
    assert rails.halts == []
    assert c.broker_position() == 1
    assert c.broker_position_detail().avg_price == 100.0


def test_events_after_a_restart_are_read(tmp_path):
    owner = _owner(tmp_path)
    owner.append("fill", timestamp=TS.isoformat(), side="BUY",
                 fill_price=100.0, quantity=1, fee=0.1)
    c = _client(tmp_path)
    owner.append("fill", timestamp=TS.isoformat(), side="SELL",
                 fill_price=101.0, quantity=1, fee=0.1)
    (fill,) = c.poll_fills(_market())
    assert fill.side == "SELL"


def test_a_restarted_client_continues_the_intent_sequence(tmp_path):
    first = _client(tmp_path)
    first.submit(_order(), current_price=100.0)
    first.cancel()
    restarted = _client(tmp_path)
    restarted.submit(_order(), current_price=100.0)
    seqs = [r["seq"]
            for r in JsonlStream(intent_path(tmp_path, "day")).read_new()]
    assert seqs == [1, 2, 3]


def test_each_deployment_uses_its_own_files(tmp_path):
    day, night = _client(tmp_path, "day"), _client(tmp_path, "night")
    day.submit(_order(), current_price=100.0)
    night.submit(_order(side="SELL"), current_price=100.0)
    day_records = JsonlStream(intent_path(tmp_path, "day")).read_new()
    night_records = JsonlStream(intent_path(tmp_path, "night")).read_new()
    assert [r["side"] for r in day_records] == ["BUY"]
    assert [r["side"] for r in night_records] == ["SELL"]


def test_heartbeat_is_written_even_when_flat_and_quiet(tmp_path):
    c = _client(tmp_path)
    c.heartbeat()
    (record,) = JsonlStream(intent_path(tmp_path, "day")).read_new()
    assert record["kind"] == "heartbeat"


# ---- the heartbeat timer ---------------------------------------------------

def _beats(tmp_path, slug="day"):
    return [r for r in JsonlStream(intent_path(tmp_path, slug))._read_all()
            if r["kind"] == "heartbeat"]


def test_the_first_clock_tick_beats_at_once(tmp_path):
    """The owner should hear a deployment as soon as its loop runs."""
    c = _client(tmp_path)
    assert c.maybe_heartbeat(1000.0) is True
    assert len(_beats(tmp_path)) == 1


def test_beats_are_paced_by_heartbeat_seconds(tmp_path):
    """The clock block runs every second; the owner needs one beat per 30 s."""
    c = _client(tmp_path)
    for second in range(0, 91):                     # 0..90 s inclusive
        c.maybe_heartbeat(1000.0 + second)
    assert len(_beats(tmp_path)) == 4, "at 0, 30, 60 and 90 s"


def test_a_beating_deployment_is_never_stale_to_the_owner(tmp_path):
    """2026-10-07: nothing called heartbeat(), so the owner timed fullday from
    its last ORDER and printed it as silent every 30 s."""
    from forecast_eval.live.owner_process import OwnerProcess

    owner = OwnerProcess(None, None, directory=tmp_path, slugs=["day"],
                         account="ACC", deployment_timeout=60.0)
    path = intent_path(tmp_path, "day")
    path.write_text(json.dumps({"seq": 1, "ts": 0.0, "kind": "submit"}) + "\n",
                    encoding="utf-8")
    assert owner.stale_deployments() == ["day"], "an old order alone is stale"
    import time
    _client(tmp_path).maybe_heartbeat(time.monotonic())
    assert owner.stale_deployments() == []


def test_the_heartbeat_interval_is_well_inside_the_owners_timeout():
    from forecast_eval.live.broker.client import DEFAULT_HEARTBEAT_SECONDS
    from forecast_eval.live.owner_process import DEFAULT_DEPLOYMENT_TIMEOUT
    assert DEFAULT_DEPLOYMENT_TIMEOUT >= 3 * DEFAULT_HEARTBEAT_SECONDS


# ---- the month an intent is for (2026-10-20) --------------------------------

def test_submit_names_the_month_this_deployment_trades(tmp_path):
    c = _client(tmp_path, contract_code="TXFJ6")
    c.submit(_order(), current_price=100.0)
    (record,) = JsonlStream(intent_path(tmp_path, "day")).read_new()
    assert record["contract"] == "TXFJ6"


def test_a_roll_moves_the_month_later_intents_name(tmp_path):
    c = _client(tmp_path, contract_code="TXFJ6")
    c.switch_contract(type("C", (), {"code": "TXFR2", "target_code": "TXFK6"})())
    c.submit(_order(), current_price=100.0)
    (record,) = JsonlStream(intent_path(tmp_path, "day")).read_new()
    assert record["contract"] == "TXFK6", "the dated month, never the alias"


def test_no_month_sends_no_field(tmp_path):
    c = _client(tmp_path)
    c.submit(_order(), current_price=100.0)
    (record,) = JsonlStream(intent_path(tmp_path, "day")).read_new()
    assert "contract" not in record


# ---- settling a lot the exchange settled at expiry -------------------------

class _Tick:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_settle_names_the_held_month_and_the_size(tmp_path):
    c = _client(tmp_path, contract_code="TXFJ6")
    c.settle(-1, price=46420.0, timestamp=TS)
    (record,) = JsonlStream(intent_path(tmp_path, "day")).read_new()
    assert (record["kind"], record["contract"], record["signed_qty"]) == (
        "settle", "TXFJ6", -1)
    assert record["price"] == 46420.0


def test_positions_from_before_the_owner_settled_are_not_compared(tmp_path):
    owner = _owner(tmp_path)
    owner.append("position", signed_qty=1, avg_price=100.0)
    c = _client(tmp_path, contract_code="TXFJ6")
    c.settle(1, price=100.0, timestamp=TS)
    owner.append("position", signed_qty=1, avg_price=100.0)   # written earlier
    assert c.broker_position() is None
    owner.append("settled", contract="TXFJ6", signed_qty=1)
    owner.append("position", signed_qty=0)
    assert c.broker_position() == 0


def test_an_owner_that_never_confirms_a_settlement_halts_the_run(tmp_path):
    rails, clock = _Rails(), _Tick()
    owner = _owner(tmp_path)
    c = _client(tmp_path, contract_code="TXFJ6", rails=rails, clock=clock)
    c.settle(1, price=100.0, timestamp=TS)
    clock.t += c.owner_timeout + 1
    owner.append("position", signed_qty=1, avg_price=100.0)
    assert c.broker_position() is None
    assert any("did not confirm the settlement of TXFJ6" in r
               for r, _ in rails.halts)
