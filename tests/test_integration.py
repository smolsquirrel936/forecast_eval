"""End-to-end integration: known data + deterministic emitter → exact PnL.

Plus the SPEC §6 Phase 5 baselines: buy-and-hold floor and a fee
sensitivity sweep that bracket what realistic execution costs do to
the strategy.
"""
from typing import List, Literal, Optional

import pandas as pd
import pytest

from forecast_eval.events import Forecast
from forecast_eval.forecaster.base import Forecaster
from forecast_eval.metrics import buy_and_hold_pnl, compute_metrics
from forecast_eval.run import fee_sensitivity_sweep, run_backtest
from forecast_eval.strategy.base import SignalEmitter
from forecast_eval.strategy.threshold import ThresholdEmitter


class _ScriptedEmitter(SignalEmitter):
    """Plays a fixed signal sequence in order; HOLD after exhaustion."""

    def __init__(self, sequence: List[str]):
        self._iter = iter(sequence)

    def emit(self, forecast: Optional[Forecast] = None
             ) -> Literal["BUY", "SELL", "HOLD"]:
        return next(self._iter, "HOLD")  # type: ignore[return-value]


def _make_ticks(prices, start="2024-01-02 09:00:00"):
    ts = pd.date_range(start, periods=len(prices), freq="1s")
    return pd.DataFrame({"timestamp": ts, "price": [float(p) for p in prices],
                         "volume": [1] * len(prices)})


# -------- Trivial-model integration: exact PnL on a known stream -------------

def test_known_data_exact_pnl_one_round_trip():
    """Construct a tick stream where the fill outcome is fully predictable.

    Sequence:
      tick 0  price 100  signal=BUY        → submit BUY limit 103 (marketable)
      tick 1  price 100  fill at 100, long; signal=HOLD
      tick 2  price 100  HOLD
      tick 3  price 102  HOLD
      tick 4  price 102  HOLD
      tick 5  price 102  signal=SELL       → submit SELL CLOSE limit 99 (marketable)
      tick 6  price 105  fill at 105, flat; signal=HOLD

    Expected:
      entry=100, exit=105, gross=+5 pts
      fees = (100 + 105) * 0.00015 = 0.030750
      net = 5 - 0.030750 = 4.969250
    """
    ticks = _make_ticks([100, 100, 100, 102, 102, 102, 105])
    emitter = _ScriptedEmitter(["BUY", "HOLD", "HOLD", "HOLD", "HOLD",
                                "SELL", "HOLD"])

    res = run_backtest(
        ticks,
        tick_size=1.0,
        aggression_ticks=3,
        fee_rate=0.00015,
        contract_multiplier=200.0,
        emitter=emitter,
        forecast_stride_bars=1,
        warmup_bars=0,
    )
    s = res.summary()
    assert s["n_fills"] == 2
    assert s["position"] == 0
    assert s["realized_pnl_points"] == pytest.approx(5.0)
    expected_fees = (100 + 105) * 0.00015
    assert s["total_fees_points"] == pytest.approx(expected_fees)
    assert s["net_pnl_points"] == pytest.approx(5.0 - expected_fees)


def test_known_data_signal_pnl_matches_realized_when_no_slippage():
    """With aggression=0 (at-the-touch) and zero fees, frictionless signal
    PnL should equal realized PnL — the trader plumbing eats nothing.
    """
    ticks = _make_ticks([100, 100, 100, 100, 102, 102, 102])
    emitter = _ScriptedEmitter(["BUY", "HOLD", "HOLD", "HOLD",
                                "SELL", "HOLD", "HOLD"])
    res = run_backtest(
        ticks,
        tick_size=1.0,
        aggression_ticks=0,           # at-the-touch
        fee_rate=0.0,                  # zero fees
        contract_multiplier=1.0,
        emitter=emitter,
        forecast_stride_bars=1,
        warmup_bars=0,
    )
    metrics = compute_metrics(res, ticks, forced_close=False)
    drag = metrics["attribution"]["execution_and_cost_drag_points"]
    assert drag == pytest.approx(0.0, abs=1e-9), \
        "with no aggression and no fees, realized should equal signal PnL"


# -------- Buy-and-hold baseline (SPEC §6 Phase 5: "as a floor") --------------

