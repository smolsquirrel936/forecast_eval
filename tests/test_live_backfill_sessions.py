"""Session filtering on the backfill path — parameterized, never hardcoded.

Measured 2026-08-10: `kbars` for TXFR1 returns 00:00-23:59, i.e. both sessions.
The strategy in `results.tsv` was tuned on NIGHT only; a day-session port is
planned. So the session is a *parameter* everywhere, and `fetch_bars` refuses to
assume one — an earlier default of `("DAY",)` silently contradicted the tuned
config and nothing could have detected it.
"""
import pandas as pd
import pytest

from forecast_eval.live.backfill import fetch_bars, kbars_to_bars
from forecast_eval.live.sessions import SessionPolicy


def _payload(times, price=17500.0, volume=5):
    ts = [int(pd.Timestamp(t).value) for t in times]
    n = len(times)
    return {"ts": ts, "Open": [price] * n, "High": [price] * n,
            "Low": [price] * n, "Close": [price] * n, "Volume": [volume] * n}


# Times as **kbars** sends them: right-labelled, so each arrives stamped one
# minute after the interval it describes. Comments show the post-shift label.
TIMES = [
    "2026-05-14 08:46",   # -> 08:45 DAY (day session opens)
    "2026-05-14 12:00",   # -> 11:59 DAY
    "2026-05-14 14:00",   # -> 13:59 between sessions -> None
    "2026-05-14 16:00",   # -> 15:59 NIGHT
    "2026-05-14 23:59",   # -> 23:58 NIGHT
    "2026-05-15 03:00",   # -> 02:59 NIGHT (past midnight, same trading day)
]


def test_no_policy_keeps_every_bar():
    assert len(kbars_to_bars(_payload(TIMES))) == 6


def test_night_policy_keeps_only_night():
    """The configuration results.tsv was actually tuned on."""
    df = kbars_to_bars(_payload(TIMES), policy=SessionPolicy.night())
    assert list(df["timestamp"].dt.strftime("%m-%d %H:%M")) == [
        "05-14 15:59", "05-14 23:58", "05-15 02:59"]


def test_day_policy_keeps_only_day():
    """The planned port. Same code, one argument different."""
    df = kbars_to_bars(_payload(TIMES), policy=SessionPolicy.day())
    assert list(df["timestamp"].dt.strftime("%m-%d %H:%M")) == [
        "05-14 08:45", "05-14 11:59"]


def test_both_policy_keeps_both_sessions():
    df = kbars_to_bars(_payload(TIMES), policy=SessionPolicy.both())
    assert len(df) == 5, "the inter-session gap bar belongs to no session"


def test_inter_session_gap_is_dropped_under_any_policy():
    for policy in (SessionPolicy.night(), SessionPolicy.day(),
                   SessionPolicy.both()):
        assert kbars_to_bars(_payload(["2026-05-14 14:00"]),
                             policy=policy).empty


# ---- fetch_bars requires an explicit session -------------------------------

class _Api:
    def kbars(self, contract, start, end):
        return _payload(TIMES)


def test_fetch_bars_refuses_to_guess_a_session():
    """No default: the wrong one is undetectable once it ships."""
    with pytest.raises(TypeError):
        fetch_bars(_Api(), "TXFR1", "2026-05-14", "2026-05-14")


def test_fetch_bars_honours_the_policy_it_is_given():
    night = fetch_bars(_Api(), "TXFR1", "2026-05-14", "2026-05-14",
                       policy=SessionPolicy.night())
    day = fetch_bars(_Api(), "TXFR1", "2026-05-14", "2026-05-14",
                     policy=SessionPolicy.day())
    assert len(night) == 3 and len(day) == 2
    assert set(night["timestamp"].dt.hour) == {15, 23, 2}
    assert set(day["timestamp"].dt.hour) == {8, 11}


# ---- the shift must precede filtering, for either session ------------------

def test_shift_happens_before_filtering_for_the_day_session():
    """kbars' 13:45 bar is the day session's genuine last minute; unshifted it
    classifies as None (DAY is 08:45 <= t < 13:45) and would be dropped."""
    df = kbars_to_bars(_payload(["2026-05-14 13:45"]),
                       policy=SessionPolicy.day())
    assert list(df["timestamp"].dt.strftime("%H:%M")) == ["13:44"]


def test_shift_happens_before_filtering_for_the_night_session():
    """Symmetrically, kbars' 05:00 bar is the night session's last minute;
    NIGHT is `t >= 15:00 or t < 05:00`, so unshifted it is dropped."""
    df = kbars_to_bars(_payload(["2026-05-15 05:00"]),
                       policy=SessionPolicy.night())
    assert list(df["timestamp"].dt.strftime("%H:%M")) == ["04:59"]


def test_a_full_day_session_survives_the_shift_intact():
    times = pd.date_range("2026-05-14 08:46", "2026-05-14 13:45", freq="1min")
    df = kbars_to_bars(_payload([str(t) for t in times]),
                       policy=SessionPolicy.day())
    assert len(df) == 300
    assert df["timestamp"].iloc[0] == pd.Timestamp("2026-05-14 08:45")
    assert df["timestamp"].iloc[-1] == pd.Timestamp("2026-05-14 13:44")


def test_a_full_night_session_survives_the_shift_intact():
    """15:01..05:00 as kbars sends it -> 840 bars, 15:00..04:59."""
    times = pd.date_range("2026-05-14 15:01", "2026-05-15 05:00", freq="1min")
    df = kbars_to_bars(_payload([str(t) for t in times]),
                       policy=SessionPolicy.night())
    assert len(df) == 840
    assert df["timestamp"].iloc[0] == pd.Timestamp("2026-05-14 15:00")
    assert df["timestamp"].iloc[-1] == pd.Timestamp("2026-05-15 04:59")
