"""Tests for live/owner_process.py — the venue-facing half of the owner (§18.2).

Driven end to end through a real `ClientBroker` where it matters: the wire is the
thing most likely to be subtly wrong, and a fake on both sides would not prove it.
"""
import pandas as pd
import pytest

from forecast_eval.events import MarketEvent, OrderEvent
from forecast_eval.live.broker.client import ClientBroker
from forecast_eval.live.intents import JsonlStream, event_path
from forecast_eval.live.owner import COVER, NEW, OrderBook
from forecast_eval.live.owner_process import OwnerProcess

TS = pd.Timestamp("2026-09-07 10:00:00")


# ---- a fake shioaji, small enough to read ----------------------------------

class _Enum(str):
    """Stands in for sj.Action.Buy etc — compares by value, prints like one."""


class _FakeOrder:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _FakeSJ:
    Action = type("Action", (), {"Buy": _Enum("Buy"), "Sell": _Enum("Sell")})
    FuturesPriceType = type("P", (), {"LMT": _Enum("LMT")})
    OrderType = type("O", (), {"ROD": _Enum("ROD")})
    FuturesOCType = type("OC", (), {"New": _Enum("New"), "Cover": _Enum("Cover")})
    FuturesOrder = staticmethod(lambda **kw: _FakeOrder(**kw))


class _FakeTrade:
    def __init__(self, oid, ordno=None):
        self.order = type("o", (), {"id": oid, "ordno": ordno})()


class _FakeAPI:
    """Records placements; hands out order ids; answers list_positions."""

    def __init__(self, positions=None, raise_on_place=False):
        self.placed = []
        self.contracts = []
        self.cancelled = []
        self.callback = None
        self.positions = positions if positions is not None else []
        self.raise_on_place = raise_on_place
        self._n = 0

    def set_order_callback(self, cb):
        self.callback = cb

    def place_order(self, contract, order):
        if self.raise_on_place:
            raise RuntimeError("venue said no")
        self._n += 1
        oid = f"OID{self._n}"
        self.placed.append((order, oid))
        self.contracts.append(contract)
        return _FakeTrade(oid, ordno=f"NO{self._n}")

    def cancel_order(self, trade):
        self.cancelled.append(trade)

    def list_positions(self, account=None):
        return self.positions


class _Row:
    def __init__(self, qty, direction="Buy", code="TXFI6"):
        self.quantity, self.direction, self.code = qty, direction, code


class _Contract:
    code = "TXFI6"
    target_code = "TXFI6"


def _owner(tmp_path, api=None, slugs=("day", "night"), **kw):
    api = api or _FakeAPI()
    owner = OwnerProcess(api, _Contract(), directory=tmp_path, slugs=slugs,
                         account="ACC", sj=_FakeSJ, **kw)
    return owner, api


def _deal(owner, ordno, side="Buy", qty=1, price=100.0, seq="1", trade_id=None):
    owner._on_event(type("S", (), {"name": "FuturesDeal"})(), {
        "ordno": ordno, "trade_id": trade_id, "exchange_seq": seq,
        "action": side, "price": price, "quantity": qty, "ts": TS.timestamp(),
    })


def _order_event(owner, ordno, oid, op_type="New", op_code="00", msg=""):
    owner._on_event(type("S", (), {"name": "FuturesOrder"})(), {
        "operation": {"op_type": op_type, "op_code": op_code, "op_msg": msg},
        "order": {"ordno": ordno, "id": oid},
        "contract": {"code": "TXFI6"},
    })


# ---- placing ---------------------------------------------------------------

def test_a_submit_intent_becomes_a_venue_order(tmp_path):
    owner, api = _owner(tmp_path)
    client = ClientBroker(tmp_path, "day")
    client.submit(OrderEvent(timestamp=TS, side="BUY", limit_price=100.0,
                             intent="OPEN", quantity=1), current_price=100.0)
    owner.poll()
    assert len(api.placed) == 1
    placed, _ = api.placed[0]
    assert placed.action == "Buy"
    assert placed.quantity == 1
    assert placed.octype == "New"
    assert placed.price == 100.0


def test_a_crossing_intent_becomes_two_orders_cover_first(tmp_path):
    """§18.9: +1 crossed to -1 is Cover 1 then New 1, decided here not at the client."""
    owner, api = _owner(tmp_path)
    owner.seed_from({"day": 1, "night": 0})
    client = ClientBroker(tmp_path, "day")
    client.submit(OrderEvent(timestamp=TS, side="SELL", limit_price=100.0,
                             intent="CLOSE", quantity=2), current_price=100.0)
    owner.poll()
    assert [p.octype for p, _ in api.placed] == ["Cover", "New"]
    assert [p.quantity for p, _ in api.placed] == [1, 1]


