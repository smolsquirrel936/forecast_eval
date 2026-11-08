"""The price-ridge forecaster (strategy_test_1108_ridge) in the night and fullday
packages.

``fit_ridge`` fits a linear model of the next ``target_bars`` log return on the
tape's own past returns and volatility, using only bars whose target ends by
``fit_end``; ``RidgeForecaster`` applies the frozen weights. The wiring builds it
for ``signal_source='ridge'`` with ``ridge_coef=<json>``.
"""
import json

import numpy as np
import pandas as pd
import pytest

from forecast_eval.environment import Environment
from forecast_eval.run import precompute_forecasts
from forecast_eval.strategy.fullday import ridge as fullday_ridge
from forecast_eval.strategy.fullday import wiring as fullday_wiring
from forecast_eval.strategy.night import ridge as night_ridge
from forecast_eval.strategy.night import wiring as night_wiring

PACKAGES = [(night_ridge, night_wiring), (fullday_ridge, fullday_wiring)]
IDS = ["night", "fullday"]
N = 6000


def _tape(n=N, seed=0):
    rng = np.random.default_rng(seed)
    prices = 20000.0 * np.exp(np.cumsum(rng.normal(0, 0.001, n)))
    ts = pd.date_range("2020-01-02 08:45", periods=n, freq="min")
    return pd.DataFrame({"timestamp": ts, "price": prices, "volume": 1})


def _coef(mod, target=30):
    h = _tape()
    return mod.fit_ridge(h.timestamp, h.price, fit_end=h.timestamp.iloc[-1], target_bars=target)


@pytest.mark.parametrize("mod,_", PACKAGES, ids=IDS)
def test_fit_reads_no_price_after_fit_end(mod, _):
    h = _tape()
    c = mod.fit_ridge(h.timestamp, h.price, fit_end=h.timestamp.iloc[4500], target_bars=30)
    moved = h.copy()
    moved.loc[4501:, "price"] *= 3.0  # anything after fit_end must not matter
    c2 = mod.fit_ridge(moved.timestamp, moved.price, fit_end=h.timestamp.iloc[4500], target_bars=30)
    assert c == c2
    assert c["fit_rows"] == len(range(mod.LOOKBACK_BARS, 4500 - 30 + 1, 10))


@pytest.mark.parametrize("mod,_", PACKAGES, ids=IDS)
def test_fit_recovers_a_planted_reversal(mod, _):
    # next-30-bar return = -0.5 x last-60-bar return + noise: the ret60 weight must be the big negative one
    rng = np.random.default_rng(1)
    r = rng.normal(0, 0.001, 40000)
    for t in range(60, len(r)):
        r[t] += -0.5 / 30 * r[t - 60:t].sum() / 2
    p = 20000.0 * np.exp(np.cumsum(r))
    ts = pd.date_range("2020-01-02", periods=len(p), freq="min")
    c = mod.fit_ridge(ts, p, fit_end=ts[-1], target_bars=30)
    w = dict(zip(c["features"], c["weights"]))
    assert w["ret60"] < 0 and abs(w["ret60"]) == max(abs(v) for v in w.values())


@pytest.mark.parametrize("mod,_", PACKAGES, ids=IDS)
def test_forecast_is_the_frozen_linear_prediction(mod, _):
    c = _coef(mod)
    fc = mod.RidgeForecaster(warmup_bars=0, forecast_stride_bars=1, coefficients=c)
    h = _tape()
    f = fc.forecast(h.iloc[:5000])
    logp = np.log(h.price.to_numpy())
    z = (mod.features_at(logp, 4999) - np.array(c["mu"])) / np.array(c["sd"])
    assert f.payload["predicted_return"] == pytest.approx(np.expm1(z @ np.array(c["weights"]) + c["intercept"]))
    assert f.payload["last_price"] == pytest.approx(h.price.iloc[4999])
    assert f.horizon_bars == 30 and f.timestamp == h.timestamp.iloc[4999]
    assert "prob_up" not in f.payload and "median_path" not in f.payload


