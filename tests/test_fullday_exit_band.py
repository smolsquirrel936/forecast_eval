"""fullday's position-aware exit band (reversal loop decision 8, phase E).

The knob lives in the emitter: when wiring's ``make_trader`` binds the emitter
to the trader it builds, an opposite-side signal clears at the narrower
``|predicted_return| >= exit_threshold`` while positioned; from flat, and on
the same side, the full ±band applies. ``exit_threshold == buy_threshold`` is
the inert control — the cuts are exactly today's, so a run at the control is
byte-identical to one without the knob, and an unbound emitter behaves as
today regardless of the knob's value.
"""
import pandas as pd
import pytest

from forecast_eval.events import Forecast, MarketEvent
from forecast_eval.execution import Execution
from forecast_eval.strategy.fullday.emitter import ThresholdEmitter
from forecast_eval.strategy.fullday.wiring import make_strategy


def _fc(predicted_return, prob_up=None, last_price=17500.0):
    """A Forecast whose payload mimics Toto2's, with controllable fields."""
    payload = {"predicted_return": predicted_return, "last_price": last_price}
    if prob_up is not None:
        payload["prob_up"] = prob_up
    return Forecast(timestamp=pd.Timestamp("2024-01-02 09:00:00"),
                    horizon_bars=30, payload=payload)


def _market(price: float = 17500.0) -> MarketEvent:
    return MarketEvent(
        timestamp=pd.Timestamp("2024-01-02 09:00:00"),
        price=price,
        volume=1,
        session="DAY",
    )


def _emitter(**kw):
    # Band/prob knobs at the package's pinned values; the two confirmation
    # filters default OFF here so the band logic is tested in isolation (they
    # get their own tests below).
    kw.setdefault("buy_threshold", 0.001)
    kw.setdefault("sell_threshold", -0.001)
    kw.setdefault("min_prob", 0.50)
    kw.setdefault("full_confidence_prob", 0.9)
    kw.setdefault("trend_filter", False)
    kw.setdefault("path_momentum", False)
    return ThresholdEmitter(**kw)


# -------- a weak opposite forecast exits while positioned ---------------------

def test_weak_opposite_forecast_sells_while_long():
    em = _emitter(exit_threshold=0.0005)
    em.bind_position(lambda: 1)
    # -0.0006 is inside the ±0.001 entry band but past the -0.0005 exit cut.
    assert em.emit_signal(_fc(-0.0006, prob_up=0.30)).direction == "SELL"


def test_weak_opposite_forecast_buys_while_short():
    em = _emitter(exit_threshold=0.0005)
    em.bind_position(lambda: -1)
    assert em.emit_signal(_fc(+0.0006, prob_up=0.70)).direction == "BUY"


def test_exit_cut_is_inclusive():
    em = _emitter(exit_threshold=0.0005)
    em.bind_position(lambda: 1)
    assert em.emit_signal(_fc(-0.0005, prob_up=0.30)).direction == "SELL"
    assert em.emit_signal(_fc(-0.0004999, prob_up=0.30)).direction == "HOLD"


def test_full_band_still_exits_while_positioned():
    # A forecast past the full opposite band exits whatever the exit cut is.
    em = _emitter(exit_threshold=0.0005)
    em.bind_position(lambda: 1)
    assert em.emit_signal(_fc(-0.0012, prob_up=0.30)).direction == "SELL"


# -------- from flat, and on the same side, the full band applies ---------------

def test_same_forecast_from_flat_opens_nothing():
    em = _emitter(exit_threshold=0.0005)
    em.bind_position(lambda: 0)
    assert em.emit_signal(_fc(-0.0006, prob_up=0.30)).direction == "HOLD"
    assert em.emit_signal(_fc(+0.0006, prob_up=0.70)).direction == "HOLD"


def test_same_direction_signal_keeps_the_full_band_while_positioned():
    em = _emitter(exit_threshold=0.0005)
    em.bind_position(lambda: 1)
    # A weak same-side (BUY, while long) forecast must clear the full band.
    assert em.emit_signal(_fc(+0.0006, prob_up=0.90)).direction == "HOLD"
    assert em.emit_signal(_fc(+0.001, prob_up=0.90)).direction == "BUY"


def test_exit_then_reentry_needs_the_full_band():
    # The getter is read live, so the moment the position reports flat the
    # narrowed cut is gone — a weak opposite forecast after the close is HOLD,
    # which is what stops the exit band from churning re-entries.
    position = 1
    em = _emitter(exit_threshold=0.0005)
    em.bind_position(lambda: position)
    assert em.emit_signal(_fc(-0.0006, prob_up=0.30)).direction == "SELL"
    position = 0
    assert em.emit_signal(_fc(-0.0006, prob_up=0.30)).direction == "HOLD"


# -------- the gates still apply to the exit signal ----------------------------

def test_min_prob_still_gates_the_exit_signal():
    em = _emitter(exit_threshold=0.0005, min_prob=0.65)
    em.bind_position(lambda: 1)
    # dir_prob(SELL) = 1 - 0.40 = 0.60 < 0.65: held despite clearing the cut.
    assert em.emit_signal(_fc(-0.0006, prob_up=0.40)).direction == "HOLD"
    # dir_prob(SELL) = 0.70 >= 0.65: exits.
    assert em.emit_signal(_fc(-0.0006, prob_up=0.30)).direction == "SELL"


