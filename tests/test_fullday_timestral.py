"""GPU-free tests for the fullday Timestral forecaster wiring.

The model itself needs the GPU; these pin the parts that must not regress:
the knob plumbing (wiring resolves and forwards `forecaster`, `trend_filter`
and `path_momentum`), the payload contract (identical keys to Toto2's), and
the decile-ladder reduction — all torch-free.
"""
import numpy as np
import pytest

from forecast_eval.strategy.fullday.timestral import (
    DECILE_IDX_21,
    DECILE_LEVELS,
    MEDIAN_IDX,
    TimestralForecaster,
)
from forecast_eval.strategy.fullday.wiring import PARAMS, make_strategy


def _mkt():
    import pandas as pd

    from forecast_eval.events import MarketEvent
    return MarketEvent(timestamp=pd.Timestamp("2024-01-02 21:05:00"),
                       price=17500.0, volume=1, session="NIGHT")


class TestTimeExit:
    def test_fires_after_bars(self):
        from datetime import datetime

        from forecast_eval.events import MarketEvent
        from forecast_eval.exits.base import PositionState
        from forecast_eval.strategy.fullday.time_exit import BarsInTradeExit

        rule = BarsInTradeExit(bars=5)
        pos = PositionState(side="SHORT", size=1, entry_price=100.0,
                            entry_bar_idx=100,
                            entry_timestamp=datetime(2026, 1, 5))
        mkt = MarketEvent(timestamp=datetime(2026, 1, 5), price=100.0,
                          volume=1, session="NIGHT")
        assert rule.should_exit(pos, mkt, bar_idx=104) is False
        assert rule.should_exit(pos, mkt, bar_idx=105) is True

    def test_rejects_nonpositive(self):
        from forecast_eval.strategy.fullday.time_exit import BarsInTradeExit

        with pytest.raises(ValueError, match="bars must be"):
            BarsInTradeExit(bars=0)

    def test_adoptable_as_expired_and_wired(self):
        from forecast_eval.strategy.fullday.time_exit import BarsInTradeExit

        # simtrade port (2026-11-19): adoptable, an adopted lot is expired
        assert BarsInTradeExit(bars=5).adoptable is True
        # iter14 default on — a bare make_strategy builds the 5-bar rule alone
        base = make_strategy(warmup_bars=3008)
        assert isinstance(base["exit_rule"], BarsInTradeExit)
        assert base["exit_rule"].bars == 5
        off = make_strategy(warmup_bars=3008, time_exit_bars=None)
        assert off["exit_rule"] is None

    def test_adopted_sentinel_fires_on_the_first_bar(self):
        from forecast_eval.exits.base import PositionState
        from forecast_eval.strategy.fullday.time_exit import BarsInTradeExit

        rule = BarsInTradeExit(bars=5)
        adopted = PositionState(
            side="LONG", size=1, entry_price=17500.0, entry_bar_idx=-1,
            entry_timestamp=_mkt().timestamp,
        )
        assert rule.should_exit(adopted, _mkt(), bar_idx=0) is True

    @pytest.mark.usefixtures("cheap_strategy")
    def test_a_live_resume_adopts_and_closes_the_held_lot(self):
        from forecast_eval.live.broker.base import HeldPosition

        from .test_live_resume import NOW, _runtime
        rt = _runtime(HeldPosition(1, 46616.0))
        rule = rt.loop.exit_rule = make_strategy(warmup_bars=100)["exit_rule"]
        adopted = rt.adopt_position(timestamp=NOW)
        assert adopted["position"] == 1
        assert rule.should_exit(rt.trader.position_state(), _mkt(), bar_idx=0) is True


