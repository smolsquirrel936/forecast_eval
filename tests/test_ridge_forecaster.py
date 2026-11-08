"""The night package's price-ridge forecaster (adopted 2026-11-09).

``fit_ridge`` fits a linear model of the next ``target_bars`` log return on the
tape's own past returns and volatility, using only bars whose target ends by
``fit_end``; ``RidgeForecaster`` applies the frozen weights. The night wiring
builds it from ``ridge_h1140.json``.
"""
import json

import numpy as np
import pandas as pd
import pytest

from forecast_eval.environment import Environment
from forecast_eval.run import precompute_forecasts
from forecast_eval.strategy.night import ridge
from forecast_eval.strategy.night import wiring

N = 6000


def _tape(n=N, seed=0):
    rng = np.random.default_rng(seed)
    prices = 20000.0 * np.exp(np.cumsum(rng.normal(0, 0.001, n)))
    ts = pd.date_range("2020-01-02 08:45", periods=n, freq="min")
    return pd.DataFrame({"timestamp": ts, "price": prices, "volume": 1})


def _coef(target=30):
    h = _tape()
    return ridge.fit_ridge(h.timestamp, h.price, fit_end=h.timestamp.iloc[-1], target_bars=target)


def test_fit_reads_no_price_after_fit_end():
    h = _tape()
    c = ridge.fit_ridge(h.timestamp, h.price, fit_end=h.timestamp.iloc[4500], target_bars=30)
    moved = h.copy()
    moved.loc[4501:, "price"] *= 3.0  # anything after fit_end must not matter
    assert c == ridge.fit_ridge(moved.timestamp, moved.price, fit_end=h.timestamp.iloc[4500], target_bars=30)
    assert c["fit_rows"] == len(range(ridge.LOOKBACK_BARS, 4500 - 30 + 1, 10))


def test_fit_recovers_a_planted_reversal():
    # next-30-bar return leans against the last 60 bars: the ret60 weight must be the big negative one
    rng = np.random.default_rng(1)
    r = rng.normal(0, 0.001, 40000)
    for t in range(60, len(r)):
        r[t] += -0.5 / 30 * r[t - 60:t].sum() / 2
    p = 20000.0 * np.exp(np.cumsum(r))
    ts = pd.date_range("2020-01-02", periods=len(p), freq="min")
    w = dict(zip(*(lambda c: (c["features"], c["weights"]))(ridge.fit_ridge(ts, p, fit_end=ts[-1], target_bars=30))))
    assert w["ret60"] < 0 and abs(w["ret60"]) == max(abs(v) for v in w.values())


def test_forecast_is_the_frozen_linear_prediction():
    c = _coef()
    f = ridge.RidgeForecaster(warmup_bars=0, forecast_stride_bars=1, coefficients=c).forecast(_tape().iloc[:5000])
    logp = np.log(_tape().price.to_numpy())
    z = (ridge.features_at(logp, 4999) - np.array(c["mu"])) / np.array(c["sd"])
    assert f.payload["predicted_return"] == pytest.approx(np.expm1(z @ np.array(c["weights"]) + c["intercept"]))
    assert f.horizon_bars == 30 and "prob_up" not in f.payload and "median_path" not in f.payload


def test_short_history_predicts_zero():
    fc = ridge.RidgeForecaster(warmup_bars=0, forecast_stride_bars=1, coefficients=_coef())
    assert fc.forecast(_tape().iloc[:ridge.LOOKBACK_BARS]).payload["predicted_return"] == 0.0


def test_batch_matches_one_at_a_time():
    fc = ridge.RidgeForecaster(warmup_bars=0, forecast_stride_bars=1, coefficients=_coef())
    h = _tape()
    bs = [0, ridge.LOOKBACK_BARS - 1, ridge.LOOKBACK_BARS, 4000, N - 1]
    batch = fc.forecast_series_batch(list(h.timestamp), list(h.price), bs)
    single = [fc.forecast(h.iloc[: b + 1]) for b in bs]
    assert [b.payload for b in batch] == [s.payload for s in single]
    assert [b.timestamp for b in batch] == [s.timestamp for s in single]


def test_precompute_runs_on_the_batched_path():
    # the run loop's stream drops off-hours prints; boundaries index that stream
    fc = ridge.RidgeForecaster(warmup_bars=5000, forecast_stride_bars=500, coefficients=_coef())
    ticks = _tape(n=20000)
    n = len(list(Environment(ticks).stream()))
    out = precompute_forecasts(fc, ticks)
    assert n > 6000 and sorted(out) == list(range(5000, n, 500))


def test_rejects_coefficients_for_other_features():
    with pytest.raises(ValueError):
        ridge.RidgeForecaster(warmup_bars=0, forecast_stride_bars=1, coefficients=dict(_coef(), features=["ret60"]))


# -------- the adopted configuration ------------------------------------------------

def test_wiring_builds_the_ridge_from_the_shipped_weights():
    strat = wiring.make_strategy(warmup_bars=100)
    fc = strat["forecaster"]
    assert isinstance(fc, ridge.RidgeForecaster)
    assert fc.coefficients == json.loads(wiring.RIDGE_COEF.read_text(encoding="utf-8"))
    assert (fc.warmup_bars, fc.forecast_stride_bars, fc.forecast_horizon_bars) == (100, 15, 1140)


def test_shipped_weights_are_the_backtested_fit():
    # strategy_test_1108_ridge f0c74e9, outputs/strategy_test_1108_ridge/coef/night_h1140.json
    c = json.loads(wiring.RIDGE_COEF.read_text(encoding="utf-8"))
    assert c["features"] == ridge.FEATURES and c["target_bars"] == 1140
    assert c["fit_end"] == "2024-01-18 03:35:00" and c["fit_rows"] == 132372
    assert [round(w * 1e4, 2) for w in c["weights"]] == [-0.70, -5.40, -1.33, 6.19, -1.12]


def test_adopted_knobs():
    p = wiring.PARAMS
    assert (p["buy_threshold"], p["sell_threshold"], p["horizon_bars"]) == (0.00044, -0.00044, 1140)
    assert p["checkpoint"] is None and p["min_prob"] == 0.6 and p["stop_pct"] == 0.05


def test_live_history_window_holds_the_lookback():
    # live keeps exactly context_length bars (live/runtime.py); the ridge needs LOOKBACK_BARS + 1
    assert wiring.PARAMS["context_length"] == ridge.LOOKBACK_BARS + 1
    with pytest.raises(ValueError):
        wiring.make_strategy(warmup_bars=0, context_length=3008)


def test_horizon_must_match_the_ridge_target():
    with pytest.raises(ValueError):
        wiring.make_strategy(warmup_bars=0, horizon_bars=30)
