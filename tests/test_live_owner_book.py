"""Tests for live/owner.py — the owner's N-order book (§18.3 item 1).

Two things this must never do: attribute one deployment's fill to another, and
size a leg larger than it can fully execute (§18.12).
"""
import pandas as pd
import pytest

from forecast_eval.events import OrderEvent
from forecast_eval.live.owner import COVER, NEW, Leg, OrderBook, plan_legs

TS = pd.Timestamp("2026-09-07 10:00:00")


def _order(side="BUY", qty=1, intent="OPEN", price=100.0):
    return OrderEvent(timestamp=TS, side=side, limit_price=price,
                      intent=intent, quantity=qty)


def _book(**kw):
    return OrderBook(**kw)


def _place(book, slug, side="BUY", qty=1, order_id="A1", ordno=None,
           account_net=None):
    """Plan, register and return the legs — what the owner does around a place."""
    net = book.account_net if account_net is None else account_net
    legs = plan_legs(side, qty, net)
    return [book.register(slug, _order(side=side, qty=qty), leg,
                          order_id=f"{order_id}-{i}", ordno=ordno)
            for i, leg in enumerate(legs)]


# ---- plan_legs: the §18.9 / §18.12 rule ------------------------------------

def test_opening_from_flat_is_one_new_leg():
    assert plan_legs("BUY", 1, 0) == [Leg(NEW, 1)]
    assert plan_legs("SELL", 2, 0) == [Leg(NEW, 2)]


def test_adding_to_an_existing_position_is_all_new():
    """Same direction as the book: nothing is being closed."""
    assert plan_legs("BUY", 1, 3) == [Leg(NEW, 1)]
    assert plan_legs("SELL", 2, -1) == [Leg(NEW, 2)]


def test_closing_exactly_is_one_cover_leg():
    assert plan_legs("SELL", 1, 1) == [Leg(COVER, 1)]
    assert plan_legs("BUY", 2, -2) == [Leg(COVER, 2)]


def test_closing_part_of_a_position_is_one_cover_leg():
    assert plan_legs("SELL", 1, 3) == [Leg(COVER, 1)]


def test_crossing_through_flat_is_cover_then_new():
    """The measured case: +1 crossed to -1 is Cover 1 then New 1, in that order."""
    assert plan_legs("SELL", 2, 1) == [Leg(COVER, 1), Leg(NEW, 1)]
    assert plan_legs("BUY", 3, -1) == [Leg(COVER, 1), Leg(NEW, 2)]


def test_no_leg_is_ever_larger_than_it_can_fully_execute():
    """§18.12 — an over-sized Cover clamped twice and over-reported when it did."""
    for net in range(-4, 5):
        for side in ("BUY", "SELL"):
            for qty in range(1, 6):
                legs = plan_legs(side, qty, net)
                cover = sum(l.quantity for l in legs if l.octype == COVER)
                assert cover <= abs(net)
                assert sum(l.quantity for l in legs) == qty


def test_cover_always_comes_first():
    legs = plan_legs("SELL", 5, 2)
    assert [l.octype for l in legs] == [COVER, NEW]


def test_zero_or_negative_quantity_plans_nothing():
    assert plan_legs("BUY", 0, 0) == []
    assert plan_legs("BUY", -1, 0) == []


# ---- routing: whose fill is it? --------------------------------------------

def test_a_deal_is_routed_to_the_deployment_that_placed_it():
    book = _book()
    _place(book, "day", order_id="D", ordno="111")
    _place(book, "night", side="SELL", order_id="N", ordno="222")
    result = book.on_deal(ordno="222", trade_id=None, exchange_seq="1",
                          side="SELL", price=100.0, quantity=1, timestamp=TS)
    assert result is not None
    slug, fill = result
    assert slug == "night"
    assert fill.side == "SELL" and fill.quantity == 1


def test_matching_falls_back_to_the_placement_id_before_ordno_is_known():
    """A deal can arrive before the order event that carries ordno."""
    book = _book()
    _place(book, "day", order_id="D")
    slug, _ = book.on_deal(ordno=None, trade_id="D-0", exchange_seq="1",
                           side="BUY", price=100.0, quantity=1, timestamp=TS)
    assert slug == "day"