def test_the_split_is_computed_against_the_account_not_the_deployment(tmp_path):
    """A deployment closing its own long may be OPENING at the venue."""
    owner, api = _owner(tmp_path)
    # day holds +1, night holds -1 -> the account is flat.
    owner.seed_from({"day": 1, "night": -1})
    client = ClientBroker(tmp_path, "day")
    client.submit(OrderEvent(timestamp=TS, side="SELL", limit_price=100.0,
                             intent="CLOSE", quantity=1), current_price=100.0)
    owner.poll()
    assert [p.octype for p, _ in api.placed] == ["New"], \
        "the account is flat, so this CLOSE opens at the venue"


def test_urgent_intents_are_priced_through_the_touch(tmp_path):
    owner, api = _owner(tmp_path, flatten_aggression_ticks=5, tick_size=1.0)
    client = ClientBroker(tmp_path, "day")
    with client.urgent():
        client.submit(OrderEvent(timestamp=TS, side="SELL", limit_price=100.0,
                                 intent="CLOSE", quantity=1), current_price=100.0)
    owner.poll()
    placed, _ = api.placed[0]
    assert placed.price == 95.0, "a SELL that must fill is willing to take less"


def test_a_place_failure_is_reported_to_that_deployment_only(tmp_path):
    owner, api = _owner(tmp_path, api=_FakeAPI(raise_on_place=True))
    day, night = ClientBroker(tmp_path, "day"), ClientBroker(tmp_path, "night")
    day.submit(OrderEvent(timestamp=TS, side="BUY", limit_price=100.0,
                          intent="OPEN", quantity=1), current_price=100.0)
    owner.poll()
    rails = []
    day.rails = type("R", (), {"halt": lambda s, r, a: rails.append(r)})()
    day.poll_fills(MarketEvent(timestamp=TS, price=100.0, volume=1, session="DAY"))
    assert any("owner rejected" in r for r in rails)
    assert night.has_pending() is False


def test_a_malformed_submit_intent_does_not_place(tmp_path):
    owner, api = _owner(tmp_path)
    from forecast_eval.live.intents import JsonlStream, intent_path
    JsonlStream(intent_path(tmp_path, "day")).append("submit", side="BUY")
    owner.poll()
    assert api.placed == []
    assert owner.anomalies and "malformed submit" in owner.anomalies[0]


# ---- fills come back -------------------------------------------------------

def test_a_fill_reaches_the_deployment_that_placed_it(tmp_path):
    owner, api = _owner(tmp_path)
    day, night = ClientBroker(tmp_path, "day"), ClientBroker(tmp_path, "night")
    day.submit(OrderEvent(timestamp=TS, side="BUY", limit_price=100.0,
                          intent="OPEN", quantity=1), current_price=100.0)
    owner.poll()
    _deal(owner, "NO1", side="Buy", qty=1, price=99.5)
    market = MarketEvent(timestamp=TS, price=100.0, volume=1, session="DAY")
    (fill,) = day.poll_fills(market)
    assert fill.side == "BUY" and fill.fill_price == 99.5
    assert night.poll_fills(market) == [], "a peer must not see it"


def test_done_arrives_only_after_both_legs_of_a_split_finish(tmp_path):
    """The deployment submitted ONE order and must see ONE done."""
    owner, api = _owner(tmp_path)
    owner.seed_from({"day": 1, "night": 0})
    day = ClientBroker(tmp_path, "day")
    day.submit(OrderEvent(timestamp=TS, side="SELL", limit_price=100.0,
                          intent="CLOSE", quantity=2), current_price=100.0)
    owner.poll()
    market = MarketEvent(timestamp=TS, price=100.0, volume=1, session="DAY")
    _deal(owner, "NO1", side="Sell", qty=1)
    day.poll_fills(market)
    assert day.has_pending() is True, "the New leg is still working"
    _deal(owner, "NO2", side="Sell", qty=1)
    day.poll_fills(market)
    assert day.has_pending() is False


def test_partial_fills_arrive_as_separate_events(tmp_path):
    owner, api = _owner(tmp_path)
    day = ClientBroker(tmp_path, "day")
    day.submit(OrderEvent(timestamp=TS, side="BUY", limit_price=100.0,
                          intent="OPEN", quantity=2), current_price=100.0)
    owner.poll()
    _deal(owner, "NO1", side="Buy", qty=1, price=100.0, seq="1")
    _deal(owner, "NO1", side="Buy", qty=1, price=101.0, seq="2")
    fills = day.poll_fills(MarketEvent(timestamp=TS, price=100.0, volume=1,
                                       session="DAY"))
    assert [f.quantity for f in fills] == [1, 1]
    assert [f.fill_price for f in fills] == [100.0, 101.0]


