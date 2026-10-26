"""LiveRuntime — start-up correctness, which is where a live session goes wrong.

Two classes of failure are covered: a session-policy mismatch between the
backfill and the live aggregator (previously unrepresentable in code but
undetectable at runtime), and a warm-up that leaves the model or the emitter
cold in a way nothing downstream would notice.

Uses a cheap deterministic forecaster — the point is the assembly, not Toto2.
"""
from datetime import datetime, timedelta

import pandas as pd
import pytest

from forecast_eval.events import Forecast
from forecast_eval.forecaster.base import Forecaster
from forecast_eval.live.config import LiveConfig
from forecast_eval.live.recorder import Recorder
from forecast_eval.live import runtime as runtime_mod
from forecast_eval.live.runtime import LiveRuntime
from forecast_eval.live.sessions import SessionPolicy
from forecast_eval.live.state import RailsConfig
from forecast_eval.strategy import wiring as strategy_wiring

CTX = 40
STRIDE = 5


class _Cheap(Forecaster):
    """Deterministic, and it fills the payload keys ThresholdEmitter reads."""

    def __init__(self, **kw):
        self.warmup_bars = kw.get("warmup_bars", 0)
        self.forecast_stride_bars = STRIDE
        self.forecast_horizon_bars = 5
        self.context_length = CTX
        self.checkpoint = "cheap"
        self.device = "cpu"
        self.bar_freq = None
        self.signal_step = "last"
        self._model = None
        self.n_calls = 0

    def forecast(self, history_df):
        self.n_calls += 1
        self._model = "loaded"
        p = history_df["price"].to_numpy(dtype=float)
        last = float(p[-1])
        drift = (last - float(p[0])) / max(len(p) - 1, 1)
        return Forecast(
            timestamp=history_df["timestamp"].iloc[-1],
            horizon_bars=5,
            payload={
                "predicted_price": last + drift * 5,
                "predicted_return": (drift * 5) / last if last else 0.0,
                "last_price": last,
                "median_path": [last + drift * (i + 1) for i in range(5)],
                "prob_up": 0.95 if drift > 0 else 0.05,
            },
        )


pytestmark = pytest.mark.usefixtures("cheap_strategy")


def _config(policy=None, **kw):
    return LiveConfig(
        source_commit="test", source_calmar=0.0, source_dataset="test",
        source_note="test", policy=policy or SessionPolicy.night(), **kw,
    )


def _bars(n, start="2026-05-14 16:00", price=17500.0, step=3.0):
    # step=3.0 gives a predicted return above the 0.00045 band, so replayed
    # forecasts are BUY rather than HOLD — which is what makes the emitter's
    # deque accumulate at all (it appends only on non-HOLD).
    ts = pd.date_range(start, periods=n, freq="1min")
    return pd.DataFrame({
        "timestamp": ts,
        "price": [price + i * step for i in range(n)],
        "volume": [5] * n,
    })


def _session_bars(n, policy, start="2026-05-14 16:00", price=17500.0, step=3.0):
    # `_bars` emits contiguous minutes, which walk out of the session once n
    # exceeds one sitting (840 for NIGHT). Keep only the minutes the policy
    # trades, so a long frame spans several sessions the way a real backfill does.
    ts = [t for t in pd.date_range(start, periods=n * 4, freq="1min")
          if policy.includes(t)][:n]
    assert len(ts) == n, "widen the generated range"
    return pd.DataFrame({
        "timestamp": ts,
        "price": [price + i * step for i in range(n)],
        "volume": [5] * n,
    })


def _runtime(policy=None, **kw):
    return LiveRuntime(_config(policy), **kw)


# ---- the policy-mismatch gap this class closes -----------------------------

def test_warmup_rejects_bars_from_the_wrong_session():
    """The failure that was previously undetectable: backfill fetched with one
    policy, aggregator built with another. It would splice the model context
    from two regimes and never raise."""
    rt = _runtime(SessionPolicy.night())
    day_bars = _bars(CTX + 10, start="2026-05-14 09:00")   # DAY session
    with pytest.raises(ValueError, match="outside \\['NIGHT'\\]"):
        rt.warm_up(day_bars)


def test_warmup_accepts_bars_from_its_own_session():
    rt = _runtime(SessionPolicy.night())
    rt.warm_up(_bars(CTX + 10, start="2026-05-14 16:00"))
    assert rt.warm