def test_learn_ordno_attaches_the_venue_number_to_the_right_leg():
    book = _book()
    _place(book, "day", order_id="D")
    _place(book, "night", order_id="N")
    book.learn_ordno("N-0", "999")
    slug, _ = book.on_deal(ordno="999", trade_id=None, exchange_seq="1",
                           side="BUY", price=100.0, quantity=1, timestamp=TS)
    assert slug == "night"


def test_an_unattributable_deal_is_an_anomaly_not_a_guess():
    """There is no sole-order fallback here: guessing would misattribute a fill."""
    book = _book()
    _place(book, "day", order_id="D", ordno="111")
    assert book.on_deal(ordno="oops", trade_id="nope", exchange_seq="1",
                        side="BUY", price=100.0, quantity=1,
                        timestamp=TS) is None
    assert book.anomalies and "unknown order" in book.anomalies[0]


def test_a_lone_working_order_is_still_not_assumed():
    """ShioajiBroker may assume its single order; the owner must not."""
    book = _book()
    _place(book, "day", order_id="D", ordno="111")
    assert book.on_deal(ordno=None, trade_id=None, exchange_seq="1",
                        side="BUY", price=100.0, quantity=1,
                        timestamp=TS) is None
    assert book.anomalies


def test_anomalies_reach_the_callback():
    seen = []
    book = _book(on_anomaly=seen.append)
    book.on_deal(ordno="x", trade_id="y", exchange_seq="1", side="BUY",
                 price=100.0, quantity=1, timestamp=TS)
    assert seen and "unknown order" in seen[0]


# ---- partial fills ---------------------------------------------------------

def test_partial_fills_accumulate_against_one_leg():
    """MEASURED §18.9: a 2-lot order filled as two 1-lot deals nine seconds apart."""
    book = _book()
    (leg,) = _place(book, "day", qty=2, order_id="D", ordno="111")
    first = book.on_deal(ordno="111", trade_id=None, exchange_seq="1",
                         side="BUY", price=100.0, quantity=1, timestamp=TS)
    assert first is not None
    assert leg.filled == 1 and leg.remaining == 1 and leg.dead is False
    second = book.on_deal(ordno="111", trade_id=None, exchange_seq="2",
                          side="BUY", price=101.0, quantity=1, timestamp=TS)
    assert second is not None
    assert leg.filled == 2 and leg.remaining == 0 and leg.dead is True


def test_a_leg_is_not_finished_by_its_first_deal():
    book = _book()
    _place(book, "day", qty=2, order_id="D", ordno="111")
    book.on_deal(ordno="111", trade_id=None, exchange_seq="1", side="BUY",
                 price=100.0, quantity=1, timestamp=TS)
    assert book.has_pending("day") is True


def test_a_duplicate_deal_report_books_one_fill():
    book = _book()
    _place(book, "day", order_id="D", ordno="111")
    kw = dict(ordno="111", trade_id=None, exchange_seq="1", side="BUY",
              price=100.0, quantity=1, timestamp=TS)
    assert book.on_deal(**kw) is not None
    assert book.on_deal(**kw) is None
    assert book.account_net == 1


def test_a_deal_beyond_the_legs_own_quantity_is_refused():
    """§18.12: the venue reported a 2-lot deal for an order that moved one lot."""
    book = _book()
    _place(book, "day", qty=1, order_id="D", ordno="111")
    assert book.on_deal(ordno="111", trade_id=None, exchange_seq="1",
                        side="SELL", price=100.0, quantity=2,
                        timestamp=TS) is None
    assert book.anomalies and "overfills" in book.anomalies[0]
    assert book.account_net == 0, "nothing is booked when the report is refused"


def test_a_non_positive_deal_quantity_is_refused():
    book = _book()
    _place(book, "day", order_id="D", ordno="111")
    assert book.on_deal(ordno="111", trade_id=None, exchange_seq="1",
                        side="BUY", price=100.0, quantity=0,
                        timestamp=TS) is None
    assert book.anomalies and "non-positive" in book.anomalies[0]


