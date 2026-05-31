"""Environment: tick replay + DAY/NIGHT classification (SPEC §4.7)."""
from datetime import time
from typing import Iterator, Optional

import pandas as pd

from .events import MarketEvent

# Tech: the four TXF session edges as wall-clock times (Taiwan local).
# Why:  the night session wraps past midnight, so NIGHT_CLOSE (05:00) is a
#       *next-day* boundary; classify_session handles the wrap rather than these
#       constants, which stay as plain readable session-edge definitions.
DAY_OPEN = time(8, 45)
DAY_CLOSE = time(13, 45)
NIGHT_OPEN = time(15, 0)
NIGHT_CLOSE = time(5, 0)  # next-day boundary


def classify_session(ts: pd.Timestamp) -> Optional[str]:
    """Return 'DAY', 'NIGHT', or None for ticks outside trading hours."""
    # Tech: reduce the full timestamp to its time-of-day for window comparison.
    # Why:  session membership depends only on clock time, not the calendar date,
    #       so we compare against the time() edges and ignore the day.
    t = ts.time()
    # Tech: DAY is the simple in-range case; NIGHT is the union of the pre- and
    #       post-midnight halves; anything else (e.g. the 13:45–15:00 gap) is None.
    # Why:  the night window straddles midnight, so it can't be one `a <= t < b`
    #       check — it's "after the open OR before the next-day close". Returning
    #       None lets the caller drop off-hours prints instead of mislabeling them.
    if DAY_OPEN <= t < DAY_CLOSE:
        return "DAY"
    if t >= NIGHT_OPEN or t < NIGHT_CLOSE:
        return "NIGHT"
    return None


class Environment:
    """Replays ticks chronologically and emits MarketEvent per print."""

    def __init__(self, ticks: pd.DataFrame, *, drop_non_session: bool = True):
        # Tech: validate the frame carries the three columns the stream needs.
        # Why:  failing loudly here (with the exact missing set) beats a cryptic
        #       KeyError deep inside the per-tick loop after a long run has started.
        required = {"timestamp", "price", "volume"}
        missing = required - set(ticks.columns)
        if missing:
            raise ValueError(f"ticks missing required columns: {missing}")
        # Tech: copy, coerce timestamps to datetime, then sort chronologically and
        #       reset the index; stash the drop-non-session policy.
        # Why:  a defensive copy keeps the caller's DataFrame untouched; coercion
        #       guards against string timestamps; chronological order is the core
        #       invariant of an event-driven replay — out-of-order ticks would
        #       break fills and look-ahead defense alike.
        df = ticks.copy()
        if not pd.api.types.is_datetime64_any_dtype(df["timestamp"]):
            df["timestamp"] = pd.to_datetime(df["timestamp"])
        self._ticks = df.sort_values("timestamp").reset_index(drop=True)
        self._drop_non_session = drop_non_session

    def __len__(self) -> int:
        # Tech: number of ticks held.
        # Why:  lets callers size a progress bar (tqdm total=) without exposing the
        #       internal DataFrame.
        return len(self._ticks)

    def stream(self) -> Iterator[MarketEvent]:
        # Tech: pull the three columns into plain Python lists up front.
        # Why:  iterating Python lists is dramatically faster than per-row pandas
        #       access (.iloc/.itertuples) over millions of ticks — this loop is
        #       the hottest path in a real-data backtest.
        ts_arr = self._ticks["timestamp"].to_list()
        price_arr = self._ticks["price"].to_list()
        vol_arr = self._ticks["volume"].to_list()
        # Tech: walk the three lists in lockstep, classifying each tick's session.
        # Why:  yielding lazily (generator) keeps memory flat — the loop consumes
        #       one MarketEvent at a time rather than materializing them all.
        for ts, price, vol in zip(ts_arr, price_arr, vol_arr):
            sess = classify_session(ts)
            # Tech: when a tick falls outside trading hours, either skip it or, if
            #       configured to keep it, label it DAY as a fallback.
            # Why:  off-hours prints (auction gaps, data artifacts) are normally
            #       noise we don't want to trade on; drop_non_session=False exists
            #       only for tests/synthetic data that don't care about real hours.
            if sess is None:
                if self._drop_non_session:
                    continue
                sess = "DAY"  # fallback
            # Tech: emit the typed MarketEvent, coercing price/volume to float/int.
            # Why:  downstream code relies on these exact types; the type-ignore on
            #       session is because classify_session returns a plain str that we
            #       know is a valid Session literal here.
            yield MarketEvent(
                timestamp=ts,
                price=float(price),
                volume=int(vol),
                session=sess,  # type: ignore[arg-type]
            )
