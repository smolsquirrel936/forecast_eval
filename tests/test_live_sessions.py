"""SessionPolicy — the one place a deployment names its session.

Exists because the alternative went wrong: `fetch_bars` shipped defaulting to
DAY while the strategy was tuned on NIGHT. These tests hold the line that
nothing session-specific is baked in, and that the night and day configurations
are genuinely symmetric — the day-session port should need one argument changed.
"""
from datetime import datetime, time

import pytest

from forecast_eval.live.bars import TickAggregator
from forecast_eval.live.sessions import SessionPolicy, trading_day
from forecast_eval.live.startup import RestartGuard


def _dt(*a):
    return datetime(*a)


# ---- construction -----------------------------------------------------------

def test_named_constructors():
    assert SessionPolicy.night().sessions == frozenset({"NIGHT"})
    assert SessionPolicy.day().sessions == frozenset({"DAY"})
    assert SessionPolicy.both().sessions == frozenset({"DAY", "NIGHT"})


def test_bars_per_session():
    """DAY_SESSION_PLAN C6 — NIGHT is 14h/840min, DAY is 5h/300min; a `both()`
    policy trades one occurrence of each per calendar day, so it stacks."""
    assert SessionPolicy.night().bars_per_session() == 840
    assert SessionPolicy.day().bars_per_session() == 300
    assert SessionPolicy.both().bars_per_session() == 1140


def test_empty_policy_is_rejected():
    """An empty policy filters every bar away and looks exactly like a dead
    feed — far cheaper to catch here than at 15:00 on a live night."""
    with pytest.raises(ValueError, match="at least one session"):
        SessionPolicy(frozenset())


def test_typo_in_a_session_name_is_rejected():
    with pytest.raises(ValueError, match="unknown sessions"):
        SessionPolicy.of(["NIGTH"])


def test_policy_is_immutable():
    with pytest.raises(Exception):
        SessionPolicy.night().sessions = frozenset({"DAY"})


# ---- membership -------------------------------------------------------------

def test_includes_respects_the_configured_sessions():
    night, day = SessionPolicy.night(), SessionPolicy.day()
    noon, evening = _dt(2026, 5, 14, 12, 0), _dt(2026, 5, 14, 20, 0)
    assert day.includes(noon) and not day.includes(evening)
    assert night.includes(evening) and not night.includes(noon)


def test_inter_session_gap_belongs_to_nobody():
    gap = _dt(2026, 5, 14, 14, 0)
    for p in (SessionPolicy.night(), SessionPolicy.day(), SessionPolicy.both()):
        assert not p.includes(gap)


# ---- trading day ------------------------------------------------------------

def test_night_session_spanning_midnight_is_one_trading_day():
    p = SessionPolicy.night()
    assert p.trading_day(_dt(2026, 5, 14, 15, 0)) == "2026-05-14"
    assert p.trading_day(_dt(2026, 5, 14, 23, 59)) == "2026-05-14"
    assert p.trading_day(_dt(2026, 5, 15, 4, 59)) == "2026-05-14"


def test_day_session_sits_inside_its_own_date():
    p = SessionPolicy.day()
    assert p.trading_day(_dt(2026, 5, 14, 8, 45)) == "2026-05-14"
    assert p.trading_day(_dt(2026, 5, 14, 13, 44)) == "2026-05-14"


def test_boundary_is_configurable():
    p = SessionPolicy.night(trading_day_start=time(6, 0))
    assert p.trading_day(_dt(2026, 5, 15, 5, 0)) == "2026-05-14"
    assert p.trading_day(_dt(2026, 5, 15, 6, 0)) == "2026-05-15"


def test_restart_guard_shares_the_same_rule():
    """One boundary governs the loss limit and the backfill cap; two
    implementations is how they drift apart."""
    for ts in (_dt(2026, 5, 14, 15, 0), _dt(2026, 5, 15, 3, 0),
               _dt(2026, 5, 15, 9, 0)):
        assert RestartGuard.trading_day(ts) == trading_day(ts)


# ---- session instances ------------------------------------------------------

def test_same_night_across_midnight_is_one_instance():
    p = SessionPolicy.night()
    assert p.same_session_instance(_dt(2026, 5, 14, 23, 58),
                                   _dt(2026, 5, 15, 0, 30))


def test_two_consecutive_nights_are_different_instances():
    """Both are labelled NIGHT; only the trading day tells them apart."""
    p = SessionPolicy.night()
    assert not p.same_session_instance(_dt(2026, 5, 15, 4, 59),
                                       _dt(2026, 5, 15, 15, 1))