def test_day_runtime_is_the_mirror_image():
    """The port: same code, one constructor different."""
    rt = _runtime(SessionPolicy.day())
    rt.warm_up(_bars(CTX + 10, start="2026-05-14 09:00"))
    assert rt.warm
    with pytest.raises(ValueError, match="outside \\['DAY'\\]"):
        _runtime(SessionPolicy.day()).warm_up(
            _bars(CTX + 10, start="2026-05-14 20:00"))


def test_the_aggregator_shares_the_runtime_policy():
    rt = _runtime(SessionPolicy.night())
    assert rt.aggregator.policy is rt.policy
    assert rt.rails.policy is rt.policy


def test_fetch_warmup_bars_supplies_the_policy_itself():
    """The caller never gets to choose a session."""
    seen = {}

    class _Api:
        def kbars(self, contract, start, end):
            seen.update(contract=contract, start=start, end=end)
            ts = pd.date_range("2026-05-14 16:01", periods=5, freq="1min")
            return {"ts": [int(pd.Timestamp(t).value) for t in ts],
                    "Close": [1.0] * 5, "Volume": [1] * 5}

    rt = _runtime(SessionPolicy.night())
    out = rt.fetch_warmup_bars(_Api(), "TXFR1", "2026-05-14", "2026-05-15")
    assert seen["contract"] == "TXFR1"
    assert len(out) == 5 and all(rt.policy.includes(t) for t in out["timestamp"])


# ---- warm-up completeness ---------------------------------------------------

def test_short_context_is_refused():
    """A short context still produces plausible forecasts — from a distribution
    the strategy was never evaluated on."""
    rt = _runtime()
    with pytest.raises(ValueError, match="context_length"):
        rt.warm_up(_bars(CTX - 1))


def test_empty_backfill_is_refused_with_the_quota_hint():
    rt = _runtime()
    with pytest.raises(ValueError, match="EMPTY history"):
        rt.warm_up(_bars(0))


def test_context_is_seeded_to_exactly_context_length():
    rt = _runtime()
    rt.warm_up(_bars(CTX + 40))
    assert rt.loop.history_len() == CTX


def test_seeded_context_ends_at_the_newest_bar():
    bars = _bars(CTX + 40)
    rt = _runtime()
    rt.warm_up(bars)
    assert rt.loop._history_ts[-1] == bars["timestamp"].iloc[-1]


def test_model_is_prewarmed_during_startup():
    """Measured 18.8 s cold vs 0.09 s warm — that cost must not land on the
    first real signal."""
    rt = _runtime()
    assert rt.forecaster._model is None
    rt.warm_up(_bars(CTX + 40))
    assert rt.forecaster._model is not None


def test_emitter_deques_are_primed_by_replay():
    """`_trend_confirms` fails OPEN below 3 samples, so an un-primed restart
    trades with a risk filter silently disabled."""
    rt = _runtime()
    assert len(rt.emitter._short_prices) == 0
    rt.warm_up(_bars(CTX + 60))
    assert len(rt.emitter._short_prices) >= 3


def test_startup_refuses_when_priming_fails_on_a_quiet_stretch():
    """MEASURED: only 3.74 % of signals are non-HOLD and the longest observed
    HOLD run is 385 boundaries, so a flat market primes nothing. Below 3 samples
    `_trend_confirms` fails OPEN, so refusing to start is the safe branch."""
    rt = _runtime()
    flat = _bars(CTX + 60, step=0.0)          # no drift -> every forecast HOLD
    with pytest.raises(RuntimeError, match="priming incomplete"):
        rt.warm_up(flat)
    assert not rt.warm


def test_prime_cap_spends_every_boundary_the_frame_offers_up_to_K():
    """DAY_SESSION_PLAN §2.5. K was 500, sized against night's longest HOLD run
    (385 boundaries). That is the wrong statistic — samples are counted, not
    required to be adjacent, so a start-up is decided by the fewest non-HOLD
    signals in any K-boundary window, measured at 1 for NIGHT and 3 for DAY at
    K=500 against a floor of 3. K=900 is what makes the replay reach far enough
    to use the bars a seeded cache already holds."""
    rt = _runtime()
    n_boundaries = 600                                  # > the old cap of 500
    rt.warm_up(_session_bars(CTX + n_boundaries * STRIDE, rt.policy))
    # +1 for the prewarm forecast `warm_up` issues before seeding the context
    assert rt.forecaster.n_calls == n_boundaries + 1, (
        "the replay stopped short of the boundaries the frame offered"
    )


