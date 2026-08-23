"""V2 — ShioajiBroker against a scripted fake. No network, no credentials.

Pass criteria from SHIOAJI_PLAN.md V2:
  * exactly one FillEvent per real deal, deduped on (ordno, exchange_seq);
  * rejections surface as a halt, never a silent no-op;
  * no fill is ever invented for an order that cannot account for it.

Plus the orderings the docs warn about: deal-before-order, duplicate deals, and
a cancel that loses the race to a fill.
"""
from datetime import datetime, timedelta

import pytest

from forecast_eval.events import MarketEvent, OrderEvent
from forecast_eval.live.broker.base import Broker
from forecast_eval.live.broker.shioaji import ShioajiBroker
from forecast_eval.live.state import HaltAction, RailsConfig, SafetyRails
from forecast_eval.tests.fakes.fake_shioaji import (
    Action,
    FakePosition,
    FakeShioaji,
    FakeSj,
    FuturesOCType,
    FuturesPriceType,
    OrderType,
    make_alias_contract,
    make_contract,
)

T0 = datetime(2026, 3, 5, 9, 0, 0)
MARKET = MarketEvent(T0, 17500.0, 1, "DAY")


def _broker(**kw):
    api = FakeShioaji()
    contract = make_contract()
    rails = SafetyRails(RailsConfig(), simulation=True)
    b = ShioajiBroker(api, contract, sj=FakeSj, rails=rails, fee_rate=0.001, **kw)
    return api, b, rails


def _buy(qty=1, price=17500.0, intent="OPEN"):
    return OrderEvent(T0, "BUY", price, intent, quantity=qty)


# ---- the urgent (FLATTEN) path — V4 ----------------------------------------

def test_a_normal_order_is_a_resting_limit():
    """The baseline the urgent path departs from: LMT + ROD, priced exactly as
    the Trader asked, matching the backtest's resting-limit semantics."""
    api, b, _ = _broker()
    b.submit(_buy(price=17500.0), current_price=17500.0)
    sent = api.placed[-1]
    assert sent.price_type is FuturesPriceType.LMT
    assert sent.order_type is OrderType.ROD
    assert sent.price == 17500.0


def test_an_urgent_close_is_priced_through_the_touch_and_still_rests():
    """§5 — a FLATTEN halt must actually get risk off, and MEASURED 2026-08-12
    that aggression is what does it, not the time-in-force. An IOC close priced
    through a *stationary* market went unfilled for 20 s with the limit reachable
    throughout: the venue matches on prints, and a quiet minute has none. The
    expiry also left `has_pending()` True, which is what the flatten retry is
    gated on — the position would never have been closed."""
    api, b, _ = _broker(tick_size=1.0, flatten_aggression_ticks=5)
    with b.urgent():
        b.submit(OrderEvent(T0, "SELL", 17500.0, "CLOSE"), current_price=17500.0)
    sent = api.placed[-1]
    assert sent.order_type is OrderType.ROD, "must rest, not expire"
    assert sent.price_type is FuturesPriceType.LMT, "bounded, not MKT"
    assert sent.price == 17495.0, "a SELL must be willing to accept less"


def test_urgency_changes_the_price_and_nothing_else():
    """The whole difference is aggression. Keeping the time-in-force identical is
    what makes an unfilled flatten *rest* rather than vanish."""
    api, b, _ = _broker(tick_size=1.0, flatten_aggression_ticks=5)
    b.submit(_buy(price=17500.0), current_price=17500.0)
    ordinary = api.placed[-1]
    b.cancel()
    with b.urgent():
        b.submit(OrderEvent(T0, "BUY", 17500.0, "CLOSE"), current_price=17500.0)
    urgent = api.placed[-1]
    assert urgent.order_type is ordinary.order_type is OrderType.ROD
    assert urgent.price_type is ordinary.price_type
    assert urgent.price == 17505.0 and ordinary.price == 17500.0


