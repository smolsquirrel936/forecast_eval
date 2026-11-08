"""The persistence gate (strategy_test_1108) in the night and fullday packages.

``PersistenceEmitter`` wraps a package's emitter and emits BUY/SELL only once
that side held on ``min_count`` of the last ``window`` decisions. Each package
owns its own copy, so every case runs against both. ``persist_window`` and
``persist_min`` both ``None`` is the control: the wiring returns the bare
emitter, so the adopted packages are unchanged.

Also covers the Trader knob stride 1 needs, ``exit_survives_signal``: at stride
1 every tick emits a signal, which would cancel an exit order placed on that
same tick, so a fired stop never filled.
"""
import pytest

from forecast_eval.events import Signal
from forecast_eval.execution import Execution
from forecast_eval.strategy.base import SignalEmitter
from forecast_eval.strategy.fullday import emitter as fullday_emitter
from forecast_eval.strategy.fullday import wiring as fullday_wiring
from forecast_eval.strategy.night import emitter as night_emitter
from forecast_eval.strategy.night import wiring as night_wiring

PACKAGES = [(night_emitter, night_wiring), (fullday_emitter, fullday_wiring)]
IDS = ["night", "fullday"]


class Scripted(SignalEmitter):
    """Emits a fixed sequence of (direction, strength), one per call."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.marker = "inner"

    def emit_signal(self, forecast):
        d, s = self.script[self.calls]
        self.calls += 1
        return Signal(direction=d, strength=s)

    def emit(self, forecast):
        return self.emit_signal(forecast).direction


def _run(mod, script, window, min_count):
    w = mod.PersistenceEmitter(Scripted(script), window=window, min_count=min_count)
    return [w.emit_signal(None) for _ in script], w


@pytest.mark.parametrize("mod,_", PACKAGES, ids=IDS)
def test_needs_min_count_of_the_window(mod, _):
    script = [("SELL", .4), ("HOLD", 0), ("SELL", .6), ("HOLD", 0), ("HOLD", 0), ("HOLD", 0)]
    out, _w = _run(mod, script, window=3, min_count=2)
    assert [s.direction for s in out] == ["HOLD", "HOLD", "SELL", "HOLD", "HOLD", "HOLD"]
    assert out[2].strength == pytest.approx(0.5)  # mean of the two agreeing SELLs


@pytest.mark.parametrize("mod,_", PACKAGES, ids=IDS)
def test_a_lone_blip_is_held(mod, _):
    out, _w = _run(mod, [("HOLD", 0)] * 4 + [("BUY", 1.0)] + [("HOLD", 0)] * 4, window=5, min_count=2)
    assert all(s.direction == "HOLD" for s in out)


@pytest.mark.parametrize("mod,_", PACKAGES, ids=IDS)
def test_a_tie_between_sides_is_hold(mod, _):
    out, _w = _run(mod, [("BUY", .5), ("SELL", .5), ("BUY", .5), ("SELL", .5)], window=4, min_count=2)
    assert out[-1].direction == "HOLD"


@pytest.mark.parametrize("mod,_", PACKAGES, ids=IDS)
def test_window_one_count_one_passes_every_decision_through(mod, _):
    script = [("BUY", .3), ("HOLD", 0), ("SELL", .7)]
    out, _w = _run(mod, script, window=1, min_count=1)
    assert [(s.direction, s.strength) for s in out] == [("BUY", .3), ("HOLD", 0.0), ("SELL", .7)]


@pytest.mark.parametrize("mod,_", PACKAGES, ids=IDS)
def test_inner_runs_on_every_forecast_and_attributes_forward(mod, _):
    out, w = _run(mod, [("HOLD", 0)] * 5, window=3, min_count=2)
    assert w.inner.calls == 5
    assert w.marker == "inner"
    assert all(s.direction == "HOLD" for s in out)


@pytest.mark.parametrize("mod,_", PACKAGES, ids=IDS)
@pytest.mark.parametrize("window,min_count", [(0, 1), (3, 0), (3, 4), (3.0, 2)])
def test_rejects_bad_widths(mod, _, window, min_count):
    with pytest.raises(ValueError):
        mod.PersistenceEmitter(Scripted([]), window=window, min_count=min_count)


# -------- wiring ---------------------------------------------------------------------

@pytest.mark.parametrize("mod,wiring", PACKAGES, ids=IDS)
def test_control_leaves_the_emitter_unwrapped(mod, wiring):
    assert wiring.PARAMS["persist_window"] is None and wiring.PARAMS["persist_min"] is None
    strat = wiring.make_strategy(warmup_bars=100)
    assert type(strat["emitter"]) is mod.ThresholdEmitter


@pytest.mark.parametrize("mod,wiring", PACKAGES, ids=IDS)
def test_both_knobs_wrap_the_emitter(mod, wiring):
    strat = wiring.make_strategy(warmup_bars=100, persist_window=15, persist_min=8)
    e = strat["emitter"]
    assert isinstance(e, mod.PersistenceEmitter)
    assert (e.window, e.min_count) == (15, 8)
    assert type(e.inner) is mod.ThresholdEmitter


@pytest.mark.parametrize("mod,wiring", PACKAGES, ids=IDS)
@pytest.mark.parametrize("kw", [{"persist_window": 15}, {"persist_min": 8}])
def test_one_knob_alone_raises(mod, wiring, kw):
    with pytest.raises(ValueError, match="persist_window and persist_min"):
        wiring.make_strategy(warmup_bars=100, **kw)


# -------- exit_survives_signal (the trader knob stride 1 needs) -----------------------

def _long_trader(wiring, **kw):
    import pandas as pd
    strat = wiring.make_strategy(warmup_bars=100, **kw)
    tr = strat["make_trader"](Execution(fee_rate=0.0))
    tr.on_fill("BUY", 1, fill_price=17500.0, bar_idx=0, timestamp=pd.Timestamp("2024-01-02 09:00"))
    return tr


def _mkt(minute, price=17000.0):
    import pandas as pd
    from forecast_eval.events import MarketEvent
    return MarketEvent(timestamp=pd.Timestamp("2024-01-02 09:00") + pd.Timedelta(minutes=minute),
                       price=price, volume=1, session="DAY")


@pytest.mark.parametrize("mod,wiring", PACKAGES, ids=IDS)
def test_control_signal_cancels_a_same_tick_exit(mod, wiring):
    assert wiring.PARAMS["exit_survives_signal"] is False
    tr = _long_trader(wiring)
    tr.submit_exit(_mkt(5))
    tr.on_signal("HOLD", _mkt(5))
    assert not tr.execution.has_pending()


@pytest.mark.parametrize("mod,wiring", PACKAGES, ids=IDS)
@pytest.mark.parametrize("direction", ["HOLD", "BUY", "SELL"])
def test_knob_keeps_a_same_tick_exit_working(mod, wiring, direction):
    tr = _long_trader(wiring, exit_survives_signal=True)
    exit_order = tr.submit_exit(_mkt(5))
    assert tr.on_signal(direction, _mkt(5)) is None
    assert tr.execution.pending_order() is exit_order


@pytest.mark.parametrize("mod,wiring", PACKAGES, ids=IDS)
def test_knob_does_not_protect_an_exit_from_a_later_tick(mod, wiring):
    tr = _long_trader(wiring, exit_survives_signal=True)
    tr.submit_exit(_mkt(5))
    tr.on_signal("HOLD", _mkt(6))
    assert not tr.execution.has_pending()


def test_fullday_make_trader_binds_the_inner_emitter_through_the_wrapper():
    strat = fullday_wiring.make_strategy(warmup_bars=100, persist_window=15, persist_min=8)
    trader = strat["make_trader"](Execution(fee_rate=0.0))
    getter = strat["emitter"].inner._position_getter
    assert getter is not None and getter() == trader.position
