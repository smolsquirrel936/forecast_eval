"""The tape-spanning parity check's own fidelity — the three ways it could lie.

`tools/roll_parity.py` compares the shipped tape against the series the live
warm-up path would have built, and `TAPE_REBUILD_PLAN.md` §5.3 rests a live-path
defect on the answer. That makes the *live* side the load-bearing part: it has to
model what `live/cli.py` really does, not what it ought to do.

Three properties carry it, and each one is a way the check could be made to pass
by accident — the last of them by someone repairing the very defect it found:

  * the reach is "front month plus earlier", so a minute only a *later* month
    printed is out of reach and drops, while an *earlier* month fills;
  * the splice instant is `front_month_at` **bound to the deployment's policy**,
    because that is the argument `live/cli.py:645` passes — it passed the bare
    function until 2026-09-01, and that omission was the defect this check found;
  * out-of-session minutes never reach the loop, because `TickAggregator` drops
    them first.

`compare` is covered too, for the distinction the counts exist to make: a hole
and a month-swap are different failures with different causes.
"""
import pandas as pd
import pytest

from forecast_eval.live.sessions import SessionPolicy
from forecast_eval.tools.roll_parity import compare, live_series

# 2026-08-18 is the roll day for delivery month 202608 — 12:45 under DAY's policy,
# 15:30 under NIGHT's. The hour between those two instants is the whole finding,
# so every case below is placed relative to it rather than to an arbitrary date.


def _bars(rows):
    """`(datetime, contract, close, volume)` tuples -> an intermediate frame."""
    df = pd.DataFrame(rows, columns=["datetime", "contract", "close", "volume"])
    df["datetime"] = pd.to_datetime(df["datetime"])
    return df


def test_the_live_side_splices_on_the_deployments_own_instant():
    """The finding, and now the fix. At 13:00 on the roll day the DAY tape has
    moved to 202609, and so does the live warm-up — because `live/cli.py:645`
    binds `cfg.policy` into the argument it passes.

    Until 2026-09-01 it passed the bare function, whose splice was NIGHT's 15:30
    for every deployment, and this case asserted `202608`: 183 of 183 DAY rolls
    that traded (TAPE_REBUILD_PLAN 5.3). Both halves are pinned below, because
    "DAY follows 12:45" is only meaningful beside "NIGHT still follows 15:30" —
    that equality is what makes requiring the policy a no-op for the deployment
    that is soaking."""
    rows = [
        ("2026-08-18 13:00", "202608", 24000.0, 500.0),
        ("2026-08-18 13:00", "202609", 24100.0, 900.0),
    ]
    day = live_series(_bars(rows), SessionPolicy.day())
    assert list(day["contract"]) == ["202609"]
    assert list(day["price"]) == [24100.0]

    # the same minute is out of session for NIGHT, so ask on one that is not:
    # 15:29 is still 202608 under 15:30, and 15:31 has rolled.
    night_rows = [("2026-08-18 15:29", "202608", 24000.0, 500.0),
                  ("2026-08-18 15:29", "202609", 24100.0, 900.0),
                  ("2026-08-18 15:31", "202608", 24000.0, 500.0),
                  ("2026-08-18 15:31", "202609", 24100.0, 900.0)]
    night = live_series(_bars(night_rows), SessionPolicy.night())
    assert list(night["contract"]) == ["202608", "202609"]


def test_a_minute_only_a_later_month_printed_is_out_of_reach():
    """`_warmup_contracts` returns the listed months *up to* the front one, so the
    far month is not fetchable and the minute has no bar at all — which is what
    makes the combined tape's 8 tape-only minutes a hole rather than a relabel.

    12:44 is a minute before DAY's own 12:45 splice, so 202609 is still the far
    month here and nothing can be built from it."""
    got = live_series(_bars([
        ("2026-08-18 12:44", "202609", 24100.0, 900.0),
    ]), SessionPolicy.day())
    assert got.empty


def test_a_minute_the_front_month_missed_is_filled_from_an_earlier_month():
    """The fallback the live path really has: after 15:30 the front month is
    202609, and a minute only 202608 printed is better filled than dropped."""
    got = live_series(_bars([
        ("2026-08-18 16:00", "202608", 24000.0, 500.0),
    ]), SessionPolicy.night())
    assert list(got["contract"]) == ["202608"]


def test_the_front_month_wins_over_a_higher_volume_other_month():
    """Volume is the tie-break, never the rule — the property `collapse_front_month`
    exists for, asserted here because this check would silently agree with a
    volume-spliced tape if it were lost."""
    got = live_series(_bars([
        ("2026-08-18 16:00", "202608", 24000.0, 9999.0),
        ("2026-08-18 16:00", "202609", 24100.0, 1.0),
    ]), SessionPolicy.night())
    assert list(got["contract"]) == ["202609"]


@pytest.mark.parametrize("stamp", ["2026-08-18 13:45", "2026-08-19 05:00"])
def test_out_of_session_minutes_never_reach_the_live_series(stamp):
    """`TickAggregator` drops them live and the tape never had them, so a live
    side that kept them would report an extra minute at every boundary."""
    got = live_series(_bars([(stamp, "202609", 24000.0, 500.0)]),
                      SessionPolicy.night())
    assert got.empty


def test_compare_counts_a_hole_and_a_month_swap_apart():
    """The two failures have different causes — a missing minute is §0.2's hole,
    a swapped month is a splice-instant disagreement — so one summed number would
    hide which one a roll actually has."""
    tape = pd.DataFrame({
        "datetime": pd.to_datetime(["2026-08-18 13:00", "2026-08-18 13:01"]),
        "contract": ["202609", "202609"],
        "close": [24100.0, 24110.0],
    })
    live = pd.DataFrame({
        "timestamp": pd.to_datetime(["2026-08-18 13:00"]),
        "price": [24000.0],
        "contract": ["202608"],
    })
    got = compare(tape, live)
    assert got["only_tape"] == 1          # 13:01 — the live path has no bar
    assert got["only_live"] == 0
    assert got["diff_contract"] == 1      # 13:00 — same minute, other month
    assert got["diff_close"] == 1
    assert got["max_abs_dclose"] == 100.0


def test_compare_reports_a_clean_window_as_clean():
    """The NIGHT result is 112 of these, so the passing case has to be a real
    assertion rather than the absence of a failing one."""
    stamps = pd.to_datetime(["2026-08-18 16:00", "2026-08-18 16:01"])
    tape = pd.DataFrame({"datetime": stamps, "contract": ["202609"] * 2,
                         "close": [24100.0, 24110.0]})
    live = pd.DataFrame({"timestamp": stamps, "price": [24100.0, 24110.0],
                         "contract": ["202609"] * 2})
    got = compare(tape, live)
    assert (got["only_tape"], got["only_live"]) == (0, 0)
    assert (got["diff_contract"], got["diff_close"]) == (0, 0)
