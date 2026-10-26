"""HYBRID's router (strategy/hybrid): each session is traded by its own half.

GPU-free: the forecasters here are fakes; `make_strategy` builds real Toto2
forecasters but they load nothing until asked to forecast. The requirements
these cases pin are R1-R6 of `autoresearch_trading_plan_hybrid_split07.md` Step 0.4.
"""
from datetime import datetime

import pandas as pd
import pytest

from forecast_eval.events import Forecast, MarketEvent
from forecast_eval.execution import Execution
from forecast_eval.exits.pct_stop import EntryPercentStop
from forecast_eval.run import run_backtest
from forecast_eval.strategy import wiring as seam
from forecast_eval.strategy.hybrid import wiring
from forecast_eval.strategy.hybrid.forecaster import RoutedForecaster
from forecast_eval.strategy.hybrid.router import (
    RoutedEmitter, RoutedExitRule, RoutedTrader, session_of,
)

DAY_TS = datetime(2024, 3, 4, 10, 0)
NIGHT_TS = datetime(2024, 3, 4, 20, 0)
AFTER_MIDNIGHT_TS = datetime(2024, 3, 5, 2, 0)


def fc(ts, r=0.0007, prob_up=0.6):
    return Forecast(timestamp=ts, horizon_bars=30,
                    payload={"predicted_return": r, "prob_up": prob_up})


def mkt(ts, price=100.0):
    return MarketEvent(timestamp=ts, price=price, volume=1, session=session_of(ts))


class FakeForecaster:
    """Stamps each forecast with its own name, so routing is observable."""

    def __init__(self, name, *, checkpoint="ck", context_length=8, horizon=30):
        self.name, self.calls = name, []
        self.checkpoint, self.context_length = checkpoint, context_length
        self.forecast_horizon_bars, self.bar_freq, self.signal_step = horizon, None, "last"
        self.device = "auto"

    def forecast(self, history_df):
        self.calls.append(("one", history_df["timestamp"].iloc[-1]))
        return Forecast(history_df["timestamp"].iloc[-1], 30, {"by": self.name})

    def forecast_series_batch(self, timestamps, prices, boundaries, *, batch_size="auto",
                              progress=False):
        self.calls.append(("batch", list(boundaries)))
        return [Forecast(timestamps[b], 30, {"by": self.name, "b": b}) for b in boundaries]


def routed(day, night):
    return RoutedForecaster(day, night, warmup_bars=0, stride_bars=1, horizon_bars=30)


# ---- sessions --------------------------------------------------------------

def test_session_of_handles_the_midnight_wrap_and_refuses_the_gap():
    assert session_of(DAY_TS) == "DAY"
    assert session_of(NIGHT_TS) == "NIGHT"
    assert session_of(AFTER_MIDNIGHT_TS) == "NIGHT"
    with pytest.raises(ValueError):
        session_of(datetime(2024, 3, 4, 14, 30))


# ---- R1: emitter -----------------------------------------------------------

def test_each_half_emits_with_its_own_band():
    # +0.0007 at prob 0.6 is inside day's reset band and outside night's reset band.
    # The loop edits PARAMS (autoresearch_trading_plan_hybrid_split07.md), so the
    # reset knobs are pinned here as overrides instead of read from the committed
    # PARAMS: this test is about routing, not any iteration's band values.
    em = wiring.make_strategy(
        warmup_bars=0,
        day_buy_threshold=0.001, day_sell_threshold=-0.001, day_min_prob=0.65,
        night_buy_threshold=0.00045, night_sell_threshold=-0.00045,
        night_min_prob=0.5, night_allow_open=True,
    )["emitter"]
    assert em.emit(fc(DAY_TS)) == "HOLD"
    assert em.emit(fc(NIGHT_TS)) == "BUY"
    assert em.emit_signal(fc(AFTER_MIDNIGHT_TS)).direction == "BUY"