@pytest.mark.parametrize("mod,_", PACKAGES, ids=IDS)
def test_short_history_predicts_zero(mod, _):
    fc = mod.RidgeForecaster(warmup_bars=0, forecast_stride_bars=1, coefficients=_coef(mod))
    assert fc.forecast(_tape().iloc[:mod.LOOKBACK_BARS]).payload["predicted_return"] == 0.0


@pytest.mark.parametrize("mod,_", PACKAGES, ids=IDS)
def test_batch_matches_one_at_a_time(mod, _):
    fc = mod.RidgeForecaster(warmup_bars=0, forecast_stride_bars=1, coefficients=_coef(mod))
    h = _tape()
    bs = [0, mod.LOOKBACK_BARS - 1, mod.LOOKBACK_BARS, 4000, N - 1]
    batch = fc.forecast_series_batch(list(h.timestamp), list(h.price), bs)
    single = [fc.forecast(h.iloc[: b + 1]) for b in bs]
    assert [b.payload for b in batch] == [s.payload for s in single]
    assert [b.timestamp for b in batch] == [s.timestamp for s in single]


@pytest.mark.parametrize("mod,_", PACKAGES, ids=IDS)
def test_precompute_runs_on_the_batched_path(mod, _):
    # the run loop's stream drops off-hours prints; boundaries index that stream
    fc = mod.RidgeForecaster(warmup_bars=5000, forecast_stride_bars=500, coefficients=_coef(mod))
    ticks = _tape(n=20000)
    n = len(list(Environment(ticks).stream()))
    out = precompute_forecasts(fc, ticks)
    assert n > 6000 and sorted(out) == list(range(5000, n, 500))
    assert all(f.payload["predicted_return"] != 0.0 for f in out.values())


@pytest.mark.parametrize("mod,_", PACKAGES, ids=IDS)
def test_rejects_coefficients_for_other_features(mod, _):
    c = dict(_coef(mod), features=["ret60"])
    with pytest.raises(ValueError):
        mod.RidgeForecaster(warmup_bars=0, forecast_stride_bars=1, coefficients=c)


# -------- wiring ---------------------------------------------------------------------

def _write(tmp_path, mod, target=30):
    path = tmp_path / "coef.json"
    path.write_text(json.dumps(_coef(mod, target)), encoding="utf-8")
    return str(path)


@pytest.mark.parametrize("mod,wiring", PACKAGES, ids=IDS)
def test_ridge_is_off_by_default(mod, wiring):
    assert wiring.PARAMS["ridge_coef"] == ""
    assert type(wiring.make_strategy(warmup_bars=100)["forecaster"]).__name__ == "Toto2Forecaster"


@pytest.mark.parametrize("mod,wiring", PACKAGES, ids=IDS)
def test_ridge_source_builds_the_ridge_forecaster(mod, wiring, tmp_path):
    strat = wiring.make_strategy(warmup_bars=100, signal_source="ridge", ridge_coef=_write(tmp_path, mod))
    fc = strat["forecaster"]
    assert isinstance(fc, mod.RidgeForecaster)
    assert (fc.warmup_bars, fc.forecast_stride_bars, fc.forecast_horizon_bars) == (100, wiring.PARAMS["stride_bars"], 30)


@pytest.mark.parametrize("mod,wiring", PACKAGES, ids=IDS)
def test_horizon_must_match_the_ridge_target(mod, wiring, tmp_path):
    path = _write(tmp_path, mod, target=300)
    with pytest.raises(ValueError):
        wiring.make_strategy(warmup_bars=100, signal_source="ridge", ridge_coef=path)
    strat = wiring.make_strategy(warmup_bars=100, signal_source="ridge", ridge_coef=path, horizon_bars=300)
    assert strat["forecaster"].forecast_horizon_bars == 300


@pytest.mark.parametrize("mod,wiring", PACKAGES, ids=IDS)
def test_mismatched_ridge_knobs_raise(mod, wiring, tmp_path):
    with pytest.raises(ValueError):
        wiring.make_strategy(warmup_bars=100, signal_source="ridge")
    with pytest.raises(ValueError):
        wiring.make_strategy(warmup_bars=100, ridge_coef=_write(tmp_path, mod))