def test_urgency_is_directional():
    """A BUY closing a short has to be willing to pay *more*. Getting the sign
    wrong would price the flatten further from the market — the exact opposite
    of urgent, and it would look like it worked."""
    api, b, _ = _broker(tick_size=1.0, flatten_aggression_ticks=5)
    with b.urgent():
        b.submit(OrderEvent(T0, "BUY", 17500.0, "CLOSE"), current_price=17500.0)
    assert api.placed[-1].price == 17505.0


def test_urgency_does_not_leak_past_the_block():
    api, b, _ = _broker()
    with b.urgent():
        pass
    b.submit(_buy(price=17500.0), current_price=17500.0)
    assert api.placed[-1].order_type is OrderType.ROD
    assert api.placed[-1].price == 17500.0


def test_urgency_is_restored_even_when_the_body_raises():
    """An exception mid-flatten must not leave every later order priced as if it
    were an emergency."""
    api, b, _ = _broker()
    with pytest.raises(ValueError):
        with b.urgent():
            raise ValueError("boom")
    b.submit(_buy(price=17500.0), current_price=17500.0)
    assert api.placed[-1].order_type is OrderType.ROD


def test_the_urgent_price_stays_on_the_tick_grid():
    b = _broker(tick_size=1.0, flatten_aggression_ticks=3)[1]
    assert b.urgent_price("BUY", 17500.5) == 17504.0
    assert b.urgent_price("SELL", 17500.5) == 17498.0


def test_the_submit_record_marks_which_orders_were_urgent(tmp_path):
    """V4 reads fill rate and slippage back from the trail, so an urgent order
    must be distinguishable — it is priced differently and should not be pooled
    with the strategy's own limits when the gap is measured."""
    from forecast_eval.live.recorder import Recorder
    rec = Recorder(tmp_path / "s.jsonl", simulation=True)
    api, b, _ = _broker(recorder=rec)
    with b.urgent():
        b.submit(OrderEvent(T0, "SELL", 17500.0, "CLOSE"), current_price=17500.0)
    rec.close()

    import json
    subs = [json.loads(l) for l in (tmp_path / "s.jsonl").read_text(
        encoding="utf-8").splitlines() if '"broker_submit"' in l]
    assert subs[-1]["urgent"] is True
    assert subs[-1]["limit_price"] == 17500.0, "what the Trader asked for"
    assert subs[-1]["sent_price"] == 17495.0, "what actually went to the venue"
    # The recorded time-in-force must be read off the order that was sent.
    assert subs[-1]["order_type"] == str(
        getattr(api.placed[-1].order_type, "value", api.placed[-1].order_type))


def test_the_recorded_order_type_cannot_drift_from_the_one_sent(tmp_path):
    """Regression: this was a duplicated conditional, and when the sent value
    changed IOC→ROD the log kept saying IOC. A trail describing an order the
    venue never received is worse than none — it is the input to every later
    diagnosis, and it cost one before being caught."""
    import json
    from forecast_eval.live.recorder import Recorder
    rec = Recorder(tmp_path / "s.jsonl", simulation=True)
    api, b, _ = _broker(recorder=rec)
    b.submit(_buy(price=17500.0), current_price=17500.0)
    b.cancel()
    with b.urgent():
        b.submit(OrderEvent(T0, "SELL", 17500.0, "CLOSE"), current_price=17500.0)
    rec.close()

    subs = [json.loads(l) for l in (tmp_path / "s.jsonl").read_text(
        encoding="utf-8").splitlines() if '"broker_submit"' in l]
    for record, sent in zip(subs, api.placed):
        expected = str(getattr(sent.order_type, "value", sent.order_type))
        assert record["order_type"] == expected


# ---- interface conformance --------------------------------------------------

def test_is_a_broker():
    _, b, _ = _broker()
    assert isinstance(b, Broker)


def test_registers_its_order_callback_at_construction():
    api, b, _ = _broker()
    assert api._callback is not None


# ---- order translation ------------------------------------------------------

def test_open_maps_to_new_and_close_maps_to_cover():
    """Explicit octype, never Auto — the no-flip invariant depends on meaning
    what we said (§2.3)."""
    api, b, _ = _broker()
    b.submit(_buy(intent="OPEN"), current_price=17500.0)
    assert api.placed[-1].octype is FuturesOCType.New
    b.cancel()
    b.submit(OrderEvent(T0, "SELL", 17400.0, "CLOSE"), current_price=17500.0)
    assert api.placed[-1].octype is FuturesOCType.Cover
    assert api.placed[-1].action is Action.Sell


