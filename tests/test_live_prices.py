"""The report's price line spans the soak, not the run being rendered.

A restart opens a new run directory, so a per-run price background showed
minutes of prices beneath fills spanning weeks — and once `--resume` began
replaying fills executed in *earlier* segments, the chart could plot a fill from
a day whose prices live in another directory entirely.

The cases below pin the two inclusion rules, the caching that keeps a months-long
soak affordable, and the degradation rules — because a chart background must
never be able to cost a run anything.
"""
import json

import pandas as pd
import pytest

from forecast_eval.live.prices import (CACHE_COLUMNS, cache_path, capture_span,
                                       minute_series, soak_price_series)
from forecast_eval.live.sessions import SessionPolicy

NIGHT = SessionPolicy.night()


def _run(root, name, *, soak_id=None, start="2026-09-01 15:00", n=5, price=44000.0):
    d = root / name
    d.mkdir(parents=True)
    if soak_id is not None:
        (d / "session.jsonl").write_text(
            json.dumps({"event": "meta", "soak_id": soak_id}) + "\n", encoding="utf-8")
    stamps = pd.date_range(start, periods=n, freq="1min")
    rows = ["timestamp,received,code,price,volume,simtrade"]
    for i, ts in enumerate(stamps):
        rows.append(f"{ts.isoformat()},{ts.isoformat()},TXFI6,{price + i},1,0")
    (d / "ticks.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    return d


def test_every_segment_of_the_soak_is_included(tmp_path):
    """The point. Three restarts are three directories and one price line."""
    _run(tmp_path, "live_night_a", soak_id="s1", start="2026-09-01 15:00")
    _run(tmp_path, "live_night_b", soak_id="s1", start="2026-09-01 16:00")
    live = _run(tmp_path, "live_night_c", soak_id="s1", start="2026-09-01 17:00")

    s = soak_price_series(tmp_path, policy=NIGHT, soak_id="s1", run_dir=live)
    assert len(s) == 15
    assert str(s["timestamp"].min()) == "2026-09-01 15:00:00"
    assert str(s["timestamp"].max()) == "2026-09-01 17:04:00"


def test_another_soak_is_not_included(tmp_path):
    """`--fresh-start` mints a new epoch precisely so its report is its own."""
    _run(tmp_path, "live_night_old", soak_id="s0", start="2026-08-01 15:00")
    live = _run(tmp_path, "live_night_new", soak_id="s1", start="2026-09-01 15:00")

    s = soak_price_series(tmp_path, policy=NIGHT, soak_id="s1", run_dir=live)
    assert len(s) == 5
    assert s["timestamp"].min().month == 9


def test_a_run_covering_a_replayed_fill_is_pulled_in(tmp_path):
    """The rule that makes a resumed report coherent: the fills the ledger
    replays were executed in an earlier run, whose prices are only there. Without
    this the chart plots them against a line that does not reach them."""
    _run(tmp_path, "live_20260824_150112", start="2026-08-24 20:00", n=10)
    live = _run(tmp_path, "live_night_now", soak_id="s1", start="2026-09-01 15:00")
    fill_ts = pd.Timestamp("2026-08-24 20:05:00")

    without = soak_price_series(tmp_path, policy=NIGHT, soak_id="s1", run_dir=live)
    assert fill_ts < without["timestamp"].min(), "fixture must start after the fill"

    with_cover = soak_price_series(tmp_path, policy=NIGHT, soak_id="s1",
                                   cover=[fill_ts], run_dir=live)
    assert with_cover["timestamp"].min() <= fill_ts <= with_cover["timestamp"].max()


def test_day_and_night_price_caches_stay_separate(tmp_path):
    assert cache_path(tmp_path, NIGHT).name == "prices_night.csv"
    assert cache_path(tmp_path, SessionPolicy.day()).name == "prices_day.csv"


# ---- the cache -------------------------------------------------------------


def test_a_finished_segment_is_parsed_once(tmp_path):
    """What keeps a three-month soak's report from re-reading gigabytes of
    captures every time it renders."""
    _run(tmp_path, "live_night_a", soak_id="s1", start="2026-09-01 15:00")
    live = _run(tmp_path, "live_night_b", soak_id="s1", start="2026-09-01 16:00")

    soak_price_series(tmp_path, policy=NIGHT, soak_id="s1", run_dir=live)
    cached = pd.read_csv(cache_path(tmp_path, NIGHT))
    assert list(cached.columns) == CACHE_COLUMNS
    assert set(cached["run"]) == {"live_night_a"}, "the live segment must not cache"


def test_the_live_segment_is_re_read_so_it_never_goes_stale(tmp_path):
    """It is still growing; a cached copy would be wrong a minute later."""
    _run(tmp_path, "live_night_a", soak_id="s1", start="2026-09-01 15:00")
    live = _run(tmp_path, "live_night_b", soak_id="s1", start="2026-09-01 16:00", n=2)

    first = soak_price_series(tmp_path, policy=NIGHT, soak_id="s1", run_dir=live)
    with (live / "ticks.csv").open("a", encoding="utf-8") as fh:
        fh.write("2026-09-01T16:09:00,2026-09-01T16:09:00,TXFI6,49999.0,1,0\n")
    second = soak_price_series(tmp_path, policy=NIGHT, soak_id="s1", run_dir=live)

    assert len(second) == len(first) + 1
    assert 49999.0 in set(second["price"])


def test_a_corrupt_cache_costs_a_reparse_not_a_render(tmp_path):
    _run(tmp_path, "live_night_a", soak_id="s1")
    cache_path(tmp_path, NIGHT).write_bytes(b"\x00 not a csv")
    s = soak_price_series(tmp_path, policy=NIGHT, soak_id="s1")
    assert s is not None and len(s) == 5


# ---- the pieces ------------------------------------------------------------


def test_capture_span_reads_only_the_ends(tmp_path):
    d = _run(tmp_path, "live_night_a", soak_id="s1", start="2026-09-01 15:00", n=50)
    lo, hi = capture_span(d / "ticks.csv")
    assert str(lo) == "2026-09-01 15:00:00"
    assert str(hi) == "2026-09-01 15:49:00"


def test_minute_series_takes_the_last_print_of_each_minute(tmp_path):
    d = tmp_path / "r"
    d.mkdir()
    (d / "ticks.csv").write_text(
        "timestamp,received,code,price,volume,simtrade\n"
        "2026-09-01T15:00:01,2026-09-01T15:00:01,TXFI6,100.0,1,0\n"
        "2026-09-01T15:00:59,2026-09-01T15:00:59,TXFI6,109.0,1,0\n"
        "2026-09-01T15:01:10,2026-09-01T15:01:10,TXFI6,120.0,1,0\n",
        encoding="utf-8")
    s = minute_series(d / "ticks.csv")
    assert list(s["price"]) == [109.0, 120.0]


def test_nothing_to_chart_is_none_not_an_empty_frame(tmp_path):
    assert soak_price_series(tmp_path, policy=NIGHT, soak_id="s1") is None


def test_a_missing_capture_is_skipped_not_fatal(tmp_path):
    d = tmp_path / "live_night_a"
    d.mkdir()
    (d / "session.jsonl").write_text(
        json.dumps({"event": "meta", "soak_id": "s1"}) + "\n", encoding="utf-8")
    _run(tmp_path, "live_night_b", soak_id="s1", start="2026-09-01 16:00")
    s = soak_price_series(tmp_path, policy=NIGHT, soak_id="s1")
    assert s is not None and len(s) == 5
