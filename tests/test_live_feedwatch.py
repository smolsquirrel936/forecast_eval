"""FeedWatch — the rail that asks instead of guessing (§2.22).

The bug it replaces is concrete: a 151 s gap between prints at 02:35 tripped a
120 s watchdog and ended a healthy 23-hour soak 31 s before the market traded
again. Those exact numbers are a test case below.

Two properties matter more than the rest, because getting either wrong makes
things worse than the bug did:

  * a QUIET verdict must never resubscribe — a doubled tick stream inflates bar
    volume and silently breaks the bar equality V1 proved;
  * a failed query must never read as a miss — it is evidence about us, not
    about the market.
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

from forecast_eval.live.feedwatch import FeedWatch, Verdict

T0 = datetime(2026, 8, 13, 2, 35, 5)          # the real last print
INTERVAL = 60.0


class _Venue:
    """A snapshot source whose last-trade time the test controls."""

    def __init__(self, ts=None, total_volume=1000, error=None):
        self.ts, self.total_volume, self.error = ts, total_volume, error
        self.n_calls = 0

    def __call__(self):
        self.n_calls += 1
        if self.error is not None:
            raise self.error
        return SimpleNamespace(ts=self.ts, total_volume=self.total_volume)


def _ns(when):
    """The venue's real shape: `Snapshot.ts` is an int of epoch nanoseconds."""
    return int(pd.Timestamp(when).value)


def _watch(venue, **kw):
    resubs = []
    w = FeedWatch(snapshot=venue, resubscribe=lambda: resubs.append(1),
                  interval=INTERVAL, **kw)
    return w, resubs


# ---- the 02:37 halt, replayed ----------------------------------------------

def test_the_gap_that_ended_the_soak_now_reads_as_quiet():
    """REGRESSION (§2.21). Our last print 02:35:05, the venue's last trade the
    same one, 121 s of silence. The old rail halted here. Nothing is wrong."""
    venue = _Venue(ts=T0)                      # venue knows nothing newer
    w, resubs = _watch(venue)
    w.note_tick(T0, T0)

    assert w.check(T0 + timedelta(seconds=121)) is Verdict.QUIET
    assert resubs == [], "a quiet market must never trigger a resubscribe"


def test_a_market_that_traded_without_us_is_a_miss_and_is_repaired():
    """The other side of the same silence: the venue's last trade is newer than
    anything we received, so the stream is broken and gets replaced."""
    venue = _Venue(ts=T0 + timedelta(seconds=90))
    w, resubs = _watch(venue)
    w.note_tick(T0, T0)

    assert w.check(T0 + timedelta(seconds=121)) is Verdict.MISSED
    assert len(resubs) == 1


def test_sub_second_jitter_is_not_a_miss():
    """MEASURED: `snapshot.ts` matched our newest tick exactly 8 times in 9 and
    was 0.8 s *behind* once. A margin below that jitter would resubscribe on
    healthy noise — the one failure mode worse than the bug."""
    venue = _Venue(ts=T0 + timedelta(seconds=0.4))
    w, resubs = _watch(venue)
    w.note_tick(T0, T0)

    assert w.check(T0 + timedelta(seconds=121)) is Verdict.QUIET
    assert resubs == []


# ---- a failed query is not evidence about the market -----------------------

def test_an_unreachable_venue_is_neither_quiet_nor_missed():
    venue = _Venue(error=RuntimeError("timeout"))
    w, resubs = _watch(venue)
    w.note_tick(T0, T0)

    assert w.check(T0 + timedelta(seconds=121)) is Verdict.UNREACHABLE
    assert resubs == [], "acting on a failed query is acting on no information"


def test_a_snapshot_without_a_timestamp_is_unreachable_not_quiet():
    """Defaulting to 'quiet' on a malformed answer would disarm the check
    permanently and silently — the failure would look like a healthy market."""
    w, resubs = _watch(lambda: SimpleNamespace(total_volume=5))
    w.note_tick(T0, T0)
    assert w.check(T0 + timedelta(seconds=121)) is Verdict.UNREACHABLE
    assert resubs == []


# ---- when the check runs at all --------------------------------------------

def test_no_check_before_the_interval_has_elapsed():
    venue = _Venue(ts=T0)
    w, _ = _watch(venue)
    w.note_tick(T0, T0)
    assert w.check(T0 + timedelta(seconds=59)) is None
    assert venue.n_calls == 0, "a query per second would be a quota breach"


def test_no_check_before_any_tick_has_arrived():
    """During warm-up there is nothing to compare against; a check then would
    compare the venue to a null and could only guess."""
    venue = _Venue(ts=T0)
    w, _ = _watch(venue)
    assert w.check(T0 + timedelta(seconds=600)) is None
    assert venue.n_calls == 0


def test_continued_silence_is_rechecked_once_per_interval():
    venue = _Venue(ts=T0)
    w, _ = _watch(venue)
    w.note_tick(T0, T0)
    for i in (61, 90, 121, 150, 181):
        w.check(T0 + timedelta(seconds=i))
    assert venue.n_calls == 3, "one query per interval, not per clock tick"


def test_a_print_rearms_the_interval():
    venue = _Venue(ts=T0)
    w, _ = _watch(venue)
    w.note_tick(T0, T0)
    w.check(T0 + timedelta(seconds=61))
    t1 = T0 + timedelta(seconds=70)
    w.note_tick(t1, t1)
    assert w.check(t1 + timedelta(seconds=30)) is None


# ---- the repair is bounded --------------------------------------------------

