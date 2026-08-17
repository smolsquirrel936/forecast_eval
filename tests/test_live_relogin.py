"""ReloginScheduler — the token must never reach 24 hours old.

The soak these tests exist because of died at 24 h 0 m 36 s on an expired session
token (SHIOAJI_PLAN.md §2.24). The scheduler's whole job is that the token is
replaced before that, in a window where the tape carries only trial prints — so
the tests care about two things: that anchors fire in the quiet and nowhere else,
and that no sequence of clock ticks lets token age reach the wall.
"""
from datetime import date, datetime, time, timedelta

import pytest

from forecast_eval.live.relogin import (
    Outcome,
    ReloginScheduler,
    session_open_anchors,
)


class Recorder:
    def __init__(self):
        self.events = []

    def record(self, event, **fields):
        self.events.append((event, fields))

    def kinds(self):
        return [e for e, _ in self.events]


def _sched(**kw):
    """A scheduler whose re-login always succeeds, counting calls."""
    calls = []
    kw.setdefault("relogin", lambda: calls.append(1))
    s = ReloginScheduler(**kw)
    s.calls = calls
    return s


def _tick(sched, start, end, step_seconds=60):
    """Drive the scheduler across a span, returning the outcomes it produced."""
    out = []
    now = start
    while now < end:
        result = sched.check(now)
        if result is not None:
            out.append((now, result))
        now += timedelta(seconds=step_seconds)
    return out


MON = datetime(2026, 8, 17)          # a Monday


# ---- anchors ---------------------------------------------------------------


def test_anchors_are_five_and_one_minutes_before_each_session_open():
    """(re-login, verify, open), derived from DAY/NIGHT rather than written."""
    assert session_open_anchors() == (
        (time(8, 40), time(8, 44), time(8, 45)),
        (time(14, 55), time(14, 59), time(15, 0)),
    )


def test_anchor_leads_are_configurable():
    anchors = session_open_anchors(timedelta(minutes=15), timedelta(minutes=2))
    assert anchors[0][:2] == (time(8, 30), time(8, 43))


def test_the_two_windows_are_adjacent_and_never_overlap():
    """A retry must never still be in flight when the health check runs."""
    for relogin_at, verify_at, open_at in session_open_anchors():
        assert relogin_at < verify_at < open_at


# ---- firing ----------------------------------------------------------------


def test_fires_at_each_anchor_once_per_day():
    s = _sched()
    s.note_login(MON - timedelta(hours=12))
    fired = _tick(s, MON, MON + timedelta(days=1))
    assert [f[1] for f in fired] == [Outcome.RELOGGED, Outcome.RELOGGED]
    assert [f[0].time() for f in fired] == [time(8, 40), time(14, 55)]
    assert len(s.calls) == 2


def test_does_not_fire_twice_inside_one_window():
    """A 1 Hz caller must not re-login every second of the five-minute window."""
    s = _sched()
    s.note_login(MON - timedelta(hours=12))
    fired = _tick(s, MON.replace(hour=8, minute=40),
                  MON.replace(hour=8, minute=45), step_seconds=1)
    assert len(fired) == 1


def test_window_closes_at_the_session_open():
    """Nothing fires once real prints start — the point of the whole design."""
    s = _sched()
    s.note_login(MON - timedelta(hours=12))
    fired = _tick(s, MON.replace(hour=8, minute=45),
                  MON.replace(hour=14, minute=0))
    assert fired == []
    assert s.calls == []


def test_a_young_token_still_spends_its_anchor():
    """No age floor, deliberately.

    An earlier draft skipped an anchor when the token was under two hours old,
    to avoid a "wasted" login. That floor stranded the first anchor for a
    process started between 13:00 and 15:00 and pushed the age ceiling from
    17 h 45 m to 18 h 36 m — caught by the week-long property test. A login
    costs 2 of a 1 000/day cap; the floor bought nothing and cost margin.
    """
    s = _sched()
    s.note_login(MON.replace(hour=14, minute=50))
    fired = _tick(s, MON.replace(hour=14, minute=50),
                  MON.replace(hour=15, minute=0))
    assert [f[1] for f in fired] == [Outcome.RELOGGED]


