"""V0 — offline parity between LiveLoop and run_backtest.

The gate for the whole Shioaji effort (SHIOAJI_PLAN.md phase V0). `LiveLoop`
restructures the per-tick body from pull (a `for` over `Environment.stream()`)
to push (`on_bar` per event). This asserts that restructuring changed no
decision logic: driven over the same ticks through `SimBroker` (which wraps the
same `Execution`), the two paths must produce bit-identical fills, signals,
counters and PnL.

A failure here means the loop transcription is wrong. No Shioaji code is
involved, so it can never be blamed for one.

Three configs, per the plan: the current `strategy/wiring.py` shape, a
stop-loss config, and a forced-close config that crosses DAY<->NIGHT.
"""
from collections import Counter

import numpy as np
import pandas as pd
import pytest

from forecast_eval.environment import Environment
from forecast_eval.events import Forecast
from forecast_eval.execution import Execution
from forecast_eval.exits.stop_loss import FixedStopLoss
from forecast_eval.forecaster.base import Forecaster
from forecast_eval.live.broker.sim import SimBroker
from forecast_eval.live.loop import LiveLoop
from forecast_eval.live.recorder import Recorder
from forecast_eval.portfolio import Portfolio
from forecast_eval.run import run_backtest
from forecast_eval.strategy.threshold import ThresholdEmitter
from forecast_eval.trader import Trader

FEE_RATE = 0.00015
MULTIPLIER = 200.0
N_BARS = 50_000
WARMUP = 3_008
STRIDE = 30
HORIZON = 30
HISTORY_WINDOW = 3_008


class _PathForecaster(Forecaster):
    """Deterministic, data-dependent, and it fills the whole Toto2 payload.

    Emits `predicted_return`, `last_price`, `median_path` and `prob_up` so the
    ThresholdEmitter's *stateful* gates (`_short_prices` deque, path-momentum,
    probability ramp) all actually run. That matters: a stateful emitter fed a
    different sequence of forecasts would diverge, so exercising it is the point.

    It also stamps `ctx_len` / `ctx_sum` / `ctx_first_ts` into the payload — a
    fingerprint of the *entire* history frame it was handed. Toto2 consumes its
    whole `context_length` window, so a parity test whose forecaster only reads
    a short tail would be blind to any divergence in how the two paths build
    that frame (window trimming, off-by-one, a dropped bar). The fingerprint
    makes the payload equality assertion catch it instead.
    """

    def __init__(self, *, warmup_bars, forecast_stride_bars, forecast_horizon_bars):
        self.warmup_bars = warmup_bars
        self.forecast_stride_bars = forecast_stride_bars
        self.forecast_horizon_bars = forecast_horizon_bars

    def forecast(self, history_df: pd.DataFrame) -> Forecast:
        p = history_df["price"].to_numpy(dtype=float)
        last = float(p[-1])
        # Momentum over the trailing 60 rows, projected across the horizon.
        window = p[-60:] if len(p) >= 60 else p
        drift = (float(window[-1]) - float(window[0])) / max(len(window) - 1, 1)
        predicted = last + drift * self.forecast_horizon_bars
        pr = (predicted - last) / last if last else 0.0
        path = [last + drift * (i + 1) for i in range(self.forecast_horizon_bars)]
        # Squash the drift into a (0, 1) directional probability.
        prob_up = float(1.0 / (1.0 + np.exp(-drift * 4.0)))
        return Forecast(
            timestamp=history_df["timestamp"].iloc[-1],
            horizon_bars=self.forecast_horizon_bars,
            payload={
                "predicted_price": predicted,
                "predicted_return": pr,
                "last_price": last,
                "median_path": [float(x) for x in path],
                "prob_up": prob_up,
                "ctx_len": int(len(p)),
                "ctx_sum": float(p.sum()),
                "ctx_first_ts": history_df["timestamp"].iloc[0],
            },
        )


