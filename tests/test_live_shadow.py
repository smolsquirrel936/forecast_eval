"""ShadowBroker — V4's fill-model gap measurement.

Two properties carry the whole thing:

  * the shadow must be **invisible** to trading — only venue fills may leave
    `poll_fills`, or the measurement changes what it measures;
  * an episode must be scored even when **nothing** filled, since a fill *rate*
    cannot be computed from a log that only records fills.
"""
from datetime import datetime, timedelta

import pytest

from forecast_eval.events import MarketEvent, OrderEvent
from forecast_eval.live.broker.base import Broker
from forecast_eval.live.broker.shadow import OrderGap, ShadowBroker

T0 = datetime(2026, 3, 5, 9, 0, 0)


def _bar(i, price):
    return MarketEvent(T0 + timedelta(minutes=i), price, 1, "DAY")


def _order(side="BUY", price=17500.0, intent="OPEN"):
    return OrderEvent(T0, side, price, intent, quantity=1)


class _FakeLive(Broker):
    """A venue we can script: it fills when told, never on its own."""

    def __init__(self):
        self.submitted = []
        self.cancelled = 0
        self.pending = False
        self._queued = []
        self.urgent_entered = 0

    def submit(self, order, *, current_price):
        self.submitted.append((order, current_price))
        self.pending = True

    def cancel(self):
        self.cancelled += 1
        self.pending = False

    def has_pending(self):
        return self.pending

    def pending_order(self):
        return None

    def poll_fills(self, market):
        out, self._queued = self._queued, []
        if out:
            self.pending = False
        return out

    def broker_position(self):
        return 7

    def urgent(self):
        from contextlib import contextmanager

        @contextmanager
        def scope():
            self.urgent_entered += 1
            yield
        return scope()

    def fill(self, price, quantity=1, side="BUY"):
        from forecast_eval.events import FillEvent
        self._queued.append(FillEvent(
            timestamp=T0, side=side, fill_price=price,
            quantity=quantity, fee=0.0,
        ))


def _broker():
    live = _FakeLive()
    return live, ShadowBroker(live, fee_rate=0.0)


# ---- the shadow must not be able to affect trading -------------------------

def test_only_venue_fills_are_returned():
    """A simulated fill reaching `Trader` would move a position that does not
    exist. The shadow is an observer, on the same terms as the recorder."""
    live, b = _broker()
    b.submit(_order(price=17500.0), current_price=17500.0)
    # The offline model fills this marketable order at the next print; the venue
    # reports nothing at all.
    fills = b.poll_fills(_bar(1, 17501.0))
    assert fills == [], "the shadow's fill must not escape"
    assert b.gaps == [] or b.gaps[-1].shadow_filled is not None


def test_pending_state_comes_from_the_venue_not_the_shadow():
    """`has_pending` gates §4.4 and the flatten retry. Answering from the shadow
    would let a simulated fill convince the loop a real order had gone away."""
    live, b = _broker()
    b.submit(_order(), current_price=17500.0)
    b.poll_fills(_bar(1, 17501.0))          # shadow fills here, venue does not
    assert b.has_pending() is True
    assert b.broker_position() == 7, "the venue's position, not a simulated one"


def test_urgency_is_forwarded_to_the_venue_only():
    live, b = _broker()
    with b.urgent():
        pass
    assert live.urgent_entered == 1


# ---- scoring ---------------------------------------------------------------

def test_an_order_neither_side_filled_is_still_scored():
    """A fill *rate* cannot be computed from a log that only records fills."""
    live, b = _broker()
    b.submit(_order(side="BUY", price=17400.0), current_price=17500.0)  # passive
    b.poll_fills(_bar(1, 17500.0))          # no cross, neither side fills
    b.cancel()
    assert len(b.gaps) == 1
    g = b.gaps[0]
    assert not g.live_filled and not g.shadow_filled
    assert b.gap_summary()["neither"] == 1
    assert b.gap_summary()["live_fill_rate"] == 0.0


def test_a_new_submit_scores_the_previous_episode():
    """§4.4 replaces a working order on every signal, so a submit is also the
    end of the previous order's life."""
    live, b = _broker()
    b.submit(_order(), current_price=17500.0)
    b.submit(_order(), current_price=17501.0)
    assert len(b.gaps) == 1


def test_close_scores_the_final_open_episode():
    live, b = _broker()
    b.submit(_order(), current_price=17500.0)
    assert b.gaps == []
    b.close()
    assert len(b.gaps) == 1


