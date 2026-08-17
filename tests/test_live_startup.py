"""RestartGuard — the rails that must survive the process dying.

The scenario being defended against is an unsupervised crash-restart loop, which
against Shioaji does not merely degrade: repeated rate-limit violations get the
IP and ID suspended, and repeated logins during a suspension extend it. So the
tests care about two things — a legitimate restart is never blocked, and a
runaway one is stopped within a couple of attempts.
"""
from datetime import datetime, timedelta

import pytest

from forecast_eval.live.startup import (
    RestartBlocked,
    RestartGuard,
    RestartPolicy,
)

T0 = datetime(2026, 8, 10, 9, 0, 0)          # inside a trading day


def _guard(tmp_path, **kw):
    return RestartGuard(RestartPolicy(state_file=tmp_path / "restart.json", **kw))


# ---- happy paths ------------------------------------------------------------

def test_first_start_is_allowed_and_records_state(tmp_path):
    g = _guard(tmp_path)
    g.begin(T0)
    assert (tmp_path / "restart.json").exists()


def test_clean_stop_and_restart_pays_no_backoff(tmp_path):
    """An operator-driven restart must be instant — no penalty at all."""
    g = _guard(tmp_path)
    g.begin(T0)
    g.end(T0 + timedelta(minutes=5))
    g.begin(T0 + timedelta(minutes=5, seconds=1))   # immediate restart, allowed


def test_backfill_under_the_cap_is_fine(tmp_path):
    g = _guard(tmp_path, max_backfills_per_day=10)
    g.begin(T0)
    for i in range(9):
        g.record_backfill(T0 + timedelta(minutes=i))
    g.end(T0 + timedelta(hours=1))
    g.begin(T0 + timedelta(hours=1, seconds=1))


# ---- restart backoff --------------------------------------------------------

def test_crash_then_immediate_restart_is_blocked(tmp_path):
    """No end() call means the process died; the next start must wait."""
    g = _guard(tmp_path, base_backoff_seconds=60.0)
    g.begin(T0)
    g.heartbeat(T0 + timedelta(seconds=10))
    # ...process dies here, no end()
    with pytest.raises(RestartBlocked, match="backoff"):
        g.begin(T0 + timedelta(seconds=20))


def test_backoff_elapses_and_then_allows_the_start(tmp_path):
    g = _guard(tmp_path, base_backoff_seconds=60.0)
    g.begin(T0)
    g.heartbeat(T0 + timedelta(seconds=10))
    g.begin(T0 + timedelta(seconds=80))          # >60s since last heartbeat


def test_backoff_doubles_per_consecutive_crash(tmp_path):
    g = _guard(tmp_path, base_backoff_seconds=60.0)
    t = T0
    # crash 1 -> requires 60s
    g.begin(t)
    g.heartbeat(t)
    t += timedelta(seconds=61)
    g.begin(t)                                    # allowed
    g.heartbeat(t)
    # crash 2 -> requires 120s, so 61s is not enough
    t += timedelta(seconds=61)
    with pytest.raises(RestartBlocked):
        g.begin(t)
    t += timedelta(seconds=60)                    # 121s total
    g.begin(t)                                    # allowed


def test_repeated_begin_calls_do_not_compound_the_penalty(tmp_path):
    """The wait is measured from the previous run's last sign of life, not from
    `now`, so hammering begin() cannot inflate it."""
    g = _guard(tmp_path, base_backoff_seconds=60.0)
    g.begin(T0)
    g.heartbeat(T0)
    for _ in range(5):
        with pytest.raises(RestartBlocked):
            g.begin(T0 + timedelta(seconds=10))
    g.begin(T0 + timedelta(seconds=61))           # still just the base backoff


def test_crash_after_a_healthy_run_resets_to_a_single_failure(tmp_path):
    """One crash after hours of healthy running is not the fourth crash in two
    minutes and must not inherit its backoff."""
    g = _guard(tmp_path, base_backoff_seconds=60.0, healthy_runtime_seconds=600.0)
    t = T0
    # Two quick crashes to build the counter up.
    g.begin(t); g.heartbeat(t)
    t += timedelta(seconds=61)
    g.begin(t); g.heartbeat(t)
    t += timedelta(seconds=121)
    g.begin(t)
    # Now run healthily for two hours, then die.
    g.heartbeat(t + timedelta(hours=2))
    t += timedelta(hours=2, seconds=61)
    g.begin(t)          # back to a single failure -> only 60s needed


def test_backoff_is_capped(tmp_path):
    g = _guard(tmp_path, base_backoff_seconds=60.0, max_backoff_seconds=120.0)
    t = T0
    for _ in range(6):                            # many consecutive crashes
        try:
            g.begin(t)
            g.heartbeat(t)
        except RestartBlocked:
            pass
        t += timedelta(seconds=200)               # always > the 120s cap
    g.begin(t)                                    # cap keeps it reachable


# ---- backfill cap -----------------------------------------------------------