def _ticks(n=N_BARS, seed=7):
    """A 1-min random walk long enough to span many DAY/NIGHT sessions."""
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.0, 3.0, size=n).cumsum()
    return pd.DataFrame({
        "timestamp": pd.date_range("2024-01-02 08:45:00", periods=n, freq="1min"),
        "price": 17_500.0 + steps,
        "volume": rng.integers(1, 20, size=n),
    })


def _make_forecaster():
    return _PathForecaster(
        warmup_bars=WARMUP,
        forecast_stride_bars=STRIDE,
        forecast_horizon_bars=HORIZON,
    )


def _run_live(ticks, *, emitter, exit_rule, forced_close, recorder=None,
              **trader_kw):
    """Drive LiveLoop event-by-event over the same stream run_backtest consumes."""
    execution = Execution(fee_rate=FEE_RATE)
    broker = SimBroker(execution)
    portfolio = Portfolio(contract_multiplier=MULTIPLIER)
    # Trader is duck-typed against the Broker interface — no shim, no change to
    # trader.py. That is the claim the whole seam rests on.
    trader = Trader(execution=broker, tick_size=1.0, **trader_kw)
    loop = LiveLoop(
        forecaster=_make_forecaster(),
        emitter=emitter,
        broker=broker,
        trader=trader,
        portfolio=portfolio,
        exit_rule=exit_rule,
        forced_close_on_session_end=forced_close,
        history_window=HISTORY_WINDOW,
        recorder=recorder,
    )
    for market in Environment(ticks).stream():
        loop.on_bar(market)
    return loop.result()


def _run_backtest(ticks, *, emitter, exit_rule, forced_close, **trader_kw):
    return run_backtest(
        ticks,
        forecaster=_make_forecaster(),
        emitter=emitter,
        exit_rule=exit_rule,
        forced_close_on_session_end=forced_close,
        tick_size=1.0,
        fee_rate=FEE_RATE,
        contract_multiplier=MULTIPLIER,
        history_window=HISTORY_WINDOW,
        **trader_kw,
    )


def _assert_identical(live, back):
    # Counters.
    assert live.n_bars == back.n_bars
    assert live.n_forecasts == back.n_forecasts
    assert live.n_signals == back.n_signals
    assert live.n_orders == back.n_orders
    assert live.n_exits == back.n_exits
    assert live.n_forced_closes == back.n_forced_closes

    # The run must actually have traded, or "identical" is vacuous.
    assert back.n_forecasts > 0, "no forecasts fired — test data/config is inert"
    assert len(back.fills) > 0, "no fills — parity would be trivially true"

    # Forecasts: same timestamp and same payload, in order.
    assert len(live.forecasts) == len(back.forecasts)
    for a, b in zip(live.forecasts, back.forecasts):
        assert a.timestamp == b.timestamp
        assert a.payload == b.payload

    # Signals: same direction AND same strength at the same emit price.
    assert len(live.signals) == len(back.signals)
    for a, b in zip(live.signals, back.signals):
        assert a == b

    # Fills: element-wise identical, including fee.
    assert len(live.fills) == len(back.fills)
    for a, b in zip(live.fills, back.fills):
        assert a.timestamp == b.timestamp
        assert a.side == b.side
        assert a.fill_price == b.fill_price
        assert a.quantity == b.quantity
        assert a.fee == b.fee

    # PnL to full float precision — the operation sequence is identical, so
    # exact equality is the honest assertion here, not approx.
    assert live.portfolio.realized_pnl == back.portfolio.realized_pnl
    assert live.portfolio.total_fees == back.portfolio.total_fees
    assert live.portfolio.net_pnl() == back.portfolio.net_pnl()
    assert live.portfolio.position == back.portfolio.position
    assert (
        live.portfolio.realized_pnl_by_session
        == back.portfolio.realized_pnl_by_session
    )