def test_no_check_before_a_login_is_recorded():
    s = _sched()
    assert s.check(MON.replace(hour=8, minute=41)) is None
    assert s.calls == []


# ---- failure and backoff ---------------------------------------------------


def test_failure_backs_off_and_retries_within_the_window():
    boom = [True, True, False]          # fail, fail, then succeed
    calls = []

    def relogin():
        calls.append(1)
        if boom[len(calls) - 1]:
            raise RuntimeError("network")

    s = ReloginScheduler(relogin=relogin, recorder=(rec := Recorder()))
    s.note_login(MON - timedelta(hours=12))
    fired = _tick(s, MON.replace(hour=8, minute=40),
                  MON.replace(hour=8, minute=45), step_seconds=1)

    assert [f[1] for f in fired] == [
        Outcome.FAILED, Outcome.FAILED, Outcome.RELOGGED]
    # 60 s then 120 s of backoff, so the successful attempt lands at 08:43:00.
    assert [f[0].time() for f in fired] == [
        time(8, 40), time(8, 41), time(8, 43)]
    assert s.n_relogins == 1 and s.n_failures == 2
    assert "relogin_failed" in rec.kinds()


def test_the_window_bounds_attempts_not_a_counter():
    """§2.8b — a retry every second is the login storm that extends a ban.

    With 60 s exponential backoff inside the four-minute re-login window the
    attempts land at +0 s, +60 s and +180 s; the fourth would be +420 s, past
    the verify window. Three, structurally, with no attempt counter to tune.
    """
    calls = []

    def relogin():
        calls.append(1)
        raise RuntimeError("down")

    s = ReloginScheduler(relogin=relogin)
    s.note_login(MON - timedelta(hours=12))
    fired = _tick(s, MON.replace(hour=8, minute=40),
                  MON.replace(hour=8, minute=45), step_seconds=1)

    assert len(calls) == 3                       # not 300
    assert [f[0].time() for f in fired if f[1] is Outcome.FAILED] == [
        time(8, 40), time(8, 41), time(8, 43)]
    # The window hands over to verify at 08:44, which settles it.
    assert fired[-1] == (MON.replace(hour=8, minute=44), Outcome.ABANDONED)


def test_a_window_that_closes_unresolved_abandons_the_anchor():
    calls = []

    def relogin():
        calls.append(1)
        raise RuntimeError("down")

    s = ReloginScheduler(relogin=relogin, recorder=(rec := Recorder()))
    s.note_login(MON - timedelta(hours=12))
    fired = _tick(s, MON, MON + timedelta(days=1), step_seconds=1)

    assert [f[1] for f in fired].count(Outcome.ABANDONED) == 2   # one per anchor
    assert s.n_abandoned == 2
    assert len(calls) == 6                       # 3 per anchor, not more
    assert "relogin_abandoned" in rec.kinds()


def test_retry_state_does_not_leak_into_the_next_anchor():
    """The bug the first draft had: a window closing mid-retry left `_attempts`
    and the backoff deadline set, so the next anchor inherited a spent budget
    and a deadline from yesterday."""
    calls = []

    def relogin():
        calls.append(1)
        if len(calls) <= 3:                      # only the 08:40 anchor fails
            raise RuntimeError("down")

    s = ReloginScheduler(relogin=relogin)
    s.note_login(MON - timedelta(hours=12))
    fired = _tick(s, MON, MON + timedelta(days=1), step_seconds=1)

    assert fired[-1][1] is Outcome.RELOGGED
    assert s.n_relogins == 1 and s.n_abandoned == 1
    # The 14:55 anchor got a clean first attempt, not a fourth one.
    assert s._attempts == 0 and s._next_attempt is None


def test_backoff_is_capped():
    """A base long enough to overrun the window still yields one attempt."""
    s = ReloginScheduler(relogin=lambda: (_ for _ in ()).throw(RuntimeError()),
                         base_backoff_seconds=600.0, max_backoff_seconds=900.0)
    s.note_login(MON - timedelta(hours=12))
    fired = _tick(s, MON.replace(hour=8, minute=40),
                  MON.replace(hour=8, minute=45), step_seconds=1)
    assert [f[1] for f in fired] == [Outcome.FAILED, Outcome.ABANDONED]