def test_buy_and_hold_pnl_basic_math():
    ticks = _make_ticks([100, 101, 102, 103, 105])
    bh = buy_and_hold_pnl(ticks, fee_rate=0.00015)
    assert bh["gross_pnl_points"] == pytest.approx(5.0)
    expected_fees = (100 + 105) * 0.00015
    assert bh["total_fees_points"] == pytest.approx(expected_fees)
    assert bh["net_pnl_points"] == pytest.approx(5.0 - expected_fees)


# -------- Fee sensitivity sweep ----------------------------------------------

class _MomentumForecaster(Forecaster):
    """Deterministic, data-dependent forecaster: predicted return = last bar's
    return. Data-dependence makes the precompute<->sequential parity test
    sensitive to a mis-keyed cache (wrong forecast at the wrong bar)."""

    def __init__(self, *, warmup_bars=2, forecast_stride_bars=1,
                 forecast_horizon_bars=1):
        self.warmup_bars = warmup_bars
        self.forecast_stride_bars = forecast_stride_bars
        self.forecast_horizon_bars = forecast_horizon_bars

    def forecast(self, history_df):
        p = history_df["price"].to_numpy(dtype=float)
        last = float(p[-1])
        prev = float(p[-2]) if len(p) >= 2 else last
        pr = (last - prev) / prev if prev else 0.0
        return Forecast(
            timestamp=history_df["timestamp"].iloc[-1],
            horizon_bars=self.forecast_horizon_bars,
            payload={"predicted_price": last * (1 + pr),
                     "predicted_return": pr, "last_price": last},
        )


def test_precompute_matches_sequential():
    """The batched-precompute path must produce the exact same backtest as the
    per-tick sequential path — same forecasts, fills, and PnL. Uses the generic
    forecast_series_batch fallback (Forecaster base), so this validates the
    run.py cache keying/replay plumbing independent of any GPU model."""
    ticks = _make_ticks(
        [100, 101, 100, 102, 103, 101, 104, 103, 105, 102, 106, 104, 107, 105]
    )

    def go(precompute: bool):
        return run_backtest(
            ticks,
            forecaster=_MomentumForecaster(warmup_bars=2,
                                           forecast_stride_bars=1,
                                           forecast_horizon_bars=1),
            emitter=ThresholdEmitter(buy_threshold=0.005, sell_threshold=-0.005),
            tick_size=1.0,
            aggression_ticks=0,
            fee_rate=0.00015,
            contract_multiplier=1.0,
            precompute=precompute,
        )

    seq, pre = go(False), go(True)

    # Same number of forecasts, and each one identical (timestamp + payload).
    assert seq.n_forecasts == pre.n_forecasts > 0
    for a, b in zip(seq.forecasts, pre.forecasts):
        assert a.timestamp == b.timestamp
        assert a.payload == b.payload
    # Same trading outcome.
    assert len(seq.fills) == len(pre.fills)
    assert seq.summary()["net_pnl_points"] == pytest.approx(
        pre.summary()["net_pnl_points"]
    )


def test_fee_sweep_monotonic_in_fee_rate():
    """Higher fees → lower net PnL (or equal when no fills happen)."""
    ticks = _make_ticks(
        [100, 100, 100, 102, 102, 102, 105,
         105, 105, 103, 103, 103, 101, 101]
    )
    df = fee_sensitivity_sweep(
        ticks,
        fee_rates=(0.0, 0.00015, 0.0003),
        tick_size=1.0,
        aggression_ticks=3,
        contract_multiplier=1.0,
        forecast_stride_bars=3,
        warmup_bars=0,
        make_emitter=lambda: _ScriptedEmitter(
            ["BUY", "HOLD", "HOLD", "HOLD", "SELL", "HOLD", "HOLD"] * 3
        ),
    )
    assert list(df["fee_rate"]) == [0.0, 0.00015, 0.0003]
    # Same fills across runs → fees scale linearly, net PnL strictly decreases.
    net = df["net_pnl_points"].to_numpy()
    fees = df["total_fees_points"].to_numpy()
    assert (fees[1] > fees[0]) and (fees[2] > fees[1])
    assert net[0] >= net[1] >= net[2]