def test_both_halves_see_every_forecast_but_only_the_session_half_decides():
    seen = {"DAY": [], "NIGHT": []}

    class Rec:
        def __init__(self, sess):
            self.sess = sess

        def emit(self, f):
            seen[self.sess].append(f.timestamp)
            return "BUY" if self.sess == "DAY" else "SELL"

        def emit_signal(self, f):
            return self.emit(f)

    em = RoutedEmitter(day=Rec("DAY"), night=Rec("NIGHT"))
    out = [em.emit_signal(fc(ts)) for ts in (DAY_TS, NIGHT_TS, AFTER_MIDNIGHT_TS)]
    assert out == ["BUY", "SELL", "SELL"]
    assert seen == {"DAY": [DAY_TS, NIGHT_TS, AFTER_MIDNIGHT_TS],
                    "NIGHT": [DAY_TS, NIGHT_TS, AFTER_MIDNIGHT_TS]}
    with pytest.raises(ValueError):
        em.emit(None)


def test_identical_halves_decide_exactly_as_one_emitter():
    # The Step 0 parity property, GPU-free: with night set to day's values, the
    # routed pair must match one emitter on every forecast, trend-filter state
    # included. A trend-sensitive payload sequence crossing DAY -> NIGHT -> DAY.
    from forecast_eval.strategy.hybrid.day_half.emitter import ThresholdEmitter
    knobs = dict(buy_threshold=0.001, sell_threshold=-0.001, min_prob=0.65,
                 full_confidence_prob=0.9)
    routed_em = wiring.make_strategy(
        warmup_bars=0, night_buy_threshold=0.001, night_sell_threshold=-0.001,
        night_min_prob=0.65)["emitter"]
    single = ThresholdEmitter(**knobs)
    stamps = list(pd.date_range("2024-03-04 12:30", periods=3, freq="30min")) + \
        list(pd.date_range("2024-03-04 15:00", periods=4, freq="30min")) + \
        list(pd.date_range("2024-03-05 08:45", periods=3, freq="30min"))
    prices = [100, 104, 108, 103, 99, 110, 115, 112, 120, 118]
    for ts, px in zip(stamps, prices):
        f = Forecast(ts, 30, {"predicted_return": 0.002, "prob_up": 0.8, "last_price": px})
        assert routed_em.emit_signal(f) == single.emit_signal(f), ts


# ---- R1/R2: trader ---------------------------------------------------------

def make_trader():
    execution = Execution()
    # The loop edits PARAMS (autoresearch_trading_plan_hybrid_split07.md), so the
    # trader-level decision-path knobs are pinned here as overrides instead of
    # read from the committed PARAMS: these tests are about routing, not any
    # iteration's open-block window (the day half must be able to open at
    # DAY_TS=10:00 whatever the committed block start is).
    return execution, wiring.make_strategy(
        warmup_bars=0,
        day_open_block_start=None, day_open_block_end=None,
    )["make_trader"](execution)


def test_a_lot_opened_by_day_is_closed_by_night_and_both_mirrors_agree():
    execution, tr = make_trader()
    order = tr.on_signal("BUY", mkt(DAY_TS))
    assert order is not None and execution.has_pending()
    tr.on_fill("BUY", 1, fill_price=100.0, bar_idx=5, timestamp=DAY_TS)
    assert tr.position == 1
    assert [h.position for h in tr.halves.values()] == [1, 1]
    assert tr.position_state().entry_price == 100.0
    # A night SELL while long closes only (no-flip, SPEC 4.2), via the night half.
    close = tr.on_signal("SELL", mkt(NIGHT_TS, 105.0))
    assert close.side == "SELL" and close.quantity == 1
    tr.on_fill("SELL", 1, fill_price=105.0, bar_idx=9, timestamp=NIGHT_TS)
    assert tr.position == 0 and tr.position_state() is None


def test_signals_go_to_the_half_of_the_market_session():
    _, tr = make_trader()
    calls = []
    for sess, half in tr.halves.items():
        half.on_signal = (lambda s: lambda d, m, strength=1.0: calls.append(s))(sess)
    tr.on_signal("HOLD", mkt(NIGHT_TS))
    tr.on_signal("HOLD", mkt(DAY_TS))
    assert calls == ["NIGHT", "DAY"]