@pytest.fixture(scope="module")
def ticks():
    return _ticks()


def test_parity_wiring_config(ticks):
    """The config `strategy/wiring.py` currently ships: threshold emitter,
    aggression 0, no exit rule, no forced close."""
    cfg = dict(exit_rule=None, forced_close=False,
               aggression_ticks=0, max_position=1)

    def emitter():  # fresh per run — ThresholdEmitter is stateful
        return ThresholdEmitter(buy_threshold=0.0005, sell_threshold=-0.0005)

    live = _run_live(ticks, emitter=emitter(), **cfg)
    back = _run_backtest(ticks, emitter=emitter(), **cfg)
    _assert_identical(live, back)


def test_parity_stop_loss_config(ticks):
    """Adds an ExitRule, so step 2 fires intra-stride on both paths."""
    def cfg():
        return dict(exit_rule=FixedStopLoss(stop_loss_ticks=20), forced_close=False,
                    aggression_ticks=1, max_position=1)

    def emitter():
        return ThresholdEmitter(buy_threshold=0.0003, sell_threshold=-0.0003)

    live = _run_live(ticks, emitter=emitter(), **cfg())
    back = _run_backtest(ticks, emitter=emitter(), **cfg())
    _assert_identical(live, back)
    assert back.n_exits > 0, "stop-loss never fired — config is not exercising step 2"


def test_parity_forced_close_config(ticks):
    """Crosses DAY<->NIGHT, exercising step 4 and the session-tagging latch."""
    cfg = dict(exit_rule=None, forced_close=True,
               aggression_ticks=0, max_position=1)

    def emitter():
        return ThresholdEmitter(buy_threshold=0.0003, sell_threshold=-0.0003)

    live = _run_live(ticks, emitter=emitter(), **cfg)
    back = _run_backtest(ticks, emitter=emitter(), **cfg)
    _assert_identical(live, back)
    assert back.n_forced_closes > 0, "no session boundary crossed with a position open"


def test_parity_confidence_scaled_config(ticks):
    """Confidence-scaled aggression and sizing, so `strength` reaches the Trader
    and the payload's prob_up actually changes order price and quantity."""
    cfg = dict(exit_rule=None, forced_close=False,
               aggression_ticks=0, max_aggression_ticks=3, max_position=3)

    def emitter():
        return ThresholdEmitter(buy_threshold=0.0003, sell_threshold=-0.0003,
                                min_prob=0.5, full_confidence_prob=0.7)

    live = _run_live(ticks, emitter=emitter(), **cfg)
    back = _run_backtest(ticks, emitter=emitter(), **cfg)
    _assert_identical(live, back)
    assert any(o.quantity > 1 for o in back.orders), "sizing never engaged"


def test_parity_holds_with_a_recorder_attached(ticks, tmp_path):
    """Observation must not perturb decisions. The recorder writes every bar,
    forecast, signal, order and fill; parity must survive it untouched, and the
    trail must contain exactly the events the run produced."""
    import json

    cfg = dict(exit_rule=None, forced_close=False,
               aggression_ticks=0, max_position=1)

    def emitter():
        return ThresholdEmitter(buy_threshold=0.0005, sell_threshold=-0.0005)

    path = tmp_path / "session.jsonl"
    rec = Recorder(path, simulation=True, meta={"test": "parity"})
    live = _run_live(ticks, emitter=emitter(), recorder=rec, **cfg)
    rec.close()

    back = _run_backtest(ticks, emitter=emitter(), **cfg)
    _assert_identical(live, back)

    recs = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]
    counts = Counter(r["event"] for r in recs)
    assert counts["meta"] == 1
    assert counts["bar"] == back.n_bars
    assert counts["forecast"] == back.n_forecasts
    assert counts["signal"] == back.n_signals
    assert counts["order"] == back.n_orders
    assert counts["fill"] == len(back.fills)
    assert counts["halt"] == 0
