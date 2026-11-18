"""Passive fade entries — the fullday trader's ``entry_passive_ticks`` and
``entry_rest_forecasts`` knobs.

The 2026-11-17 Timestral research report's execution physics: the gated
5-minute fade's at-close edge is a first-minute latency effect, and a
marketable entry fills at the post-bounce next print (~10% capture). The knob
rests the ENTRY limit N ticks beyond the placement price so the fill model's
passive branch (strict cross, no price improvement — SPEC §4.5) applies:
fills happen only when the move extends, at a better price. Closes and exits
must stay marketable. These tests are GPU-free and pin exactly that split.
"""
import pandas as pd

from forecast_eval.events import MarketEvent
from forecast_eval.execution import Execution
from forecast_eval.strategy.fullday.trader import Trader
from forecast_eval.strategy.fullday.wiring import PARAMS, make_strategy


def _market(price: float = 17500.0, offset_s: int = 0) -> MarketEvent:
    return MarketEvent(
        timestamp=pd.Timestamp("2024-01-02 21:00:00") + pd.Timedelta(seconds=offset_s),
        price=price,
        volume=1,
        session="NIGHT",
    )


def _trader(**kwargs) -> Trader:
    # aggression 0 = the loop's pinned config: marketable orders rest at the
    # touch; entry_passive_ticks is the knob under test.
    return Trader(
        Execution(fee_rate=0.0), tick_size=1.0, aggression_ticks=0,
        max_position=1, **kwargs
    )


# ---- placement: entries go passive, closes/exits stay marketable --------------


def test_default_off_places_marketable_entries():
    tr = _trader()
    buy = tr.on_signal("BUY", _market(17500))
    assert buy.limit_price == 17500  # at the touch -> marketable
    tr2 = _trader()
    sell = tr2.on_signal("SELL", _market(17500))
    assert sell.limit_price == 17500


def test_passive_sell_rests_above_market():
    tr = _trader(entry_passive_ticks=2)
    order = tr.on_signal("SELL", _market(17500))
    assert order is not None
    assert order.side == "SELL" and order.intent == "OPEN"
    assert order.limit_price == 17502  # beyond the touch -> passive


def test_passive_buy_rests_below_market():
    tr = _trader(entry_passive_ticks=2)
    order = tr.on_signal("BUY", _market(17500))
    assert order is not None
    assert order.side == "BUY" and order.intent == "OPEN"
    assert order.limit_price == 17498


def test_passive_overrides_the_confidence_ramp():
    # the ramp (aggression/max_aggression) shapes marketable entries; with a
    # passive entry set, the passive offset is the entry offset, full stop.
    tr = _trader(entry_passive_ticks=3, max_aggression_ticks=9)
    order = tr.on_signal("SELL", _market(17500), strength=1.0)
    assert order.limit_price == 17503


def test_close_stays_marketable_with_passive_entries_on():
    tr = _trader(entry_passive_ticks=2)
    tr.position = 1
    tr.entry_price = 17480
    tr.entry_bar_idx = 0
    tr.entry_timestamp = pd.Timestamp("2024-01-02 21:00:00")
    order = tr.on_signal("SELL", _market(17500))
    assert order.intent == "CLOSE"
    assert order.limit_price == 17500  # at the touch -> marketable


def test_submit_exit_stays_marketable_with_passive_entries_on():
    tr = _trader(entry_passive_ticks=2)
    tr.position = -1
    tr.entry_price = 17520
    tr.entry_bar_idx = 0
    tr.entry_timestamp = pd.Timestamp("2024-01-02 21:00:00")
    order = tr.submit_exit(_market(17500))
    assert order.intent == "CLOSE"
    assert order.side == "BUY"
    assert order.limit_price == 17500


# ---- fills: the passive branch demands a strict cross (SPEC §4.5) ------------


def test_passive_sell_fills_only_on_strict_cross():
    tr = _trader(entry_passive_ticks=2)
    tr.on_signal("SELL", _market(17500))  # rests at 17502
    # a print at the limit is not a cross -> no fill
    assert tr.execution.check_fill(_market(17502, offset_s=60)) is None
    # a print above the limit crosses -> fills AT the limit
    fill = tr.execution.check_fill(_market(17503, offset_s=120))
    assert fill is not None
    assert fill.fill_price == 17502
    assert fill.side == "SELL"


def test_passive_buy_fills_only_on_strict_cross():
    tr = _trader(entry_passive_ticks=2)
    tr.on_signal("BUY", _market(17500))  # rests at 17498
    assert tr.execution.check_fill(_market(17498, offset_s=60)) is None
    fill = tr.execution.check_fill(_market(17497, offset_s=120))
    assert fill is not None
    assert fill.fill_price == 17498


def test_passive_order_survives_until_the_next_signal():
    # §4.4: a pending limit lives only until the next signal — including
    # HOLD, whose cancel is the whole point. The passive entry inherits it.
    tr = _trader(entry_passive_ticks=2)
    tr.on_signal("SELL", _market(17500))
    assert tr.execution.has_pending()
    tr.on_signal("HOLD", _market(17501, offset_s=300))
    assert not tr.execution.has_pending()


# ---- rest budget: surviving HOLD forecasts -----------------------------------