# ---- the account net -------------------------------------------------------

def test_account_net_moves_with_booked_fills():
    book = _book()
    _place(book, "day", order_id="D", ordno="111")
    book.on_deal(ordno="111", trade_id=None, exchange_seq="1", side="BUY",
                 price=100.0, quantity=1, timestamp=TS)
    assert book.account_net == 1
    _place(book, "night", side="SELL", order_id="N", ordno="222")
    book.on_deal(ordno="222", trade_id=None, exchange_seq="1", side="SELL",
                 price=100.0, quantity=1, timestamp=TS)
    assert book.account_net == 0, "two deployments net on one shared book (§14)"


def test_the_net_is_current_before_the_next_leg_is_planned():
    """plan_legs sizes against this, so it must be right the instant a fill books."""
    book = _book()
    _place(book, "day", order_id="D", ordno="111")
    book.on_deal(ordno="111", trade_id=None, exchange_seq="1", side="BUY",
                 price=100.0, quantity=1, timestamp=TS)
    assert plan_legs("SELL", 2, book.account_net) == [Leg(COVER, 1), Leg(NEW, 1)]


def test_seed_net_sets_the_net_without_booking_a_fill():
    """A restart adopts from the ledger sum (§16); adoption is not a trade."""
    book = _book()
    book.seed_net(2)
    assert book.account_net == 2
    assert book.all_working() == []


# ---- per-deployment working state ------------------------------------------

def test_working_orders_are_tracked_per_deployment():
    book = _book()
    _place(book, "day", order_id="D", ordno="111")
    _place(book, "night", order_id="N", ordno="222")
    assert book.has_pending("day") and book.has_pending("night")
    assert book.has_pending("fullday") is False
    assert len(book.all_working()) == 2


def test_a_crossing_intent_gives_one_deployment_two_legs():
    book = _book()
    book.seed_net(1)
    legs = _place(book, "day", side="SELL", qty=2, order_id="D")
    assert [l.octype for l in legs] == [COVER, NEW]
    assert len(book.working_for("day")) == 2


def test_cancel_flags_the_legs_but_keeps_them_tracked():
    """§2.2 — a cancelled order can still fill in the race window, and that fill is real."""
    book = _book()
    _place(book, "day", order_id="D", ordno="111")
    flagged = book.mark_cancel_requested("day")
    assert [w.cancel_requested for w in flagged] == [True]
    assert book.has_pending("day") is True
    result = book.on_deal(ordno="111", trade_id=None, exchange_seq="1",
                          side="BUY", price=100.0, quantity=1, timestamp=TS)
    assert result is not None, "a fill after a cancel request is still booked"
    assert book.account_net == 1


def test_cancel_only_touches_the_named_deployment():
    book = _book()
    _place(book, "day", order_id="D", ordno="111")
    _place(book, "night", order_id="N", ordno="222")
    book.mark_cancel_requested("day")
    assert [w.cancel_requested for w in book.working_for("night")] == [False]


def test_finish_removes_a_leg():
    book = _book()
    (leg,) = _place(book, "day", order_id="D", ordno="111")
    book.finish(leg)
    assert book.has_pending("day") is False
    assert book.all_working() == []


def test_forget_dead_drops_filled_legs_but_not_working_ones():
    book = _book()
    _place(book, "day", order_id="D", ordno="111")
    _place(book, "night", order_id="N", ordno="222")
    book.on_deal(ordno="111", trade_id=None, exchange_seq="1", side="BUY",
                 price=100.0, quantity=1, timestamp=TS)
    book.forget_dead()
    assert book.has_pending("day") is False
    assert book.has_pending("night") is True