# ---- the verify window -----------------------------------------------------


def test_verify_runs_once_a_minute_before_each_open():
    seen = []
    s = ReloginScheduler(relogin=lambda: None, verify=lambda: seen.append(1))
    s.note_login(MON - timedelta(hours=12))
    fired = _tick(s, MON, MON + timedelta(days=1), step_seconds=1)

    verified = [f for f in fired if f[1] is Outcome.VERIFIED]
    assert [f[0].time() for f in verified] == [time(8, 44), time(14, 59)]
    assert len(seen) == 2


def test_verify_is_skipped_when_no_check_is_injected():
    """`verify=None` disables the window rather than inventing a check."""
    s = _sched()
    s.note_login(MON - timedelta(hours=12))
    fired = _tick(s, MON, MON + timedelta(days=1), step_seconds=1)
    assert not [f for f in fired if f[1] in
                (Outcome.VERIFIED, Outcome.VERIFY_FAILED)]


def test_a_failed_verify_takes_one_recovery_relogin():
    """The point of the window: catch a dead session while it is still free."""
    logins = []
    s = ReloginScheduler(
        relogin=lambda: logins.append(1),
        verify=lambda: (_ for _ in ()).throw(RuntimeError("401 expired")),
        recorder=(rec := Recorder()),
    )
    s.note_login(MON - timedelta(hours=12))
    fired = _tick(s, MON.replace(hour=8, minute=44),
                  MON.replace(hour=8, minute=45), step_seconds=1)

    assert [f[1] for f in fired] == [Outcome.RELOGGED]
    assert len(logins) == 1
    assert "verify_failed" in rec.kinds()
    assert "relogin_after_failed_verify" in rec.kinds()


def test_a_failed_verify_and_failed_recovery_reports_verify_failed():
    def boom():
        raise RuntimeError("down")

    s = ReloginScheduler(relogin=boom, verify=boom, recorder=(rec := Recorder()))
    s.note_login(MON - timedelta(hours=12))
    fired = _tick(s, MON.replace(hour=8, minute=44),
                  MON.replace(hour=8, minute=45), step_seconds=1)

    assert [f[1] for f in fired] == [Outcome.VERIFY_FAILED]
    assert s.n_verify_failures == 1
    assert "verify_recovery_failed" in rec.kinds()


def test_verify_gets_one_shot_not_a_retry_ladder():
    """A check still retrying when the session opens has failed at its job."""
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("down")

    s = ReloginScheduler(relogin=lambda: None, verify=boom)
    s.note_login(MON - timedelta(hours=12))
    _tick(s, MON.replace(hour=8, minute=44),
          MON.replace(hour=8, minute=45), step_seconds=1)
    assert len(calls) == 1


def test_verify_backs_up_an_abandoned_relogin_window():
    """The case the backstop was rejected in favour of: every re-login attempt
    fails, and one minute before the open the health check recovers it."""
    n = [0]

    def relogin():
        n[0] += 1
        if n[0] <= 3:                     # the three windowed attempts fail
            raise RuntimeError("blip")

    s = ReloginScheduler(
        relogin=relogin,
        verify=lambda: (_ for _ in ()).throw(RuntimeError("401 expired")),
    )
    s.note_login(MON - timedelta(hours=12))
    fired = _tick(s, MON.replace(hour=8, minute=40),
                  MON.replace(hour=8, minute=45), step_seconds=1)

    kinds = [f[1] for f in fired]
    assert kinds.count(Outcome.FAILED) == 3
    assert Outcome.ABANDONED in kinds
    assert kinds[-1] is Outcome.RELOGGED     # recovered in the verify window
    assert s.n_relogins == 1


def test_verify_records_the_token_age_it_saw():
    s = ReloginScheduler(relogin=lambda: None, verify=lambda: None,
                         recorder=(rec := Recorder()))
    s.note_login(MON.replace(hour=8, minute=44) - timedelta(hours=6))
    _tick(s, MON.replace(hour=8, minute=44),
          MON.replace(hour=8, minute=45), step_seconds=1)
    ages = [f["token_age_seconds"] for e, f in rec.events if e == "verified"]
    assert ages == [6 * 3600.0]


