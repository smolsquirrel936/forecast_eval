"""ConsensusThresholdEmitter (fullday combine.py) tests.

The emitter averages predicted_return and prob_up across the last two stride
boundaries and feeds the combined figures to the inherited banded gates. Each
test drives a short boundary sequence and asserts the decision on the last one.
"""
from datetime import datetime, timedelta

from forecast_eval.events import Forecast
from forecast_eval.strategy.fullday.combine import ConsensusThresholdEmitter
from forecast_eval.strategy.fullday.emitter import ThresholdEmitter

T0 = datetime(2026, 1, 1, 9, 0, 0)


def _em(**kw):
    defaults: dict = dict(buy_threshold=0.00075, sell_threshold=-0.00075,
                          min_prob=0.5, full_confidence_prob=0.9)
    defaults.update(kw)
    return ConsensusThresholdEmitter(**defaults)


def _fc(i, *, r, p=None, last=20000.0):
    payload = {"predicted_return": r, "last_price": last}
    if p is not None:
        payload["prob_up"] = p
    return Forecast(timestamp=T0 + timedelta(minutes=30 * i),
                    horizon_bars=30, payload=payload)


def test_none_and_missing_return_hold_without_entering_history():
    em = _em()
    assert em.emit(None) == "HOLD"
    assert em.emit(Forecast(timestamp=T0, horizon_bars=30, payload={})) == "HOLD"
    # A boundary with no predicted_return must not enter the history: the next
    # decision combines only the informative boundaries (here just one -> the
    # single strong figure acts alone).
    em2 = _em()
    em2.emit(_fc(0, r=0.004, p=0.95))
    em2.emit(Forecast(timestamp=T0 + timedelta(minutes=30),
                      horizon_bars=30, payload={"last_price": 20000.0}))
    assert em2.emit(_fc(2, r=0.004, p=0.95)) == "BUY"


def test_two_agreeing_boundaries_emit_buy():
    em = _em()
    em.emit(_fc(0, r=0.003, p=0.95))
    assert em.emit(_fc(1, r=0.003, p=0.95)) == "BUY"


def test_opposing_previous_figure_vetoes_a_strong_current():
    # Current return would BUY alone (0.004 >= band, p 0.95), but the previous
    # boundary's -0.004 drags the combined return to 0.0 -> HOLD.
    em = _em()
    em.emit(_fc(0, r=-0.004, p=0.95))
    assert em.emit(_fc(1, r=0.004, p=0.95)) == "HOLD"


def test_combined_probability_vetoes_a_strong_current():
    # min_prob 0.65: current p 0.95 alone would pass, but combined with the
    # previous 0.30 the mean 0.625 fails the gate -> HOLD.
    em = _em(min_prob=0.65)
    em.emit(_fc(0, r=0.004, p=0.30))
    assert em.emit(_fc(1, r=0.004, p=0.95)) == "HOLD"


def test_combined_probability_can_rescue_a_weak_current():
    # min_prob 0.65: current p 0.50 alone would fail, combined with the previous
    # 0.90 the mean 0.70 passes -> BUY (consensus smoothes both ways).
    em = _em(min_prob=0.65)
    em.emit(_fc(0, r=0.004, p=0.90))
    assert em.emit(_fc(1, r=0.004, p=0.50)) == "BUY"


def test_strength_uses_the_combined_probability():
    em = _em()
    em.emit(_fc(0, r=0.003, p=0.60))
    sig = em.emit_signal(_fc(1, r=0.003, p=0.90))
    # Combined prob 0.75 -> strength = (0.75 - 0.5) / (0.9 - 0.5) = 0.625.
    assert sig.direction == "BUY"
    assert abs(sig.strength - 0.625) < 1e-9


def test_k1_matches_the_uncombined_emitter():
    feeds = [
        [(0.003, 0.95), (0.003, 0.95), (0.003, 0.95)],
        [(-0.004, 0.30), (0.004, 0.95), (-0.004, 0.95)],
        [(0.004, 0.30), (0.004, 0.95), (0.004, 0.30)],
    ]
    for feed in feeds:
        em = _em(ret_k=1, prob_k=1)
        base = ThresholdEmitter(buy_threshold=0.00075, sell_threshold=-0.00075,
                                min_prob=0.5, full_confidence_prob=0.9)
        for i, (r, p) in enumerate(feed):
            assert em.emit(_fc(i, r=r, p=p)) == base.emit(_fc(i, r=r, p=p))


def test_windows_are_independent_lengths():
    # ret_k=2, prob_k=1: the return combines across two boundaries but the
    # probability gate stays on the current one, so a weak *current* prob fails
    # even though the previous was strong.
    em = _em(ret_k=2, prob_k=1, min_prob=0.65)
    em.emit(_fc(0, r=0.003, p=0.90))
    assert em.emit(_fc(1, r=0.003, p=0.50)) == "HOLD"


def test_fresh_instances_do_not_share_state():
    em_a = _em()
    em_b = _em()
    em_a.emit(_fc(0, r=-0.004, p=0.95))
    # em_b's first boundary must act on its own history (nothing seen), so the
    # strong figure fires where em_a's second boundary would be vetoed.
    assert em_b.emit(_fc(0, r=0.004, p=0.95)) == "BUY"


def test_missing_prob_up_keeps_the_legacy_full_strength_path():
    # No prob_up anywhere: the parent's legacy branch trades at full strength
    # with no probability gate, and the consensus does not invent one.
    em = _em()
    em.emit(_fc(0, r=0.003))
    sig = em.emit_signal(_fc(1, r=0.003))
    assert sig.direction == "BUY"
    assert sig.strength == 1.0


def test_invalid_windows_raise():
    import pytest
    with pytest.raises(ValueError, match="ret_k/prob_k"):
        _em(ret_k=0)
    with pytest.raises(ValueError, match="ret_k/prob_k"):
        _em(prob_k=0)