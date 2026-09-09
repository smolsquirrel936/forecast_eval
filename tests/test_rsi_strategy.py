"""The plain RSI strategy — RSI(14) over 5-minute closes, BUY below the buy
level, SELL above the sell level (each package's own constants).

Both session packages hold their own copy, so every behavioural test runs against
both: a divergence between them is exactly what the split exists to allow, and
exactly what a shared-code mistake would hide. The one DELIBERATE divergence so
far: day's emitter regime-gates BUY entries (autoresearch iter05, 2026-09-08);
its gate semantics are pinned in tests/test_day_regime_gate.py, and the shared
fixture below carries a gate-passing `regime_sma` that night's copy ignores.
"""
import numpy as np
import pandas as pd
import pytest

from forecast_eval.events import Forecast
from forecast_eval.strategy.day import rsi as day_rsi
from forecast_eval.strategy.night import rsi as night_rsi

MODULES = (day_rsi, night_rsi)
IDS = ("day", "night")


def _tape(prices, start="2026-09-07 15:00", freq="1min"):
    ts = pd.date_range(start, periods=len(prices), freq=freq)
    return pd.DataFrame({"timestamp": ts, "price": np.asarray(prices, dtype=float)})


# ---- the indicator ---------------------------------------------------------

@pytest.mark.parametrize("mod", MODULES, ids=IDS)
def test_an_unbroken_rise_saturates_at_100(mod):
    """avg_loss is 0 here, so the formula divides by zero unless it is handled."""
    assert mod.rsi(pd.Series(np.linspace(100.0, 200.0, 60))) == 100.0


@pytest.mark.parametrize("mod", MODULES, ids=IDS)
def test_an_unbroken_fall_saturates_at_0(mod):
    assert mod.rsi(pd.Series(np.linspace(200.0, 100.0, 60))) == 0.0


@pytest.mark.parametrize("mod", MODULES, ids=IDS)
def test_a_flat_tape_is_neutral_not_saturated(mod):
    """No gains and no losses is 50, not 100 — the avg_gain > 0 branch decides it."""
    assert mod.rsi(pd.Series([100.0] * 60)) == 50.0


@pytest.mark.parametrize("mod", MODULES, ids=IDS)
def test_too_little_history_is_None_not_a_neutral_50(mod):
    """`None` means 'no reading'; 50.0 would mean 'read it, it was neutral'.

    The emitter holds on either, but conflating them would have the first strides
    after warm-up trading a fabricated midpoint.
    """
    assert mod.rsi(pd.Series([100.0] * mod.RSI_PERIOD)) is None
    assert mod.rsi(pd.Series([100.0] * (mod.RSI_PERIOD + 1))) is not None


@pytest.mark.parametrize("mod", MODULES, ids=IDS)
def test_it_matches_wilders_recursive_definition_exactly(mod):
    """Wilder's average is recursive with alpha = 1/period, seeded on the first
    delta — which is what `ewm(alpha=1/period, adjust=False)` computes. Checked
    against a longhand loop so a change of smoothing cannot pass unnoticed."""
    closes = pd.Series([100.0 + i for i in range(40)] + [120.0, 100.0])
    period = 14

    avg_gain = avg_loss = 0.0
    for i, d in enumerate(closes.diff().dropna().tolist()):
        gain, loss = max(d, 0.0), max(-d, 0.0)
        if i == 0:
            avg_gain, avg_loss = gain, loss
        else:
            avg_gain += (gain - avg_gain) / period
            avg_loss += (loss - avg_loss) / period
    expected = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)

    assert mod.rsi(closes, period) == pytest.approx(expected, abs=1e-9)


@pytest.mark.parametrize("mod", MODULES, ids=IDS)
def test_it_is_not_the_simple_moving_average_form(mod):
    """A 14-bar rolling mean is a different indicator wearing the same name; it
    would not match any chart this is compared against."""
    closes = pd.Series([100.0 + i for i in range(40)] + [120.0, 100.0])
    delta = closes.diff().dropna()
    gain = delta.clip(lower=0.0).rolling(14).mean().iloc[-1]
    loss = (-delta).clip(lower=0.0).rolling(14).mean().iloc[-1]
    sma_rsi = 100.0 - 100.0 / (1.0 + gain / loss)

    assert mod.rsi(closes, 14) != pytest.approx(sma_rsi, abs=1e-6)