def test_a_fill_carries_a_fee(tmp_path):
    owner, api = _owner(tmp_path, fee_rate=0.001)
    day = ClientBroker(tmp_path, "day")
    day.submit(OrderEvent(timestamp=TS, side="BUY", limit_price=100.0,
                          intent="OPEN", quantity=1), current_price=100.0)
    owner.poll()
    _deal(owner, "NO1", side="Buy", qty=1, price=200.0)
    (fill,) = day.poll_fills(MarketEvent(timestamp=TS, price=100.0, volume=1,
                                         session="DAY"))
    assert fill.fee == pytest.approx(0.2)


def test_a_fill_is_stamped_in_venue_local_time(tmp_path):
    """A deal's `ts` is a UTC epoch; every other timestamp is naive Taipei time.

    MEASURED 2026-10-08: night's SELL, ordered at 22:15 local and filled at
    49,440 at 22:16:52, was booked at 14:16:52 — between sessions, eight hours
    before its own order. `TS.timestamp()` in `_deal` reads a naive TS as UTC,
    so the other tests round-trip it and cannot see this."""
    owner, api = _owner(tmp_path)
    night = ClientBroker(tmp_path, "night")
    night.submit(OrderEvent(timestamp=pd.Timestamp("2026-10-07 22:15:00"), side="SELL",
                            limit_price=49440.0, intent="OPEN", quantity=1),
                 current_price=49440.0)
    owner.poll()
    owner._on_event(type("S", (), {"name": "FuturesDeal"})(), {
        "ordno": "NO1", "trade_id": None, "exchange_seq": "1", "action": "Sell",
        "price": 49440.0, "quantity": 1,
        "ts": pd.Timestamp("2026-10-07 14:16:52", tz="UTC").timestamp()})
    (fill,) = night.poll_fills(MarketEvent(timestamp=TS, price=49440.0, volume=1,
                                           session="NIGHT"))
    assert fill.timestamp == pd.Timestamp("2026-10-07 22:16:52")


# ---- the deal callback -----------------------------------------------------

def test_deal_is_dispatched_before_order(tmp_path):
    """`str(state)` is "OrderState.FuturesDeal" — it contains ORDER too.

    Testing ORDER first would route every deal into the order handler and
    silently drop every fill. This uses a state with no `name`, so the dispatch
    falls back to `str(state)` and the ambiguity is real.
    """
    class _Ambiguous:
        name = None

        def __str__(self):
            return "OrderState.FuturesDeal"

    owner, api = _owner(tmp_path)
    day = ClientBroker(tmp_path, "day")
    day.submit(OrderEvent(timestamp=TS, side="BUY", limit_price=100.0,
                          intent="OPEN", quantity=1), current_price=100.0)
    owner.poll()
    owner._on_event(_Ambiguous(), {
        "ordno": "NO1", "trade_id": None, "exchange_seq": "1", "action": "Buy",
        "price": 100.0, "quantity": 1, "ts": TS.timestamp()})
    assert owner.anomalies == [], "the deal must not have been misrouted"
    (fill,) = day.poll_fills(MarketEvent(timestamp=TS, price=100.0, volume=1,
                                         session="DAY"))
    assert fill.quantity == 1


def test_an_unrecognised_callback_state_is_an_anomaly(tmp_path):
    owner, _ = _owner(tmp_path)
    owner._on_event(type("S", (), {"name": "Something"})(), {})
    assert owner.anomalies and "unrecognised" in owner.anomalies[0]


def test_an_unreadable_payload_is_an_anomaly(tmp_path):
    owner, _ = _owner(tmp_path)
    owner._on_event(type("S", (), {"name": "FuturesDeal"})(), "not a mapping")
    assert owner.anomalies and "has no .get()" in owner.anomalies[0]
    # the type is named, so the next surprise from the compiled layer is
    # diagnosable from the anomaly alone rather than from another live run
    assert "type=str" in owner.anomalies[0]


class _NotADict:
    """A mapping that renders like a dict but is not a `dict` instance.

    This is the shape shioaji 1.7.2's compiled layer actually hands to the order
    callback — measured 2026-09-07 night, §22.
    """

    def __init__(self, data):
        self._data = data

    def get(self, key, default=None):
        return self._data.get(key, default)

    def __repr__(self):
        return repr(self._data)


def test_a_dict_like_payload_that_is_not_a_dict_still_books_the_deal(tmp_path):
    """§22 — the isinstance guard silently dropped every real fill.

    Two orders filled at the venue on 2026-09-07 and the owner booked neither,
    because the payload renders as a dict but fails `isinstance(x, dict)`.
    """
    owner, _ = _owner(tmp_path)
    day = ClientBroker(tmp_path, "day")
    day.submit(OrderEvent(timestamp=TS, side="BUY", limit_price=100.0,
                          intent="OPEN", quantity=1), current_price=100.0)
    owner.poll()

    owner._on_event(type("S", (), {"name": "FuturesOrder"})(), _NotADict({
        "operation": {"op_type": "New", "op_code": "00", "op_msg": ""},
        "order": {"id": "OID1", "ordno": "NO1"},
        "contract": {"code": "TXFI6"}}))
    owner._on_event(type("S", (), {"name": "FuturesDeal"})(), _NotADict({
        "trade_id": "OID1", "ordno": "NO1", "exchange_seq": "000001",
        "action": "Buy", "code": "TXFI6", "price": 100.0, "quantity": 1}))

    assert owner.anomalies == []
    assert owner.book.net_for("day") == 1
    assert owner.book.account_net == 1