def test_rest_budget_keeps_order_through_holds():
    tr = _trader(entry_passive_ticks=1, entry_rest_forecasts=2)
    tr.on_signal("SELL", _market(17500))  # rests at 17501
    # two HOLD forecasts are survived while budget lasts
    tr.on_signal("HOLD", _market(17490, offset_s=300))
    assert tr.execution.has_pending()
    tr.on_signal("HOLD", _market(17480, offset_s=600))
    assert tr.execution.has_pending()
    # budget exhausted -> the third HOLD cancels (§4.4 recorded behaviour)
    tr.on_signal("HOLD", _market(17470, offset_s=900))
    assert not tr.execution.has_pending()


def test_real_signal_replaces_a_resting_order_despite_budget():
    tr = _trader(entry_passive_ticks=1, entry_rest_forecasts=5)
    first = tr.on_signal("SELL", _market(17500))  # rests at 17501
    assert first.limit_price == 17501
    second = tr.on_signal("SELL", _market(17510, offset_s=300))
    assert second.limit_price == 17511  # re-placed at the new reference
    assert tr.execution.pending_order() is second


def test_fill_during_rest_window_still_works():
    tr = _trader(entry_passive_ticks=1, entry_rest_forecasts=2)
    tr.on_signal("SELL", _market(17500))  # rests at 17501
    tr.on_signal("HOLD", _market(17490, offset_s=300))  # survived
    fill = tr.execution.check_fill(_market(17502, offset_s=360))
    assert fill is not None and fill.fill_price == 17501


def test_default_rest_is_off():
    # None/0 = recorded §4.4: the very next forecast (HOLD included) cancels.
    tr = _trader(entry_passive_ticks=1, entry_rest_forecasts=None)
    tr.on_signal("SELL", _market(17500))
    tr.on_signal("HOLD", _market(17490, offset_s=300))
    assert not tr.execution.has_pending()


# ---- night-only opens ---------------------------------------------------------


def test_night_only_open_blocks_day_opens():
    tr = _trader(night_only_open=True)
    day = MarketEvent(
        timestamp=pd.Timestamp("2024-01-03 10:00:00"),
        price=17500.0, volume=1, session="DAY",
    )
    assert tr.on_signal("SELL", day) is None  # gated: cancel-and-stand-aside
    assert not tr.execution.has_pending()
    night = MarketEvent(
        timestamp=pd.Timestamp("2024-01-03 21:00:00"),
        price=17500.0, volume=1, session="NIGHT",
    )
    order = tr.on_signal("SELL", night)
    assert order is not None and order.intent == "OPEN"


def test_night_only_open_never_gates_closes():
    tr = _trader(night_only_open=True)
    tr.position = 1
    tr.entry_price = 17480
    tr.entry_bar_idx = 0
    tr.entry_timestamp = pd.Timestamp("2024-01-03 21:00:00")
    day = MarketEvent(
        timestamp=pd.Timestamp("2024-01-04 10:00:00"),
        price=17500.0, volume=1, session="DAY",
    )
    order = tr.on_signal("SELL", day)  # close the long during the day
    assert order is not None and order.intent == "CLOSE"
    exit_order = tr.submit_exit(day)
    assert exit_order is not None and exit_order.intent == "CLOSE"


def test_night_only_open_default_off():
    tr = _trader()
    day = MarketEvent(
        timestamp=pd.Timestamp("2024-01-03 10:00:00"),
        price=17500.0, volume=1, session="DAY",
    )
    assert tr.on_signal("SELL", day) is not None


def test_wiring_declares_night_only_open():
    base = make_strategy(warmup_bars=3008)
    assert base["params"]["night_only_open"] is True       # iter14
    assert base["make_trader"](Execution(fee_rate=0.0)).night_only_open is True
    off = make_strategy(warmup_bars=3008, night_only_open=False)
    assert off["make_trader"](Execution(fee_rate=0.0)).night_only_open is False


# ---- roll legs bypass the session gate and the passive offset -------------------


def test_a_roll_reopen_in_the_day_session_is_marketable():
    # fullday rolls at 12:45 (DAY): the re-open must go out, at the touch.
    tr = _trader(entry_passive_ticks=1, entry_rest_forecasts=3,
                 night_only_open=True)
    day = MarketEvent(
        timestamp=pd.Timestamp("2024-01-17 12:45:00"),
        price=17500.0, volume=1, session="DAY",
    )
    assert tr.on_signal("BUY", day) is None                # gated outside a roll
    with tr.rolling():
        order = tr.on_signal("BUY", day)
    assert order is not None and order.intent == "OPEN"
    assert order.limit_price == 17500.0                    # marketable, not 17499
    assert tr._resting_passive is False


# ---- wiring -------------------------------------------------------------------


def test_wiring_declares_the_iter14_values():
    assert PARAMS["entry_passive_ticks"] == 1
    assert PARAMS["entry_rest_forecasts"] == 3
    base = make_strategy(warmup_bars=3008)
    trader = base["make_trader"](Execution(fee_rate=0.0))
    assert trader.entry_passive_ticks == 1
    assert trader.entry_rest_forecasts == 3
    on = make_strategy(warmup_bars=3008, entry_passive_ticks=2,
                       entry_rest_forecasts=2)
    trader_on = on["make_trader"](Execution(fee_rate=0.0))
    assert trader_on.entry_passive_ticks == 2
    assert trader_on.entry_rest_forecasts == 2
    assert trader_on.aggression_ticks == 0
