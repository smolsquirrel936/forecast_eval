"""DAY's regime gate (iter05): BUY entries require price >= a 6 000-bar SMA.

Grounds: iter02's drawdown concentrated in longs entered while price sat below
its one-month average (the 2020/2022/2025 long buckets carried the damage); a
full-tape replay of iter02's boundary stream put the gated config at dd -1 662
vs -4 226 ungated. These tests pin the gate's semantics and that the batch and
per-call paths still agree on the new payload field.
"""
import numpy as np
import pandas as pd
import pytest

from forecast_eval.events import Forecast
from forecast_eval.strategy.day import rsi as day_rsi

# a small window so the fixtures stay short
W = 10


def _tape(prices, start="2026-09-08 08:45", freq="1min"):
    ts = pd.date_range(start, periods=len(prices), freq=freq)
    return pd.DataFrame({"timestamp": ts, "price": np.asarray(prices, dtype=float)})


def _fc(value, last_price, sma):
    payload = {"rsi": value, "last_price": last_price}
    if sma is not ...:
        payload["regime_sma"] = sma
    return Forecast(timestamp=None, horizon_bars=30, payload=payload)


# ---- the gate's decision table ----------------------------------------------

@pytest.mark.parametrize("sma,last,expected", [
    (100.0, 100.0, "BUY"),     # at the SMA is allowed (inclusive)
    (100.0, 100.5, "BUY"),     # above the SMA
    (100.0, 99.5, "HOLD"),     # below the SMA -- the bear-phase veto
    (None, 99.5, "HOLD"),      # unreadable regime is not regime fine
    (..., 100.5, "HOLD"),      # payload never carried the field
])
def test_the_gate_on_buy_candidates(sma, last, expected):
    assert day_rsi.RsiEmitter().emit(_fc(15.0, last, sma)) == expected


def test_nan_sma_holds_too():
    assert day_rsi.RsiEmitter().emit(_fc(15.0, 100.5, float("nan"))) == "HOLD"


@pytest.mark.parametrize("sma", [100.0, None, ...])
def test_the_gate_never_blocks_the_sell_exit(sma):
    """SELL is the long's exit path; gating it would strand the position."""
    assert day_rsi.RsiEmitter().emit(_fc(85.0, 90.0, sma)) == "SELL"


@pytest.mark.parametrize("value,sma,expected", [
    (50.0, 100.0, "HOLD"),     # neutral RSI: gate is irrelevant
    (15.0, 100.0, "BUY"),      # gated candidate passing
])
def test_hold_zone_is_untouched(value, sma, expected):
    assert day_rsi.RsiEmitter().emit(_fc(value, 100.5, sma)) == expected


# ---- the forecaster's SMA ----------------------------------------------------

def test_regime_sma_of_a_flat_tape_is_the_price():
    px = [100.0] * W
    assert day_rsi.regime_sma(px, W) == 100.0


def test_regime_sma_is_the_trailing_window_mean_inclusive_of_the_last():
    px = list(range(1, W + 2))          # 1..11, window 10 -> mean(2..11)
    assert day_rsi.regime_sma(px, W) == pytest.approx(sum(range(2, W + 2)) / W)


def test_too_few_prices_is_None():
    assert day_rsi.regime_sma([100.0] * (W - 1), W) is None


def test_the_forecaster_payload_carries_the_sma():
    f = day_rsi.RsiForecaster(warmup_bars=0, forecast_stride_bars=30,
                              forecast_horizon_bars=30, regime_sma_bars=W)
    fc = f.forecast(_tape([100.0 + i * 0.1 for i in range(200)]))
    assert fc.payload["regime_sma"] == pytest.approx(
        sum(100.0 + i * 0.1 for i in range(200 - W, 200)) / W)


def test_batch_and_per_call_agree_on_the_sma():
    """The property that makes --precompute the same strategy as one
    forecast(): the SMA read at a boundary must be the trailing mean of that
    boundary's own row, in both paths."""
    rng = np.random.default_rng(7)
    n = 400
    ts = pd.date_range("2026-09-08 08:45", periods=n, freq="1min")
    px = rng.standard_normal(n).cumsum() + 20_000.0
    f = day_rsi.RsiForecaster(warmup_bars=0, forecast_stride_bars=30,
                              forecast_horizon_bars=30, regime_sma_bars=W)
    boundaries = list(range(W + 5, n, 37))
    batch = f.forecast_series_batch(ts, px, boundaries)
    for b, got in zip(boundaries, batch):
        one = f.forecast(pd.DataFrame({"timestamp": ts[: b + 1],
                                       "price": px[: b + 1]}))
        assert got.payload["regime_sma"] == pytest.approx(
            one.payload["regime_sma"], abs=1e-9)


def test_the_sma_never_reads_past_its_boundary():
    rng = np.random.default_rng(5)
    n = 300
    ts = pd.date_range("2026-09-08 08:45", periods=n, freq="1min")
    px = rng.standard_normal(n).cumsum() + 20_000.0
    f = day_rsi.RsiForecaster(warmup_bars=0, forecast_stride_bars=30,
                              forecast_horizon_bars=30, regime_sma_bars=W)
    b = 200
    before = f.forecast_series_batch(ts, px, [b])[0].payload["regime_sma"]
    tampered = px.copy()
    tampered[b + 1:] += 5_000.0
    after = f.forecast_series_batch(ts, tampered, [b])[0].payload["regime_sma"]
    assert before == pytest.approx(after, abs=1e-12)