def test_out_of_session_timestamps_are_never_the_same_instance():
    p = SessionPolicy.night()
    assert not p.same_session_instance(_dt(2026, 5, 14, 20, 0),
                                       _dt(2026, 5, 15, 9, 0))


# ---- the tick aggregator honours the policy --------------------------------

def _feed(agg, times, price=100.0):
    out = []
    for t in times:
        out.extend(agg.add_tick(t, price))
    out.extend(agg.flush(times[-1].replace(hour=23, minute=59)))
    return out


def test_aggregator_emits_only_the_policy_sessions():
    times = [_dt(2026, 5, 14, 12, 0, 5), _dt(2026, 5, 14, 12, 1, 5),
             _dt(2026, 5, 14, 20, 0, 5), _dt(2026, 5, 14, 20, 1, 5)]

    night = _feed(TickAggregator(policy=SessionPolicy.night()), times)
    assert [b.session for b in night] == ["NIGHT"] * len(night)
    assert all(b.timestamp.hour == 20 for b in night)

    day = _feed(TickAggregator(policy=SessionPolicy.day()), times)
    assert [b.session for b in day] == ["DAY"] * len(day)
    assert all(b.timestamp.hour == 12 for b in day)


def test_aggregator_defaults_to_both_sessions():
    times = [_dt(2026, 5, 14, 12, 0, 5), _dt(2026, 5, 14, 12, 1, 5),
             _dt(2026, 5, 14, 20, 0, 5), _dt(2026, 5, 14, 20, 1, 5)]
    bars = _feed(TickAggregator(), times)
    assert {b.session for b in bars} == {"DAY", "NIGHT"}


def test_filtered_bars_are_counted_not_silently_lost():
    agg = TickAggregator(policy=SessionPolicy.night())
    _feed(agg, [_dt(2026, 5, 14, 12, 0, 5), _dt(2026, 5, 14, 12, 1, 5)])
    assert agg.n_bars_dropped_off_session > 0


# ---- trading days (§2.25) ---------------------------------------------------

from datetime import date as _date  # noqa: E402


def test_a_weekend_is_not_a_trading_day():
    """The bug: `classify_session` reads time-of-day only, so Saturday 15:00 was
    a NIGHT session to every consumer and the staleness backstop halted a soak
    at Saturday 15:16."""
    p = SessionPolicy.night()
    assert p.is_trading_day(datetime(2026, 8, 14, 20, 0)) is True    # Friday
    assert p.is_trading_day(datetime(2026, 8, 15, 20, 0)) is False   # Saturday
    assert p.is_trading_day(datetime(2026, 8, 16, 20, 0)) is False   # Sunday
    assert p.is_trading_day(datetime(2026, 8, 17, 20, 0)) is True    # Monday


def test_a_night_session_that_crosses_into_saturday_is_still_trading():
    """The case a raw `ts.weekday()` gets backwards. Friday's night session runs
    to 05:00 Saturday; treating 02:00 Saturday as a weekend would halt the loop
    mid-session, every Saturday morning."""
    p = SessionPolicy.night()
    saturday_small_hours = datetime(2026, 8, 15, 2, 0)
    assert saturday_small_hours.weekday() == 5          # genuinely a Saturday
    assert p.is_trading_day(saturday_small_hours) is True
    assert p.includes(saturday_small_hours) is True


def test_saturday_afternoon_opens_no_session():
    p = SessionPolicy.night()
    assert p.includes(datetime(2026, 8, 15, 15, 16)) is False
    assert p.includes(datetime(2026, 8, 15, 20, 0)) is False


def test_holidays_are_data_not_code():
    """Empty by default — the weekday test is wrong on ~10-15 weekday market
    holidays a year, and carrying them as a set means closing that gap is a
    list rather than a redesign."""
    plain = SessionPolicy.night()
    assert plain.holidays == frozenset()
    assert plain.includes(datetime(2026, 8, 17, 20, 0)) is True

    closed = SessionPolicy(frozenset({"NIGHT"}),
                           holidays=frozenset({_date(2026, 8, 17)}))
    assert closed.is_trading_day(datetime(2026, 8, 17, 20, 0)) is False
    assert closed.includes(datetime(2026, 8, 17, 20, 0)) is False


def test_the_trading_day_mapping_still_holds_across_the_weekend():
    """`trading_day` is a date mapping and must stay unaffected — RestartGuard
    reads it before any policy exists."""
    p = SessionPolicy.night()
    assert p.trading_day(datetime(2026, 8, 15, 2, 0)) == "2026-08-14"