def test_a_live_resume_adopts_the_lot_into_both_halves():
    """`LiveRuntime.adopt_position` seeds the trader by assignment. On the router
    `position` was read-only, so a `--resume` into a held lot raised; the lot
    must land in both halves, since either may manage it next."""
    _, tr = make_trader()
    tr.position = -1
    tr.entry_price = 101.0
    tr.entry_bar_idx = -1
    tr.entry_timestamp = NIGHT_TS
    for half in tr.halves.values():
        assert (half.position, half.entry_price, half.entry_bar_idx,
                half.entry_timestamp) == (-1, 101.0, -1, NIGHT_TS)
    assert (tr.position, tr.entry_price, tr.entry_bar_idx,
            tr.entry_timestamp) == (-1, 101.0, -1, NIGHT_TS)
    # The adopted short is closed by a BUY like any other lot (no-flip).
    close = tr.on_signal("BUY", mkt(DAY_TS, 99.0))
    assert close.side == "BUY" and close.quantity == 1


def test_a_roll_enters_every_halfs_rolling_hook():
    """A trader with entry filters exempts a roll's re-open through `rolling()`;
    the runtime finds the hook on the router, which must open each half's."""
    from contextlib import contextmanager
    _, tr = make_trader()
    seen = []

    def hook(name):
        @contextmanager
        def rolling():
            seen.append(("in", name))
            yield
            seen.append(("out", name))
        return rolling

    tr.halves["DAY"].rolling = hook("DAY")
    with tr.rolling():
        seen.append("body")
    assert seen == [("in", "DAY"), "body", ("out", "DAY")]


def test_a_fill_that_reached_one_half_only_is_loud():
    _, tr = make_trader()
    tr.halves["DAY"].on_fill("BUY", 1, fill_price=1.0, bar_idx=0, timestamp=DAY_TS)
    with pytest.raises(RuntimeError):
        tr.position


# ---- exit rule -------------------------------------------------------------

def test_only_the_half_with_a_stop_can_exit():
    _, tr = make_trader()
    tr.on_fill("BUY", 1, fill_price=100.0, bar_idx=0, timestamp=DAY_TS)
    rule = RoutedExitRule(None, EntryPercentStop(stop_pct=0.05))
    pos = tr.position_state()
    assert rule.should_exit(pos, mkt(DAY_TS, 80.0), bar_idx=1) is False
    assert rule.should_exit(pos, mkt(NIGHT_TS, 80.0), bar_idx=1) is True
    assert rule.adoptable is True


def test_the_exit_rule_is_none_until_a_half_sets_a_stop():
    assert wiring.make_strategy(warmup_bars=0)["exit_rule"] is None
    rule = wiring.make_strategy(warmup_bars=0, night_stop_pct=0.05)["exit_rule"]
    assert rule.halves["DAY"] is None and rule.halves["NIGHT"].stop_pct == 0.05


# ---- R3/R4/R5: forecaster ---------------------------------------------------

STAMPS = [DAY_TS, NIGHT_TS, AFTER_MIDNIGHT_TS, datetime(2024, 3, 5, 9, 0)]


def test_batch_splits_by_session_and_keeps_the_boundary_order():
    day, night = FakeForecaster("day"), FakeForecaster("night", context_length=16)
    out = routed(day, night).forecast_series_batch(STAMPS, [1.0] * 4, [0, 1, 2, 3])
    assert [f.payload["by"] for f in out] == ["day", "night", "night", "day"]
    assert [f.payload["b"] for f in out] == [0, 1, 2, 3]
    assert day.calls == [("batch", [0, 3])] and night.calls == [("batch", [1, 2])]


def test_identical_halves_share_one_instance_and_one_batch_call():
    day, night = FakeForecaster("day"), FakeForecaster("night")
    r = routed(day, night)
    assert r.shared and r.halves["NIGHT"] is day
    out = r.forecast_series_batch(STAMPS, [1.0] * 4, [0, 1, 2, 3])
    assert day.calls == [("batch", [0, 1, 2, 3])] and night.calls == []
    assert [f.payload["b"] for f in out] == [0, 1, 2, 3]


def test_sequential_forecast_routes_by_the_last_history_row():
    day, night = FakeForecaster("day"), FakeForecaster("night", context_length=16)
    r = routed(day, night)
    hist = pd.DataFrame({"timestamp": [DAY_TS, NIGHT_TS], "price": [1.0, 2.0]})
    assert r.forecast(hist).payload["by"] == "night"


