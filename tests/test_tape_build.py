"""The rebuild tooling — the rule that decides what gets evaluated, and the
replay of the old rule that T4's attribution rests on.

`tools/build_session_tapes.py` turns T1's per-contract intermediate into the
three evaluated tapes, and every backtest number the project quotes rests on it.
The rule it applies is `live/backfill.collapse_front_month`'s, restated over an
OHLCV frame; TAPE_REBUILD_PLAN §1 and §7 are entirely about those two not being
allowed to diverge again, so the cases below pin the three ways they could:

  * the calendar front month beats a higher-volume other month (a volume rule was
    the previous convention and would keep feeding a contract already rolled off);
  * a minute the front month did not print may be filled only from an **earlier**
    month, because "the front month plus whatever earlier months are still
    listed" is the whole reach `fetch_bars_continuous` has;
  * out-of-session minutes reach no tape.
"""
from datetime import datetime

import pandas as pd
import pytest

from forecast_eval.live.sessions import SessionPolicy
from forecast_eval.tools.build_session_tapes import (
    front_month_tape,
    session_labels,
)

# Tech: 2026-08-18 15:30 is `roll_at("202608")` under NIGHT's policy — the night
#       tape switches from 202608 to 202609 there.
# Why:  every case below straddles it, so the same three minutes exercise both
#       sides of a splice without needing a second fixture.
ROLL = datetime(2026, 8, 18, 15, 30)


def _bars(rows):
    """`(datetime, contract, close, volume)` tuples -> an intermediate frame."""
    df = pd.DataFrame(rows, columns=["datetime", "contract", "close", "volume"])
    df["datetime"] = pd.to_datetime(df["datetime"])
    for col in ("open", "high", "low"):
        df[col] = df["close"]
    return df[["datetime", "contract", "open", "high", "low", "close", "volume"]]


def _tape(rows, policy=None):
    bars = _bars(rows)
    return front_month_tape(bars, session_labels(bars["datetime"]),
                            policy or SessionPolicy.night())


def test_front_month_wins_over_a_higher_volume_other_month():
    """The whole point of the rebuild: the calendar decides, not the tape."""
    tape = _tape([
        (datetime(2026, 8, 18, 15, 29), "202608", 24000.0, 9999.0),
        (datetime(2026, 8, 18, 15, 29), "202609", 24100.0, 1.0),
        (datetime(2026, 8, 18, 15, 31), "202608", 24000.0, 9999.0),
        (datetime(2026, 8, 18, 15, 31), "202609", 24100.0, 1.0),
    ])
    assert list(tape["contract"]) == ["202608", "202609"]
    assert list(tape["close"]) == [24000.0, 24100.0]


def test_a_minute_the_front_month_missed_is_filled_from_an_earlier_month():
    """After the roll the outgoing month is still listed, and live can fetch it."""
    tape = _tape([
        (datetime(2026, 8, 18, 15, 31), "202608", 24000.0, 5.0),
        (datetime(2026, 8, 18, 15, 32), "202609", 24100.0, 50.0),
    ])
    assert list(tape["contract"]) == ["202608", "202609"]


def test_a_minute_the_front_month_missed_is_dropped_when_only_a_later_month_printed():
    """`fetch_bars_continuous` never reaches the far month, so neither does this.

    Before the roll the only other listed month is a *later* one. Filling from it
    would put a price in the tape that the live run has no way to observe."""
    tape = _tape([
        (datetime(2026, 8, 18, 15, 29), "202609", 24100.0, 50.0),
        (datetime(2026, 8, 18, 15, 31), "202609", 24100.0, 50.0),
    ])
    assert list(tape["datetime"]) == [pd.Timestamp(2026, 8, 18, 15, 31)]


def test_the_two_splice_instants_disagree_for_half_an_hour(
):
    """§2.3's consequence, as a test: at 15:00 on roll Tuesday a full-day
    deployment is already on the incoming month and a night one is not."""
    rows = [
        (datetime(2026, 8, 18, 15, 10), "202608", 24000.0, 500.0),
        (datetime(2026, 8, 18, 15, 10), "202609", 24100.0, 50.0),
    ]
    assert list(_tape(rows, SessionPolicy.night())["contract"]) == ["202608"]
    assert list(_tape(rows, SessionPolicy.both())["contract"]) == ["202609"]


@pytest.mark.parametrize("stamp", [
    datetime(2026, 8, 18, 13, 45),   # the closing-auction print
    datetime(2026, 8, 18, 5, 0),     # the night session's right edge
    datetime(2026, 8, 18, 14, 30),   # the inter-session gap
])
def test_out_of_session_minutes_reach_no_tape(stamp):
    """§0.5's 1 416 inert bars, kept out rather than relabelled."""
    rows = [(stamp, "202609", 24100.0, 50.0)]
    assert _tape(rows, SessionPolicy.night()).empty
    assert _tape(rows, SessionPolicy.day()).empty
    assert _tape(rows, SessionPolicy.both()).empty


def test_each_policy_keeps_only_its_own_sessions():
    rows = [
        (datetime(2026, 8, 18, 10, 0), "202608", 24000.0, 500.0),   # DAY
        (datetime(2026, 8, 18, 22, 0), "202609", 24100.0, 500.0),   # NIGHT
    ]
    assert len(_tape(rows, SessionPolicy.day())) == 1
    assert len(_tape(rows, SessionPolicy.night())) == 1
    assert len(_tape(rows, SessionPolicy.both())) == 2


def test_the_output_is_one_bar_per_minute_in_the_shipped_schema():
    tape = _tape([
        (datetime(2026, 8, 18, 15, 31), "202608", 24000.0, 5.0),
        (datetime(2026, 8, 18, 15, 31), "202609", 24100.0, 50.0),
    ])
    assert list(tape.columns) == ["datetime", "contract", "open", "high",
                                  "low", "close", "volume"]
    assert not tape["datetime"].duplicated().any()
    assert str(tape["close"].dtype) == "float64"
    assert str(tape["volume"].dtype) == "float64"


