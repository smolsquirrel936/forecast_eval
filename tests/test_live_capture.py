"""TickCapture — the raw feed recorded for V1's live aggregation diff.

The property that matters is completeness *before* filtering: a file containing
only the ticks that survived the simtrade and session filters cannot be used to
verify those filters ran correctly. The round-trip test is the real one — a
capture must replay through `TickAggregator` and produce the same bars the live
session produced.
"""
from datetime import datetime, timedelta

import pandas as pd

from forecast_eval.live.bars import TickAggregator
from forecast_eval.live.capture import TickCapture, read_ticks
from forecast_eval.live.sessions import SessionPolicy

T0 = datetime(2026, 5, 14, 20, 0, 0)


def test_writes_a_header_and_one_row_per_tick(tmp_path):
    p = tmp_path / "ticks.csv"
    with TickCapture(p) as cap:
        for i in range(5):
            cap.write(T0 + timedelta(seconds=i), 17500.0 + i, 3)
    df = read_ticks(p)
    assert len(df) == 5
    assert list(df.columns) == ["timestamp", "received", "code", "price",
                                "volume", "simtrade"]
    assert cap.n == 5


# ---- the `received` clock (SHIOAJI_PLAN.md §2.16) ---------------------------

def test_received_is_stamped_at_write_time_not_taken_from_the_tick(tmp_path):
    """The grace has to cover exchange-timestamp → *we can act on it*, and
    `write` is called from `on_tick` on the same consumer thread as `on_clock` —
    so this instant is exactly what races the flush."""
    p = tmp_path / "ticks.csv"
    before = datetime.now()
    with TickCapture(p) as cap:
        cap.write(T0, 17500.0, 1)              # exchange stamp is far in the past
    after = datetime.now()

    r = read_ticks(p).iloc[0]
    assert r["timestamp"] == pd.Timestamp(T0)
    assert before <= r["received"].to_pydatetime() <= after
    assert r["received"] - r["timestamp"] > pd.Timedelta(0), \
        "the two clocks must be recorded independently"


def test_received_round_trips_when_supplied(tmp_path):
    """Pinned explicitly so a replay can measure the delivery lag exactly."""
    p = tmp_path / "ticks.csv"
    with TickCapture(p) as cap:
        cap.write(T0, 17500.0, 1, received=T0 + timedelta(milliseconds=350))
    r = read_ticks(p).iloc[0]
    assert r["received"] == pd.Timestamp(T0) + pd.Timedelta(milliseconds=350)
    assert (r["received"] - r["timestamp"]).total_seconds() == 0.35


def test_a_capture_written_before_the_column_existed_still_reads(tmp_path):
    """Two live sessions were captured before this column — including the only
    one holding observed simtrade prints. The V1 diff still reads those files, so
    the reader must not reject a capture for predating the schema."""
    p = tmp_path / "legacy.csv"
    p.write_text(
        "timestamp,code,price,volume,simtrade\n"
        "2026-08-11T15:00:00,TXFH6,44958.0,568,0\n"
        "2026-08-11T15:00:00.024000,TXFH6,44959.0,2,0\n",
        encoding="utf-8",
    )
    df = read_ticks(p)
    assert len(df) == 2 and "received" not in df.columns
    assert df["timestamp"].iloc[1] == pd.Timestamp("2026-08-11 15:00:00.024")
    assert df["simtrade"].dtype == bool


def test_round_trips_types_losslessly(tmp_path):
    p = tmp_path / "ticks.csv"
    with TickCapture(p) as cap:
        cap.write(pd.Timestamp("2026-05-14 20:00:01.250"), 17501.5, 7,
                  simtrade=True, code="TXFH6")
    df = read_ticks(p)
    r = df.iloc[0]
    assert r["timestamp"] == pd.Timestamp("2026-05-14 20:00:01.250")
    assert r["price"] == 17501.5 and r["volume"] == 7
    assert r["simtrade"] is True or bool(r["simtrade"]) is True
    assert r["code"] == "TXFH6"