def test_a_rejection_reaches_the_owning_deployment_and_finishes_the_leg(tmp_path):
    owner, api = _owner(tmp_path)
    day = ClientBroker(tmp_path, "day")
    day.submit(OrderEvent(timestamp=TS, side="BUY", limit_price=100.0,
                          intent="OPEN", quantity=1), current_price=100.0)
    owner.poll()
    _order_event(owner, "NO1", "OID1", op_code="99", msg="margin")
    assert owner.book.has_pending("day") is False
    rails = []
    day.rails = type("R", (), {"halt": lambda s, r, a: rails.append(r)})()
    day.poll_fills(MarketEvent(timestamp=TS, price=100.0, volume=1, session="DAY"))
    assert any("owner rejected" in r for r in rails)


def test_a_confirmed_cancel_finishes_the_leg(tmp_path):
    owner, api = _owner(tmp_path)
    day = ClientBroker(tmp_path, "day")
    day.submit(OrderEvent(timestamp=TS, side="BUY", limit_price=100.0,
                          intent="OPEN", quantity=1), current_price=100.0)
    owner.poll()
    _order_event(owner, "NO1", "OID1", op_type="Cancel")
    assert owner.book.has_pending("day") is False
    day.poll_fills(MarketEvent(timestamp=TS, price=100.0, volume=1, session="DAY"))
    assert day.has_pending() is False


# ---- cancelling ------------------------------------------------------------

def test_cancel_reaches_the_venue_for_that_deployment_only(tmp_path):
    owner, api = _owner(tmp_path)
    day, night = ClientBroker(tmp_path, "day"), ClientBroker(tmp_path, "night")
    day.submit(OrderEvent(timestamp=TS, side="BUY", limit_price=100.0,
                          intent="OPEN", quantity=1), current_price=100.0)
    night.submit(OrderEvent(timestamp=TS, side="BUY", limit_price=100.0,
                            intent="OPEN", quantity=1), current_price=100.0)
    owner.poll()
    day.cancel()
    owner.poll()
    assert len(api.cancelled) == 1
    assert owner.book.has_pending("night") is True


def test_a_fill_after_a_cancel_request_is_still_booked(tmp_path):
    """§2.2 — a cancelled order can still fill, and that fill is real money."""
    owner, api = _owner(tmp_path)
    day = ClientBroker(tmp_path, "day")
    day.submit(OrderEvent(timestamp=TS, side="BUY", limit_price=100.0,
                          intent="OPEN", quantity=1), current_price=100.0)
    owner.poll()
    day.cancel()
    owner.poll()
    _deal(owner, "NO1", side="Buy", qty=1)
    (fill,) = day.poll_fills(MarketEvent(timestamp=TS, price=100.0, volume=1,
                                         session="DAY"))
    assert fill.quantity == 1
    assert owner.book.account_net == 1


# ---- positions and reconciliation ------------------------------------------

def test_each_deployment_is_told_its_own_size_not_the_accounts(tmp_path):
    owner, api = _owner(tmp_path)
    owner.seed_from({"day": 1, "night": -1})
    owner.publish_positions()
    day, night = ClientBroker(tmp_path, "day"), ClientBroker(tmp_path, "night")
    assert day.broker_position() == 1
    assert night.broker_position() == -1
    assert owner.book.account_net == 0


def test_reconcile_passes_when_the_venue_agrees(tmp_path):
    owner, api = _owner(tmp_path, api=_FakeAPI(positions=[_Row(2, "Buy")]))
    owner.seed_from({"day": 2, "night": 0})
    assert owner.reconcile() is True
    assert owner.anomalies == []


def test_reconcile_fails_and_names_the_split_when_the_venue_disagrees(tmp_path):
    owner, api = _owner(tmp_path, api=_FakeAPI(positions=[_Row(3, "Buy")]))
    owner.seed_from({"day": 1, "night": 1})
    assert owner.reconcile() is False
    assert owner.anomalies and "mirror 2 != venue 3" in owner.anomalies[0]
    assert "'day': 1" in owner.anomalies[0]


def test_reconcile_degrades_to_none_when_the_venue_will_not_answer(tmp_path):
    class _Broken(_FakeAPI):
        def list_positions(self, account=None):
            raise RuntimeError("timeout")

    owner, _ = _owner(tmp_path, api=_Broken())
    assert owner.reconcile() is None, "cannot compare is not a mismatch"