# ---- the 5-minute resample -------------------------------------------------

@pytest.mark.parametrize("mod", MODULES, ids=IDS)
def test_the_forecaster_reads_five_minute_closes(mod):
    f = mod.make_rsi_forecaster(warmup_bars=0, stride_bars=30, horizon_bars=30)
    fc = f.forecast(_tape(np.linspace(20000.0, 19000.0, 400)))
    assert fc.payload["bars"] == 79, (
        "400 one-minute rows span 80 five-minute bars, and the last is withheld "
        "until the next one starts -- deliberately conservative, because at the "
        "final tick we cannot yet know no further tick lands in that bar"
    )


@pytest.mark.parametrize("mod", MODULES, ids=IDS)
def test_each_bar_takes_its_LAST_price_not_its_first(mod):
    """'Close' is the last print in the bar. Taking the first would shift every
    reading by four minutes and quietly change every signal."""
    prices = list(range(100, 115))          # 15 minutes -> 3 bars, 2 complete
    ts = pd.date_range("2026-09-07 15:00", periods=len(prices), freq="1min")
    closes = mod.completed_closes(ts, prices)
    assert list(closes) == [104.0, 109.0], (
        "104 and 109 are the LAST price in each bar; 100 and 105 would be the "
        "first, which shifts every reading by four minutes"
    )

    f = mod.make_rsi_forecaster(warmup_bars=0, stride_bars=30, horizon_bars=30)
    fc = f.forecast(_tape(prices))
    assert fc.payload["bars"] == 2
    assert fc.payload["last_price"] == 114.0, "the raw last tick, not a bar close"


@pytest.mark.parametrize("mod", MODULES, ids=IDS)
def test_a_gap_in_the_tape_drops_the_empty_bar_rather_than_yielding_NaN(mod):
    """TXF's night session has thin stretches. A NaN close would propagate
    through diff() and void the average gain/loss for `period` bars after it."""
    a = pd.date_range("2026-09-07 15:00", periods=60, freq="1min")
    b = pd.date_range("2026-09-07 18:00", periods=60, freq="1min")   # 2-hour gap
    hist = pd.DataFrame({
        "timestamp": list(a) + list(b),
        "price": [100.0 + i for i in range(120)],
    })
    f = mod.make_rsi_forecaster(warmup_bars=0, stride_bars=30, horizon_bars=30)
    fc = f.forecast(hist)
    assert fc.payload["bars"] == 23, (
        "12 bars before the gap, 11 after it -- none for the gap itself, and the "
        "in-progress final bar withheld"
    )
    assert fc.payload["rsi"] is not None and not np.isnan(fc.payload["rsi"])


@pytest.mark.parametrize("mod", MODULES, ids=IDS)
def test_the_payload_carries_no_predicted_return(mod):
    """An indicator predicts nothing; a fabricated number would reach
    forecasts.csv and any comparison made from it."""
    f = mod.make_rsi_forecaster(warmup_bars=0, stride_bars=30, horizon_bars=30)
    fc = f.forecast(_tape(np.linspace(20000.0, 19000.0, 400)))
    assert "predicted_return" not in fc.payload


# ---- the emitter -----------------------------------------------------------

def _fc(value):
    # Tech: one payload that satisfies BOTH packages' entry gates -- stamped at
    #       16:00, inside night's window (ENTRY_FIRST_HOUR 15 .. ENTRY_LAST_HOUR
    #       20 after its iter08), and carrying a regime_sma that day's gate
    #       clears (last_price 100.0 above sma 99.0).
    # Why:  the two packages diverged on different axes and each branch had
    #       taught this shared fixture only its own: day vets BUY candidates
    #       against the regime average and ignores the clock; night windows them
    #       by the forecast's own hour and ignores regime_sma. Supplying both
    #       keeps ONE fixture serving both, so only the deliberate divergences
    #       show up in the expectations rather than in the setup.
    return Forecast(timestamp=pd.Timestamp("2026-09-07 16:00"),
                    horizon_bars=30,
                    payload={"rsi": value, "last_price": 100.0,
                             "regime_sma": 99.0})


