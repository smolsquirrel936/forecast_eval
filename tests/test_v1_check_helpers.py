"""V1 live-half diff — the parts that need no credentials.

The comparison and the replay are pure functions precisely so they can be pinned
here; `main()` is the thin shell that logs in. The cases worth defending are the
ones where a wrong answer would look like a *venue* problem: a fabricated
trailing bar, a window that over-reaches, or a missing minute averaged away by a
healthy-looking ratio.
"""
from datetime import datetime, timedelta

import pandas as pd
import pytest

from forecast_eval.live.sessions import SessionPolicy
from forecast_eval.live.v1_check import (
    CLOSE_MATCH_REQUIRED, compare_bars, format_report, replay_capture,
)

N0 = datetime(2026, 8, 11, 20, 0, 0)          # a NIGHT-session minute
NIGHT = SessionPolicy.night()


def _bars(times, prices, volumes=None):
    times = [pd.Timestamp(t) for t in times]
    return pd.DataFrame({
        "timestamp": times,
        "price": [float(p) for p in prices],
        "volume": [int(v) for v in (volumes or [1] * len(times))],
    })


def _ticks(rows):
    """rows: (timestamp, price, volume, simtrade)."""
    return pd.DataFrame({
        "timestamp": [pd.Timestamp(r[0]) for r in rows],
        "price": [float(r[1]) for r in rows],
        "volume": [int(r[2]) for r in rows],
        "simtrade": [bool(r[3]) for r in rows],
        "code": ["TXFH6"] * len(rows),
    })


# ---- compare_bars -----------------------------------------------------------

def test_identical_sources_pass_at_one_hundred_percent():
    b = _bars([N0, N0 + timedelta(minutes=1)], [100.0, 101.0])
    r = compare_bars(b, b.copy())
    assert r.matched == 2 and r.compared == 2
    assert r.close_match_ratio == 1.0 and r.passed


def test_a_close_difference_is_reported_with_both_values():
    t = _bars([N0], [100.0])
    k = _bars([N0], [100.5])
    r = compare_bars(t, k)
    assert r.matched == 0
    assert r.close_mismatch == [(pd.Timestamp(N0), 100.0, 100.5)]
    assert not r.passed


def test_kbars_is_clipped_to_the_window_the_ticks_cover():
    """`kbars` returns whole days and the capture is minutes. Without clipping,
    every minute outside the run counts as missing — true, and meaningless."""
    t = _bars([N0], [100.0])
    k = _bars([N0 - timedelta(minutes=5), N0, N0 + timedelta(minutes=5)],
              [98.0, 100.0, 102.0])
    r = compare_bars(t, k)
    assert r.compared == 1 and r.matched == 1 and r.passed


def test_a_minute_missing_from_one_side_fails_however_good_the_ratio():
    """A missing minute is a structural disagreement, not a near-miss. Scoring
    against the intersection would let 200 perfect matches bury it."""
    times = [N0 + timedelta(minutes=i) for i in range(400)]
    t = _bars(times, [100.0] * 400)
    k = _bars(times[:-1], [100.0] * 399)          # kbars never produced the last
    r = compare_bars(t, k)
    assert r.only_in_ticks == [pd.Timestamp(times[-1])]
    assert r.close_match_ratio > CLOSE_MATCH_REQUIRED, "ratio alone looks fine"
    assert not r.passed, "but an unmatched minute must still fail"


def test_a_minute_only_kbars_produced_is_caught_inside_the_window():
    """`only_in_kbars` is necessarily bounded by the tick window — anything the
    venue reported outside it was clipped as out of scope. What must survive is
    a minute *inside* the window that our aggregation never produced."""
    t = _bars([N0, N0 + timedelta(minutes=2)], [100.0, 102.0])
    k = _bars([N0, N0 + timedelta(minutes=1), N0 + timedelta(minutes=2)],
              [100.0, 101.0, 102.0])
    r = compare_bars(t, k)
    assert r.only_in_kbars == [pd.Timestamp(N0 + timedelta(minutes=1))]
    assert r.matched == 2 and r.compared == 3, "the union, not the intersection"
    assert not r.passed