def test_reconcile_ignores_a_foreign_contract(tmp_path):
    owner, _ = _owner(tmp_path,
                      api=_FakeAPI(positions=[_Row(9, "Buy", code="MXFI6")]))
    assert owner.reconcile() is True


# ---- the grace for fills in flight (2026-10-06) ----------------------------

class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _working_buy(tmp_path, api, clock, slug="day"):
    """An owner with one BUY 1 leg placed for `slug` and not yet filled."""
    owner, _ = _owner(tmp_path, api=api, clock=clock)
    ClientBroker(tmp_path, slug).submit(
        OrderEvent(timestamp=TS, side="BUY", limit_price=100.0, intent="OPEN",
                   quantity=1), current_price=100.0)
    owner.poll()
    assert len(api.placed) == 1
    return owner


def test_a_fill_the_venue_reports_before_its_deal_is_waited_out(tmp_path):
    """The 2026-10-06 sequence: 14AD46 filled at 09:14:04.5, the reconcile at
    ~09:14:07 read venue +1 against a book at 0, and the owner stopped before
    the deal arrived. A working BUY leg explains +1, so it is pending."""
    clock, api = _Clock(), _FakeAPI(positions=[_Row(1, "Buy")])
    owner = _working_buy(tmp_path, api, clock)
    clock.t = 2.5
    assert owner.reconcile() is True
    assert owner.anomalies == []
    assert "explained by orders in flight" in owner.reconcile_note

    clock.t = 40.0                       # §23.7 measured a 42 s booking
    _deal(owner, "NO1", side="Buy", qty=1)
    assert owner.reconcile() is True
    assert owner.reconcile_note is None, "settled once the deal is booked"


def test_an_explained_difference_fails_once_the_grace_runs_out(tmp_path):
    """A deal callback that never comes is a real desync (§23.6)."""
    clock, api = _Clock(), _FakeAPI(positions=[_Row(1, "Buy")])
    owner = _working_buy(tmp_path, api, clock)
    assert owner.reconcile() is True
    clock.t = 120.0
    assert owner.reconcile() is True, "the grace is inclusive"
    clock.t = 121.0
    assert owner.reconcile() is False
    assert "past the 120s grace" in owner.anomalies[-1]
    assert "'day': 0" in owner.anomalies[-1]


def test_a_difference_on_the_wrong_side_fails_at_once(tmp_path):
    """A working BUY cannot explain the venue being SHORT of the book."""
    clock, api = _Clock(), _FakeAPI(positions=[_Row(1, "Sell")])
    owner = _working_buy(tmp_path, api, clock)
    assert owner.reconcile() is False
    assert "mirror 0 != venue -1" in owner.anomalies[-1]


def test_a_difference_larger_than_the_working_legs_fails_at_once(tmp_path):
    clock, api = _Clock(), _FakeAPI(positions=[_Row(2, "Buy")])
    owner = _working_buy(tmp_path, api, clock)
    assert owner.reconcile() is False


def test_a_venue_still_catching_up_to_a_booked_deal_is_waited_out(tmp_path):
    """The other side of the race: booked first, `list_positions` ~0.1 s
    behind (§18.10). The leg is finished by then, so only the booked deal
    explains it — and only for the grace."""
    clock, api = _Clock(), _FakeAPI(positions=[])
    owner = _working_buy(tmp_path, api, clock)
    _deal(owner, "NO1", side="Buy", qty=1)
    clock.t = 0.1
    assert owner.reconcile() is True
    assert owner.anomalies == []
    clock.t = 200.0
    assert owner.reconcile() is False, "a deal the venue never shows is a desync"


def test_the_grace_restarts_after_the_venue_agrees(tmp_path):
    clock, api = _Clock(), _FakeAPI(positions=[_Row(1, "Buy")])
    owner = _working_buy(tmp_path, api, clock)
    assert owner.reconcile() is True               # pending from t=0
    clock.t = 100.0
    api.positions = []
    assert owner.reconcile() is True               # agrees: the timer resets
    clock.t = 110.0
    api.positions = [_Row(1, "Buy")]
    assert owner.reconcile() is True               # pending from t=110
    clock.t = 200.0
    assert owner.reconcile() is True, "90 s into a new wait, not 200 s"


def test_start_up_has_no_grace(tmp_path):
    """A fresh owner has nothing in flight, so a seed the venue disputes is
    still refused at once — the start-up check is unchanged."""
    owner, _ = _owner(tmp_path, api=_FakeAPI(positions=[_Row(1, "Buy")]),
                      clock=_Clock())
    owner.seed_from({"day": 0, "night": 0})
    assert owner.reconcile() is False


# ---- freezing --------------------------------------------------------------

