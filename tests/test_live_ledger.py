"""A resumed run's record covers the soak, not one segment of it.

`--resume` adopts the venue's position but not its history, and `build_trades`
walks fills from flat — so before `live/ledger.py` the inherited position's close
was booked as an OPEN and every pair after it shifted. The first case here is
that bug, stated as the arithmetic it actually produced.

The rest are the three properties the fix has to hold:

  * **session-sensitive** — DAY and NIGHT keep separate ledgers and can never
    replay each other's fills, the C5 collision one file over;
  * **soak-scoped** — only the live epoch's rows come back, so `--fresh-start`
    means a clean slate in the artifacts too;
  * **never a dependency** — a missing, truncated or unreadable ledger degrades
    to a shorter history, never to a wrong one or a stopped run.
"""
import pandas as pd
import pytest

from forecast_eval.events import FillEvent
from forecast_eval.live.ledger import (LEDGER_COLUMNS, FillLedger, ledger_path,
                                       net_position)
from forecast_eval.live.sessions import SessionPolicy
from forecast_eval.metrics import build_trades


def _f(ts, side, px, qty=1):
    return FillEvent(timestamp=pd.Timestamp(ts), side=side, fill_price=px,
                     quantity=qty, fee=0.0)


# the history: a LONG opened in an earlier segment at 44555
OPENED_EARLIER = [_f("2026-09-01 16:00", "BUY", 44555.0)]
# what this segment then does: closes it, opens another, closes that
THIS_SEGMENT = [_f("2026-09-02 16:00", "SELL", 44700.0),
                _f("2026-09-03 16:00", "BUY", 44600.0),
                _f("2026-09-05 16:00", "SELL", 44900.0)]


def test_without_the_replay_every_trade_is_mispaired():
    """The bug, as measured. Kept as a test so the fix cannot be quietly undone:
    `build_trades` starts flat, so a resumed run's first fill — economically a
    CLOSE — opens a phantom position of the opposite side."""
    trades = build_trades(THIS_SEGMENT)
    assert len(trades) == 1                      # two real trades happened
    assert trades[0].side == "SHORT"             # both real trades were LONG
    assert trades[0].pnl_points == pytest.approx(100.0)   # truth: +145 and +300


def test_replaying_the_history_pairs_every_trade_correctly(tmp_path):
    """The fix. With the opening fill replayed, the same segment books the two
    LONGs that actually happened, at the prices they actually happened at."""
    led = FillLedger(tmp_path / "fills_night.csv", soak_id="soak_1")
    led.sync(OPENED_EARLIER)

    resumed = FillLedger(tmp_path / "fills_night.csv", soak_id="soak_1")
    inherited = resumed.replay()
    trades = build_trades(inherited + THIS_SEGMENT)

    assert [t.side for t in trades] == ["LONG", "LONG"]
    assert [round(t.pnl_points) for t in trades] == [145, 300]
    assert trades[0].entry_price == 44555.0      # the price from the old segment


def test_a_replayed_fill_is_not_appended_a_second_time(tmp_path):
    """The high-water mark. Without it every resumed run would duplicate the
    whole history it just replayed, and the next resume would double it again."""
    path = tmp_path / "fills_night.csv"
    FillLedger(path, soak_id="s").sync(OPENED_EARLIER)

    led = FillLedger(path, soak_id="s")
    inherited = led.replay()
    led.sync(inherited + THIS_SEGMENT)           # the loop's list, seeded

    rows = pd.read_csv(path)
    assert len(rows) == len(OPENED_EARLIER) + len(THIS_SEGMENT)
    assert list(rows.columns) == LEDGER_COLUMNS


def test_the_synthetic_fill_is_held_back_from_the_ledger(tmp_path):
    """A synthetic opening fill stands in for an execution this process never
    saw. Writing it would make the *next* resume replay a fill that never
    happened, so `mark_written` keeps it out of the file while still letting it
    sit in the loop's list where `build_trades` needs it."""
    path = tmp_path / "fills_night.csv"
    led = FillLedger(path, soak_id="s")
    synthetic = [_f("2026-09-02 15:00", "BUY", 44555.0)]
    led.mark_written(len(synthetic))
    led.sync(synthetic + THIS_SEGMENT)

    rows = pd.read_csv(path)
    assert len(rows) == len(THIS_SEGMENT), "the synthetic fill reached the file"
    assert float(rows.iloc[0]["fill_price"]) == 44700.0