def test_both_filling_is_matched_and_slippage_is_measured():
    live, b = _broker()
    b.submit(_order(side="BUY", price=17500.0), current_price=17500.0)
    live.fill(17503.0)                       # venue paid 3 more than the sim will
    b.poll_fills(_bar(1, 17500.0))           # sim fills at this print: 17500
    b.cancel()

    g = b.gaps[0]
    assert g.live_filled and g.shadow_filled
    assert g.live_price == 17503.0 and g.shadow_price == 17500.0
    assert g.slippage_points == 3.0
    s = b.gap_summary()
    assert s["matched"] == 1 and s["slippage_mean_points"] == 3.0


def test_slippage_is_signed_so_positive_always_means_live_did_worse():
    """Paying 3 more on a BUY and receiving 3 less on a SELL are the same
    outcome; an unsigned difference would let them cancel to zero."""
    buy = OrderGap(T0, "BUY", "OPEN", 1, 17500.0, 17500.0)
    buy.live_price, buy.shadow_price = 17503.0, 17500.0
    sell = OrderGap(T0, "SELL", "CLOSE", 1, 17500.0, 17500.0)
    sell.live_price, sell.shadow_price = 17497.0, 17500.0
    assert buy.slippage_points == 3.0
    assert sell.slippage_points == 3.0


def test_slippage_is_none_when_only_one_side_filled():
    """There is no price difference to report against a fill that never
    happened, and inventing a zero would read as 'no cost'."""
    live, b = _broker()
    b.submit(_order(side="BUY", price=17400.0), current_price=17500.0)  # passive
    live.fill(17400.0)
    b.poll_fills(_bar(1, 17500.0))           # venue filled; sim did not cross
    b.cancel()
    g = b.gaps[0]
    assert g.live_filled and not g.shadow_filled
    assert g.slippage_points is None
    assert b.gap_summary()["live_only"] == 1


def test_slippage_statistics_ignore_unmatched_episodes():
    """Averaging over unmatched orders would treat 'did not fill' as 'filled at
    no cost' — the opposite of the truth, since a missed entry is the
    adverse-selection case §2.1 is about."""
    live, b = _broker()
    b.submit(_order(price=17500.0), current_price=17500.0)
    live.fill(17502.0)
    b.poll_fills(_bar(1, 17500.0))           # both fill; slippage 2
    b.cancel()
    b.submit(_order(price=17400.0), current_price=17500.0)
    b.poll_fills(_bar(2, 17500.0))           # neither fills
    b.cancel()

    s = b.gap_summary()
    assert s["orders"] == 2 and s["matched"] == 1
    assert s["slippage_mean_points"] == 2.0, "the average is over matches only"
    assert s["live_fill_rate"] == 0.5


def test_bars_to_fill_is_counted_per_side():
    live, b = _broker()
    b.submit(_order(price=17500.0), current_price=17500.0)
    b.poll_fills(_bar(1, 17500.0))           # sim fills on bar 1
    live.fill(17500.0)
    b.poll_fills(_bar(2, 17500.0))           # venue reports on bar 2
    b.cancel()
    g = b.gaps[0]
    assert g.shadow_bars == 1 and g.live_bars == 2
    assert g.bars == 2


# ---- the report ------------------------------------------------------------

def test_the_summary_is_recorded_per_order(tmp_path):
    """≥5 sessions have to be aggregated later, so each episode must survive in
    the trail rather than only in a console line."""
    import json
    from forecast_eval.live.recorder import Recorder

    rec = Recorder(tmp_path / "s.jsonl", simulation=True)
    live = _FakeLive()
    b = ShadowBroker(live, fee_rate=0.0, recorder=rec)
    b.submit(_order(price=17500.0), current_price=17500.0)
    live.fill(17502.0)
    b.poll_fills(_bar(1, 17500.0))
    b.close()
    rec.close()

    rows = [json.loads(l) for l in (tmp_path / "s.jsonl").read_text(
        encoding="utf-8").splitlines() if '"fill_gap_order"' in l]
    assert len(rows) == 1
    assert rows[0]["slippage_points"] == 2.0
    assert rows[0]["live_filled"] is True


def test_the_formatted_summary_says_which_direction_is_bad():
    live, b = _broker()
    b.submit(_order(price=17500.0), current_price=17500.0)
    live.fill(17502.0)
    b.poll_fills(_bar(1, 17500.0))
    b.close()
    text = b.format_summary()
    assert "live worse" in text and "+2.00" in text


def test_an_empty_session_reports_cleanly():
    _, b = _broker()
    assert "no orders" in b.format_summary()
    assert b.gap_summary()["orders"] == 0
    assert b.gap_summary()["live_fill_rate"] is None
