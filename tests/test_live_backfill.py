"""kbars -> bar-frame conversion, the warm-up half of the data path.

Live bars come from ticks (validated by V1); warm-up bars come from `kbars` and
are validated here offline plus by `live/v1c_check.py` against the real API.
The strongest test is the last one: take real RPT bars, wrap them in a kbars-
shaped payload, and require the converter to reproduce `load_minute_bars`
exactly — so the two sources are provably interchangeable *given* that kbars
uses the same labelling, which only v1c_check can confirm.
"""
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from forecast_eval.live.backfill import fetch_bars, kbars_to_bars
from forecast_eval.live.sessions import SessionPolicy
from forecast_eval.real_data import load_minute_bars

DATA = Path(r"\\DESKTOP-SQUIRRE\dataset\RPT\TXF_RPT_minute_day.csv")


def _ns(*times):
    return [int(pd.Timestamp(t).value) for t in times]


def _payload(times, closes, volumes):
    """A kbars-shaped dict: epoch-nanosecond ts plus OHLCV columns."""
    return {
        "ts": _ns(*times),
        "Open": list(closes),
        "High": list(closes),
        "Low": list(closes),
        "Close": list(closes),
        "Volume": list(volumes),
        "Amount": [c * v for c, v in zip(closes, volumes)],
    }


# ---- shape and schema -------------------------------------------------------

def test_converts_nanosecond_epochs_to_timestamps():
    df = kbars_to_bars(_payload(
        ["2026-03-05 08:45", "2026-03-05 08:46"], [17500.0, 17501.0], [10, 20]),
        source_label="left")
    assert list(df.columns) == ["timestamp", "price", "volume"]
    assert df["timestamp"].iloc[0] == pd.Timestamp("2026-03-05 08:45")
    assert df["price"].iloc[1] == 17501.0


# ---- the right-label shift --------------------------------------------------

def test_right_labelled_input_is_shifted_back_one_minute():
    """MEASURED 2026-08-10: kbars sends bar `T` covering `(T-1min, T]` while RPT
    labels the same interval `T-1min`. Shifting renames the bar; it moves no
    data."""
    df = kbars_to_bars(_payload(["2026-03-05 08:46"], [17500.0], [3]))
    assert df["timestamp"].iloc[0] == pd.Timestamp("2026-03-05 08:45")
    assert df["price"].iloc[0] == 17500.0, "the price must not move with the label"


def test_left_labelled_input_is_left_alone():
    df = kbars_to_bars(_payload(["2026-03-05 08:46"], [17500.0], [3]),
                       source_label="left")
    assert df["timestamp"].iloc[0] == pd.Timestamp("2026-03-05 08:46")


def test_shift_default_is_right_because_that_is_what_shioaji_sends():
    a = kbars_to_bars(_payload(["2026-03-05 08:46"], [1.0], [1]))
    b = kbars_to_bars(_payload(["2026-03-05 08:46"], [1.0], [1]),
                      source_label="right")
    assert list(a["timestamp"]) == list(b["timestamp"])


def test_unknown_source_label_is_rejected():
    with pytest.raises(ValueError, match="source_label"):
        kbars_to_bars(_payload(["2026-03-05 08:46"], [1.0], [1]),
                      source_label="middle")


def test_price_is_the_close_not_the_open():
    df = kbars_to_bars({
        "ts": _ns("2026-03-05 08:45"),
        "Open": [17400.0], "High": [17600.0], "Low": [17300.0],
        "Close": [17500.0], "Volume": [5],
    })
    assert df["price"].iloc[0] == 17500.0


def test_accepts_an_object_payload_not_only_a_dict():
    """The concrete KBars type is unverified; attribute access must work too."""
    obj = SimpleNamespace(
        ts=_ns("2026-03-05 08:45"), Open=[1.0], High=[1.0], Low=[1.0],
        Close=[17500.0], Volume=[3], Amount=[1.0],
    )
    df = kbars_to_bars(obj)
    assert len(df) == 1 and df["price"].iloc[0] == 17500.0


def test_already_datetime_ts_passes_through():
    df = kbars_to_bars({
        "ts": [datetime(2026, 3, 5, 8, 45)],
        "Close": [17500.0], "Volume": [1],
    }, source_label="left")
    assert df["timestamp"].iloc[0] == pd.Timestamp("2026-03-05 08:45")


def test_missing_required_columns_raises():
    with pytest.raises(ValueError, match="missing required columns"):
        kbars_to_bars({"Open": [1.0], "Volume": [1]})


def test_output_is_sorted_chronologically():
    df = kbars_to_bars(_payload(
        ["2026-03-05 08:47", "2026-03-05 08:45", "2026-03-05 08:46"],
        [3.0, 1.0, 2.0], [1, 1, 1]))
    assert list(df["price"]) == [1.0, 2.0, 3.0]


# ---- the RPT convention -----------------------------------------------------

def test_zero_volume_filler_rows_are_dropped():
    """The RPT file provably contains no zero-volume bar, so if kbars fills
    empty minutes those rows are not observations the model ever saw."""
    df = kbars_to_bars(_payload(
        ["2026-03-05 08:45", "2026-03-05 08:46", "2026-03-05 08:47"],
        [17500.0, 17500.0, 17502.0], [10, 0, 5]), source_label="left")
    assert len(df) == 2
    assert list(df["timestamp"].dt.strftime("%H:%M")) == ["08:45", "08:47"]