# ---- the invariant ---------------------------------------------------------


@pytest.mark.parametrize("start_hour", list(range(24)))
def test_token_age_never_reaches_24h_over_a_week(start_hour):
    """The property the whole module exists for, from every start hour.

    §2.24: the token dies at 24 h. Anchors at 08:40 and 14:55 are 6 h 15 m and
    17 h 45 m apart, so the age ceiling is 17 h 45 m — but only if no start time
    strands the first anchor.
    """
    s = _sched()
    start = datetime(2026, 8, 17, start_hour, 3)
    s.note_login(start)
    now = start
    worst = timedelta(0)
    end = start + timedelta(days=7)
    while now < end:
        s.check(now)
        worst = max(worst, s.token_age(now))
        now += timedelta(minutes=1)
    assert worst < timedelta(hours=24), f"token reached {worst}"
    assert worst <= timedelta(hours=17, minutes=46), f"ceiling slipped: {worst}"


def test_a_week_of_failing_logins_is_bounded_in_attempts():
    """The pathological case: the venue refuses all week. Attempts stay linear."""
    calls = []

    def relogin():
        calls.append(1)
        raise RuntimeError("refused")

    s = ReloginScheduler(relogin=relogin)
    s.note_login(MON)
    now = MON
    while now < MON + timedelta(days=7):
        s.check(now)
        now += timedelta(seconds=1)
    # 2 anchors/day * 3 attempts * 7 days — 42, three orders of magnitude under
    # the 1 000/day login cap of §2.8b.
    assert len(calls) == 42


def test_a_lost_window_still_reaches_the_wall_when_verify_fails_too():
    """THE REMAINING GAP, documented rather than hidden (§2.24).

    Anchors are 6 h 15 m and 17 h 45 m apart, so losing one entirely means the
    next successful login is 24 h after the last — exactly the expiry. The
    verify window is what makes this need **two** independent failures (every
    re-login attempt *and* the health check) instead of one; an age-based
    backstop was rejected in its favour, because a backstop fires on a clock
    reading mid-session while this fires on evidence, in the quiet.
    """
    n = [0]

    def relogin():
        # All three attempts of the first window (Tue 08:40) fail, so it is
        # abandoned; everything after succeeds.
        n[0] += 1
        if n[0] <= 3:
            raise RuntimeError("venue down")

    # The health check fails too — otherwise 08:44 would recover it.
    s = ReloginScheduler(
        relogin=relogin,
        verify=lambda: (_ for _ in ()).throw(RuntimeError("unreachable")),
    )
    # Token issued at Monday's 14:55 anchor; the clock starts just after that
    # window closes, so the first anchor the scheduler meets is Tuesday 08:40.
    s.note_login(MON.replace(hour=14, minute=55))
    start = MON.replace(hour=15, minute=1)
    now, worst = start, timedelta(0)
    while now < start + timedelta(days=3):
        s.check(now)
        worst = max(worst, s.token_age(now))
        now += timedelta(minutes=1)

    assert s.n_abandoned == 1
    # The gap between successful logins is exactly 24 h; sampled on a one-minute
    # grid the age peaks at 23:59 immediately before the replacement lands. A
    # dead heat with the expiry, against a 17 h 45 m ceiling when no anchor is
    # lost.
    assert worst >= timedelta(hours=23, minutes=30), (
        "a lost anchor no longer reaches the wall — if a backstop was added, "
        "this test should be replaced by one asserting the new ceiling")


# ---- observation -----------------------------------------------------------


def test_summary_counts_are_reported():
    s = ReloginScheduler(relogin=lambda: None, verify=lambda: None)
    s.note_login(MON - timedelta(hours=12))
    _tick(s, MON, MON + timedelta(days=1), step_seconds=1)
    assert s.summary() == {
        "relogins": 2, "relogin_failures": 0, "relogin_abandoned": 0,
        "session_verified": 2, "session_verify_failures": 0}


def test_recorder_is_optional():
    s = _sched(recorder=None)
    s.note_login(MON - timedelta(hours=12))
    assert len(_tick(s, MON, MON + timedelta(days=1))) == 2