def test_reads_a_capture_that_mixes_whole_second_and_sub_second_stamps(tmp_path):
    """SHIOAJI_PLAN.md §2.17. `_iso` writes `ts.isoformat()`, which omits the
    fraction entirely when microseconds are zero — so every real session mixes
    the two forms. Bare `pd.to_datetime` infers a format from the first row and
    then raises on the first row that differs, which made every real capture
    unreadable while the uniform fixtures above all passed.

    The whole-second tick goes FIRST on purpose: that is the order that pins the
    strict format and makes the sub-second row the one that blows up."""
    p = tmp_path / "ticks.csv"
    with TickCapture(p) as cap:
        cap.write(pd.Timestamp("2026-08-11 15:00:00"), 44958.0, 1)
        cap.write(pd.Timestamp("2026-08-11 15:00:00.024000"), 44959.0, 2)
        cap.write(pd.Timestamp("2026-08-11 15:00:59.965000"), 44960.0, 3)
    df = read_ticks(p)
    assert len(df) == 3
    assert df["timestamp"].iloc[0] == pd.Timestamp("2026-08-11 15:00:00")
    assert df["timestamp"].iloc[2] == pd.Timestamp("2026-08-11 15:00:59.965")
    assert str(df["timestamp"].dtype).startswith("datetime64")


def test_simtrade_flag_survives_as_bool_not_int(tmp_path):
    """Left as 0/1 ints, a replay would treat every tick as genuine."""
    p = tmp_path / "ticks.csv"
    with TickCapture(p) as cap:
        cap.write(T0, 1.0, 1, simtrade=False)
        cap.write(T0, 2.0, 1, simtrade=True)
    df = read_ticks(p)
    assert df["simtrade"].dtype == bool
    assert list(df["simtrade"]) == [False, True]


def test_captures_filtered_ticks_too(tmp_path):
    """simtrade and out-of-session prints must be in the file — otherwise the
    file cannot audit the filters that dropped them."""
    p = tmp_path / "ticks.csv"
    with TickCapture(p) as cap:
        cap.write(T0, 17500.0, 1)                                   # NIGHT, real
        cap.write(T0 + timedelta(seconds=1), 17501.0, 1, simtrade=True)
        cap.write(datetime(2026, 5, 15, 9, 0), 17502.0, 1)          # DAY
    df = read_ticks(p)
    assert len(df) == 3
    assert df["simtrade"].sum() == 1


def test_buffered_rows_reach_disk_on_close(tmp_path):
    """The final partial buffer holds the session's last minutes — where
    end-of-session behaviour gets audited."""
    p = tmp_path / "ticks.csv"
    cap = TickCapture(p, flush_every=1000)
    for i in range(7):
        cap.write(T0 + timedelta(seconds=i), 17500.0, 1)
    cap.close()
    assert len(read_ticks(p)) == 7


def test_periodic_flush_lands_rows_before_close(tmp_path):
    p = tmp_path / "ticks.csv"
    cap = TickCapture(p, flush_every=3)
    for i in range(6):
        cap.write(T0 + timedelta(seconds=i), 17500.0, 1)
    assert len(read_ticks(p)) >= 6      # no close() yet
    cap.close()


def test_close_is_idempotent(tmp_path):
    cap = TickCapture(tmp_path / "t.csv")
    cap.close()
    cap.close()


# ---- the property V1's live half depends on --------------------------------

def test_replaying_a_capture_reproduces_the_live_bars(tmp_path):
    """A capture must aggregate to exactly the bars the live session produced —
    that equality is what makes the kbars diff meaningful."""
    p = tmp_path / "ticks.csv"
    policy = SessionPolicy.night()
    live_agg = TickAggregator(policy=policy)
    live_bars = []

    cap = TickCapture(p)
    base = datetime(2026, 5, 14, 20, 0, 0)
    ticks = []
    for i in range(240):                       # 4 minutes of prints
        ts = base + timedelta(seconds=i)
        px = 17500.0 + (i % 7)
        sim = (i % 50 == 0)                    # a few trial prints
        ticks.append((ts, px, sim))
        cap.write(ts, px, 2, simtrade=sim)
        live_bars.extend(live_agg.add_tick(ts, px, 2, simtrade=sim))
    live_bars.extend(live_agg.flush(base + timedelta(minutes=10)))
    cap.close()

    replay_agg = TickAggregator(policy=policy)
    replay_bars = []
    df = read_ticks(p)
    for _, r in df.iterrows():
        replay_bars.extend(replay_agg.add_tick(
            r["timestamp"], r["price"], int(r["volume"]),
            simtrade=bool(r["simtrade"])))
    replay_bars.extend(replay_agg.flush(base + timedelta(minutes=10)))

    assert len(live_bars) == len(replay_bars) > 0
    for a, b in zip(live_bars, replay_bars):
        assert a.timestamp == b.timestamp
        assert a.price == b.price
        assert a.volume == b.volume
        assert a.session == b.session
    assert replay_agg.n_simtrade_dropped == live_agg.n_simtrade_dropped > 0