def test_prime_cap_takes_what_exists_when_the_frame_is_shorter_than_K():
    """NIGHT is in exactly this state today: its live-accumulated cache holds
    14 669 bars — 388 boundaries — against a cap of 900. Falling back to
    "all of them" is what keeps raising K safe for a deployment whose cache has
    not been seeded."""
    rt = _runtime()
    rt.warm_up(_bars(CTX + 20 * STRIDE))
    assert rt.warm and rt.forecaster.n_calls == 21


def test_priming_uses_the_fixed_batch_size_not_auto():
    """MEASURED 2026-10-03: "auto" sizes the priming batch to 80 % of the VRAM
    free at start-up (it picked 1024 and held 21.5 GiB), so the first of three
    deployments claimed the card and the others over-committed it to 63 GB."""
    rt = _runtime()
    seen = []
    real = rt.forecaster.forecast_series_batch

    def spy(*args, **kw):
        seen.append(kw.get("batch_size", "auto"))
        return real(*args, **kw)

    rt.forecaster.forecast_series_batch = spy
    rt.warm_up(_bars(CTX + 60))
    assert seen == [runtime_mod.PRIME_BATCH_SIZE]
    assert isinstance(runtime_mod.PRIME_BATCH_SIZE, int)


def test_priming_releases_the_gpu_cache_before_trading():
    """The priming batch is the largest allocation a deployment makes; its
    working set must not stay reserved for the whole soak."""
    calls = []

    class _Cuda:
        @staticmethod
        def is_available():
            return True

        @staticmethod
        def empty_cache():
            calls.append("empty_cache")

    class _Torch:
        cuda = _Cuda

    rt = _runtime()
    rt.forecaster._torch = _Torch
    rt.warm_up(_bars(CTX + 60))
    assert calls == ["empty_cache"]


def test_releasing_the_gpu_cache_is_a_no_op_without_torch():
    """A CPU or test forecaster never loaded torch; warm-up must not need it."""
    rt = _runtime()
    assert getattr(rt.forecaster, "_torch", None) is None
    rt.warm_up(_bars(CTX + 60))
    assert rt.warm


def test_the_prime_cap_is_deliberately_wider_than_the_fetch_it_is_sized_against():
    """These two numbers are NOT meant to agree, and a future reader will want
    to reconcile them. `warmup_days_default` sizes the *request* (a guaranteed
    500 boundaries); the cap governs the *replay*, and the bar cache routinely
    carries more than the request — a seeded DAY cache holds 30 000 bars, or 899
    boundaries. Raising the request to match would widen every warm-up fetch
    across delisted months (C7) to buy what is already on disk."""
    import inspect
    cap = inspect.signature(LiveRuntime.__init__).parameters[
        "emitter_prime_boundaries"].default
    request = inspect.signature(LiveConfig.warmup_days_default).parameters[
        "emitter_prime_boundaries"].default
    assert cap == 900 and request == 500 and cap > request


def test_priming_check_is_skipped_when_the_filter_is_off():
    """Nothing to leave open if the trend filter is disabled."""
    rt = LiveRuntime(_config(), strategy_overrides={"trend_filter": False})
    rt.warm_up(_bars(CTX + 60, step=0.0))
    assert rt.warm


def _routed_emitter(rt, night_band=0.00045):
    # Hybrid's shape: one emitter per half behind a router. The day half is the
    # fixture's own emitter; the night half gets its own band.
    from forecast_eval.strategy.hybrid.router import RoutedEmitter
    from forecast_eval.strategy.night.emitter import ThresholdEmitter
    night = ThresholdEmitter(buy_threshold=night_band, sell_threshold=-night_band,
                             min_prob=0.5, full_confidence_prob=0.9)
    return RoutedEmitter(day=rt.emitter, night=night)


def test_priming_checks_every_half_of_a_routed_emitter():
    """Hybrid's halves each run a trend filter behind one router. The check used
    to read `_short_prices` off the router itself, found none, and skipped -- so
    a half that primed nothing traded with its filter open."""
    rt = _runtime()
    rt.emitter = _routed_emitter(rt, night_band=1.0)    # night never non-HOLD
    with pytest.raises(RuntimeError, match="priming incomplete"):
        rt.warm_up(_bars(CTX + 60))
    assert not rt.warm


def test_a_routed_emitter_whose_halves_both_prime_starts():
    rt = _runtime()
    rt.emitter = _routed_emitter(rt)
    rt.warm_up(_bars(CTX + 60))
    assert rt.warm
    assert all(len(h._short_prices) >= 3 for h in rt.emitter.halves.values())