def test_trend_filter_still_gates_the_exit_signal():
    # short_window=3 (deque of 4): the first two calls have too little
    # history to veto; from the third, a rising tape vetoes the SELL exit
    # (needs late-early <= -3 ticks) and a falling one confirms it.
    em = _emitter(exit_threshold=0.0005, trend_filter=True)
    em.bind_position(lambda: 1)
    rising = [17500.0, 17502.0, 17504.0, 17506.0]
    got = [em.emit_signal(_fc(-0.0006, prob_up=0.30, last_price=px)).direction
           for px in rising]
    assert got == ["SELL", "SELL", "HOLD", "HOLD"]

    em2 = _emitter(exit_threshold=0.0005, trend_filter=True)
    em2.bind_position(lambda: 1)
    falling = [17506.0, 17504.0, 17502.0, 17500.0]
    got = [em2.emit_signal(_fc(-0.0006, prob_up=0.30, last_price=px)).direction
           for px in falling]
    assert got == ["SELL", "SELL", "SELL", "SELL"]


# -------- the control is inert -------------------------------------------------

def test_control_equals_the_unbound_emitter():
    # A bound emitter at exit_threshold == buy_threshold must return exactly
    # today's (direction, strength) for every forecast, at every position —
    # long, short and flat, same-side and opposite-side payloads alike.
    cases = [
        (+0.0012, 0.80), (-0.0012, 0.20), (+0.0006, 0.70), (-0.0006, 0.30),
        (+0.0020, 0.95), (-0.0009, 0.25), (+0.0000, 0.50), (-0.0015, 0.10),
        (+0.001, 0.90), (-0.001, 0.10),
    ]
    for position in (1, -1, 0):
        bound = _emitter(exit_threshold=0.001)
        bound.bind_position(lambda p=position: p)
        plain = _emitter()  # no knob, no binding — today's emitter
        for r, p in cases:
            assert bound.emit_signal(_fc(r, prob_up=p)) == \
                plain.emit_signal(_fc(r, prob_up=p)), (position, r, p)


def test_unbound_emitter_behaves_as_today_even_with_the_knob():
    em = _emitter(exit_threshold=0.0005)  # no bind_position call
    assert em.emit_signal(_fc(-0.0006, prob_up=0.30)).direction == "HOLD"
    assert em.emit_signal(_fc(-0.0012, prob_up=0.30)).direction == "SELL"


# -------- ValueError cases ------------------------------------------------------

def test_asymmetric_band_is_rejected():
    with pytest.raises(ValueError, match="symmetric"):
        ThresholdEmitter(buy_threshold=0.001, sell_threshold=-0.0009,
                         exit_threshold=0.0005,
                         trend_filter=False, path_momentum=False)


def test_negative_exit_threshold_is_rejected():
    with pytest.raises(ValueError, match="exit_threshold"):
        _emitter(exit_threshold=-0.0001)


def test_exit_threshold_above_the_band_is_rejected():
    with pytest.raises(ValueError, match="exit_threshold"):
        _emitter(exit_threshold=0.002)


# -------- wiring: pass-through, binding, and validation -------------------------

def test_make_strategy_passes_exit_threshold_to_the_emitter():
    strat = make_strategy(warmup_bars=100)
    assert strat["emitter"].exit_threshold == 0.001


def test_make_strategy_binds_the_emitter_to_the_trader_it_builds():
    strat = make_strategy(warmup_bars=100)
    tr = strat["make_trader"](Execution(fee_rate=0.0))
    assert strat["emitter"]._position_getter() == 0
    tr.position = 1
    assert strat["emitter"]._position_getter() == 1


def test_make_strategy_rejects_asymmetric_band():
    with pytest.raises(ValueError, match="symmetric"):
        make_strategy(warmup_bars=100, exit_threshold=0.0005,
                      sell_threshold=-0.0009)


def test_make_strategy_rejects_exit_threshold_above_the_band():
    with pytest.raises(ValueError, match="exit_threshold"):
        make_strategy(warmup_bars=100, exit_threshold=0.002)


def test_make_strategy_rejects_negative_exit_threshold():
    with pytest.raises(ValueError, match="exit_threshold"):
        make_strategy(warmup_bars=100, exit_threshold=-0.0001)


# -------- through the trader: a weak opposite signal closes ---------------------

def test_weak_opposite_signal_closes_the_long():
    strat = make_strategy(warmup_bars=100, exit_threshold=0.0005)
    em, tr = strat["emitter"], strat["make_trader"](Execution(fee_rate=0.0))
    tr.position = 1
    tr.entry_price = 17480.0
    tr.entry_bar_idx = 0
    tr.entry_timestamp = pd.Timestamp("2024-01-02 09:00:00")
    sig = em.emit_signal(_fc(-0.0006, prob_up=0.30))
    assert sig.direction == "SELL"
    order = tr.on_signal(sig.direction, _market(), strength=sig.strength)
    assert order is not None
    assert order.side == "SELL" and order.intent == "CLOSE"


def test_weak_opposite_signal_closes_the_short():
    strat = make_strategy(warmup_bars=100, exit_threshold=0.0005)
    em, tr = strat["emitter"], strat["make_trader"](Execution(fee_rate=0.0))
    tr.position = -1
    tr.entry_price = 17520.0
    tr.entry_bar_idx = 0
    tr.entry_timestamp = pd.Timestamp("2024-01-02 09:00:00")
    sig = em.emit_signal(_fc(+0.0006, prob_up=0.70))
    assert sig.direction == "BUY"
    order = tr.on_signal(sig.direction, _market(), strength=sig.strength)
    assert order is not None
    assert order.side == "BUY" and order.intent == "CLOSE"


def test_weak_opposite_signal_from_flat_submits_nothing():
    strat = make_strategy(warmup_bars=100, exit_threshold=0.0005)
    em, tr = strat["emitter"], strat["make_trader"](Execution(fee_rate=0.0))
    sig = em.emit_signal(_fc(-0.0006, prob_up=0.30))
    assert sig.direction == "HOLD"
    assert tr.on_signal(sig.direction, _market(), strength=sig.strength) is None
