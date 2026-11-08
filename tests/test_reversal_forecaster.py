"""The model-free reversal forecaster (strategy_test_1108, plan step 2) in the
night and fullday packages.

``ReversalForecaster`` predicts ``-(last / price_N_bars_ago - 1)``. The wiring
builds it for ``signal_source='reversal'`` with ``reversal_bars=N``; the default
``'toto2'`` is the control and leaves the adopted forecaster in place.
"""
import pandas as pd
import pytest

from forecast_eval.run import precompute_forecasts
from forecast_eval.strategy.fullday import reversal as fullday_reversal
from forecast_eval.strategy.fullday import wiring as fullday_wiring
from forecast_eval.strategy.night import reversal as night_reversal
from forecast_eval.strategy.night import wiring as night_wiring

PACKAGES = [(night_reversal, night_wiring), (fullday_reversal, fullday_wiring)]
IDS = ["night", "fullday"]
PRICES = [100.0, 101.0, 102.0, 99.0, 98.0, 100.0, 103.0]


def _hist(prices, start="2024-01-02 15:00"):
    ts = pd.date_range(start, periods=len(prices), freq="min")
    return pd.DataFrame({"timestamp": ts, "price": prices, "volume": 1})


def _make(mod, n=2, warmup=0, stride=1):
    return mod.ReversalForecaster(warmup_bars=warmup, forecast_stride_bars=stride,
                                  forecast_horizon_bars=30, lookback_bars=n)


@pytest.mark.parametrize("mod,_", PACKAGES, ids=IDS)
def test_predicts_the_lookback_move_reversed(mod, _):
    f = _make(mod, n=2).forecast(_hist(PRICES[:5]))  # last 98, two bars back 102
    assert f.payload["predicted_return"] == pytest.approx(-(98 / 102 - 1))
    assert f.payload["last_price"] == 98.0
    assert f.payload["predicted_price"] == pytest.approx(98 * (1 + f.payload["predicted_return"]))
    assert "prob_up" not in f.payload and "median_path" not in f.payload
    assert f.timestamp == _hist(PRICES[:5]).timestamp.iloc[-1]


@pytest.mark.parametrize("mod,_", PACKAGES, ids=IDS)
def test_short_history_predicts_zero(mod, _):
    assert _make(mod, n=5).forecast(_hist(PRICES[:3])).payload["predicted_return"] == 0.0


@pytest.mark.parametrize("mod,_", PACKAGES, ids=IDS)
def test_batch_matches_one_at_a_time(mod, _):
    fc = _make(mod, n=2)
    h = _hist(PRICES)
    batch = fc.forecast_series_batch(list(h.timestamp), list(h.price), [0, 1, 2, 4, 6])
    single = [fc.forecast(h.iloc[: b + 1]) for b in [0, 1, 2, 4, 6]]
    assert [b.payload for b in batch] == [s.payload for s in single]
    assert [b.timestamp for b in batch] == [s.timestamp for s in single]


@pytest.mark.parametrize("mod,_", PACKAGES, ids=IDS)
def test_precompute_runs_on_the_batched_path(mod, _):
    out = precompute_forecasts(_make(mod, n=2, warmup=2, stride=2), _hist(PRICES))
    assert sorted(out) == [2, 4, 6]
    assert out[6].payload["predicted_return"] == pytest.approx(-(103 / 98 - 1))


@pytest.mark.parametrize("mod,_", PACKAGES, ids=IDS)
@pytest.mark.parametrize("n", [0, -1, 2.0, True])
def test_rejects_a_bad_lookback(mod, _, n):
    with pytest.raises(ValueError):
        _make(mod, n=n)


# -------- wiring ---------------------------------------------------------------------

@pytest.mark.parametrize("mod,wiring", PACKAGES, ids=IDS)
def test_toto2_is_the_control(mod, wiring):
    assert wiring.PARAMS["signal_source"] == "toto2" and wiring.PARAMS["reversal_bars"] is None
    strat = wiring.make_strategy(warmup_bars=100)
    assert type(strat["forecaster"]).__name__ == "Toto2Forecaster"


@pytest.mark.parametrize("mod,wiring", PACKAGES, ids=IDS)
def test_reversal_source_builds_the_reversal_forecaster(mod, wiring):
    strat = wiring.make_strategy(warmup_bars=100, signal_source="reversal", reversal_bars=30)
    fc = strat["forecaster"]
    assert isinstance(fc, mod.ReversalForecaster)
    assert (fc.lookback_bars, fc.warmup_bars, fc.forecast_stride_bars) == (30, 100, wiring.PARAMS["stride_bars"])


@pytest.mark.parametrize("mod,wiring", PACKAGES, ids=IDS)
@pytest.mark.parametrize("kw", [{"signal_source": "reversal"}, {"reversal_bars": 30},
                                {"signal_source": "naive"}])
def test_mismatched_source_knobs_raise(mod, wiring, kw):
    with pytest.raises(ValueError):
        wiring.make_strategy(warmup_bars=100, **kw)


@pytest.mark.parametrize("mod,wiring", PACKAGES, ids=IDS)
def test_emitter_trades_a_reversal_forecast_at_full_strength(mod, wiring):
    # no prob_up in the payload: the probability gate is skipped, strength is 1.0
    strat = wiring.make_strategy(warmup_bars=0, signal_source="reversal", reversal_bars=1)
    em = strat["emitter"]
    if hasattr(em, "inner"):
        em = em.inner
    em.trend_filter = False
    f = strat["forecaster"].forecast(_hist([100.0, 90.0]))  # fell 10% -> predicts +11%
    sig = em.emit_signal(f)
    assert (sig.direction, sig.strength) == ("BUY", 1.0)
