"""NIGHT's RSI entries are windowed (night autoresearch iter03): BUY only
15:00-21:59.

iter02's round trips split by entry hour into +20,733 pts for 15:00-21:59 and
-9,891 pts for 22:00-04:59 — a persistent pattern across 2020-2026, not one
disaster trade. The thin globex stretch after 22:00 produces 5-minute RSI dips
that keep falling, so the NIGHT emitter HOLDs a BUY candidate outside the
window. SELL is never windowed (an exit must always be able to fire), and the
gate reads the forecast's own timestamp, so it applies identically on the
per-call and batch forecast paths.
"""
import pandas as pd

from forecast_eval.events import Forecast
from forecast_eval.strategy.night.rsi import RsiEmitter


def _forecast(rsi: float, ts: str) -> Forecast:
    return Forecast(
        timestamp=pd.Timestamp(ts),
        horizon_bars=30,
        payload={"rsi": rsi, "last_price": 100.0, "bars": 100},
    )


def _emitter() -> RsiEmitter:
    return RsiEmitter()


def test_buy_inside_window_passes():
    em = _emitter()
    assert em.emit(_forecast(12.0, "2026-09-08 15:00:00")) == "BUY"
    assert em.emit(_forecast(12.0, "2026-09-08 21:59:00")) == "BUY"


def test_buy_in_the_overnight_stretch_holds():
    em = _emitter()
    assert em.emit(_forecast(12.0, "2026-09-08 22:00:00")) == "HOLD"
    assert em.emit(_forecast(12.0, "2026-09-08 23:30:00")) == "HOLD"
    assert em.emit(_forecast(12.0, "2026-09-09 03:05:00")) == "HOLD"
    assert em.emit(_forecast(12.0, "2026-09-09 04:59:00")) == "HOLD"


def test_window_reopens_at_the_session_start():
    em = _emitter()
    assert em.emit(_forecast(12.0, "2026-09-09 15:00:00")) == "BUY"


def test_sell_is_never_windowed():
    """The RSI>=80 exit path must fire overnight or a position strands."""
    em = _emitter()
    assert em.emit(_forecast(88.0, "2026-09-09 03:00:00")) == "SELL"
    assert em.emit(_forecast(88.0, "2026-09-08 22:30:00")) == "SELL"
    assert em.emit(_forecast(88.0, "2026-09-08 16:00:00")) == "SELL"


def test_midband_holds_at_any_hour():
    em = _emitter()
    assert em.emit(_forecast(50.0, "2026-09-08 16:00:00")) == "HOLD"
    assert em.emit(_forecast(50.0, "2026-09-09 02:00:00")) == "HOLD"


def test_buy_with_a_missing_timestamp_holds():
    em = _emitter()
    bare = Forecast(timestamp=None, horizon_bars=30,
                    payload={"rsi": 12.0, "last_price": 100.0, "bars": 100})
    assert em.emit(bare) == "HOLD"


def test_emit_signal_strength_follows_the_gate():
    em = _emitter()
    blocked = em.emit_signal(_forecast(12.0, "2026-09-09 03:00:00"))
    assert blocked.direction == "HOLD" and blocked.strength == 0.0
    passed = em.emit_signal(_forecast(12.0, "2026-09-08 16:00:00"))
    assert passed.direction == "BUY" and passed.strength == 1.0