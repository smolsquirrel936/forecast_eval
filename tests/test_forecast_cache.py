"""forecast_cache.py: per-bar reuse across strides, key isolation, exactness.

The fake forecaster below is deterministic in the history it is given and counts
the bars it is asked for, so "computed nothing" is an assertion, not a hope.
"""
import json

import numpy as np
import pandas as pd
import pytest

from forecast_eval import forecast_cache as fcache
from forecast_eval.events import Forecast
from forecast_eval.run import precompute_forecasts, run_backtest
from forecast_eval.strategy.threshold import ThresholdEmitter

RUNTIME = {"gpu": "test", "torch": "0", "cuda": "0", "toto-2": "0"}


def _ticks(n=300):
    ts = pd.date_range("2024-01-02 08:45", periods=n, freq="min")
    px = 17000 + 300 * np.sin(np.arange(n) / 9.0) + np.arange(n) * 0.3
    return pd.DataFrame({"timestamp": ts, "price": px, "volume": 1})


class _Fake:
    checkpoint = "fake"
    context_length = 8
    bar_freq = None
    signal_step = "last"

    def __init__(self, warmup=20, stride=5, horizon=4):
        self.warmup_bars = warmup
        self.forecast_stride_bars = stride
        self.forecast_horizon_bars = horizon
        self._model = None
        self.asked = []

    def _one(self, ts, px, b):
        last = float(px[b])
        ret = float((px[b] - px[b - 3]) / px[b - 3])
        return Forecast(timestamp=ts[b], horizon_bars=self.forecast_horizon_bars,
                        payload={"predicted_return": ret, "last_price": last,
                                 "median_path": [last * (1 + ret * k) for k in range(4)],
                                 "prob_up": min(0.99, max(0.01, 0.5 + 5 * ret))})

    def forecast(self, history_df):
        ts = list(history_df["timestamp"])
        px = history_df["price"].to_numpy()
        self.asked.append(len(ts) - 1)
        return self._one(ts, px, len(ts) - 1)

    def forecast_series_batch(self, ts, px, boundaries, **kw):
        self.asked.extend(boundaries)
        return [self._one(ts, px, b) for b in boundaries]


def _load(fc, tmp_path, ticks=None, **kw):
    return fcache.load_or_compute(fc, _ticks() if ticks is None else ticks,
                                  tmp_path, runtime=RUNTIME, **kw)


def test_a_cold_cache_returns_exactly_what_precompute_returns(tmp_path):
    want = precompute_forecasts(_Fake(), _ticks())
    got, stats = _load(_Fake(), tmp_path)
    assert stats["computed"] == len(want) and stats["hit"] == 0
    assert got.keys() == want.keys()
    for b in want:
        assert got[b].timestamp == want[b].timestamp
        assert got[b].horizon_bars == want[b].horizon_bars
        assert got[b].payload == want[b].payload
        assert list(got[b].payload) == list(want[b].payload)   # field order too


def test_a_warm_cache_computes_nothing(tmp_path):
    _load(_Fake(), tmp_path)
    fc = _Fake()
    _, stats = _load(fc, tmp_path)
    assert stats["computed"] == 0 and fc.asked == []


def test_a_coarser_stride_is_served_from_a_finer_one(tmp_path):
    _load(_Fake(stride=5), tmp_path)
    fc = _Fake(stride=15)
    got, stats = _load(fc, tmp_path)
    assert fc.asked == [] and stats["hit"] == len(got) > 0


def test_a_finer_stride_computes_only_the_bars_it_lacks(tmp_path):
    _, first = _load(_Fake(stride=15), tmp_path)
    fc = _Fake(stride=5)
    got, stats = _load(fc, tmp_path)
    assert stats["hit"] == first["requested"]
    assert sorted(fc.asked) == sorted(set(got) - set(range(20, 300, 15)))


def test_the_key_separates_runtime_config_and_tape(tmp_path):
    base = fcache.cache_key(_Fake(), forecaster_sha="a", tape="t", runtime=RUNTIME)[0]
    other_gpu = fcache.cache_key(_Fake(), forecaster_sha="a", tape="t",
                                 runtime={**RUNTIME, "torch": "1"})[0]
    other_sha = fcache.cache_key(_Fake(), forecaster_sha="b", tape="t", runtime=RUNTIME)[0]
    other_tape = fcache.cache_key(_Fake(), forecaster_sha="a", tape="u", runtime=RUNTIME)[0]
    longer = _Fake(horizon=8)
    other_h = fcache.cache_key(longer, forecaster_sha="a", tape="t", runtime=RUNTIME)[0]
    assert len({base, other_gpu, other_sha, other_tape, other_h}) == 5


def test_stride_and_warmup_are_not_in_the_key():
    a = fcache.cache_key(_Fake(warmup=20, stride=5), forecaster_sha="a", tape="t", runtime=RUNTIME)
    b = fcache.cache_key(_Fake(warmup=40, stride=15), forecaster_sha="a", tape="t", runtime=RUNTIME)
    assert a[0] == b[0]


def test_a_changed_tape_lands_in_another_folder(tmp_path):
    _, s1 = _load(_Fake(), tmp_path)
    t = _ticks()
    t.loc[100, "price"] += 1.0
    _, s2 = _load(_Fake(), tmp_path, ticks=t)
    assert s1["folder"] != s2["folder"] and s2["hit"] == 0


def test_a_meta_key_mismatch_refuses(tmp_path):
    _, s = _load(_Fake(), tmp_path)
    meta = tmp_path / s["key"][:16] / "meta.json"
    m = json.loads(meta.read_text(encoding="utf-8"))
    m["key"] = "0" * 64
    meta.write_text(json.dumps(m), encoding="utf-8")
    with pytest.raises(RuntimeError, match="refusing to mix"):
        _load(_Fake(), tmp_path)


def test_no_temp_files_are_left_behind(tmp_path):
    _, s = _load(_Fake(), tmp_path)
    folder = tmp_path / s["key"][:16]
    assert not list(folder.glob("*.tmp"))
    assert len(list(folder.glob("part-*.npz"))) == 1


def test_a_non_numeric_payload_field_is_rejected():
    f = Forecast(timestamp=pd.Timestamp("2024-01-02"), horizon_bars=1,
                 payload={"note": "x"})
    with pytest.raises(TypeError, match="numbers"):
        fcache._encode([0], [f])


def test_a_backtest_on_the_cache_trades_exactly_as_one_on_precompute(tmp_path):
    def go(**kw):
        return run_backtest(_ticks(), forecaster=_Fake(), aggression_ticks=0,
                            emitter=ThresholdEmitter(buy_threshold=0.001,
                                                     sell_threshold=-0.001), **kw)
    fresh = go(precompute=True)
    cache, _ = _load(_Fake(), tmp_path)
    cached = go(forecasts_cache=cache)
    assert len(fresh.fills) > 0
    assert [(f.timestamp, f.side, f.fill_price) for f in cached.fills] == \
           [(f.timestamp, f.side, f.fill_price) for f in fresh.fills]