def test_order_is_a_resting_limit_matching_the_backtest():
    api, b, _ = _broker()
    b.submit(_buy(qty=2, price=17499.0), current_price=17500.0)
    o = api.placed[-1]
    assert o.price_type is FuturesPriceType.LMT
    assert o.order_type is OrderType.ROD
    assert o.price == 17499.0 and o.quantity == 2
    assert o.account is api.futopt_account


# ---- the normal path --------------------------------------------------------

def test_single_deal_produces_exactly_one_fill():
    api, b, _ = _broker()
    b.submit(_buy(), current_price=17500.0)
    trade = api.trades[-1]
    api.emit_order(trade=trade, ordno="N0001")
    api.emit_deal(trade=trade, ordno="N0001", price=17501.0, quantity=1)

    fills = b.poll_fills(MARKET)
    assert len(fills) == 1
    f = fills[0]
    assert f.side == "BUY" and f.fill_price == 17501.0 and f.quantity == 1
    assert f.fee == pytest.approx(17501.0 * 0.001 * 1)
    assert b.poll_fills(MARKET) == [], "poll_fills must drain, not peek"


def test_fully_filled_order_stops_being_pending():
    api, b, _ = _broker()
    b.submit(_buy(), current_price=17500.0)
    trade = api.trades[-1]
    assert b.has_pending()
    api.emit_order(trade=trade, ordno="N0001")
    api.emit_deal(trade=trade, ordno="N0001")
    assert not b.has_pending()


# ---- the documented nasty orderings ----------------------------------------

def test_deal_arriving_before_the_order_event_is_still_booked():
    """The docs warn deal events can precede order events. Correlation therefore
    cannot depend on having learned `ordno` first."""
    api, b, _ = _broker()
    b.submit(_buy(), current_price=17500.0)
    trade = api.trades[-1]
    api.emit_deal(trade=trade, ordno="N0001", price=17502.0)   # deal FIRST
    api.emit_order(trade=trade, ordno="N0001")                 # order after
    fills = b.poll_fills(MARKET)
    assert len(fills) == 1 and fills[0].fill_price == 17502.0


def test_duplicate_deal_is_booked_once():
    """Isolates dedup specifically: the order is for 3 lots and only 1 has
    filled, so the order is still working and the repeat is well within its
    quantity. Nothing but the (ordno, exchange_seq) key can reject it — an
    earlier version of this test used a fully-filled 1-lot order, where the
    repeats were rejected as unattributable and dedup was never exercised."""
    api, b, rails = _broker()
    b.submit(_buy(qty=3), current_price=17500.0)
    trade = api.trades[-1]
    api.emit_order(trade=trade, ordno="N0001", quantity=3)
    for _ in range(3):
        api.emit_deal(trade=trade, ordno="N0001", exchange_seq="E1", quantity=1)

    fills = b.poll_fills(MARKET)
    assert len(fills) == 1 and fills[0].quantity == 1
    assert b.has_pending(), "2 lots are still working"
    assert not rails.halted, "a duplicate is expected traffic, not an anomaly"


def test_distinct_exchange_seqs_are_distinct_fills():
    """Dedup must key on the venue sequence, not merely on the order."""
    api, b, _ = _broker()
    b.submit(_buy(qty=2), current_price=17500.0)
    trade = api.trades[-1]
    api.emit_order(trade=trade, ordno="N0001", quantity=2)
    api.emit_deal(trade=trade, ordno="N0001", exchange_seq="E1", quantity=1)
    api.emit_deal(trade=trade, ordno="N0001", exchange_seq="E2", quantity=1)
    assert len(b.poll_fills(MARKET)) == 2