def test_zero_volume_rows_can_be_kept_for_diagnosis():
    """v1c_check needs the raw payload to detect whether kbars fills at all."""
    df = kbars_to_bars(_payload(
        ["2026-03-05 08:45", "2026-03-05 08:46"], [17500.0, 17500.0], [10, 0]),
        drop_zero_volume=False)
    assert len(df) == 2


def test_nan_closes_are_dropped():
    """Toto2 propagates NaN, so they must never reach the context."""
    df = kbars_to_bars(_payload(
        ["2026-03-05 08:45", "2026-03-05 08:46"], [17500.0, np.nan], [10, 5]))
    assert len(df) == 1


# ---- fetch_bars -------------------------------------------------------------

def test_fetch_bars_passes_the_span_through_and_converts():
    calls = {}

    class _Api:
        # 08:46 as kbars sends it -> 08:45 after the shift, the day session's
        # first minute.
        def kbars(self, contract, start, end):
            calls.update(contract=contract, start=start, end=end)
            return _payload(["2026-03-05 08:46"], [17500.0], [7])

    df = fetch_bars(_Api(), "TXFR1", "2026-03-01", "2026-03-05",
                    policy=SessionPolicy.day())
    assert calls == {"contract": "TXFR1", "start": "2026-03-01",
                     "end": "2026-03-05"}
    assert len(df) == 1 and df["volume"].iloc[0] == 7
    assert df["timestamp"].iloc[0] == pd.Timestamp("2026-03-05 08:45")


# ---- the in-progress trailing bar (SHIOAJI_PLAN.md §2.14) -------------------

# Times as kbars sends them (right-labelled); post-shift these are the night
# bars 04:08, 04:09, 04:10 and 04:11 of 2026-08-11 — the real incident.
_SEAM = ["2026-08-11 04:09", "2026-08-11 04:10",
         "2026-08-11 04:11", "2026-08-11 04:12"]


class _SeamApi:
    def kbars(self, contract, start, end):
        return _payload(_SEAM, [44730.0, 44731.0, 44732.0, 44733.0], [5] * 4)


def _seam(now):
    return fetch_bars(_SeamApi(), "TXFR1", "2026-08-11", "2026-08-11",
                      policy=SessionPolicy.night(), now=now)


def test_the_in_progress_minute_is_dropped():
    """MEASURED 2026-08-11: a backfill at 04:11:29 came back with a bar for the
    minute still being traded. Feeding it to the model is wrong twice over — it
    is a fragment of a minute, and the live aggregator then emits its own bar
    for that same minute, putting one timestamp in the context twice."""
    df = _seam(pd.Timestamp("2026-08-11 04:11:29"))
    assert list(df["timestamp"]) == [pd.Timestamp("2026-08-11 04:08"),
                                     pd.Timestamp("2026-08-11 04:09"),
                                     pd.Timestamp("2026-08-11 04:10")]


def test_a_minute_is_kept_the_instant_it_completes():
    """Bar T covers [T, T+1min), so at exactly T+1min it is finished. The
    boundary is tested because flooring `now` instead would discard a complete
    bar every time the fetch landed on a minute edge."""
    assert _seam(pd.Timestamp("2026-08-11 04:12:00"))["timestamp"].iloc[-1] == \
        pd.Timestamp("2026-08-11 04:11")
    assert _seam(pd.Timestamp("2026-08-11 04:11:59.999"))["timestamp"].iloc[-1] == \
        pd.Timestamp("2026-08-11 04:10")


def test_nothing_is_dropped_when_every_bar_has_elapsed():
    df = _seam(pd.Timestamp("2026-08-11 06:00"))
    assert len(df) == 4


def test_the_index_is_reset_after_truncation():
    """`warm_up` slices with `.iloc[-ctx:]` and `seed_history` reads columns
    positionally; a gappy index would not raise, it would just misalign."""
    df = _seam(pd.Timestamp("2026-08-11 04:11:29"))
    assert list(df.index) == [0, 1, 2]


# ---- equivalence with the offline loader, on real data ----------------------

@pytest.mark.skipif(not DATA.exists(), reason="RPT dataset not reachable")
def test_reproduces_load_minute_bars_on_real_data():
    """Wrap real RPT bars in a kbars-shaped payload and require the converter to
    reproduce `load_minute_bars` exactly.

    Pinned to `source_label="left"` because the payload is built from RPT rows,
    which are already left-labelled — this isolates the conversion mechanics
    (epoch parsing, NaN/zero-volume drops, sorting, schema). The right-label
    shift that real kbars needs is covered separately above; V1c measured it
    rather than this test assuming it."""
    expected = load_minute_bars(DATA, 3000)
    payload = {
        "ts": [int(pd.Timestamp(t).value) for t in expected["timestamp"]],
        "Open": list(expected["price"]),
        "High": list(expected["price"]),
        "Low": list(expected["price"]),
        "Close": list(expected["price"]),
        "Volume": list(expected["volume"]),
    }
    got = kbars_to_bars(payload, source_label="left")

    assert len(got) == len(expected)
    assert list(got["timestamp"]) == list(expected["timestamp"])
    assert list(got["price"]) == list(expected["price"])
    assert list(got["volume"]) == list(expected["volume"])