def test_backfill_cap_blocks_further_starts(tmp_path):
    g = _guard(tmp_path, max_backfills_per_day=3)
    g.begin(T0)
    for i in range(3):
        g.record_backfill(T0 + timedelta(minutes=i))
    g.end(T0 + timedelta(minutes=10))
    with pytest.raises(RestartBlocked, match="backfill cap"):
        g.begin(T0 + timedelta(minutes=11))


def test_backfill_counter_resets_on_the_new_trading_day(tmp_path):
    g = _guard(tmp_path, max_backfills_per_day=3)
    g.begin(T0)
    for i in range(3):
        g.record_backfill(T0 + timedelta(minutes=i))
    g.end(T0 + timedelta(minutes=10))
    # Next day, after the 08:00 quota reset.
    g.begin(T0 + timedelta(days=1))


def test_trading_day_rolls_at_0800_not_midnight(tmp_path):
    """A night session runs to 05:00 and belongs to the previous trading day."""
    g = _guard(tmp_path)
    assert g.trading_day(datetime(2026, 8, 10, 9, 0)) == "2026-08-10"
    assert g.trading_day(datetime(2026, 8, 10, 23, 30)) == "2026-08-10"
    assert g.trading_day(datetime(2026, 8, 11, 3, 0)) == "2026-08-10"
    assert g.trading_day(datetime(2026, 8, 11, 7, 59)) == "2026-08-10"
    assert g.trading_day(datetime(2026, 8, 11, 8, 0)) == "2026-08-11"


def test_failure_counter_survives_the_day_roll(tmp_path):
    """The quota resets daily; the health of the process does not. After a crash
    carries across the day boundary, the *next* crash must escalate to 120s
    rather than restarting the ladder at 60s."""
    g = _guard(tmp_path, base_backoff_seconds=60.0)
    g.begin(T0)
    g.heartbeat(T0)
    # Crash 1 scored on the next day's start; a full day has passed so the 60s
    # backoff is long satisfied and the start is allowed.
    t = T0 + timedelta(days=1)
    g.begin(t)
    g.heartbeat(t)
    # Crash 2: if the counter had reset at the day roll this would need only 60s.
    with pytest.raises(RestartBlocked, match="backoff"):
        g.begin(t + timedelta(seconds=61))
    g.begin(t + timedelta(seconds=121))


# ---- usage headroom ---------------------------------------------------------

class _Usage:
    def __init__(self, remaining):
        self.remaining_bytes = remaining


def test_low_traffic_headroom_blocks_startup(tmp_path):
    """Over quota, Shioaji returns EMPTY history rather than erroring — which
    would silently yield an empty model context."""
    g = _guard(tmp_path, min_remaining_bytes=50 * 1024 * 1024)
    with pytest.raises(RestartBlocked, match="headroom"):
        g.check_usage(_Usage(1024))


def test_ample_headroom_passes(tmp_path):
    g = _guard(tmp_path, min_remaining_bytes=50 * 1024 * 1024)
    g.check_usage(_Usage(500 * 1024 * 1024))


def test_usage_accepts_a_dict_and_skips_when_unknown(tmp_path):
    g = _guard(tmp_path, min_remaining_bytes=1000)
    g.check_usage({"remaining_bytes": 5000})
    g.check_usage(None)                # unavailable -> not an error
    g.check_usage(object())            # no such field -> not an error
    with pytest.raises(RestartBlocked):
        g.check_usage({"remaining_bytes": 10})


# ---- state file robustness --------------------------------------------------

def test_corrupt_state_file_is_charged_a_failure_not_a_clean_slate(tmp_path):
    """A damaged file is not evidence of health — otherwise a crash loop that
    scribbles on it would bypass the backoff entirely."""
    p = tmp_path / "restart.json"
    p.write_text("{not json")
    g = RestartGuard(RestartPolicy(state_file=p, base_backoff_seconds=60.0))
    g.begin(T0)                       # no prior timestamps -> cannot block yet
    g.heartbeat(T0)
    with pytest.raises(RestartBlocked):
        g.begin(T0 + timedelta(seconds=5))


def test_missing_state_file_is_a_genuine_first_run(tmp_path):
    g = _guard(tmp_path)
    g.begin(T0)                       # no file at all -> allowed immediately


def test_state_survives_a_new_guard_instance(tmp_path):
    """The instance dies with the process; the file is the point."""
    _guard(tmp_path, base_backoff_seconds=60.0).begin(T0)
    _guard(tmp_path, base_backoff_seconds=60.0).heartbeat(T0)
    with pytest.raises(RestartBlocked):
        _guard(tmp_path, base_backoff_seconds=60.0).begin(T0 + timedelta(seconds=5))


def test_no_login_retry_helper_is_exposed():
    """Deliberately absent: there must be nothing here to reach for. A failed
    login has to surface and stop (SHIOAJI_PLAN.md §2.8b)."""
    assert not [a for a in dir(RestartGuard)
                if "login" in a.lower() or "retry" in a.lower()]