def test_a_frozen_owner_rejects_new_submits(tmp_path):
    owner, api = _owner(tmp_path)
    owner.freeze("account mirror 0 != venue 1")
    day = ClientBroker(tmp_path, "day")
    day.submit(OrderEvent(timestamp=TS, side="BUY", limit_price=100.0,
                          intent="OPEN", quantity=1), current_price=100.0)
    owner.poll()
    assert api.placed == []
    events = [e["kind"] for e in JsonlStream(event_path(tmp_path, "day"))._read_all()]
    assert "rejected" in events
    day.poll_fills(MarketEvent(timestamp=TS, price=100.0, volume=1,
                               session="DAY"))
    assert day.has_pending() is False, "the rejection reaches the deployment"


def test_a_frozen_owner_still_books_a_deal_and_tells_its_deployment(tmp_path):
    """The point of freezing instead of exiting: the fill in flight lands."""
    owner, api = _owner(tmp_path)
    day = ClientBroker(tmp_path, "day")
    day.submit(OrderEvent(timestamp=TS, side="BUY", limit_price=100.0,
                          intent="OPEN", quantity=1), current_price=100.0)
    owner.poll()
    owner.freeze("account mirror 0 != venue 1")
    _deal(owner, "NO1", side="Buy", qty=1)
    owner.publish_positions()
    (fill,) = day.poll_fills(MarketEvent(timestamp=TS, price=100.0, volume=1,
                                         session="DAY"))
    assert fill.quantity == 1
    assert day.broker_position() == 1


def test_a_frozen_owner_still_cancels(tmp_path):
    """A cancel can only reduce what is working."""
    owner, api = _owner(tmp_path)
    day = ClientBroker(tmp_path, "day")
    day.submit(OrderEvent(timestamp=TS, side="BUY", limit_price=100.0,
                          intent="OPEN", quantity=1), current_price=100.0)
    owner.poll()
    owner.freeze("x")
    day.cancel()
    owner.poll()
    assert len(api.cancelled) == 1


def test_the_first_freeze_reason_is_kept(tmp_path):
    owner, _ = _owner(tmp_path)
    owner.freeze("first")
    owner.freeze("second")
    assert owner.frozen == "first"


# ---- restarts do not replay the wire ---------------------------------------

def test_a_restarted_owner_does_not_place_old_intents(tmp_path):
    """A fresh stream reads from byte 0: after 2026-10-06 the restarted owner
    would have re-sent fullday's 09:13 BUY on its first pass."""
    ClientBroker(tmp_path, "day").submit(
        OrderEvent(timestamp=TS, side="BUY", limit_price=100.0, intent="OPEN",
                   quantity=1), current_price=100.0)
    owner, api = _owner(tmp_path)
    owner.poll()
    assert api.placed == []
    assert owner.skipped_intents == {"day": 1, "night": 0}


def test_intents_written_after_the_owner_starts_are_placed(tmp_path):
    day = ClientBroker(tmp_path, "day")
    day.submit(OrderEvent(timestamp=TS, side="BUY", limit_price=100.0,
                          intent="OPEN", quantity=1), current_price=100.0)
    owner, api = _owner(tmp_path)
    day.submit(OrderEvent(timestamp=TS, side="SELL", limit_price=101.0,
                          intent="CLOSE", quantity=1), current_price=101.0)
    owner.poll()
    assert [p.action for p, _ in api.placed] == ["Sell"]


# ---- the owner's trail -----------------------------------------------------

class _ListRecorder:
    def __init__(self):
        self.records = []

    def record(self, event, **fields):
        self.records.append((event, fields))

    def of(self, event):
        return [f for e, f in self.records if e == event]


def test_each_placement_is_recorded_with_its_deployment(tmp_path):
    """On 2026-10-06 nothing on disk said whose order 14AD46 was."""
    rec = _ListRecorder()
    owner, api = _owner(tmp_path, recorder=rec)
    ClientBroker(tmp_path, "night").submit(
        OrderEvent(timestamp=TS, side="BUY", limit_price=100.0, intent="OPEN",
                   quantity=1), current_price=100.0)
    owner.poll()
    (placed,) = rec.of("owner_placed")
    assert placed["slug"] == "night"
    assert placed["ordno"] == "NO1" and placed["order_id"] == "OID1"


def test_a_pending_and_a_failed_reconcile_are_recorded(tmp_path):
    rec = _ListRecorder()
    clock, api = _Clock(), _FakeAPI(positions=[_Row(1, "Buy")])
    owner, _ = _owner(tmp_path, api=api, clock=clock, recorder=rec)
    ClientBroker(tmp_path, "day").submit(
        OrderEvent(timestamp=TS, side="BUY", limit_price=100.0, intent="OPEN",
                   quantity=1), current_price=100.0)
    owner.poll()
    owner.reconcile()
    clock.t = 500.0
    owner.reconcile()
    assert rec.of("owner_reconcile_pending")
    assert rec.of("owner_process_anomaly")


# ---- liveness --------------------------------------------------------------

