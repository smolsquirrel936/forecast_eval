"""fullday's re-entry cooldown (reversal loop decision 12, phase I).

After a fill that flattens a position, an OPEN on the closed position's side
is skipped for ``reentry_cooldown_signals`` signals (HOLDs count toward the
budget); the opposite side opens freely. ``reentry_cooldown_signals == 0`` is
the inert control — the cooldown never arms, so a run at the control is
byte-identical to one without the knob.
"""
import pandas as pd
import pytest

from forecast_eval.events import MarketEvent
from forecast_eval.execution import Execution
from forecast_eval.strategy.fullday.trader import Trader
from forecast_eval.strategy.fullday.wiring import make_strategy


def _market(price: float = 17500.0) -> MarketEvent:
    return MarketEvent(
        timestamp=pd.Timestamp("2024-01-02 09:00:00"),
        price=price,
        volume=1,
        session="DAY",
    )


def _fill(trader, side, *, bar=0):
    """Book a fill the way run.py does, at a fixed price."""
    trader.on_fill(
        side,
        1,
        fill_price=17500.0,
        bar_idx=bar,
        timestamp=pd.Timestamp("2024-01-02 09:00:00"),
    )


def _trader(**kw):
    kw.setdefault("tick_size", 1.0)
    kw.setdefault("aggression_ticks", 0)
    kw.setdefault("max_position", 1)
    return Trader(Execution(fee_rate=0.0), **kw)


# -------- the cooldown blocks a same-side re-entry ------------------------------

def test_same_side_reentry_is_skipped_during_cooldown():
    tr = _trader(reentry_cooldown_signals=2)
    assert tr.on_signal("BUY", _market(), strength=1.0) is not None
    _fill(tr, "BUY", bar=0)                      # long opened
    _fill(tr, "SELL", bar=5)                     # flattened -> cooldown, BUYs blocked
    # First BUY after the flat: skipped (and consumes one of the K).
    assert tr.on_signal("BUY", _market(), strength=1.0) is None
    # Second BUY: the last of the K, still skipped.
    assert tr.on_signal("BUY", _market(), strength=1.0) is None
    # Third BUY: the cooldown is spent, the open goes through.
    order = tr.on_signal("BUY", _market(), strength=1.0)
    assert order is not None and order.side == "BUY" and order.intent == "OPEN"


def test_opposite_side_opens_freely_during_cooldown():
    tr = _trader(reentry_cooldown_signals=16)
    assert tr.on_signal("BUY", _market(), strength=1.0) is not None
    _fill(tr, "BUY", bar=0)
    _fill(tr, "SELL", bar=5)                     # long closed -> BUYs blocked
    order = tr.on_signal("SELL", _market(), strength=1.0)
    assert order is not None and order.side == "SELL" and order.intent == "OPEN"


def test_holds_count_toward_the_cooldown():
    tr = _trader(reentry_cooldown_signals=2)
    assert tr.on_signal("BUY", _market(), strength=1.0) is not None
    _fill(tr, "BUY", bar=0)
    _fill(tr, "SELL", bar=5)
    # Two HOLDs consume the whole budget...
    assert tr.on_signal("HOLD", _market()) is None
    assert tr.on_signal("HOLD", _market()) is None
    # ...so the very next BUY opens.
    order = tr.on_signal("BUY", _market(), strength=1.0)
    assert order is not None and order.intent == "OPEN"


def test_below_floor_signals_still_count():
    # A gated (below entry floor) signal is still a signal seen while cooling.
    tr = _trader(reentry_cooldown_signals=1, entry_min_prob=0.6,
                 full_confidence_prob=0.9)
    assert tr.on_signal("BUY", _market(), strength=1.0) is not None
    _fill(tr, "BUY", bar=0)
    _fill(tr, "SELL", bar=5)
    assert tr.on_signal("BUY", _market(), strength=0.0) is None  # gated anyway
    order = tr.on_signal("BUY", _market(), strength=1.0)        # budget spent
    assert order is not None and order.intent == "OPEN"


# -------- the cooldown restarts at each flatten, and only arms on a flat fill ----

def test_cooldown_rearms_on_each_flattening_fill():
    tr = _trader(reentry_cooldown_signals=1)
    assert tr.on_signal("BUY", _market(), strength=1.0) is not None
    _fill(tr, "BUY", bar=0)
    _fill(tr, "SELL", bar=5)
    assert tr.on_signal("BUY", _market(), strength=1.0) is None  # blocked
    assert tr.on_signal("SELL", _market(), strength=1.0) is not None  # opens short
    _fill(tr, "SELL", bar=10)
    _fill(tr, "BUY", bar=15)                    # short closed -> SELLs blocked now
    assert tr.on_signal("SELL", _market(), strength=1.0) is None
    assert tr.on_signal("BUY", _market(), strength=1.0) is not None


def test_cooldown_does_not_grow_while_positioned():
    # Signals seen while positioned do not pre-consume a later cooldown: the
    # budget is armed fresh at the flattening fill.
    tr = _trader(reentry_cooldown_signals=1)
    assert tr.on_signal("BUY", _market(), strength=1.0) is not None
    _fill(tr, "BUY", bar=0)
    assert tr.on_signal("SELL", _market(), strength=1.0) is not None  # close order
    _fill(tr, "SELL", bar=5)
    assert tr.on_signal("BUY", _market(), strength=1.0) is None      # K = 1 again