def _routed_forecaster():
    from forecast_eval.strategy.hybrid.forecaster import RoutedForecaster
    return RoutedForecaster(_Cheap(), _Cheap(), warmup_bars=0,
                            stride_bars=STRIDE, horizon_bars=5)


def test_a_routed_forecaster_is_prewarmed():
    """Hybrid's forecaster is a router with no `_model` of its own; the prewarm
    read it directly and raised AttributeError."""
    rt = _runtime()
    rt.forecaster = _routed_forecaster()
    rt.warm_up(_bars(CTX + 60))
    assert rt.warm
    assert all(h._model is not None for h in rt.forecaster.halves.values())


def test_a_routed_forecaster_releases_the_gpu_cache():
    """The router holds no `_torch`; its halves do, after their lazy load."""
    calls = []

    class _Cuda:
        @staticmethod
        def is_available():
            return True

        @staticmethod
        def empty_cache():
            calls.append("empty_cache")

    class _Torch:
        cuda = _Cuda

    rt = _runtime()
    rt.forecaster = _routed_forecaster()
    rt.forecaster.halves["DAY"]._torch = _Torch
    rt.warm_up(_bars(CTX + 60))
    assert calls == ["empty_cache"]


def test_priming_does_not_trade_the_past():
    """Replayed forecasts must be discarded, not acted on."""
    rt = _runtime()
    rt.warm_up(_bars(CTX + 60))
    r = rt.result()
    assert r.n_orders == 0 and r.n_signals == 0 and len(r.fills) == 0
    assert rt.trader.position == 0


def test_seeding_does_not_count_as_live_bars():
    rt = _runtime()
    rt.warm_up(_bars(CTX + 40))
    assert rt.result().n_bars == 0


# ---- the live paths ---------------------------------------------------------

def _feed(rt, n, start="2026-05-14 18:00", price=17500.0, step=3.0):
    t0 = pd.Timestamp(start)
    for i in range(n):
        rt.on_tick(t0 + timedelta(minutes=i, seconds=5), price + i * step, 4)
    rt.on_clock(t0 + timedelta(minutes=n + 1))


def test_ticks_before_warm_up_are_refused():
    rt = _runtime()
    with pytest.raises(RuntimeError, match="before warm_up"):
        rt.on_tick(pd.Timestamp("2026-05-14 18:00"), 17500.0)


def test_live_ticks_produce_bars_forecasts_and_signals():
    rt = _runtime()
    rt.warm_up(_bars(CTX + 60))
    _feed(rt, 20)
    r = rt.result()
    assert r.n_bars > 0 and r.n_forecasts > 0 and r.n_signals > 0


def test_simtrade_ticks_do_not_refresh_the_staleness_watchdog():
    """A trial-matching print is not evidence the feed is alive."""
    rt = _runtime(rails_config=RailsConfig(quote_staleness_seconds=60.0))
    rt.warm_up(_bars(CTX + 60))
    t0 = pd.Timestamp("2026-05-14 18:00")
    rt.on_tick(t0, 17500.0)
    rt.on_tick(t0 + timedelta(seconds=90), 17501.0, simtrade=True)
    rt.on_clock(t0 + timedelta(seconds=120))
    assert rt.rails.halted, "only the real print should have refreshed the clock"


def test_off_session_ticks_are_dropped_not_traded():
    rt = _runtime(SessionPolicy.night())
    rt.warm_up(_bars(CTX + 60))
    t0 = pd.Timestamp("2026-05-15 09:00")          # DAY, not our session
    for i in range(5):
        rt.on_tick(t0 + timedelta(minutes=i, seconds=5), 17500.0 + i)
    rt.on_clock(t0 + timedelta(minutes=10))
    assert rt.result().n_bars == 0
    assert rt.aggregator.n_bars_dropped_off_session > 0


def test_tick_capture_records_everything_including_what_is_filtered(tmp_path):
    """V1's live half needs the raw stream: a file holding only the ticks that
    survived the filters cannot verify the filters."""
    from forecast_eval.live.capture import TickCapture, read_ticks

    cap = TickCapture(tmp_path / "ticks.csv")
    rt = _runtime(SessionPolicy.night(), tick_capture=cap)
    rt.warm_up(_bars(CTX + 60))
    t0 = pd.Timestamp("2026-05-14 18:00")
    rt.on_tick(t0, 17500.0, 3, code="TXFH6")                      # kept
    rt.on_tick(t0 + timedelta(seconds=5), 17501.0, 1, simtrade=True)  # dropped
    rt.on_tick(pd.Timestamp("2026-05-15 09:00"), 17502.0, 1)      # off-session
    cap.close()

    df = read_ticks(tmp_path / "ticks.csv")
    assert len(df) == 3, "all three must be captured, filters notwithstanding"
    assert df["simtrade"].sum() == 1
    assert df["code"].iloc[0] == "TXFH6"
    assert rt.summary()["ticks_captured"] == 3