def test_volume_differences_are_reported_but_do_not_fail_the_verdict():
    """The model is univariate on close, so a volume gap cannot change a
    decision — but a systematic factor would be a real finding about the feed."""
    t = _bars([N0], [100.0], [10])
    k = _bars([N0], [100.0], [20])
    r = compare_bars(t, k)
    assert r.volume_mismatch == [(pd.Timestamp(N0), 10, 20)]
    assert r.matched == 1 and r.passed


def test_an_empty_tick_side_is_not_a_silent_pass():
    r = compare_bars(_bars([], []), _bars([N0], [100.0]))
    assert r.compared == 0 and not r.passed


# ---- replay_capture ---------------------------------------------------------

def test_replay_does_not_fabricate_the_final_partial_minute():
    """The gotcha this tool exists to avoid. Flushing past the last captured tick
    emits a bar the live process never completed, which then shows up as a kbars
    mismatch and gets blamed on the venue."""
    ticks = _ticks([
        (N0 + timedelta(seconds=5), 100.0, 1, False),
        (N0 + timedelta(minutes=1, seconds=5), 101.0, 1, False),   # closes 20:00
        (N0 + timedelta(minutes=1, seconds=30), 102.0, 1, False),  # 20:01 partial
    ])
    bars, _ = replay_capture(ticks, policy=NIGHT)
    assert list(bars["timestamp"]) == [pd.Timestamp(N0)]
    assert bars["price"].iloc[0] == 100.0


def test_replay_drops_simtrade_and_reports_it():
    ticks = _ticks([
        (N0 - timedelta(minutes=1), 999.0, 5, True),      # trial print
        (N0 + timedelta(seconds=5), 100.0, 1, False),
        (N0 + timedelta(minutes=1, seconds=5), 101.0, 1, False),
    ])
    bars, agg = replay_capture(ticks, policy=NIGHT)
    assert agg.n_simtrade_dropped == 1
    assert list(bars["price"]) == [100.0], "a trial print must not reach a bar"


def test_replay_keeps_file_order_rather_than_sorting():
    """File order is what the live aggregator saw. Sorting would repair
    out-of-order delivery the live run had to cope with, so the replay would
    agree with kbars for a reason the live path never enjoyed."""
    ticks = _ticks([
        (N0 + timedelta(seconds=5), 100.0, 1, False),
        (N0 + timedelta(minutes=1, seconds=5), 101.0, 1, False),   # closes 20:00
        (N0 + timedelta(seconds=59), 555.0, 1, False),             # arrives late
        (N0 + timedelta(minutes=2, seconds=5), 102.0, 1, False),
    ])
    bars, agg = replay_capture(ticks, policy=NIGHT)
    assert agg.n_late_dropped == 1, "the late print must be counted, not merged"
    assert bars["price"].iloc[0] == 100.0, "and must not rewrite the closed bar"


def test_replay_filters_to_the_policy_session():
    day = datetime(2026, 8, 11, 9, 0, 0)          # DAY, not the night policy
    ticks = _ticks([
        (day, 100.0, 1, False),
        (day + timedelta(minutes=1), 101.0, 1, False),
        (day + timedelta(minutes=2), 102.0, 1, False),
    ])
    bars, agg = replay_capture(ticks, policy=NIGHT)
    assert bars.empty and agg.n_bars_dropped_off_session > 0


# ---- the report -------------------------------------------------------------

def test_report_states_the_verdict_and_the_threshold():
    b = _bars([N0], [100.0])
    text = format_report(compare_bars(b, b.copy()))
    assert "VERDICT: PASS" in text and "99.5%" in text


def test_report_shows_a_failing_diff_rather_than_only_a_ratio():
    t, k = _bars([N0], [100.0]), _bars([N0], [100.5])
    text = format_report(compare_bars(t, k))
    assert "VERDICT: FAIL" in text
    assert "100.0" in text and "100.5" in text, "the values must be visible"