def test_the_cache_key_attributes_move_with_either_half():
    base = routed(FakeForecaster("d"), FakeForecaster("n"))
    moved = routed(FakeForecaster("d"), FakeForecaster("n", context_length=16))
    assert base.context_length == "day=8;night=8"
    assert moved.context_length == "day=8;night=16"
    assert base.forecast_horizon_bars == 30


def test_a_per_half_horizon_is_refused():
    with pytest.raises(ValueError, match="horizon"):
        routed(FakeForecaster("d"), FakeForecaster("n", horizon=60))


def test_halves_on_one_checkpoint_share_the_loaded_model():
    day, night = FakeForecaster("d"), FakeForecaster("n", context_length=16)
    day._model, day._torch, day._patch_size = object(), object(), 64
    night._model = night._torch = night._patch_size = None
    r = routed(day, night)
    r.forecast_series_batch(STAMPS, [1.0] * 4, [1])
    assert night._model is day._model and night._patch_size == 64


# ---- R6 + make_strategy guards ---------------------------------------------

def test_unedited_package_shares_one_forecaster_and_builds_fresh_objects():
    a, b = wiring.make_strategy(warmup_bars=0), wiring.make_strategy(warmup_bars=0)
    assert a["forecaster"].shared
    assert a["emitter"] is not b["emitter"]
    assert a["forecaster"].halves["DAY"] is not b["forecaster"].halves["DAY"]
    assert a["forced_close"] is False


@pytest.mark.parametrize("knob", wiring.INERT)
def test_an_unprefixed_band_knob_is_refused(knob):
    with pytest.raises(ValueError, match="day_/night_"):
        wiring.make_strategy(warmup_bars=0, **{knob: 1})


def test_the_history_window_must_cover_the_wider_half():
    with pytest.raises(ValueError, match="context_length"):
        wiring.make_strategy(warmup_bars=0, night_context_length=4096)
    s = wiring.make_strategy(warmup_bars=0, night_context_length=4096, context_length=4096)
    assert not s["forecaster"].shared


def test_hybrid_sha_covers_the_router_and_both_halves():
    assert seam.FORECASTER_FILES["hybrid"] == (
        "forecaster.py", "day_half/forecaster.py", "night_half/forecaster.py")
    assert len(seam.forecaster_sha("hybrid")) == 12


# ---- the engine accepts the routers ----------------------------------------

def test_a_lot_carries_across_the_session_change_in_a_real_run():
    # Day, night, then the next day, with a fake forecaster that is bullish at
    # 0.0007: only the night half buys, and the lot is still held through the
    # next DAY session (day's band HOLDs it; nothing closes at the change).
    # The reset knobs and night_allow_open=True are pinned as overrides because
    # the loop edits PARAMS: this test is about the router's handover semantics
    # (a lot opened by the night half carries into DAY), which the night half's
    # entry-suppression knob must not disable.
    ts = list(pd.date_range("2024-03-04 13:30", periods=10, freq="1min")) + \
        list(pd.date_range("2024-03-04 15:00", periods=10, freq="1min")) + \
        list(pd.date_range("2024-03-05 08:45", periods=10, freq="1min"))
    ticks = pd.DataFrame({"timestamp": ts, "price": [100.0] * 30, "volume": [1] * 30})

    class Bull(FakeForecaster):
        def forecast(self, history_df):
            return fc(history_df["timestamp"].iloc[-1])

    s = wiring.make_strategy(
        warmup_bars=2,
        day_buy_threshold=0.001, day_sell_threshold=-0.001, day_min_prob=0.65,
        night_buy_threshold=0.00045, night_sell_threshold=-0.00045,
        night_min_prob=0.5, night_allow_open=True,
    )
    f = RoutedForecaster(Bull("d"), Bull("n"), warmup_bars=2, stride_bars=2, horizon_bars=30)
    res = run_backtest(ticks, forecaster=f, emitter=s["emitter"], make_trader=s["make_trader"],
                       exit_rule=s["exit_rule"], forced_close_on_session_end=s["forced_close"])
    assert [x.side for x in res.fills] == ["BUY"]
    assert session_of(res.fills[0].timestamp) == "NIGHT"
    day_signals = [x for x in res.signals if x["session"] == "DAY"]
    assert day_signals and day_signals[-1]["timestamp"] >= pd.Timestamp("2024-03-05 08:45")
    assert res.n_forced_closes == 0