# Tech: the level cells are derived from each module's OWN constants, not
#       hardcoded 20/80 -- night moved RSI_BUY_BELOW 20 -> 30 in its iter06.
# Why:  the invariant under test is "each package trades at ITS declared levels,
#       inclusive, mean-reversion direction" -- which is what survives a session
#       legitimately moving its own knob inside its own file. Hardcoding 20/80
#       would make the test fail for a legal divergence, and loosening it to
#       "just some BUY somewhere" would make it vacuous. Reading the constants
#       keeps day's cells exactly what they were (20/80) and night's honest.
@pytest.mark.parametrize("mod", MODULES, ids=IDS)
def test_the_levels_are_the_module_constants_and_inclusive(mod):
    b, s = mod.RSI_BUY_BELOW, mod.RSI_SELL_ABOVE
    cases = [
        (0.0, "BUY"), (b - 0.1, "BUY"), (b, "BUY"),      # at the level, inclusive
        (b + 0.1, "HOLD"),
        ((b + s) / 2.0, "HOLD"), (s - 0.1, "HOLD"),
        (s, "SELL"), (s + 0.1, "SELL"), (100.0, "SELL"),
    ]
    for value, expected in cases:
        assert mod.RsiEmitter().emit(_fc(value)) == expected, f"{value} -> {expected}"


@pytest.mark.parametrize("mod", MODULES, ids=IDS)
def test_it_is_mean_reversion_not_momentum(mod):
    """Oversold BUYS. Stated as a test because the opposite reading of the same
    number is just as coherent, and inverting it would still 'work'."""
    assert mod.RsiEmitter().emit(_fc(10.0)) == "BUY"
    assert mod.RsiEmitter().emit(_fc(90.0)) == "SELL"


@pytest.mark.parametrize("mod", MODULES, ids=IDS)
def test_no_reading_holds(mod):
    e = mod.RsiEmitter()
    assert e.emit(None) == "HOLD"
    assert e.emit(_fc(None)) == "HOLD"
    assert e.emit(Forecast(timestamp=None, horizon_bars=30, payload="junk")) == "HOLD"


@pytest.mark.parametrize("mod", MODULES, ids=IDS)
def test_crossed_levels_are_refused_at_construction(mod):
    """Otherwise some RSI is both oversold and overbought and the first branch
    silently wins."""
    with pytest.raises(ValueError):
        mod.RsiEmitter(buy_below=80.0, sell_above=20.0)
    with pytest.raises(ValueError):
        mod.RsiEmitter(buy_below=50.0, sell_above=50.0)


@pytest.mark.parametrize("mod", MODULES, ids=IDS)
def test_strength_is_full_on_a_signal_and_zero_on_a_hold(mod):
    e = mod.RsiEmitter()
    assert e.emit_signal(_fc(10.0)).strength == 1.0
    assert e.emit_signal(_fc(50.0)).strength == 0.0


# ---- the wiring ------------------------------------------------------------

@pytest.mark.parametrize("session", IDS)
def test_make_strategy_builds_the_rsi_pair_and_needs_no_gpu(session):
    """The point of routing RSI through the Forecaster seam: `make_strategy`
    must not import torch or reach for a checkpoint."""
    mod = __import__(f"forecast_eval.strategy.{session}.wiring",
                     fromlist=["wiring"])
    built = mod.make_strategy(warmup_bars=100)
    assert type(built["forecaster"]).__name__ == "RsiForecaster"
    assert type(built["emitter"]).__name__ == "RsiEmitter"


@pytest.mark.parametrize("session", IDS)
def test_the_pinned_classes_stay_reachable_from_wiring(session):
    """`tests/test_strategy_isolation.py` constructs `wiring.ThresholdEmitter` by
    name, and `live/config.py` still trades it. Dropping the import would fail the
    isolation gate for a reason that has nothing to do with isolation."""
    mod = __import__(f"forecast_eval.strategy.{session}.wiring",
                     fromlist=["wiring"])
    assert mod.ThresholdEmitter is not None
    assert mod.make_forecaster is not None


def test_the_two_packages_hold_distinct_rsi_classes():
    """Same guarantee the split sells for the emitter and Trader, for the new
    files: editing day's RSI must not move night's."""
    assert day_rsi.RsiEmitter is not night_rsi.RsiEmitter
    assert day_rsi.RsiForecaster is not night_rsi.RsiForecaster


# ---- the batch path (§ performance) ----------------------------------------
#
# The model-free strategy first ran SLOWER than the GPU model it replaced: the
# base `forecast_series_batch` rebuilds a full DataFrame per boundary and calls
# `forecast()` on it, which over the night tape is 61k rebuilds of a ~900k-row
# frame. `RsiForecaster` overrides that seam. These tests pin the property that
# makes the override legitimate — it must compute the SAME numbers.