# ---- session sensitivity: the C5 collision, one file over -------------------


def test_day_and_night_keep_separate_ledgers():
    assert ledger_path("outputs", SessionPolicy.night()).name == "fills_night.csv"
    assert ledger_path("outputs", SessionPolicy.day()).name == "fills_day.csv"
    assert (ledger_path("outputs", SessionPolicy.night())
            != ledger_path("outputs", SessionPolicy.day()))


def test_a_day_run_cannot_replay_a_night_runs_fills(tmp_path):
    """The whole point of keying by policy. A night trade replayed into a day
    run would put a position in `trades.csv` that the day deployment never took,
    and then argue with the venue about a lot belonging to another deployment."""
    night = FillLedger(ledger_path(tmp_path, SessionPolicy.night()), soak_id="s")
    night.sync(OPENED_EARLIER)

    day = FillLedger(ledger_path(tmp_path, SessionPolicy.day()), soak_id="s")
    assert day.replay() == []


# ---- soak scoping ----------------------------------------------------------


def test_only_the_live_soaks_rows_are_replayed(tmp_path):
    """`--fresh-start` mints a new epoch, so scoping the replay by `soak_id` is
    what makes it mean a clean slate in the artifacts and not only in the state
    file. The retired soak stays in the file and stays readable."""
    path = tmp_path / "fills_night.csv"
    FillLedger(path, soak_id="soak_old").sync(OPENED_EARLIER)
    FillLedger(path, soak_id="soak_new").sync(THIS_SEGMENT[:1])

    assert len(FillLedger(path, soak_id="soak_old").replay()) == 1
    assert len(FillLedger(path, soak_id="soak_new").replay()) == 1
    assert len(pd.read_csv(path)) == 2, "retired rows must not be deleted"


# ---- reconciliation and degradation ----------------------------------------


def test_net_position_is_what_the_reconciliation_compares():
    assert net_position(OPENED_EARLIER) == 1
    assert net_position(OPENED_EARLIER + THIS_SEGMENT[:1]) == 0
    assert net_position([_f("2026-09-01 16:00", "SELL", 1.0, qty=2)]) == -2


def test_a_missing_ledger_is_ordinary(tmp_path):
    """The first resume after this shipped has no ledger at all."""
    led = FillLedger(tmp_path / "nope.csv", soak_id="s")
    assert led.replay() == []
    assert led.failures == 0


def test_an_unreadable_ledger_degrades_instead_of_raising(tmp_path):
    """A truncated or corrupt file must cost history, never the run — the bar
    cache's rule, for the same reason.

    Note it does not necessarily *raise*: `read_csv` is lenient enough to
    parse a truncated write as one nameless column, which is why the schema
    check has to count as a failure too."""
    path = tmp_path / "fills_night.csv"
    path.write_bytes(b"\x00\x01 not,a,csv\x00")
    led = FillLedger(path, soak_id="s")
    assert led.replay() == []
    assert led.failures == 1


def test_a_ledger_missing_columns_replays_nothing_and_says_so(tmp_path):
    """A file from some other tool that happens to share the name. Replaying
    nothing is right; doing it silently is not."""
    path = tmp_path / "fills_night.csv"
    pd.DataFrame({"a": [1], "b": [2]}).to_csv(path, index=False)
    led = FillLedger(path, soak_id="s")
    assert led.replay() == []
    assert led.failures == 1


def test_an_empty_ledger_is_not_a_failure(tmp_path):
    """A header-only file is what the first run of a soak leaves behind."""
    path = tmp_path / "fills_night.csv"
    pd.DataFrame(columns=LEDGER_COLUMNS).to_csv(path, index=False)
    led = FillLedger(path, soak_id="s")
    assert led.replay() == []
    assert led.failures == 0


def test_a_failed_append_is_counted_and_never_raised(tmp_path, monkeypatch):
    """A process holding a position must not die because a disk filled."""
    led = FillLedger(tmp_path / "fills_night.csv", soak_id="s")

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr("pathlib.Path.open", boom)
    assert led.sync(THIS_SEGMENT) == 0
    assert led.failures == 1
    assert led.summary()["ledger_failures"] == 1