def test_a_deployment_that_has_never_spoken_is_not_stale(tmp_path):
    """Absent at start-up is not the same as having gone away."""
    owner, _ = _owner(tmp_path)
    assert owner.stale_deployments() == []


def test_a_silent_deployment_is_reported(tmp_path):
    import json
    import time
    from forecast_eval.live.intents import intent_path
    intent_path(tmp_path, "day").write_text(
        json.dumps({"seq": 1, "ts": time.time() - 9999, "kind": "heartbeat"})
        + "\n", encoding="utf-8")
    owner, _ = _owner(tmp_path, deployment_timeout=60.0)
    assert owner.stale_deployments() == ["day"]


def test_a_fresh_heartbeat_is_not_stale(tmp_path):
    owner, _ = _owner(tmp_path, deployment_timeout=60.0)
    ClientBroker(tmp_path, "day").heartbeat()
    assert owner.stale_deployments() == []


def test_start_publishes_an_opening_position_so_clients_can_begin(tmp_path):
    """A client treats a stream that never spoke as 'cannot see the book'."""
    owner, api = _owner(tmp_path)
    day = ClientBroker(tmp_path, "day")
    assert day.broker_position() is None
    owner.start()
    assert api.callback is not None
    assert day.broker_position() == 0


def test_a_corrupt_intent_stream_does_not_stall_the_others(tmp_path):
    from forecast_eval.live.intents import intent_path
    intent_path(tmp_path, "day").write_text("not json\n", encoding="utf-8")
    owner, api = _owner(tmp_path)
    ClientBroker(tmp_path, "night").submit(
        OrderEvent(timestamp=TS, side="BUY", limit_price=100.0, intent="OPEN",
                   quantity=1), current_price=100.0)
    owner.poll()
    assert owner.anomalies and "corrupt record" in owner.anomalies[0]
    assert len(api.placed) == 1, "night's order still went through"


def test_every_callback_is_counted_by_kind(tmp_path):
    """§23.6 — "the deal never arrived" and "it arrived and was dropped" look
    identical from the book, and need opposite fixes."""
    owner, _ = _owner(tmp_path)
    assert owner.events_seen == {}

    owner._on_event(type("S", (), {"name": "FuturesOrder"})(), _NotADict({
        "operation": {"op_code": "00", "op_type": "New"},
        "order": {"id": "OID1", "ordno": "NO1"}}))
    owner._on_event(type("S", (), {"name": "FuturesDeal"})(), _NotADict({
        "trade_id": "OID1", "ordno": "NO1", "exchange_seq": "1",
        "action": "Buy", "price": 100.0, "quantity": 1}))

    assert owner.events_seen == {"FUTURESORDER": 1, "FUTURESDEAL": 1}


def test_a_payload_that_cannot_be_read_is_still_counted(tmp_path):
    """The count must reflect what the VENUE sent, not what we could parse —
    otherwise a parsing bug hides itself in the diagnostic meant to find it."""
    owner, _ = _owner(tmp_path)
    owner._on_event(type("S", (), {"name": "FuturesDeal"})(), "not a mapping")
    assert owner.events_seen == {"FUTURESDEAL": 1}
    assert owner.anomalies, "and it is still an anomaly"


# ---- the month each order is for (2026-10-20) -------------------------------
#
# simtrade_operations §11: the owner placed every order on the month it resolved
# at start-up, so night's and fullday's roll re-opens were October trades, and
# night's BUY closed fullday's SELL at the venue. Here `_Contract` (TXFI6) is the
# start-up month and TXFJ6 the month the deployments roll to.

class _Month:
    def __init__(self, code):
        self.code = self.target_code = code


def _resolving_owner(tmp_path, positions=None, **kw):
    resolved = []

    def resolve(code):
        resolved.append(code)
        if code != "TXFJ6":
            raise RuntimeError(f"contract {code!r} not found")
        return _Month(code)

    owner, api = _owner(tmp_path, api=_FakeAPI(positions=positions),
                        slugs=("night", "day_night"), resolve=resolve, **kw)
    return owner, api, resolved


def _submit(tmp_path, slug, side, contract=None):
    ClientBroker(tmp_path, slug, contract_code=contract).submit(
        OrderEvent(timestamp=TS, side=side, limit_price=100.0, intent="OPEN",
                   quantity=1), current_price=100.0)


def test_an_order_is_placed_on_the_month_its_intent_names(tmp_path):
    owner, api, resolved = _resolving_owner(tmp_path)
    _submit(tmp_path, "day_night", "SELL", contract="TXFJ6")
    owner.poll()
    assert resolved == ["TXFJ6"]
    assert api.contracts[-1].code == "TXFJ6"


def test_an_intent_naming_no_month_goes_to_the_start_up_month(tmp_path):
    owner, api, resolved = _resolving_owner(tmp_path)
    _submit(tmp_path, "night", "BUY")
    owner.poll()
    assert resolved == []
    assert api.contracts[-1].code == "TXFI6"


