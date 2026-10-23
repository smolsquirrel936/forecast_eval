"""TickAggregator — ticks to the 1-min bars the offline pipeline produces.

Two layers:

  * unit cases pinning the RPT convention (left-labelled, close-only, empty
    minutes omitted) plus the two live-only filters (simtrade, late prints);
  * an equivalence test on the **real** RPT file: bars fed back through the
    aggregator must reproduce, event for event, what `Environment` yields from
    `load_minute_bars`. That is the offline half of SHIOAJI_PLAN.md V1 — the
    other half needs captured live ticks and a real session.
"""
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from forecast_eval.environment import Environment
from forecast_eval.live.bars import TickAggregator
from forecast_eval.real_data import load_minute_bars

DATA = Path(r"\\DESKTOP-SQUIRRE\dataset\RPT\TXF_RPT_minute_day.csv")

# A DAY-session minute (08:45 <= t < 13:45).
D0 = datetime(2024, 3, 5, 9, 0, 0)


def _at(base, *, m=0, s=0):
    return base + timedelta(minutes=m, seconds=s)


# ---- the RPT convention -----------------------------------------------------

def test_bar_is_left_labelled_and_closes_on_the_next_minute():
    agg = TickAggregator()
    assert agg.add_tick(_at(D0, s=1), 100.0) == []
    assert agg.add_tick(_at(D0, s=59), 101.0) == []
    # First tick of the next minute closes the 09:00 bar.
    bars = agg.add_tick(_at(D0, m=1, s=0), 102.0)
    assert len(bars) == 1
    assert bars[0].timestamp == D0          # labelled at the LEFT edge
    assert bars[0].price == 101.0           # close = last trade in the window


def test_close_is_last_price_and_volume_is_the_sum():
    agg = TickAggregator()
    for s, px, v in [(1, 100.0, 3), (20, 105.0, 2), (50, 99.0, 4)]:
        agg.add_tick(_at(D0, s=s), px, v)
    bar = agg.add_tick(_at(D0, m=1), 110.0)[0]
    assert bar.price == 99.0
    assert bar.volume == 9


def test_empty_minutes_produce_no_bar():
    """The RPT file omits minutes with no trades — 194 of 3756 day-session days
    carry fewer than 300 bars, and none has volume 0. A gap must stay a gap."""
    agg = TickAggregator()
    agg.add_tick(_at(D0, s=10), 100.0)
    # Next trade is four minutes later: only the 09:00 bar exists.
    bars = agg.add_tick(_at(D0, m=4, s=10), 108.0)
    assert len(bars) == 1
    assert bars[0].timestamp == D0
    bars = agg.add_tick(_at(D0, m=5, s=1), 109.0)
    assert len(bars) == 1
    assert bars[0].timestamp == _at(D0, m=4)   # not 09:01/09:02/09:03


def test_first_tick_emits_nothing():
    assert TickAggregator().add_tick(D0, 100.0) == []


# ---- live-only filters ------------------------------------------------------

def test_simtrade_ticks_are_dropped():
    """Trial-matching prints are not trades and must never reach a bar."""
    agg = TickAggregator()
    agg.add_tick(_at(D0, s=1), 100.0, 5)
    agg.add_tick(_at(D0, s=10), 999.0, 7, simtrade=True)
    bar = agg.add_tick(_at(D0, m=1), 101.0)[0]
    assert bar.price == 100.0     # the simtrade price did not become the close
    assert bar.volume == 5        # nor did its volume land in the bar
    assert agg.n_simtrade_dropped == 1


def test_late_print_is_dropped_not_back_applied():
    """The loop has already traded on a closed bar; rewriting it would put the
    live series permanently out of step with an offline replay."""
    agg = TickAggregator()
    agg.add_tick(_at(D0, s=10), 100.0)
    agg.add_tick(_at(D0, m=1, s=5), 101.0)      # closes the 09:00 bar
    assert agg.add_tick(_at(D0, s=30), 555.0) == []
    assert agg.n_late_dropped == 1
    bar = agg.add_tick(_at(D0, m=2), 102.0)[0]
    assert bar.timestamp == _at(D0, m=1)
    assert bar.price == 101.0                    # untouched by the late print