# ---- the backfill<->live seam (SHIOAJI_PLAN.md §2.14) ----------------------

def _events(path):
    import json
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]


def test_a_live_bar_repeating_a_seeded_minute_is_recorded_as_an_anomaly(tmp_path):
    """The §2.14 detector. `fetch_bars` removes the known cause, but a source
    that hands the same minute to both the backfill and the live stream puts one
    timestamp in the context twice, and nothing downstream can see it."""
    rec = Recorder(tmp_path / "s.jsonl", simulation=True)
    rt = _runtime(recorder=rec)
    bars = _bars(CTX + 60)
    rt.warm_up(bars)
    last = bars["timestamp"].iloc[-1]

    rt.on_tick(last + timedelta(seconds=30), 17500.0, 3)   # same minute again
    rt.on_clock(last + timedelta(minutes=2))               # flush the bucket
    rec.close()

    anomalies = [r for r in _events(tmp_path / "s.jsonl")
                 if r["event"] == "bar_anomaly"]
    assert len(anomalies) == 1
    assert anomalies[0]["reason"] == "non_monotonic_bar"
    assert anomalies[0]["timestamp"] == last.isoformat()
    assert anomalies[0]["previous"] == last.isoformat()


def test_the_anomaly_is_recorded_without_halting_or_dropping_the_bar(tmp_path):
    """Recording, not raising: a data quirk must not kill a process that may be
    holding a position. Keeping the bar leaves `bar_idx` — and every stride
    boundary counted from it — untouched."""
    rec = Recorder(tmp_path / "s.jsonl", simulation=True)
    rt = _runtime(recorder=rec)
    bars = _bars(CTX + 60)
    rt.warm_up(bars)
    last = bars["timestamp"].iloc[-1]

    rt.on_tick(last + timedelta(seconds=30), 17500.0, 3)
    rt.on_clock(last + timedelta(minutes=2))
    rec.close()

    assert rt.rails.halted is False
    assert rt.result().n_bars == 1


def test_an_advancing_bar_sequence_records_no_anomaly(tmp_path):
    """Non-vacuity: the detector must be silent on the normal seam, which is
    what `fetch_bars`' truncation now guarantees."""
    rec = Recorder(tmp_path / "s.jsonl", simulation=True)
    rt = _runtime(recorder=rec)
    bars = _bars(CTX + 60)
    rt.warm_up(bars)
    last = bars["timestamp"].iloc[-1]

    for i in (1, 2, 3):
        rt.on_tick(last + timedelta(minutes=i, seconds=30), 17500.0 + i, 3)
    rt.on_clock(last + timedelta(minutes=5))
    rec.close()

    assert not [r for r in _events(tmp_path / "s.jsonl")
                if r["event"] == "bar_anomaly"]
    assert rt.result().n_bars == 3


def test_the_live_runtime_opts_into_the_flush_grace():
    """The grace defaults to zero on the aggregator so offline replay stays pure;
    the live path is the only thing with a delivery race to lose, and this is its
    single construction site."""
    from forecast_eval.live.bars import LIVE_FLUSH_GRACE
    assert _runtime().aggregator.flush_grace == LIVE_FLUSH_GRACE
    assert LIVE_FLUSH_GRACE.total_seconds() == 2


def test_max_position_above_one_is_refused_at_startup():
    with pytest.raises(ValueError, match="average-entry"):
        LiveRuntime(_config(), strategy_overrides={"max_position": 2})


def test_summary_reports_config_and_ingest_counters(tmp_path):
    rec = Recorder(tmp_path / "s.jsonl", simulation=True)
    rt = _runtime(recorder=rec)
    rt.warm_up(_bars(CTX + 60))
    _feed(rt, 10)
    s = rt.summary()
    rec.close()
    assert s["simulation"] is True
    assert s["bars"] > 0 and s["ticks"] > 0
    # `describe()` reports the deployment's package, not the fake stack this
    # test injects, so assert the line's identity rather than the checkpoint.
    assert s["config"].startswith("LiveConfig[test calmar=0.0 ['NIGHT']]")
    assert s["halted"] is False