def test_a_month_is_resolved_once(tmp_path):
    owner, api, resolved = _resolving_owner(tmp_path)
    _submit(tmp_path, "day_night", "SELL", contract="TXFJ6")
    owner.poll()
    _submit(tmp_path, "day_night", "SELL", contract="TXFJ6")
    owner.poll()
    assert resolved == ["TXFJ6"]
    assert len(api.contracts) == 2


def test_a_lot_in_one_month_is_not_covered_by_an_order_in_another(tmp_path):
    # Night still holds +1 on the old month while fullday re-opens short on the
    # new one: that is a New on the new month, never a Cover of night's lot.
    owner, api, _ = _resolving_owner(tmp_path)
    owner.seed_from({"night": 1, "day_night": 0})
    _submit(tmp_path, "day_night", "SELL", contract="TXFJ6")
    owner.poll()
    (placed, _oid), = api.placed
    assert placed.octype == "New"
    assert owner.book.net_in("TXFI6") == 1


def test_a_month_the_owner_cannot_resolve_is_refused_not_placed(tmp_path):
    owner, api, resolved = _resolving_owner(tmp_path)
    _submit(tmp_path, "day_night", "SELL", contract="TXF202611")
    owner.poll()
    assert api.placed == []
    events = JsonlStream(event_path(tmp_path, "day_night")).read_new()
    assert any(e["kind"] == "rejected" and "TXF202611" in e["reason"]
               for e in events)


def test_without_a_resolver_a_new_month_is_refused(tmp_path):
    owner, api = _owner(tmp_path, slugs=("night",))
    _submit(tmp_path, "night", "BUY", contract="TXFJ6")
    owner.poll()
    assert api.placed == []


def test_a_deal_moves_the_net_of_the_month_it_was_placed_in(tmp_path):
    owner, api, _ = _resolving_owner(tmp_path)
    _submit(tmp_path, "day_night", "SELL", contract="TXFJ6")
    owner.poll()
    _deal(owner, "NO1", side="Sell")
    assert owner.book.net_in("TXFJ6") == -1
    assert owner.book.net_in("TXFI6") == 0
    assert owner.book.account_net == -1


def test_reconcile_compares_each_month_on_its_own(tmp_path):
    # The 10-20 books: +1 on one month and -1 on the other sum to the venue's
    # flat account, and that sum is exactly what hid the wrong month.
    owner, api, _ = _resolving_owner(tmp_path, positions=[])
    owner.seed_from({"night": 1, "day_night": 0})
    _submit(tmp_path, "day_night", "SELL", contract="TXFJ6")
    owner.poll()
    _deal(owner, "NO1", side="Sell")
    assert owner.book.account_net == 0
    assert owner.reconcile() is False
    assert any("in TXFI6" in a for a in owner.anomalies)


def test_reconcile_agrees_when_each_month_matches(tmp_path):
    owner, api, _ = _resolving_owner(tmp_path, positions=[
        _Row(1, "Buy", code="TXFI6"), _Row(1, "Sell", code="TXFJ6")])
    owner.seed_from({"night": 1, "day_night": 0})
    _submit(tmp_path, "day_night", "SELL", contract="TXFJ6")
    owner.poll()
    _deal(owner, "NO1", side="Sell")
    assert owner.reconcile() is True
    assert owner.anomalies == []


def test_a_fill_in_flight_in_one_month_does_not_excuse_another(tmp_path):
    # Venue one lot long of the book on the new month. A BUY working THERE is
    # the 10-06 race and waits; a BUY working on the OLD month explains nothing,
    # though summed across months it would have.
    clock = _Clock()
    owner, api, _ = _resolving_owner(tmp_path, positions=[], clock=clock)
    _submit(tmp_path, "day_night", "SELL", contract="TXFJ6")
    owner.poll()
    _deal(owner, "NO1", side="Sell")                    # book -1 on TXFJ6
    clock.t += owner.reconcile_grace + 1             # the deal is old news
    _submit(tmp_path, "night", "BUY")                   # working on TXFI6
    owner.poll()
    assert owner.reconcile() is False
    assert any("in TXFJ6" in a for a in owner.anomalies)


def test_a_fill_in_flight_in_its_own_month_still_waits(tmp_path):
    clock = _Clock()
    owner, api, _ = _resolving_owner(tmp_path, positions=[], clock=clock)
    _submit(tmp_path, "day_night", "SELL", contract="TXFJ6")
    owner.poll()
    _deal(owner, "NO1", side="Sell")
    clock.t += owner.reconcile_grace + 1             # the deal is old news
    _submit(tmp_path, "night", "BUY", contract="TXFJ6")
    owner.poll()
    assert owner.reconcile() is True
    assert "in TXFJ6" in owner.reconcile_note