def test_off_session_bars_are_dropped_like_environment():
    """13:45-15:00 is not a session; Environment drops it and so must this."""
    gap = datetime(2024, 3, 5, 14, 0, 0)
    agg = TickAggregator()
    agg.add_tick(_at(gap, s=1), 100.0)
    assert agg.add_tick(_at(gap, m=1), 101.0) == []
    assert agg.n_bars_dropped_off_session == 1


# ---- flush ------------------------------------------------------------------

def test_flush_closes_a_quiet_bar_on_the_wall_clock():
    """Without flush, a completed bar would wait for the next trade to arrive."""
    agg = TickAggregator()
    agg.add_tick(_at(D0, s=10), 100.0)
    assert agg.flush(_at(D0, s=59)) == []        # bar not over yet
    bars = agg.flush(_at(D0, m=1, s=0))          # right edge reached
    assert len(bars) == 1 and bars[0].timestamp == D0
    assert agg.flush(_at(D0, m=5)) == []         # nothing left open


# ---- the clock-flush race (SHIOAJI_PLAN.md §2.16) ---------------------------

def test_a_straggler_after_a_flush_does_not_reopen_the_minute():
    """MEASURED live 2026-08-11: 20 bars for 15 minutes. `flush` fires on the
    *local* clock while bar boundaries come from *exchange* timestamps, so a tick
    stamped :59.9 can arrive after its bar was closed and cleared. It must be
    counted late, not start a second bucket for the same minute."""
    agg = TickAggregator()
    agg.add_tick(_at(D0, s=10), 100.0, 500)
    bars = agg.flush(_at(D0, m=1, s=0))                 # closes 09:00
    assert len(bars) == 1 and bars[0].volume == 500

    assert agg.add_tick(_at(D0, s=59), 101.0, 2) == []  # the straggler
    assert agg.n_late_dropped == 1

    # The next real minute must still open and close normally, exactly once.
    agg.add_tick(_at(D0, m=1, s=5), 102.0, 7)
    more = agg.flush(_at(D0, m=2, s=0))
    assert [b.timestamp for b in more] == [_at(D0, m=1)]
    assert agg.n_bars_emitted == 2, "one bar per minute, not two for 09:00"


def test_the_minute_is_remembered_even_when_its_bar_was_filtered_out():
    """A dropped bar still *closed* that minute. Recording it only for bars that
    survive the session filter would let a straggler reopen an off-session
    minute — the same defect by a quieter route."""
    off = datetime(2024, 3, 5, 14, 0, 0)                # between sessions
    agg = TickAggregator()
    agg.add_tick(_at(off, s=10), 100.0)
    assert agg.flush(_at(off, m=1, s=0)) == []          # emitted nothing
    assert agg.n_bars_dropped_off_session == 1

    assert agg.add_tick(_at(off, s=59), 101.0) == []
    assert agg.n_late_dropped == 1
    assert agg.n_bars_dropped_off_session == 1, "must not close that minute twice"


def test_an_ordinary_tick_sequence_is_unaffected():
    """Non-vacuity: the new guard must not swallow bars in the normal case."""
    agg = TickAggregator()
    for m in range(5):
        agg.add_tick(_at(D0, m=m, s=10), 100.0 + m)
    bars = agg.flush(_at(D0, m=5, s=0))
    assert agg.n_bars_emitted == 5 and len(bars) == 1
    assert agg.n_late_dropped == 0


# ---- the flush grace (SHIOAJI_PLAN.md §2.16 residual) ----------------------

GRACE = timedelta(seconds=2)


def test_flush_waits_out_the_grace_before_closing():
    agg = TickAggregator(flush_grace=GRACE)
    agg.add_tick(_at(D0, s=10), 100.0)
    assert agg.flush(_at(D0, m=1, s=0)) == [], "right edge reached, grace running"
    assert agg.flush(_at(D0, m=1, s=1)) == []
    bars = agg.flush(_at(D0, m=1, s=2))
    assert len(bars) == 1 and bars[0].timestamp == D0


