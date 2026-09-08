"""NIGHT's depression exit (night autoresearch iter09): a dip that never
recovers is a breakdown.

The 2026 crash-week longs entered on a valid RSI<=30 dip and then the tape kept
falling for the whole week; the only exit that reached them was the one-week
time-stop, whose cap is a clock cliff. The depression exit is the shape-aware
fix: once RSI has sat at/below the buy level for DEPRESSION_K consecutive
stride boundaries (5 here, i.e. 2.5 h of non-recovery), the emitter SELLs —
closing the long — and, while flat, stands in for a BUY (no new knife entry
until RSI recovers above the buy level and dips again). V-shaped dips (the
winners) cross back above the buy level within a couple of boundaries and are
untouched. The exit is never windowed: an exit that could not fire overnight
could strand a position.
"""
import pandas as pd
import pytest

from forecast_eval.events import Forecast
from forecast_eval.strategy.night import rsi as night_rsi
from forecast_eval.strategy.night import wiring


def _forecast(rsi: float, ts: str) -> Forecast:
    return Forecast(
        timestamp=pd.Timestamp(ts),
        horizon_bars=30,
        payload={"rsi": rsi, "last_price": 100.0, "bars": 100},
    )


def _emitter(**kw) -> night_rsi.RsiEmitter:
    return night_rsi.RsiEmitter(**kw)


def test_a_sustained_oversold_streak_exits_on_the_kth_boundary():
    """Inside the window, the first k-1 depressed readings still BUY (the dip is
    fresh); the k-th matures the streak and SELLs (the dip is not recovering)."""
    em = _emitter()
    stamps = ["2026-09-08 15:00:00", "2026-09-08 15:30:00", "2026-09-08 16:00:00",
              "2026-09-08 16:30:00", "2026-09-08 17:00:00"]
    for ts in stamps[:-1]:
        assert em.emit(_forecast(12.0, ts)) == "BUY"
    assert em.emit(_forecast(12.0, stamps[-1])) == "SELL"


def test_the_depression_exit_is_not_windowed():
    """An exit must always be able to fire, or it strands a position overnight.
    The first k-1 overnight depressed readings HOLD (the window blocks the BUY);
    the k-th SELLs regardless of the clock."""
    em = _emitter()
    stamps = ["2026-09-08 22:00:00", "2026-09-08 22:30:00", "2026-09-08 23:00:00",
              "2026-09-08 23:30:00", "2026-09-08 23:59:00"]
    for ts in stamps[:-1]:
        assert em.emit(_forecast(12.0, ts)) == "HOLD"
    assert em.emit(_forecast(12.0, stamps[-1])) == "SELL"


def test_a_recovered_reading_resets_the_streak():
    """A V-shaped dip: RSI crosses back above the buy level, breaking the streak.
    The counter resets, so the later dip never matures and no exit fires."""
    em = _emitter()
    seq = [("15:00", 12.0), ("15:30", 12.0), ("16:00", 50.0),
           ("16:30", 12.0), ("17:00", 12.0), ("17:30", 12.0)]
    out = [em.emit(_forecast(r, f"2026-09-08 {h}")) for h, r in seq]
    assert out == ["BUY", "BUY", "HOLD", "BUY", "BUY", "BUY"]


def test_an_overbought_reading_resets_the_streak_and_sells():
    """An overbought print is itself a SELL and also breaks the depression streak,
    so a later dip starts from zero rather than inheriting the old count."""
    em = _emitter()
    seq = [("15:00", 12.0), ("15:30", 88.0), ("16:00", 12.0),
           ("16:30", 12.0), ("17:00", 12.0)]
    out = [em.emit(_forecast(r, f"2026-09-08 {h}")) for h, r in seq]
    assert out == ["BUY", "SELL", "BUY", "BUY", "BUY"]


def test_depression_k_is_respected():
    """A tighter threshold matures sooner: k=3 exits on the third depressed
    reading, not the fifth."""
    em = _emitter(depression_k=3)
    assert em.emit(_forecast(12.0, "2026-09-08 15:00:00")) == "BUY"
    assert em.emit(_forecast(12.0, "2026-09-08 15:30:00")) == "BUY"
    assert em.emit(_forecast(12.0, "2026-09-08 16:00:00")) == "SELL"


def test_depression_sell_carries_full_strength():
    em = _emitter()
    for h in ("15:00", "15:30", "16:00", "16:30"):
        em.emit_signal(_forecast(12.0, f"2026-09-08 {h}"))
    sig = em.emit_signal(_forecast(12.0, "2026-09-08 17:00:00"))
    assert sig.direction == "SELL" and sig.strength == 1.0


def test_depression_k_must_be_positive():
    with pytest.raises(ValueError):
        _emitter(depression_k=0)
    with pytest.raises(ValueError):
        _emitter(depression_k=-2)


def test_the_wired_emitter_uses_the_module_depression_k():
    """The live strategy picks up the module constant, so moving DEPRESSION_K in
    rsi.py moves the backtest without touching wiring."""
    em = wiring.make_strategy(warmup_bars=100)["emitter"]
    assert em.depression_k == night_rsi.DEPRESSION_K