class TestWiring:

    def test_defaults_are_iter14(self):
        # Tech: PARAMS carry fullday_split_iter14's forecaster and filters.
        # Why:  the simtrade port (2026-11-19) bakes the loop's command-line
        #       config into PARAMS; a bare `--strategy fullday` replays iter14
        #       byte for byte (verified against its run folder).
        assert PARAMS["forecaster"] == "timestral"
        assert PARAMS["trend_filter"] is False
        assert PARAMS["path_momentum"] is True
        assert PARAMS["checkpoint"] == "mistralai/Timestral-209M"
        assert (PARAMS["context_length"], PARAMS["horizon_bars"],
                PARAMS["stride_bars"]) == (2048, 5, 5)

    def test_make_strategy_builds_timestral(self):
        s = make_strategy(
            warmup_bars=3008,
            forecaster="timestral",
            checkpoint="mistralai/Timestral-209M",
            context_length=2048,
            horizon_bars=5,
            stride_bars=5,
        )
        fc = s["forecaster"]
        assert isinstance(fc, TimestralForecaster)
        assert fc.context_length == 2048
        assert fc.forecast_horizon_bars == 5
        assert fc.forecast_stride_bars == 5
        assert fc.warmup_bars == 3008
        assert fc.checkpoint == "mistralai/Timestral-209M"
        # model stays unloaded (lazy) — no torch needed to build
        assert fc._model is None

    def test_make_strategy_forwards_filter_knobs(self):
        base = make_strategy(warmup_bars=3008)
        on = make_strategy(warmup_bars=3008, trend_filter=True,
                           path_momentum=False)
        assert base["emitter"].trend_filter is False
        assert on["emitter"].trend_filter is True
        assert on["emitter"].path_momentum is False

    def test_unknown_forecaster_rejected(self):
        from forecast_eval.strategy.fullday.forecaster import make_forecaster

        with pytest.raises(ValueError, match="unknown forecaster"):
            make_forecaster(
                warmup_bars=3008, stride_bars=5, horizon_bars=5,
                context_length=2048, checkpoint="x",
                forecaster="chronos2",
            )

    def test_unknown_override_still_rejected(self):
        with pytest.raises(ValueError, match="unknown strategy override"):
            make_strategy(warmup_bars=3008, nonsense=1)


class TestDecileLadder:
    def test_decile_indices_match_levels(self):
        # the 21-level ladder: 0.01, 0.05, then 0.05 steps to 0.95, then 0.99
        full = [0.01, 0.05] + [round(0.05 * i, 2) for i in range(2, 20)] \
            + [0.99]
        assert len(full) == 21
        picked = [full[i] for i in DECILE_IDX_21]
        assert picked == DECILE_LEVELS

    def test_fans_from_quantiles_layout(self):
        fc = TimestralForecaster(
            warmup_bars=10, forecast_stride_bars=5, forecast_horizon_bars=3,
        )
        # (n=2, variate=1, H=3, 21 levels); decile index 4 (=0.2) carries a
        # distinctive marker value
        q = np.zeros((2, 1, 3, 21))
        q[:, :, :, 4] = 7.0
        fans = fc._fans_from_quantiles(q)
        assert fans.shape == (2, 9, 3)
        # DECILE_LEVELS[1] == 0.2 -> row 1 of the fan
        assert (fans[:, 1, :] == 7.0).all()


class TestPayload:
    def test_payload_contract_matches_toto2(self):
        # Tech: build a fan with known geometry and check the payload keys
        #       and values the emitter consumes.
        # Why:  the copied ThresholdEmitter reads predicted_return, prob_up,
        #       last_price and median_path — any key drift silently turns
        #       the strategy into HOLD (or full-strength legacy behaviour),
        #       so the contract is pinned here instead.
        fc = TimestralForecaster(
            warmup_bars=10, forecast_stride_bars=5, forecast_horizon_bars=4,
            signal_step="last",
        )
        last = 100.0
        # evenly spaced fan: decile d at 99 + d * 0.5 -> median 101
        fan = np.array([[99.0 + 0.5 * i * 2 for _ in range(4)]
                        for i in range(9)])  # (9, H), row i = level i
        fan[0, :] = 90.0   # stretch Q0.1 far below
        fan[-1, :] = 110.0  # and Q0.9 far above
        f = fc._make_forecast(fan, last, ts := "2026-01-05 21:00:00")
        assert f.timestamp == ts
        assert f.horizon_bars == 4
        p = f.payload
        assert set(p) == {
            "predicted_price", "predicted_return", "last_price",
            "median_path", "quantile_levels", "quantile_prices",
            "quantile_returns", "prob_up", "band_return_80",
        }
        assert p["last_price"] == last
        # median row (index 4) = 99 + 4*1.0 = 103 at every step
        assert p["predicted_price"] == pytest.approx(103.0)
        assert p["predicted_return"] == pytest.approx(0.03)
        assert p["median_path"] == [103.0] * 4
        # prob_up: F(100) on the fan at step H — decile prices are
        # [90, 100, 101, 102, 103, 104, 105, 106, 110]; 100 sits exactly on
        # the Q0.2 row -> F=0.2 -> prob_up 0.8
        assert p["prob_up"] == pytest.approx(0.8)
        assert p["band_return_80"] == pytest.approx(110.0 / 100 - 90.0 / 100)

    def test_prob_up_clamps_and_median(self):
        fc = TimestralForecaster(
            warmup_bars=10, forecast_stride_bars=5, forecast_horizon_bars=2,
        )
        flat_high = np.full(9, 200.0)
        assert fc._prob_up(flat_high, 100.0) == pytest.approx(0.9)
        assert fc._prob_up(flat_high, 300.0) == pytest.approx(0.1)
