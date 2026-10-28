"""A stateful exit rule survives a restart (`live/exit_state.py`).

2026-10-29: a network drop stopped every deployment while fullday held a long,
and `--resume` refused it — its break-even lock tracks the best price since
entry, and that number died with the process. The lock could not be adopted,
and the account's netting meant flatten could not close fullday's lot alone.

What is asserted here: a `restorable` rule's state is written to disk while
live, read back by `adopt_position` only for the same lot, overridable by a
typed `--exit-state`, and refused loudly whenever neither source can be trusted
— never armed on an invented value.
"""
import json
from datetime import datetime

import pytest

from forecast_eval.events import MarketEvent
from forecast_eval.exits.base import ExitRule, PositionState
from forecast_eval.live import cli, exit_state
from forecast_eval.live.broker.base import HeldPosition
from forecast_eval.live.exit_state import ExitStateFile
from forecast_eval.live.recorder import Recorder

from .test_live_resume import NOW, _feed_one_bar, _runtime

pytestmark = pytest.mark.usefixtures("cheap_strategy")


class _BestPrice(ExitRule):
    """A minimal restorable rule: tracks the best price since entry, never fires."""
    restorable = True
    state_keys = ("best_price",)

    def __init__(self):
        self._best = None
        self._key = None

    def should_exit(self, position, market, *, bar_idx):
        key = (position.entry_bar_idx, position.entry_price, position.side)
        if key != self._key:
            self._key, self._best = key, market.price
        pick = max if position.side == "LONG" else min
        self._best = pick(self._best, market.price)
        return False

    def export_state(self):
        return None if self._best is None else {"best_price": self._best}

    def restore_state(self, position, state):
        self._key = (position.entry_bar_idx, position.entry_price, position.side)
        self._best = state["best_price"]


def _record(path, *, side="LONG", entry=46616.0, state=None):
    path.write_text(json.dumps({
        "slug": "day_night", "side": side, "entry_price": entry,
        "state": {"best_price": 47376.0} if state is None else state,
        "written_at": "2026-10-28T15:23:01",
    }), encoding="utf-8")


def _position(side="LONG", entry=46616.0):
    return PositionState(side=side, size=1, entry_price=entry,
                         entry_bar_idx=-1, entry_timestamp=NOW)


def _market(price, minute=0):
    return MarketEvent(timestamp=datetime(2026, 10, 29, 9, minute),
                       price=price, volume=1, session="DAY")


# ---- parsing the typed form --------------------------------------------------

def test_a_typed_state_parses_to_floats():
    assert exit_state.parse_injection(["best_price=47376"]) == {"best_price": 47376.0}


@pytest.mark.parametrize("item,match", [
    ("best_price", "KEY=VALUE"),
    ("=47376", "KEY=VALUE"),
    ("best_price=high", "not a number"),
    ("best_price=nan", "not finite"),
])
def test_a_malformed_typed_state_is_refused(item, match):
    with pytest.raises(ValueError, match=match):
        exit_state.parse_injection([item])


def test_a_repeated_key_is_refused():
    with pytest.raises(ValueError, match="given twice"):
        exit_state.parse_injection(["best_price=1", "best_price=2"])


# ---- choosing the state ------------------------------------------------------

def test_the_file_is_used_for_the_same_lot(tmp_path):
    path = tmp_path / "exit_state_day_night.json"
    _record(path)
    state, source = exit_state.resolve(keys=("best_price",), side="LONG",
                                       entry_price=46616.0, path=path)
    assert state == {"best_price": 47376.0} and source == "file"


def test_a_typed_state_overrides_the_file(tmp_path):
    path = tmp_path / "exit_state_day_night.json"
    _record(path)
    state, source = exit_state.resolve(
        keys=("best_price",), side="LONG", entry_price=46616.0, path=path,
        injected={"best_price": 47000.0})
    assert state == {"best_price": 47000.0} and source == "injected"


def test_a_typed_state_with_the_wrong_keys_is_refused(tmp_path):
    with pytest.raises(ValueError, match="needs exactly"):
        exit_state.resolve(keys=("best_price",), side="LONG", entry_price=1.0,
                           path=tmp_path / "x.json", injected={"best": 1.0})


def test_no_file_refuses_and_says_what_to_type(tmp_path):
    with pytest.raises(ValueError, match="--exit-state best_price=<value>"):
        exit_state.resolve(keys=("best_price",), side="LONG",
                           entry_price=46616.0, path=tmp_path / "absent.json")


@pytest.mark.parametrize("side,entry", [("SHORT", 46616.0), ("LONG", 46700.0)],
                         ids=["other-side", "other-entry"])
def test_a_file_from_another_lot_is_refused(tmp_path, side, entry):
    """The file outlives a crash between an entry fill and its first write, and a
    roll moves the venue's average entry. Lending one trade's best price to
    another would arm the lock on a number this lot never saw."""
    path = tmp_path / "exit_state_day_night.json"
    _record(path, side=side, entry=entry)
    with pytest.raises(ValueError, match="different lot"):
        exit_state.resolve(keys=("best_price",), side="LONG",
                           entry_price=46616.0, path=path)


def test_a_file_with_the_wrong_keys_is_refused(tmp_path):
    path = tmp_path / "exit_state_day_night.json"
    _record(path, state={"trail": 1.0})
    with pytest.raises(ValueError, match="needs exactly"):
        exit_state.resolve(keys=("best_price",), side="LONG",
                           entry_price=46616.0, path=path)