def test_partial_fill_leaves_the_order_working():
    api, b, _ = _broker()
    b.submit(_buy(qty=3), current_price=17500.0)
    trade = api.trades[-1]
    api.emit_order(trade=trade, ordno="N0001", quantity=3)
    api.emit_deal(trade=trade, ordno="N0001", exchange_seq="E1", quantity=1)
    assert b.has_pending(), "2 lots still working"
    api.emit_deal(trade=trade, ordno="N0001", exchange_seq="E2", quantity=2)
    assert not b.has_pending()
    assert sum(f.quantity for f in b.poll_fills(MARKET)) == 3


def test_cancel_race_fill_is_booked_not_discarded():
    """A cancel that loses the race still produced a real position. Dropping the
    fill would leave us holding contracts we do not know about."""
    api, b, _ = _broker()
    b.submit(_buy(), current_price=17500.0)
    trade = api.trades[-1]
    api.emit_order(trade=trade, ordno="N0001")
    b.cancel()                                   # request sent...
    api.emit_deal(trade=trade, ordno="N0001")    # ...but it filled first
    fills = b.poll_fills(MARKET)
    assert len(fills) == 1
    assert api.cancelled, "the cancel was still genuinely requested"


def test_cancel_frees_the_book_immediately_for_a_replacement():
    """§4.4 requires a new signal to cancel and re-submit at once; waiting for the
    venue's confirmation would stall the decision."""
    api, b, _ = _broker()
    b.submit(_buy(), current_price=17500.0)
    api.emit_order(trade=api.trades[-1], ordno="N0001")
    b.cancel()
    assert not b.has_pending()
    b.submit(_buy(price=17490.0), current_price=17500.0)
    assert b.has_pending()
    assert len(api.placed) == 2


def test_no_fill_after_a_confirmed_cancel():
    api, b, rails = _broker()
    b.submit(_buy(), current_price=17500.0)
    trade = api.trades[-1]
    api.emit_order(trade=trade, ordno="N0001")
    b.cancel()
    api.emit_order(trade=trade, ordno="N0001", op_type="Cancel")   # confirmed
    api.emit_deal(trade=trade, ordno="N0001", exchange_seq="E9")   # stray
    assert b.poll_fills(MARKET) == []
    assert b.anomalies, "a deal after a confirmed cancel must be surfaced"


# ---- rejections -------------------------------------------------------------

def test_rejection_halts_the_rails():
    api, b, rails = _broker()
    b.submit(_buy(), current_price=17500.0)
    api.emit_order(trade=api.trades[-1], op_code="03",
                   op_msg="margin insufficient")
    assert rails.halted and rails.halt_action is HaltAction.STOP
    assert "margin insufficient" in rails.halt_reason
    assert not b.has_pending(), "a rejected order is not working"


def test_rejection_is_never_a_silent_no_op():
    api, b, rails = _broker()
    b.submit(_buy(), current_price=17500.0)
    api.emit_order(trade=api.trades[-1], op_code="99")
    assert b.anomalies


# ---- anomalies --------------------------------------------------------------

def test_deal_for_an_unknown_order_is_an_anomaly_not_a_fill():
    """Booking an unattributable fill would invent a position from nothing."""
    api, b, rails = _broker()
    b.submit(_buy(), current_price=17500.0)
    api.emit_order(trade=api.trades[-1], ordno="N0001")
    api.emit_deal(ordno="ZZZZ", trade_id="nope", exchange_seq="E5")
    assert b.poll_fills(MARKET) == []
    assert rails.halted


def test_overfilling_deal_is_rejected():
    """The quantity bound is the backstop against any stray report inventing
    contracts."""
    api, b, rails = _broker()
    b.submit(_buy(qty=1), current_price=17500.0)
    trade = api.trades[-1]
    api.emit_order(trade=trade, ordno="N0001")
    api.emit_deal(trade=trade, ordno="N0001", exchange_seq="E1", quantity=5)
    assert b.poll_fills(MARKET) == []
    assert rails.halted and "overfill" in rails.halt_reason


def test_unrecognised_callback_state_is_surfaced():
    """The enum literals are unverified until first contact with the real API; a
    wrong guess must fail loudly rather than discard every fill."""
    api, b, rails = _broker()
    api._emit("SomethingElse", {})
    assert b.anomalies and rails.halted