def test_resubscribes_are_capped_within_one_silence():
    """A stream that will not come back produces MISSED forever. Unbounded
    unsubscribe/subscribe is a submit storm; after the budget we stop and let the
    rails' staleness backstop halt, which is what a human needs to see."""
    venue = _Venue(ts=T0 + timedelta(seconds=600))
    w, resubs = _watch(venue, max_resubscribes=3)
    w.note_tick(T0, T0)
    for i in range(1, 11):
        w.check(T0 + timedelta(seconds=60 * i + 1))
    assert w.n_missed == 10
    assert len(resubs) == 3


def test_a_recovered_stream_gets_a_fresh_budget():
    venue = _Venue(ts=T0 + timedelta(seconds=600))
    w, resubs = _watch(venue, max_resubscribes=1)
    w.note_tick(T0, T0)
    w.check(T0 + timedelta(seconds=61))
    assert len(resubs) == 1

    t1 = T0 + timedelta(seconds=700)                 # data came back
    w.note_tick(t1, t1)
    venue.ts = t1 + timedelta(seconds=90)            # and broke again later
    w.check(t1 + timedelta(seconds=61))
    assert len(resubs) == 2


def test_a_failing_resubscribe_does_not_kill_the_loop():
    """The repair runs on the consumer thread. An exception escaping here would
    end the process — the outcome the whole module exists to prevent."""
    def _boom():
        raise RuntimeError("unsubscribe failed")

    w = FeedWatch(snapshot=_Venue(ts=T0 + timedelta(seconds=90)),
                  resubscribe=_boom, interval=INTERVAL)
    w.note_tick(T0, T0)
    assert w.check(T0 + timedelta(seconds=121)) is Verdict.MISSED


# ---- the venue's real types (the crash of 2026-08-13) ----------------------

def test_an_integer_nanosecond_ts_is_compared_not_crashed_on():
    """REGRESSION. `Snapshot.ts` is an **int** of epoch nanoseconds while a
    tick's `datetime` is a `datetime`; comparing them raises `TypeError: '>' not
    supported between instances of 'int' and 'datetime.datetime'` and killed a
    live soak 42 bars in.

    The probe that pinned `ts`'s meaning had already converted it this way — the
    conversion never reached the module, and the fakes here handed over
    `datetime`s, so the tests agreed with the assumption instead of the venue."""
    venue = _Venue(ts=_ns(T0))                       # exactly what the SDK sends
    w, resubs = _watch(venue)
    w.note_tick(T0, T0)
    assert w.check(T0 + timedelta(seconds=121)) is Verdict.QUIET
    assert resubs == []


def test_an_integer_ts_can_still_be_a_miss():
    """The conversion must not only avoid the crash — it has to preserve the
    comparison it exists for."""
    venue = _Venue(ts=_ns(T0 + timedelta(seconds=90)))
    w, resubs = _watch(venue)
    w.note_tick(T0, T0)
    assert w.check(T0 + timedelta(seconds=121)) is Verdict.MISSED
    assert len(resubs) == 1


def test_a_wrong_unit_is_refused_rather_than_read_as_quiet():
    """The dangerous failure is not an exception. If `ts` ever arrives in
    seconds, `pd.Timestamp` reads it as nanoseconds and yields a **1970** date —
    which is older than our newest tick, so every check would answer QUIET and
    the rail would be permanently and silently disarmed."""
    venue = _Venue(ts=int(pd.Timestamp(T0).value // 1_000_000_000))   # seconds
    w, resubs = _watch(venue)
    w.note_tick(T0, T0)
    assert w.check(T0 + timedelta(seconds=121)) is Verdict.UNREACHABLE
    assert resubs == []


def test_an_unreadable_ts_is_unreachable_not_a_crash():
    venue = _Venue(ts="not a timestamp at all")
    w, _ = _watch(venue)
    w.note_tick(T0, T0)
    assert w.check(T0 + timedelta(seconds=121)) is Verdict.UNREACHABLE


def test_a_pandas_timestamp_tick_also_works():
    """The CLI passes `tick.datetime`, but a replay hands `pd.Timestamp`. Both
    go through the same normaliser, so neither can reach the comparison raw."""
    venue = _Venue(ts=_ns(T0))
    w, _ = _watch(venue)
    w.note_tick(pd.Timestamp(T0), T0)
    assert w.check(T0 + timedelta(seconds=121)) is Verdict.QUIET


# ---- the trail --------------------------------------------------------------

def test_verdicts_reach_the_recorder():
    import tempfile
    from pathlib import Path

    from forecast_eval.live.recorder import Recorder
    with tempfile.TemporaryDirectory() as d:
        rec = Recorder(Path(d) / "s.jsonl", simulation=True)
        w = FeedWatch(snapshot=_Venue(ts=T0 + timedelta(seconds=90)),
                      resubscribe=lambda: None, interval=INTERVAL, recorder=rec)
        w.note_tick(T0, T0)
        w.check(T0 + timedelta(seconds=121))
        rec.close()
        text = (Path(d) / "s.jsonl").read_text(encoding="utf-8")
    assert '"verdict": "missed"' in text
    assert "feed_resubscribed" in text


def test_the_summary_counts_every_verdict():
    w, _ = _watch(_Venue(ts=T0))
    w.note_tick(T0, T0)
    w.check(T0 + timedelta(seconds=61))
    s = w.summary()
    assert s["feed_checks"] == 1 and s["feed_quiet"] == 1
    assert s["feed_missed"] == 0 and s["feed_resubscribes"] == 0