def test_the_grace_lets_a_trailing_tick_reach_its_own_bar():
    """The incident replayed with its real numbers: 15:00 traded 568 lots, and
    its last tick — 2 lots at 44959, stamped :59.965 — was still in flight when
    the local clock passed the edge. With the grace it lands in its own minute,
    so the live bar equals what an offline replay of the same ticks produces
    (close 44959, volume 570) instead of splitting into 568 + a 2-lot fragment."""
    agg = TickAggregator(flush_grace=GRACE)
    agg.add_tick(_at(D0, s=0), 44958.0, 568)
    assert agg.flush(_at(D0, m=1, s=0)) == []              # edge passed, waiting
    assert agg.add_tick(_at(D0, s=59), 44959.0, 2) == []   # straggler absorbed
    bars = agg.add_tick(_at(D0, m=1, s=0), 44955.0, 1)     # next minute closes it
    assert len(bars) == 1
    assert bars[0].price == 44959.0 and bars[0].volume == 570
    assert agg.n_late_dropped == 0, "nothing was late — it arrived in time"


def test_the_grace_does_not_delay_the_tick_driven_close():
    """Only the blind clock path waits. A tick for the next minute already proves
    the previous one is complete — delivery is in order (measured: 1 106 ticks,
    zero out-of-order) — so it closes at once. Delaying this path too would push
    every bar 2 s late for no gain."""
    agg = TickAggregator(flush_grace=GRACE)
    agg.add_tick(_at(D0, s=10), 100.0)
    bars = agg.add_tick(_at(D0, m=1, s=0), 101.0)
    assert len(bars) == 1 and bars[0].timestamp == D0


def test_a_straggler_arriving_after_the_grace_is_still_dropped():
    """The grace shortens the §2.16 window; it does not remove the guard."""
    agg = TickAggregator(flush_grace=GRACE)
    agg.add_tick(_at(D0, s=10), 100.0, 500)
    assert len(agg.flush(_at(D0, m=1, s=2))) == 1
    assert agg.add_tick(_at(D0, s=59), 101.0, 2) == []
    assert agg.n_late_dropped == 1 and agg.n_bars_emitted == 1


def test_the_grace_defaults_to_zero_so_offline_replay_is_unchanged():
    """An offline replay has the whole stream in hand — nothing is ever in
    flight — so the aggregator stays a pure function of its ticks by default."""
    agg = TickAggregator()
    agg.add_tick(_at(D0, s=10), 100.0)
    assert len(agg.flush(_at(D0, m=1, s=0))) == 1


# ---- equivalence with the offline pipeline, on real data --------------------

@pytest.mark.skipif(not DATA.exists(), reason="RPT dataset not reachable")
def test_matches_offline_pipeline_on_real_rpt_bars():
    """Replay real RPT bars as ticks; the aggregator must reproduce exactly the
    MarketEvent stream `Environment` builds from the same file — same labels,
    same prices, same volumes, same session tags, same count."""
    ticks = load_minute_bars(DATA, 3000)
    expected = list(Environment(ticks).stream())
    assert len(expected) > 2500, "expected a meaningful slice of real bars"

    agg = TickAggregator()
    produced = []
    for ts, price, vol in zip(ticks["timestamp"], ticks["price"], ticks["volume"]):
        # Place each bar's trade inside its own minute; flooring must recover
        # the original left-edge label.
        produced.extend(agg.add_tick(ts + timedelta(seconds=30), price, int(vol)))
    produced.extend(agg.flush(ticks["timestamp"].iloc[-1] + timedelta(minutes=2)))

    assert len(produced) == len(expected)
    for got, want in zip(produced, expected):
        assert got.timestamp == want.timestamp
        assert got.price == want.price
        assert got.volume == want.volume
        assert got.session == want.session
    assert agg.n_late_dropped == 0


# ---- the contract roll's discard (2026-10-20) ------------------------------

def test_a_discarded_bucket_is_never_emitted_and_its_minute_can_reopen():
    agg = TickAggregator(drop_non_session=False)
    base = datetime(2026, 10, 20, 15, 40)
    agg.add_tick(base, 46419.0, 1)                     # the old month
    agg.discard_open()
    agg.add_tick(base + timedelta(seconds=4), 46578.0, 2)
    (bar,) = agg.add_tick(base + timedelta(minutes=1), 46580.0, 1)
    assert bar.timestamp == base
    assert bar.price == 46578.0 and bar.volume == 2
    assert agg.n_buckets_discarded == 1


def test_discarding_with_nothing_open_is_harmless():
    agg = TickAggregator(drop_non_session=False)
    agg.discard_open()
    assert agg.flush(datetime(2026, 10, 20, 16, 0)) == []