def test_non_positive_deal_quantity_is_rejected():
    api, b, rails = _broker()
    b.submit(_buy(), current_price=17500.0)
    trade = api.trades[-1]
    api.emit_order(trade=trade, ordno="N0001")
    api.emit_deal(trade=trade, ordno="N0001", quantity=0)
    assert b.poll_fills(MARKET) == []
    assert rails.halted


# ---- position reconciliation -----------------------------------------------

def test_broker_position_sums_signed_quantity_for_our_contract():
    api, b, _ = _broker()
    api.positions = [
        FakePosition(code="TXFH6", direction="Buy", quantity=2),
        FakePosition(code="MXFH6", direction="Sell", quantity=7),   # other product
    ]
    assert b.broker_position() == 2


def test_short_position_is_negative():
    api, b, _ = _broker()
    api.positions = [FakePosition(code="TXFH6", direction="Sell", quantity=1)]
    assert b.broker_position() == -1


def test_flat_is_zero_not_none():
    api, b, _ = _broker()
    api.positions = []
    assert b.broker_position() == 0


def test_alias_contract_still_matches_the_resolved_position_code():
    """REGRESSION (measured 2026-08-10). Ordering through the `TXFR1` alias
    yields callbacks and positions stamped with the resolved code `TXFH6`, but
    the alias object reports `code='TXFR1'`. Matching on the alias alone filtered
    our own position out, so `broker_position()` returned 0 while the mirror said
    1 — and the reconciliation rail halted the loop the moment anything was
    held."""
    api = FakeShioaji()
    rails = SafetyRails(RailsConfig(), simulation=True)
    b = ShioajiBroker(api, make_alias_contract(), sj=FakeSj, rails=rails)

    api.positions = [FakePosition(code="TXFH6", direction="Buy", quantity=1)]
    b.submit(_buy(), current_price=17500.0)
    api.emit_order(trade=api.trades[-1], ordno="N0001", code="TXFH6")

    assert b.broker_position() == 1
    rails.on_reconcile(mirror=1, venue=b.broker_position())
    assert not rails.halted


def test_resolved_code_is_learned_from_a_deal_when_absent_up_front():
    api = FakeShioaji()
    b = ShioajiBroker(api, make_alias_contract(), sj=FakeSj, fee_rate=0.0)
    b.submit(_buy(), current_price=17500.0)
    api.emit_deal(trade=api.trades[-1], ordno="N0001", code="TXFH6")

    api.positions = [FakePosition(code="TXFH6", direction="Buy", quantity=1)]
    assert b.broker_position() == 1


def test_other_products_are_still_excluded_after_learning():
    """Widening the accepted set must not turn into 'count everything'."""
    api = FakeShioaji()
    b = ShioajiBroker(api, make_alias_contract(), sj=FakeSj)
    b.submit(_buy(), current_price=17500.0)
    api.emit_order(trade=api.trades[-1], ordno="N0001", code="TXFH6")

    api.positions = [
        FakePosition(code="TXFH6", direction="Buy", quantity=1),
        FakePosition(code="MXFH6", direction="Sell", quantity=9),
    ]
    assert b.broker_position() == 1


def test_dispatch_matches_the_real_enum_values():
    """VERIFIED against the SDK: FuturesDeal='FDEAL', FuturesOrder='FORDER'.
    Neither value contains a mixed-case 'Deal'/'Order', and `str(state)` for a
    deal is 'OrderState.FuturesDeal' — which contains ORDER too. Dispatch must
    upper-case and test DEAL first, or every fill routes into the order handler
    and is silently lost."""
    api = FakeShioaji()
    b = ShioajiBroker(api, make_contract(), sj=FakeSj, fee_rate=0.0)
    b.submit(_buy(), current_price=17500.0)
    trade = api.trades[-1]

    # Drive the callback with the raw enum, exactly as the SDK delivers it.
    api._emit(FakeSj.OrderState.FuturesOrder, {
        "operation": {"op_type": "New", "op_code": "00", "op_msg": ""},
        "order": {"id": trade.order.id, "ordno": "N0001"},
        "contract": {"code": "TXFH6"},
    })
    api._emit(FakeSj.OrderState.FuturesDeal, {
        "trade_id": trade.order.id, "ordno": "N0001", "exchange_seq": "E1",
        "action": "Buy", "price": 17500.0, "quantity": 1,
        "ts": T0, "code": "TXFH6",
    })
    assert len(b.poll_fills(MARKET)) == 1, "the deal must not route to _handle_order"
    assert not b.anomalies


