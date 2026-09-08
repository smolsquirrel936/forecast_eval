"""NIGHT's wiring carries a one-week time-stop (night autoresearch iter05).

iter03's drawdown anatomy: the only exits were RSI>=80 or a later opposing
signal, so in a decline the losers rode with no exit at all — four disaster
longs held 8 to 35 days and carried the max drawdown. `wiring.make_strategy`
now returns `TimeStop(TIME_STOP_BARS)` alongside the RSI emitter, so the cap
bounds that tail while the RSI>=80 exit keeps its winners untouched. These
tests pin the wiring contract; the exit behavior itself is the shared
`exits.time_stop` machinery, frozen and covered by its own tests.
"""
import pandas as pd

from forecast_eval.events import MarketEvent
from forecast_eval.exits.time_stop import TimeStop
from forecast_eval.exits.base import ExitRule, PositionState
from forecast_eval.strategy.night import wiring


def _market(price: float = 17500.0, ts: str = "2026-09-08 15:00:00") -> MarketEvent:
    return MarketEvent(
        timestamp=pd.Timestamp(ts), price=price, volume=1, session="NIGHT",
    )


def test_make_strategy_returns_a_time_stop():
    stack = wiring.make_strategy(warmup_bars=100)
    rule = stack["exit_rule"]
    assert isinstance(rule, TimeStop)
    assert isinstance(rule, ExitRule)
    assert rule.max_bars == wiring.TIME_STOP_BARS


def test_the_cap_is_about_one_week_of_night_sessions():
    """~840 bars per session -> 4000 bars ~ 4.8 sessions ~ one calendar week."""
    assert 2000 <= wiring.TIME_STOP_BARS <= 8000


def test_the_time_stop_fires_at_the_cap_on_a_long():
    rule = TimeStop(wiring.TIME_STOP_BARS)
    pos = PositionState(
        side="LONG", size=1, entry_price=17500.0, entry_bar_idx=1000,
        entry_timestamp=pd.Timestamp("2026-09-01 15:00:00"),
    )
    assert rule.should_exit(pos, _market(), bar_idx=1000 + wiring.TIME_STOP_BARS - 1) is False
    assert rule.should_exit(pos, _market(), bar_idx=1000 + wiring.TIME_STOP_BARS) is True


def test_each_call_gets_a_fresh_time_stop_instance():
    a = wiring.make_strategy(warmup_bars=100)["exit_rule"]
    b = wiring.make_strategy(warmup_bars=100)["exit_rule"]
    assert a is not b


def test_the_rsi_pair_is_untouched_by_the_wiring_change():
    stack = wiring.make_strategy(warmup_bars=100)
    assert type(stack["forecaster"]).__name__ == "RsiForecaster"
    assert type(stack["emitter"]).__name__ == "RsiEmitter"
    assert stack["forced_close"] is False