def test_a_torn_file_is_refused_not_read_as_absent(tmp_path):
    path = tmp_path / "exit_state_day_night.json"
    path.write_text('{"side": "LO', encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        exit_state.resolve(keys=("best_price",), side="LONG",
                           entry_price=46616.0, path=path)


# ---- writing it while live ---------------------------------------------------

def test_the_writer_writes_only_on_change_and_round_trips(tmp_path):
    path = tmp_path / "exit_state_day_night.json"
    rule = _BestPrice()
    sink = ExitStateFile(path, rule=rule, slug="day_night", contract="TXFK6",
                         soak_id="soak_x")
    pos = _position()
    for minute, price in enumerate([46700.0, 46900.0, 46800.0, 46850.0]):
        m = _market(price, minute)
        rule.should_exit(pos, m, bar_idx=minute)
        sink.update(pos, m)
    assert sink.writes == 2, "the best moved twice; the other bars change nothing"
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["state"] == {"best_price": 46900.0}
    assert record["contract"] == "TXFK6" and record["soak_id"] == "soak_x"
    assert not path.with_name(path.name + ".tmp").exists()
    state, _ = exit_state.resolve(keys=rule.state_keys, side="LONG",
                                  entry_price=46616.0, path=path)
    assert state == {"best_price": 46900.0}


def test_going_flat_deletes_the_file(tmp_path):
    path = tmp_path / "exit_state_day_night.json"
    rule = _BestPrice()
    sink = ExitStateFile(path, rule=rule, slug="day_night")
    rule.should_exit(_position(), _market(46700.0), bar_idx=0)
    sink.update(_position(), _market(46700.0))
    assert path.exists()
    sink.update(None, _market(46700.0, 1))
    assert not path.exists()


def test_a_flat_start_removes_a_stale_file(tmp_path):
    path = tmp_path / "exit_state_day_night.json"
    _record(path)
    ExitStateFile(path, rule=_BestPrice(), slug="day_night").update(
        None, _market(46700.0))
    assert not path.exists()


def test_a_failed_write_is_recorded_not_raised(tmp_path):
    """The loop is holding a live position; a full disk must not stop it."""
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("", encoding="utf-8")
    rec = Recorder(tmp_path / "s.jsonl", simulation=True)
    rule = _BestPrice()
    sink = ExitStateFile(blocker / "exit_state.json", rule=rule,
                         slug="day_night", recorder=rec)
    rule.should_exit(_position(), _market(46700.0), bar_idx=0)
    sink.update(_position(), _market(46700.0))
    rec.close()
    assert sink.failures == 1
    assert "exit_state_write_failed" in (tmp_path / "s.jsonl").read_text(
        encoding="utf-8")


# ---- adopting with it ----------------------------------------------------------

def test_adoption_restores_the_saved_state(tmp_path):
    path = tmp_path / "exit_state_day_night.json"
    _record(path, entry=17480.0)
    rt = _runtime(HeldPosition(1, 17480.0))
    rule = rt.loop.exit_rule = _BestPrice()
    adopted = rt.adopt_position(timestamp=NOW, exit_state_path=path)
    assert adopted["exit_state"] == {"best_price": 47376.0}
    assert adopted["exit_state_source"] == "file"
    assert rule.export_state() == {"best_price": 47376.0}


def test_the_restored_state_survives_the_first_bar(tmp_path):
    """Restored under the adopted entry identity, so the rule's new-trade check
    does not reset it on the first bar — the best stays above every print."""
    path = tmp_path / "exit_state_day_night.json"
    rt = _runtime(HeldPosition(1, 17480.0))
    rule = rt.loop.exit_rule = _BestPrice()
    rt.adopt_position(timestamp=NOW, exit_state_path=path,
                      exit_state_injected={"best_price": 19000.0})
    rt.loop.exit_state_sink = ExitStateFile(path, rule=rule, slug="night")
    _feed_one_bar(rt)
    assert rule.export_state() == {"best_price": 19000.0}
    assert json.loads(path.read_text(encoding="utf-8"))["state"] == {
        "best_price": 19000.0}


def test_adoption_without_any_state_is_refused_and_seeds_nothing(tmp_path):
    rt = _runtime(HeldPosition(1, 17480.0))
    rt.loop.exit_rule = _BestPrice()
    with pytest.raises(ValueError, match="--exit-state best_price=<value>"):
        rt.adopt_position(timestamp=NOW,
                          exit_state_path=tmp_path / "absent.json")
    assert rt.trader.position == 0 and rt.portfolio.position == 0


def test_a_typed_state_for_a_rule_without_state_is_refused(tmp_path):
    from forecast_eval.exits.pct_stop import EntryPercentStop
    rt = _runtime(HeldPosition(1, 17480.0))
    rt.loop.exit_rule = EntryPercentStop(stop_pct=0.05)
    with pytest.raises(ValueError, match="no restorable state"):
        rt.adopt_position(timestamp=NOW, exit_state_path=tmp_path / "x.json",
                          exit_state_injected={"best_price": 1.0})


def test_a_flat_account_ignores_the_state(tmp_path):
    rt = _runtime(HeldPosition(0, 0.0))
    rt.loop.exit_rule = _BestPrice()
    assert rt.adopt_position(timestamp=NOW, exit_state_path=tmp_path / "x.json",
                             exit_state_injected={"best_price": 1.0}) is None


# ---- the command line --------------------------------------------------------

@pytest.mark.parametrize("argv", [
    ["--exit-state", "best_price=47376"],
    ["--resume", "--exit-state", "best_price"],
], ids=["without-resume", "malformed"])
def test_a_bad_exit_state_flag_is_refused_before_anything_runs(tmp_path, argv):
    rc = cli.main(argv + ["--out", str(tmp_path / "run")])
    assert rc == 5
    assert not (tmp_path / "run").exists(), "refused before any file is written"