def test_a_late_duplicate_is_still_deduped_before_forget_dead():
    """The fill and the leg's disappearance are two different moments."""
    book = _book()
    _place(book, "day", order_id="D", ordno="111")
    kw = dict(ordno="111", trade_id=None, exchange_seq="1", side="BUY",
              price=100.0, quantity=1, timestamp=TS)
    book.on_deal(**kw)
    assert book.on_deal(**kw) is None
    assert book.anomalies == [], "a duplicate is a dedup hit, not an unknown order"


# ---- the per-deployment entry price ----------------------------------------

def _fill(book, slug, side, qty, price, ordno, seq="1"):
    return book.on_deal(ordno=ordno, trade_id=None, exchange_seq=seq, side=side,
                        price=price, quantity=qty, timestamp=TS)


def test_entry_starts_at_the_first_fills_price():
    book = _book()
    _place(book, "day", order_id="D", ordno="111")
    _fill(book, "day", "BUY", 1, 17480.0, "111")
    assert book.entry_for("day") == 17480.0


def test_adding_to_a_side_averages_by_size():
    book = _book()
    _place(book, "day", qty=1, order_id="D1", ordno="111")
    _fill(book, "day", "BUY", 1, 100.0, "111")
    _place(book, "day", qty=3, order_id="D2", ordno="222")
    _fill(book, "day", "BUY", 3, 200.0, "222")
    assert book.net_for("day") == 4
    assert book.entry_for("day") == pytest.approx(175.0)


def test_reducing_a_position_leaves_the_entry_alone():
    """The remaining lots cost what they cost; re-averaging would rewrite their PnL."""
    book = _book()
    _place(book, "day", qty=2, order_id="D1", ordno="111")
    _fill(book, "day", "BUY", 2, 100.0, "111")
    _place(book, "day", side="SELL", qty=1, order_id="D2", ordno="222")
    _fill(book, "day", "SELL", 1, 500.0, "222")
    assert book.net_for("day") == 1
    assert book.entry_for("day") == 100.0


def test_closing_to_flat_clears_the_entry():
    book = _book()
    _place(book, "day", order_id="D1", ordno="111")
    _fill(book, "day", "BUY", 1, 100.0, "111")
    _place(book, "day", side="SELL", order_id="D2", ordno="222")
    _fill(book, "day", "SELL", 1, 120.0, "222")
    assert book.net_for("day") == 0
    assert book.entry_for("day") == 0.0


def test_crossing_through_flat_takes_the_new_sides_price():
    """Carrying a long's average onto a short would price it at what the long cost."""
    book = _book()
    _place(book, "day", order_id="D1", ordno="111")
    _fill(book, "day", "BUY", 1, 100.0, "111")
    # The §18.9 split: Cover 1 then New 1, two legs, two fills.
    book.register("day", _order(side="SELL", qty=2), Leg(COVER, 1),
                  order_id="D2", ordno="222")
    book.register("day", _order(side="SELL", qty=2), Leg(NEW, 1),
                  order_id="D3", ordno="333")
    _fill(book, "day", "SELL", 1, 300.0, "222")
    _fill(book, "day", "SELL", 1, 305.0, "333")
    assert book.net_for("day") == -1
    assert book.entry_for("day") == 305.0, "the short opened at the New leg's price"


def test_entries_are_tracked_separately_per_deployment():
    book = _book()
    _place(book, "day", order_id="D", ordno="111")
    _place(book, "night", order_id="N", ordno="222")
    _fill(book, "day", "BUY", 1, 100.0, "111")
    _fill(book, "night", "BUY", 1, 900.0, "222")
    assert book.entry_for("day") == 100.0
    assert book.entry_for("night") == 900.0


def test_seed_slug_entry_survives_a_restart():
    book = _book()
    book.seed_slug_net("day", 2)
    book.seed_slug_entry("day", 17480.0)
    assert book.net_for("day") == 2 and book.entry_for("day") == 17480.0


def test_a_refused_deal_does_not_move_the_entry():
    book = _book()
    _place(book, "day", qty=1, order_id="D", ordno="111")
    _fill(book, "day", "BUY", 1, 100.0, "111")
    _fill(book, "day", "BUY", 5, 999.0, "111", seq="2")   # overfills the leg
    assert book.entry_for("day") == 100.0
