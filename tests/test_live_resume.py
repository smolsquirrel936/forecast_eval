"""Resuming into a position the process did not open (§2.20).

The strategy holds a median 4.5 days, so a multi-day soak that is killed and
restarted almost always comes back to an open position. Before this, three
separate layers said no: the CLI refused to start, the reconciliation rail
compared a zero mirror against the venue and halted on the first bar, and
nothing seeded the mirror in the first place — so the no-flip machine would have
read "flat" and opened a second lot.

What is asserted here is that adoption is *faithful*: the venue's size and the
venue's entry price, in both mirrors, or a loud refusal. An adoption that
silently rounded the entry to the current market would flatter every restart of
a losing trade, and nothing downstream could tell.
"""
from datetime import datetime, timedelta

import pandas as pd
import pytest

from forecast_eval.events import FillEvent
from forecast_eval.execution import Execution
from forecast_eval.live.broker.base import HeldPosition
from forecast_eval.live.broker.dryrun import DryRunBroker
from forecast_eval.live.broker.shadow import ShadowBroker
from forecast_eval.live.broker.shioaji import ShioajiBroker
from forecast_eval.live.recorder import Recorder
from forecast_eval.live.runtime import LiveRuntime
from forecast_eval.live.state import HaltAction

from .fakes.fake_shioaji import FakePosition, FakeShioaji, make_contract
from .test_live_runtime import CTX, _bars, _config

NOW = datetime(2026, 5, 14, 18, 0)


class _HeldBroker(DryRunBroker):
    """A broker that reports a venue position. Everything else is the offline
    fill model, so a bar can be driven through the loop without a feed."""

    # Tech: this double stands in for a broker with a real account.
    # Why:  it answers `broker_position_detail`, so a None from it means the
    #       query failed rather than "there is no venue" — which is the case the
    #       reconciliation must refuse rather than skip.
    has_venue = True

    def __init__(self, detail, **kw):
        super().__init__(Execution(fee_rate=0.0), **kw)
        self._detail = detail

    def broker_position_detail(self):
        return self._detail

    def broker_position(self):
        return None if self._detail is None else self._detail.signed_qty


def _runtime(detail, **kw):
    from forecast_eval.live.runtime import LiveRuntime
    return LiveRuntime(_config(), broker=_HeldBroker(detail), **kw)


# ---- what adoption puts where ----------------------------------------------

def test_adopting_a_long_seeds_both_mirrors():
    rt = _runtime(HeldPosition(1, 17480.0))
    adopted = rt.adopt_position(timestamp=NOW)

    assert adopted == {"position": 1, "entry_price": 17480.0, "adopted_at": NOW,
                       "source": "venue"}
    assert rt.trader.position == 1
    assert rt.trader.entry_price == 17480.0
    assert rt.trader.entry_timestamp == NOW
    # The Portfolio is the other mirror, and the one that realizes the money.
    assert rt.portfolio.position == 1
    assert rt.portfolio.avg_entry_price == 17480.0


def test_adopting_a_short_keeps_the_sign():
    rt = _runtime(HeldPosition(-1, 17520.0))
    rt.adopt_position(timestamp=NOW)
    assert rt.trader.position == -1
    assert rt.portfolio.position == -1
    assert rt.trader.position_state().side == "SHORT"


def test_the_venue_entry_price_survives_into_realized_pnl():
    """The point of carrying `avg_price` at all. Adopt a long at 17480, close at
    17530: the 50 points must be there. An adoption that reset entry to the
    market would book zero."""
    rt = _runtime(HeldPosition(1, 17480.0))
    rt.adopt_position(timestamp=NOW)
    rt.portfolio.apply_fill(
        FillEvent(timestamp=NOW + timedelta(minutes=1), side="SELL",
                  fill_price=17530.0, quantity=1, fee=0.0),
        session="NIGHT")
    assert rt.portfolio.realized_pnl == pytest.approx(50.0)
    assert rt.portfolio.position == 0


def test_position_state_is_answerable_immediately_after_adopting():
    """`position_state()` asserts a non-zero position always has an entry
    timestamp. Seeding size without it would trip that assertion on the first
    bar rather than at adoption time."""
    rt = _runtime(HeldPosition(1, 17480.0))
    rt.adopt_position(timestamp=NOW)
    state = rt.trader.position_state()
    assert state.side == "LONG" and state.entry_price == 17480.0


def test_the_adoption_is_recorded():
    """A resumed run reads differently from a fresh one; the trail has to say
    which it was."""
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as d:
        rec = Recorder(Path(d) / "s.jsonl", simulation=True)
        rt = _runtime(HeldPosition(-1, 17520.0), recorder=rec)
        rt.adopt_position(timestamp=NOW)
        rec.close()
        text = (Path(d) / "s.jsonl").read_text(encoding="utf-8")
    assert "position_adopted" in text and "17520" in text