def test_position_query_failure_degrades_to_unknown():
    """A transient failure must mean 'cannot compare', not a fabricated mismatch
    that halts a healthy loop."""
    api, b, rails = _broker()
    api.list_positions_error = RuntimeError("timeout")
    assert b.broker_position() is None


# ---- no shioaji import ------------------------------------------------------

def test_never_touches_the_shioaji_package_when_sj_is_injected(monkeypatch):
    """The lazy-import seam: with an `sj` namespace supplied, the real package is
    never reached. Asserted by making the loader explode — so this holds whether
    or not `shioaji` happens to be installed, unlike an earlier version of this
    test which merely asserted the package was absent and started failing the
    moment it was installed for the V1c check."""
    import forecast_eval.live.broker.shioaji as mod

    def _boom():
        raise AssertionError("_load_sj must not be called when sj is injected")

    monkeypatch.setattr(mod, "_load_sj", _boom)

    api, b, _ = _broker()
    b.submit(_buy(), current_price=17500.0)
    trade = api.trades[-1]
    api.emit_order(trade=trade, ordno="N0001")
    api.emit_deal(trade=trade, ordno="N0001")
    assert len(b.poll_fills(MARKET)) == 1


def test_module_has_no_import_time_dependency_on_shioaji():
    """`import shioaji` must appear only inside the lazy loader, never at module
    scope — otherwise importing the live package would require the SDK."""
    import inspect
    import forecast_eval.live.broker.shioaji as mod

    src = inspect.getsource(mod)
    module_level = [
        line for line in src.splitlines()
        if line.startswith("import shioaji") or line.startswith("from shioaji")
    ]
    assert module_level == []


# ---- callback re-attachment (§2.24) ----------------------------------------

def test_attach_is_re_callable_and_restores_the_callback():
    """MEASURED 2026-08-17: `login(force_refresh=True)` replaces the session
    object and every callback registered against the old one stops firing —
    order callbacks went 2 -> 0 across a re-login. So registration cannot be a
    one-shot in `__init__`."""
    api, b, _ = _broker()
    api._callback = None                       # what a re-login leaves behind
    b.attach()
    assert api._callback is not None
    # and it is still the broker's own handler, i.e. fills are seen again
    assert api._callback == b._on_order_state


def test_attach_clears_before_registering_when_the_sdk_allows_it():
    """If registration ever appends rather than replaces, a repeated repair
    would deliver every fill twice — and a double-counted fill corrupts the
    position mirror and the PnL together."""
    api, b, _ = _broker()
    cleared = []
    api.clear_order_callback = lambda: cleared.append(1)
    b.attach()
    assert cleared == [1]


def test_attach_survives_an_sdk_without_a_clear_method():
    """Losing the callback entirely is far worse than the duplicate the clear
    guards against, so a missing or throwing `clear` must not stop the
    register. The fake has no `clear_order_callback` at all."""
    api, b, _ = _broker()
    assert not hasattr(api, "clear_order_callback")
    api._callback = None
    b.attach()
    assert api._callback is not None


def test_attach_survives_a_throwing_clear():
    api, b, _ = _broker()

    def boom():
        raise RuntimeError("clear exploded")

    api.clear_order_callback = boom
    api._callback = None
    b.attach()
    assert api._callback is not None


# ---- the contract roll (§2.6) ----------------------------------------------
#
# A soak died holding an object the venue had retired. `switch_contract` is the
# broker half of the repair, and the code set is the part with teeth: it decides
# which of the venue's positions count as ours.

def test_switch_contract_repoints_the_broker_at_the_new_month():
    api, b, _rails = _broker()
    september = make_contract(code="TXFI6", delivery_month="202609")
    b.switch_contract(september)
    assert b.contract is september


