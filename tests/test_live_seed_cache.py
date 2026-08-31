"""The offline cache seeder (DAY_SESSION_PLAN C7 / W3).

A first DAY run cannot fetch its own warm-up: expired months are delisted and
`_earlier_months` reaches back only one month, so ~2 of the ~3 months C6 needs
are unavailable from the venue. The seeder fills the cache from the RPT tape
instead. What these tests hold down is that a seeded cache is something
`warm_up` will actually accept -- an unfiltered seed would make every start-up
raise rather than merely be untidy.
"""
import pandas as pd
import pytest

from forecast_eval.live.backfill import BAR_COLUMNS, load_bar_cache
from forecast_eval.live.seed_cache import rpt_to_cache_frame, main
from forecast_eval.live.sessions import SessionPolicy


def _rpt(tmp_path, rows):
    """Write a minimal RPT-schema minute file."""
    p = tmp_path / "rpt.csv"
    pd.DataFrame(rows, columns=["datetime", "contract", "open", "high", "low",
                                "close", "volume"]).to_csv(p, index=False)
    return p


def _session_rows(day, times, contract="202609", close=17500.0):
    return [(f"{day} {t}", contract, close, close, close, close, 100.0)
            for t in times]


def test_only_the_policys_own_bars_survive(tmp_path):
    """The whole point: `runtime.warm_up` raises on any bar outside the policy's
    sessions, so a seed that carries both is a start-up failure, not a nuisance."""
    rows = (_session_rows("2026-08-18", ["09:00:00", "10:00:00", "13:00:00"])
            + _session_rows("2026-08-18", ["16:00:00", "20:00:00"]))
    out = rpt_to_cache_frame(_rpt(tmp_path, rows), SessionPolicy.day())
    assert len(out) == 3
    assert all(SessionPolicy.day().includes(t) for t in out["timestamp"])


def test_the_night_policy_is_the_mirror_image(tmp_path):
    rows = (_session_rows("2026-08-18", ["09:00:00", "10:00:00"])
            + _session_rows("2026-08-18", ["16:00:00", "20:00:00", "23:00:00"]))
    out = rpt_to_cache_frame(_rpt(tmp_path, rows), SessionPolicy.night())
    assert len(out) == 3
    assert all(SessionPolicy.night().includes(t) for t in out["timestamp"])


def test_the_frame_is_exactly_the_cache_schema(tmp_path):
    rows = _session_rows("2026-08-18", ["09:00:00", "09:01:00"])
    out = rpt_to_cache_frame(_rpt(tmp_path, rows), SessionPolicy.day())
    assert list(out.columns) == BAR_COLUMNS


def test_price_is_the_close_not_the_open(tmp_path):
    """`load_minute_bars` uses the close as the tick price; a seeded bar that
    used any other field would be a different series from the evaluated one."""
    p = tmp_path / "rpt.csv"
    pd.DataFrame([("2026-08-18 09:00:00", "202609", 1.0, 9.0, 0.5, 7.25, 42.0)],
                 columns=["datetime", "contract", "open", "high", "low",
                          "close", "volume"]).to_csv(p, index=False)
    out = rpt_to_cache_frame(p, SessionPolicy.day())
    assert out["price"].iloc[0] == 7.25
    assert out["volume"].iloc[0] == 42.0


def test_nan_closes_are_dropped(tmp_path):
    """Toto2 propagates NaN, so a gap in the seed poisons the context it exists
    to provide."""
    p = tmp_path / "rpt.csv"
    pd.DataFrame([("2026-08-18 09:00:00", "202609", 1.0, 1.0, 1.0, None, 5.0),
                  ("2026-08-18 09:01:00", "202609", 1.0, 1.0, 1.0, 17500.0, 5.0)],
                 columns=["datetime", "contract", "open", "high", "low",
                          "close", "volume"]).to_csv(p, index=False)
    out = rpt_to_cache_frame(p, SessionPolicy.day())
    assert len(out) == 1


def test_the_contract_month_survives_as_text(tmp_path):
    """`load_bar_cache` is explicit that a delivery month must not become an
    int64 -- the splice rule compares it as a string."""
    rows = _session_rows("2026-08-18", ["09:00:00"], contract="202609")
    out = rpt_to_cache_frame(_rpt(tmp_path, rows), SessionPolicy.day())
    assert out["contract"].iloc[0] == "202609"


def test_keep_trims_from_the_tail(tmp_path):
    """The newest bars are the ones worth keeping -- the cache exists to carry
    the month that is about to be delisted."""
    times = ["09:%02d:00" % m for m in range(10)]
    out = rpt_to_cache_frame(_rpt(tmp_path, _session_rows("2026-08-18", times)),
                             SessionPolicy.day(), keep=3)
    assert len(out) == 3
    assert out["timestamp"].iloc[-1] == pd.Timestamp("2026-08-18 09:09:00")


def test_until_makes_the_seed_reproducible(tmp_path):
    times = ["09:00:00", "09:01:00", "09:02:00"]
    out = rpt_to_cache_frame(_rpt(tmp_path, _session_rows("2026-08-18", times)),
                             SessionPolicy.day(), until="2026-08-18 09:01:00")
    assert len(out) == 2


def test_a_seeded_file_round_trips_through_load_bar_cache(tmp_path):
    """The seeder writes what the live loader reads -- not merely a similar CSV."""
    rows = _session_rows("2026-08-18", ["09:00:00", "09:01:00"])
    src = _rpt(tmp_path, rows)
    dst = tmp_path / "bars_cache_day.csv"
    assert main(["--data", str(src), "--out", str(dst), "--session", "day"]) == 0
    back = load_bar_cache(dst)
    assert list(back.columns) == BAR_COLUMNS
    assert len(back) == 2
    assert back["contract"].iloc[0] == "202609"


def test_a_seed_with_no_matching_session_fails_loudly(tmp_path):
    """Pointing the day seeder at the night file is a plausible slip; it must
    not write an empty cache and report success."""
    rows = _session_rows("2026-08-18", ["16:00:00", "20:00:00"])
    src = _rpt(tmp_path, rows)
    dst = tmp_path / "bars_cache_day.csv"
    assert main(["--data", str(src), "--out", str(dst), "--session", "day"]) == 2
    assert not dst.exists()