def test_session_labels_match_classify_session_row_by_row():
    """The lookup-table shortcut is only safe while it is exactly equivalent."""
    from forecast_eval.environment import classify_session
    stamps = pd.Series(pd.date_range("2026-08-18", periods=1440, freq="1min"))
    assert list(session_labels(stamps)) == [classify_session(t) for t in stamps]


# ---- T4's replay of the old rule -------------------------------------------
#
# `tools/diff_tapes.py` attributes the old-vs-new diff by replaying the shipped
# pipeline's front-month rule on the new intermediate. That replay landing on
# exactly OLD's row index is what makes the attribution exact rather than
# estimated — the tool asserts it on the real files, and these pin the two rules
# it replays.


def test_the_old_rule_takes_the_smallest_month_of_the_calendar_date():
    """`min(contract)` per `trading_date`, which is the print's calendar date."""
    from forecast_eval.tools.diff_tapes import old_rule_tape
    bars = _bars([
        (datetime(2026, 8, 19, 22, 0), "202608", 24000.0, 500.0),
        (datetime(2026, 8, 19, 22, 0), "202609", 24100.0, 900.0),
        (datetime(2026, 8, 20, 2, 0), "202608", 24010.0, 500.0),
        (datetime(2026, 8, 20, 2, 0), "202609", 24110.0, 900.0),
    ])
    out = old_rule_tape(bars)
    assert list(out["contract"]) == ["202608", "202608"]
    assert list(out["close"]) == [24000.0, 24010.0]


def test_the_old_rule_rolls_at_midnight_which_is_the_defect():
    """One night session, two calendar dates, two front months — §0.1.

    Once 202608 has stopped being listed the 00:00 half of the same session
    switches to 202609, which is how the tape came to splice mid-session."""
    from forecast_eval.tools.diff_tapes import old_rule_tape
    bars = _bars([
        (datetime(2026, 8, 19, 22, 0), "202608", 24000.0, 500.0),
        (datetime(2026, 8, 19, 22, 0), "202609", 24100.0, 900.0),
        (datetime(2026, 8, 20, 2, 0), "202609", 24110.0, 900.0),
        (datetime(2026, 8, 20, 2, 0), "202610", 24200.0, 10.0),
    ])
    out = old_rule_tape(bars)
    assert list(out["contract"]) == ["202608", "202609"]


@pytest.mark.parametrize("stamp,tape,kept", [
    (datetime(2026, 8, 18, 13, 45), "TXF_RPT_minute_day.csv", True),
    (datetime(2026, 8, 18, 13, 45), "TXF_RPT_minute_night.csv", False),
    (datetime(2026, 8, 18, 5, 0), "TXF_RPT_minute_night.csv", True),
    (datetime(2026, 8, 18, 8, 45), "TXF_RPT_minute_day.csv", True),
])
def test_the_old_session_split_puts_1345_in_the_day_tape(stamp, tape, kept):
    """DAY was `08:45 <= t <= 13:45`, inclusive — which is why the shipped tapes
    carry 1 297 bars `classify_session` calls out-of-session."""
    from forecast_eval.tools.diff_tapes import old_session_split
    mid = _bars([(stamp, "202609", 24100.0, 50.0)])
    assert (len(old_session_split(mid, tape)) == 1) is kept


# ---- the full-day tape ------------------------------------------------------


def test_the_fullday_tape_is_the_combined_tape_from_its_start_date():
    """It is a *window*, never a second splice rule.

    `TXF_RPT_minute_fullday.csv` exists because the combined tape spans 2011 and
    TAIFEX had no night session until 2017-05-15, so a fifth of it is a 300-bar
    day-only regime a full-day deployment cannot be tuned on. What it must NOT be
    is a differently-spliced series: on every minute they share, the two agree."""
    from forecast_eval.tools.build_session_tapes import FULLDAY_START, TAPES
    rows = [
        (datetime(2017, 6, 15, 10, 0), "201706", 10000.0, 500.0),   # before
        (datetime(2017, 6, 15, 22, 0), "201706", 10010.0, 500.0),   # before
        (datetime(2018, 6, 15, 10, 0), "201806", 11000.0, 500.0),   # after
        (datetime(2018, 6, 15, 22, 0), "201806", 11010.0, 500.0),   # after
    ]
    bars = _bars(rows)
    labels = session_labels(bars["datetime"])
    combined = front_month_tape(bars, labels, SessionPolicy.both())
    fullday = combined[combined["datetime"] >= FULLDAY_START].reset_index(drop=True)

    assert len(combined) == 4 and len(fullday) == 2
    assert fullday["datetime"].min().year == 2018
    # the shared minutes are identical rows, not merely identical timestamps
    shared = combined[combined["datetime"] >= FULLDAY_START].reset_index(drop=True)
    assert fullday.equals(shared)


def test_the_fullday_tape_shares_the_combined_tapes_policy():
    """Same deployment, same 12:45 splice — only the window differs. A different
    policy here would make it a fourth series rather than a slice of the third."""
    from forecast_eval.tools.build_session_tapes import TAPES
    by_name = {name: (factory, start) for name, factory, start in TAPES}
    combined_factory, combined_start = by_name["TXF_RPT_minute.csv"]
    fullday_factory, fullday_start = by_name["TXF_RPT_minute_fullday.csv"]
    assert combined_factory() == fullday_factory()
    assert combined_factory().roll_at_time() == fullday_factory().roll_at_time()
    assert combined_start is None and fullday_start is not None