# ---- when there is nothing to adopt ----------------------------------------

def test_a_flat_account_adopts_nothing():
    rt = _runtime(HeldPosition(0, 0.0))
    assert rt.adopt_position(timestamp=NOW) is None
    assert rt.trader.position == 0


def test_a_broker_that_cannot_answer_adopts_nothing():
    """None is "cannot compare", not "flat" — the same degradation the
    reconciliation rail makes. It must not become a fabricated position."""
    rt = _runtime(None)
    assert rt.adopt_position(timestamp=NOW) is None
    assert rt.trader.position == 0


def test_offline_brokers_opt_out_by_inheritance():
    """`DryRunBroker` and `SimBroker` always start flat, so the adopt step has to
    be a no-op for them rather than something each must implement."""
    assert DryRunBroker(Execution(fee_rate=0.0)).broker_position_detail() is None


# ---- what adoption refuses --------------------------------------------------

def test_a_position_above_max_position_is_refused():
    """`Trader.on_fill` has no average-entry branch for a second lot (§2.10) —
    the same reason `check_startup` raises. Adopting two lots would produce
    wrong accounting rather than an error."""
    rt = _runtime(HeldPosition(2, 17480.0))
    with pytest.raises(ValueError, match="above max_position"):
        rt.adopt_position(timestamp=NOW)
    assert rt.trader.position == 0


def test_adoption_is_refused_when_an_exit_rule_is_configured():
    """The entry *bar index* belongs to a process that is gone. Live runs
    configure no exit rule today; the day one is added, a stop timed from a
    sentinel must fail loudly instead of firing at the wrong moment."""
    rt = _runtime(HeldPosition(1, 17480.0))
    rt.loop.exit_rule = object()
    with pytest.raises(ValueError, match="ExitRule"):
        rt.adopt_position(timestamp=NOW)


def test_adopting_twice_is_refused():
    rt = _runtime(HeldPosition(1, 17480.0))
    rt.adopt_position(timestamp=NOW)
    with pytest.raises(ValueError, match="already at"):
        rt.adopt_position(timestamp=NOW)


# ---- the rail this exists to satisfy ---------------------------------------

def _feed_one_bar(rt):
    rt.warm_up(_bars(CTX + 10))
    t0 = pd.Timestamp("2026-05-14 18:00")
    for i in range(2):
        rt.on_tick(t0 + timedelta(minutes=i, seconds=5), 17500.0 + i, 4)
    rt.on_clock(t0 + timedelta(minutes=3))


def test_the_reconciliation_rail_halts_a_restart_that_did_not_adopt():
    """The failure being fixed, asserted directly: mirror 0 against venue 1 is a
    dispute, and the rail's answer to a dispute is STOP (§2.9)."""
    rt = _runtime(HeldPosition(1, 17480.0))
    _feed_one_bar(rt)
    assert rt.rails.halted
    assert rt.rails.halt_action is HaltAction.STOP
    assert "mirror 0 != venue 1" in rt.rails.halt_reason


def test_after_adopting_the_same_restart_runs():
    rt = _runtime(HeldPosition(1, 17480.0))
    rt.adopt_position(timestamp=NOW)
    _feed_one_bar(rt)
    assert not rt.rails.halted
    assert rt.result().n_bars > 0


# ---- reading the position off the real adapter -----------------------------

def _broker(api, **kw):
    # The alias as the SDK really presents it: `code='TXFR1'` with the resolved
    # month in `target_code` (field verified on `sj.Future`, 2026-08-12). That
    # pairing is what lets adoption see a position *before* the first order —
    # `_codes` otherwise learns the concrete code from an order callback, which
    # at start-up has not happened yet.
    contract = make_contract(code="TXFR1", target_code="TXFH6")
    return ShioajiBroker(api, contract, sj=_sj(), **kw)


def _sj():
    from .fakes import fake_shioaji
    return fake_shioaji


def test_the_adapter_reports_size_and_entry_price():
    api = FakeShioaji()
    api.positions = [FakePosition(code="TXFH6", direction="Buy", quantity=1,
                                  price=17480.0)]
    detail = _broker(api).broker_position_detail()
    assert detail == HeldPosition(1, 17480.0)


def test_a_short_reads_back_negative():
    api = FakeShioaji()
    api.positions = [FakePosition(code="TXFH6", direction="Action.Sell",
                                  quantity=1, price=17520.0)]
    assert _broker(api).broker_position_detail() == HeldPosition(-1, 17520.0)