@pytest.mark.parametrize("mod", MODULES, ids=IDS)
def test_batch_and_per_call_agree_exactly(mod):
    """If these ever diverge, --precompute silently backtests a different
    strategy from the one a single forecast() describes."""
    rng = np.random.default_rng(11)
    n = 6_000
    ts = pd.date_range("2026-09-07 15:00", periods=n, freq="1min")
    px = rng.standard_normal(n).cumsum() + 20_000.0

    f = mod.make_rsi_forecaster(warmup_bars=0, stride_bars=30, horizon_bars=30)
    boundaries = list(range(1_000, n, 250))
    batch = f.forecast_series_batch(ts, px, boundaries)

    for b, got in zip(boundaries, batch):
        one = f.forecast(pd.DataFrame({"timestamp": ts[: b + 1],
                                       "price": px[: b + 1]}))
        assert got.payload["rsi"] == pytest.approx(one.payload["rsi"], abs=1e-9)
        assert got.payload["last_price"] == one.payload["last_price"]


@pytest.mark.parametrize("mod", MODULES, ids=IDS)
def test_the_in_progress_bar_is_excluded(mod):
    """A partial bar's 'close' moves as ticks arrive, so including it would make
    the closes for history-through-t stop being a prefix of the full series —
    which is the property the one-pass batch relies on."""
    prices = list(range(100, 113))          # 13 minutes: 2 complete bars + 3 min
    ts = pd.date_range("2026-09-07 15:00", periods=len(prices), freq="1min")
    closes = mod.completed_closes(ts, prices)
    assert list(closes) == [104.0, 109.0], "the 15:10-15:12 partial bar is excluded"


@pytest.mark.parametrize("mod", MODULES, ids=IDS)
def test_completed_closes_of_a_prefix_is_a_prefix_of_the_whole(mod):
    """The exact property that lets one causal pass replace n recomputations."""
    n = 400
    ts = pd.date_range("2026-09-07 15:00", periods=n, freq="1min")
    px = np.linspace(20_000.0, 19_000.0, n)
    whole = mod.completed_closes(ts, px)
    for cut in (137, 251, 399):
        part = mod.completed_closes(ts[: cut + 1], px[: cut + 1])
        assert list(part) == list(whole[: len(part)])


@pytest.mark.parametrize("mod", MODULES, ids=IDS)
def test_the_batch_never_reads_past_its_boundary(mod):
    """Look-ahead check: rewriting every price after boundary b must not change
    b's reading."""
    rng = np.random.default_rng(3)
    n = 3_000
    ts = pd.date_range("2026-09-07 15:00", periods=n, freq="1min")
    px = rng.standard_normal(n).cumsum() + 20_000.0
    f = mod.make_rsi_forecaster(warmup_bars=0, stride_bars=30, horizon_bars=30)

    b = 2_000
    before = f.forecast_series_batch(ts, px, [b])[0].payload["rsi"]
    tampered = px.copy()
    tampered[b + 1:] += 5_000.0            # a future that could not be ignored
    after = f.forecast_series_batch(ts, tampered, [b])[0].payload["rsi"]
    assert before == pytest.approx(after, abs=1e-12)


@pytest.mark.parametrize("mod", MODULES, ids=IDS)
def test_one_call_does_not_scan_the_whole_history(mod):
    """`forecast()` keeps a bounded tail, so its cost cannot grow with the run.

    Measured as a ratio rather than a wall-clock budget so it does not turn into
    a flaky timing test on a loaded machine.
    """
    import time

    f = mod.make_rsi_forecaster(warmup_bars=0, stride_bars=30, horizon_bars=30)

    def elapsed(n):
        ts = pd.date_range("2020-01-01", periods=n, freq="1min")
        px = np.linspace(20_000.0, 19_000.0, n)
        hist = pd.DataFrame({"timestamp": ts, "price": px})
        f.forecast(hist)                                    # warm any caches
        t0 = time.perf_counter()
        for _ in range(3):
            f.forecast(hist)
        return (time.perf_counter() - t0) / 3

    small, large = elapsed(20_000), elapsed(400_000)
    assert large < small * 5, (
        f"a 20x longer history cost {large / small:.1f}x more per call; "
        f"the tail slice is not bounding the work"
    )