# -------- the control is inert ----------------------------------------------------

def test_control_is_byte_identical_in_behaviour():
    # At cooldown 0 every same-side re-entry opens exactly as the pre-knob
    # Trader does — the cooldown never arms.
    for direction in ("BUY", "SELL"):
        tr = _trader(reentry_cooldown_signals=0)
        assert tr.on_signal(direction, _market(), strength=1.0) is not None
        _fill(tr, direction, bar=0)
        _fill(tr, "SELL" if direction == "BUY" else "BUY", bar=5)
        order = tr.on_signal(direction, _market(), strength=1.0)
        assert order is not None and order.intent == "OPEN"


def test_control_never_arms_state():
    tr = _trader(reentry_cooldown_signals=0)
    assert tr.on_signal("BUY", _market(), strength=1.0) is not None
    _fill(tr, "BUY", bar=0)
    _fill(tr, "SELL", bar=5)
    assert tr._cooldown_left == 0


# -------- validation ----------------------------------------------------------------

def test_rejects_negative_counts():
    with pytest.raises(ValueError, match="reentry_cooldown_signals"):
        _trader(reentry_cooldown_signals=-1)


def test_rejects_non_int_counts():
    with pytest.raises(ValueError, match="reentry_cooldown_signals"):
        _trader(reentry_cooldown_signals=1.5)


# -------- wiring ---------------------------------------------------------------------

def test_make_strategy_passes_the_knob_to_the_trader():
    # Pinned to PARAMS, not a literal: the loop moves this knob between
    # iterations (kept values become the new default), and the invariant is
    # that whatever PARAMS declares is what the built Trader carries.
    strat = make_strategy(warmup_bars=100)
    tr = strat["make_trader"](Execution(fee_rate=0.0))
    from forecast_eval.strategy.fullday import wiring
    assert tr.reentry_cooldown_signals == wiring.PARAMS["reentry_cooldown_signals"]


def test_make_strategy_passes_an_override_through():
    strat = make_strategy(warmup_bars=100, reentry_cooldown_signals=16)
    tr = strat["make_trader"](Execution(fee_rate=0.0))
    assert tr.reentry_cooldown_signals == 16


def test_wiring_rejects_a_bad_override():
    with pytest.raises(ValueError, match="reentry_cooldown_signals"):
        make_strategy(warmup_bars=100, reentry_cooldown_signals=-2)


# -------- roll legs (live/runtime.py `rolling()` hook, main 825f069) ----------
#
# A roll re-opens as an OPEN from flat on the side it just closed. Without the
# hook the cooldown read that as a same-side re-entry: the settled leg's single
# shot was dropped, and each scheduled-roll ladder rung burned one count.

def _armed(k=48):
    tr = _trader(reentry_cooldown_signals=k)
    _fill(tr, "BUY", bar=0)                      # long opened
    _fill(tr, "SELL", bar=5)                     # the roll's close -> BUYs blocked
    assert tr._cooldown_left == k
    return tr


def test_a_roll_re_open_passes_through_the_cooldown():
    tr = _armed()
    with tr.rolling():
        order = tr.on_signal("BUY", _market(), strength=1.0)
    assert order is not None and order.side == "BUY" and order.intent == "OPEN"


def test_a_roll_re_open_clears_the_cooldown():
    # The flatten was the roll's, not a strategy exit: nothing is left to guard.
    tr = _armed()
    with tr.rolling():
        tr.on_signal("BUY", _market(), strength=1.0)
    assert tr._cooldown_left == 0


def test_the_cooldown_still_blocks_outside_the_hook():
    tr = _armed(k=2)
    with tr.rolling():
        pass                                     # entered and left, no call
    assert tr.on_signal("BUY", _market(), strength=1.0) is None
    assert tr._cooldown_left == 1


def test_the_hook_is_left_even_when_the_call_raises():
    tr = _armed()
    with pytest.raises(RuntimeError):
        with tr.rolling():
            raise RuntimeError("broker down")
    assert tr._rolling is False
    assert tr.on_signal("BUY", _market(), strength=1.0) is None


@pytest.mark.usefixtures("cheap_strategy")
def test_a_live_settled_leg_is_re_entered_by_the_fullday_trader():
    # End to end through LiveRuntime: the settlement's fill arms the cooldown,
    # and the re-entry still goes out.
    from datetime import datetime
    from forecast_eval.tests.test_live_roll import _Market, _rolling_runtime
    rt, rec = _rolling_runtime()
    tr = Trader(rt.broker, tick_size=1.0, aggression_ticks=0,
                reentry_cooldown_signals=48)
    tr.position, tr.entry_price = 1, 17500.0
    rt.trader = rt.loop.trader = tr
    rt.settle_rolled_leg(price=17600.0, timestamp=datetime(2026, 8, 19, 14, 55))
    assert tr.position == 0 and tr._cooldown_left == 48
    rt._resume_rolled_exposure(_Market(datetime(2026, 8, 19, 16, 0), 17610.0))
    resumed = [f for e, f in rec.events if e == "roll_exposure_resumed"]
    assert resumed and resumed[0]["quantity"] == 1
    assert tr._cooldown_left == 0