def test_positions_in_other_contracts_are_ignored():
    """The alias trap (§2.18b) in its second form: a position in a *different*
    future must not be adopted as ours."""
    api = FakeShioaji()
    api.positions = [
        FakePosition(code="MXFH6", direction="Buy", quantity=3, price=17000.0),
        FakePosition(code="TXFH6", direction="Buy", quantity=1, price=17480.0),
    ]
    assert _broker(api).broker_position_detail() == HeldPosition(1, 17480.0)


def test_multiple_lots_average_by_size():
    api = FakeShioaji()
    api.positions = [
        FakePosition(code="TXFH6", direction="Buy", quantity=1, price=17400.0),
        FakePosition(code="TXFH6", direction="Buy", quantity=3, price=17500.0),
    ]
    assert _broker(api).broker_position_detail() == HeldPosition(4, 17475.0)


def test_a_failed_query_reads_as_unknown_not_flat():
    api = FakeShioaji()
    api.list_positions_error = RuntimeError("boom")
    assert _broker(api).broker_position_detail() is None
    assert _broker(api).broker_position() is None


def test_the_shadow_passes_the_venue_position_through():
    """`ShadowBroker` wraps the live broker for V4. Answering from its own
    offline `Execution` would report flat and a resumed run would adopt
    nothing."""
    api = FakeShioaji()
    api.positions = [FakePosition(code="TXFH6", direction="Buy", quantity=1,
                                  price=17480.0)]
    shadow = ShadowBroker(_broker(api), fee_rate=0.0)
    assert shadow.broker_position_detail() == HeldPosition(1, 17480.0)


# ---- adopting when there is no venue to ask (a dry run's own ledger) ---------


def _ledger_fills():
    from forecast_eval.events import FillEvent as _F
    # the night ledger as it stands: a closed SHORT and one BUY left open
    return [_F(timestamp=NOW, side="SELL", fill_price=44893.0, quantity=1, fee=0.0),
            _F(timestamp=NOW, side="BUY", fill_price=44520.0, quantity=1, fee=0.0),
            _F(timestamp=NOW, side="BUY", fill_price=44555.0, quantity=1, fee=0.0)]


def test_venue_position_separates_a_flat_account_from_no_account():
    """The distinction the ledger reconciliation turns on. `adopt_position`
    returns None for both, which is why the answer is recorded separately."""
    flat = _runtime(HeldPosition(0, 0.0))
    flat.adopt_position(timestamp=NOW)
    assert flat.venue_position == 0             # a venue answered: flat

    from forecast_eval.live.ledger import open_position
    offline = LiveRuntime(_config(), broker=DryRunBroker(Execution(fee_rate=0.0)))
    offline.adopt_position(timestamp=NOW, fallback=open_position(_ledger_fills()))
    assert offline.venue_position is None       # nobody was asked


def test_a_broker_with_no_venue_adopts_from_the_ledger():
    """MEASURED on `live_night_20260902_082640`: a dry run's ledger replayed to
    +1 against a venue that was never asked, so the history was refused on every
    start and the run wrote an empty fills.csv and trades.csv."""
    from forecast_eval.live.ledger import open_position

    rt = LiveRuntime(_config(), broker=DryRunBroker(Execution(fee_rate=0.0)))
    adopted = rt.adopt_position(timestamp=NOW,
                                fallback=open_position(_ledger_fills()))
    assert adopted["source"] == "ledger"
    assert adopted["position"] == 1
    assert adopted["entry_price"] == 44555.0     # the lot left open, not an average
    # Both mirrors, or the seeded fills net +1 against a Trader that thinks it is
    # flat and the next fill pairs against a lot it does not know it holds.
    assert rt.trader.position == 1
    assert rt.portfolio.position == 1
    assert rt.portfolio.avg_entry_price == 44555.0


def test_a_failed_venue_query_never_adopts_from_the_ledger():
    """`ShioajiBroker.broker_position_detail` also returns None when
    `list_positions` raised. Substituting the ledger there would seed a history
    nothing checked against the real account — the exact confidently-wrong
    trades.csv the reconciliation exists to prevent."""
    from forecast_eval.live.ledger import open_position

    rt = _runtime(None)                          # has_venue=True, query failed
    assert rt.adopt_position(timestamp=NOW,
                             fallback=open_position(_ledger_fills())) is None
    assert rt.trader.position == 0
    assert rt.venue_position is None


def test_a_flat_venue_still_refuses_a_ledger_that_disagrees():
    """The fallback must not become a back door around the reconciliation: a
    real account reporting flat is an answer, and a ledger claiming +1 against
    it is still wrong."""
    from forecast_eval.live.ledger import open_position

    rt = _runtime(HeldPosition(0, 0.0))
    assert rt.adopt_position(timestamp=NOW,
                             fallback=open_position(_ledger_fills())) is None
    assert rt.trader.position == 0
    assert rt.venue_position == 0                # which is what cli.py compares