def test_switch_contract_forgets_the_settled_month_instead_of_accumulating():
    # Carrying the expired code forward would let a settled leg keep counting
    # toward broker_position(), so the reconciliation rail would compare a mirror
    # holding one month against a venue sum across two.
    api, b, _rails = _broker()
    api.positions = [FakePosition(code="TXFH6", direction="Buy", quantity=1)]
    assert b.broker_position() == 1

    b.switch_contract(make_contract(code="TXFI6", delivery_month="202609"))
    assert b.broker_position() == 0
    assert "TXFH6" not in b._codes


def test_switch_contract_is_recorded_with_both_the_code_and_the_month():
    class Rec:
        def __init__(self):
            self.events = []

        def record(self, event, **fields):
            self.events.append((event, fields))

    rec = Rec()
    api, b, _rails = _broker(recorder=rec)
    b.switch_contract(make_contract(code="TXFI6", delivery_month="202609"))
    event, fields = rec.events[-1]
    assert event == "broker_contract_switched"
    assert fields["code"] == "TXFI6" and fields["delivery_month"] == "202609"


# ---- roll legs go out IOC, priced through the touch (§2.6) ------------------
#
# IOC is right *only* because the ladder retries: §2.19 measured that an IOC
# alone expires into nothing, which is why the FLATTEN path uses ROD. With a
# retry the trade-off inverts — nothing rests, so re-pricing needs no cancel and
# §2.2's asynchronous cancel/replace race never happens.

def test_an_ordinary_order_is_still_lmt_rod():
    api, b, _rails = _broker()
    b.submit(_buy(), current_price=17500.0)
    sent = api.placed[-1]
    assert str(getattr(sent.order_type, "value", sent.order_type)) == "ROD"


def test_a_roll_leg_goes_out_ioc():
    api, b, _rails = _broker()
    with b.rolling(5):
        b.submit(_buy(), current_price=17500.0)
    sent = api.placed[-1]
    assert str(getattr(sent.order_type, "value", sent.order_type)) == "IOC"


def test_the_rung_prices_the_limit_through_the_touch():
    api, b, _rails = _broker()
    with b.rolling(15):
        b.submit(_buy(price=17500.0), current_price=17500.0)
    assert api.placed[-1].price == 17515.0             # a BUY pays more


def test_a_selling_roll_leg_goes_the_other_way():
    api, b, _rails = _broker()
    order = OrderEvent(T0, "SELL", 17500.0, "CLOSE", quantity=1)
    with b.rolling(15):
        b.submit(order, current_price=17500.0)
    assert api.placed[-1].price == 17485.0


def test_the_roll_scope_is_restored_even_if_the_body_raises():
    api, b, _rails = _broker()
    with pytest.raises(RuntimeError):
        with b.rolling(5):
            raise RuntimeError("boom")
    b.submit(_buy(), current_price=17500.0)
    sent = api.placed[-1]
    assert str(getattr(sent.order_type, "value", sent.order_type)) == "ROD"


def test_an_unresolved_ioc_stops_counting_as_pending_after_its_deadline():
    """The gate the whole ladder hangs on. §2.19 saw an expired IOC leave
    `has_pending()` True; whether the venue reports the expiry is unmeasured, so
    the ladder must not depend on it — or every rung is blocked and the ladder is
    inert while logging beautifully."""
    api, b, _rails = _broker()
    with b.rolling(5):
        b.submit(_buy(), current_price=17500.0)
    assert b.has_pending() is True

    b._ioc_deadline -= 10.0                    # the deadline has passed
    assert b.has_pending() is False


def test_a_timed_out_ioc_is_retired_not_forgotten():
    """The venue may simply be slower than our deadline. A fill we asked for and
    then forgot would be a position nobody owns."""
    api, b, _rails = _broker()
    with b.rolling(5):
        b.submit(_buy(), current_price=17500.0)
    live = b._live
    b._ioc_deadline -= 10.0
    b.has_pending()
    assert b._live is None
    assert live in b._retiring


def test_an_ordinary_rod_order_never_times_out():
    api, b, _rails = _broker()
    b.submit(_buy(), current_price=17500.0)
    assert b._ioc_deadline is None
    assert b.has_pending() is True
