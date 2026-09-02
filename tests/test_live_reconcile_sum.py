"""Reconciling against the SUM of our own fill ledgers (STRATEGY_SPLIT_PLAN §16).

MEASURED 2026-09-02 (§14), live: a deployment's order callback hears **only its
own** fills, but `list_positions` returns the **combined** book for the account.
`on_reconcile` compares those two numbers, so the first time two deployments hold
a position at once, both halt on `mirror != venue` — with nothing actually wrong.

The fix is a change of operand, not of machinery: the mirror becomes what *we*
recorded for the whole account, summed across every deployment's ledger. Then
both sides of the comparison mean "the account", and a disagreement means what
the rail was written to catch — a fill missed or counted twice.

What must hold:

  * the sum spans deployments and soak epochs, and a BUY and a SELL cancel;
  * an absent or unreadable ledger yields None, never a confident zero — a
    wrong-but-confident number would manufacture the mismatch this removes;
  * `None` from the supplier falls back to the deployment's own position, so a
    single-deployment run and every offline test are byte-identical to before;
  * the two-deployment case that used to halt no longer does, and a genuine
    desync still does.
"""
import csv

import pytest

from forecast_eval.live.ledger import LEDGER_COLUMNS, account_net_position
from forecast_eval.live.sessions import SessionPolicy
from forecast_eval.live.state import HaltAction, RailsConfig, SafetyRails


def _ledger(directory, slug, rows, soak="epoch-1"):
    """Write a `fills_<slug>.csv` holding `rows` of (side, quantity)."""
    path = directory / f"fills_{slug}.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(LEDGER_COLUMNS)
        for side, qty in rows:
            writer.writerow(["2026-09-02 15:00:00", side, 46000.0, qty, 1.2, soak])
    return path


# ---- the sum itself --------------------------------------------------------

def test_no_ledger_is_None_not_zero(tmp_path):
    """A confident zero would under-report the account and manufacture exactly
    the mismatch this exists to remove."""
    assert account_net_position(tmp_path / "missing") is None
    assert account_net_position(tmp_path) is None


def test_the_sum_spans_deployments(tmp_path):
    _ledger(tmp_path, "night", [("BUY", 1)])
    assert account_net_position(tmp_path) == 1
    _ledger(tmp_path, "day", [("BUY", 1)])
    assert account_net_position(tmp_path) == 2, "the two deployments must add"
    _ledger(tmp_path, "day_night", [("SELL", 1)])
    assert account_net_position(tmp_path) == 1, "a SELL must net against BUYs"


def test_opposing_deployments_net_to_flat(tmp_path):
    # Tech: DAY long 1 against NIGHT short 1.
    # Why:  this is the case §14 measured at the venue — the account really is
    #       flat, and our record must say so too, or the rail sees a phantom.
    _ledger(tmp_path, "day", [("BUY", 1)])
    _ledger(tmp_path, "night", [("SELL", 1)])
    assert account_net_position(tmp_path) == 0


def test_all_soak_epochs_are_counted(tmp_path):
    # Tech: two epochs in one ledger; both contribute.
    # Why:  `FillLedger.replay` scopes to `soak_id` because it rebuilds one run's
    #       HISTORY. This asks what the ACCOUNT holds, and a fill from a retired
    #       epoch moved the position just as much. In practice retired epochs net
    #       to zero — `--fresh-start` refuses unless the account is flat — so
    #       including them is correct and normally free.
    path = tmp_path / "fills_night.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(LEDGER_COLUMNS)
        w.writerow(["2026-09-01 15:00:00", "BUY", 46000.0, 1, 1.2, "old-epoch"])
        w.writerow(["2026-09-01 16:00:00", "SELL", 46010.0, 1, 1.2, "old-epoch"])
        w.writerow(["2026-09-02 15:00:00", "BUY", 46100.0, 1, 1.2, "live-epoch"])
    assert account_net_position(tmp_path) == 1


def test_an_unreadable_ledger_is_None_not_a_partial_sum(tmp_path):
    """A partial sum is a confident wrong answer; None degrades to 'cannot
    compare', the same way `broker_position()` does when the venue is silent."""
    _ledger(tmp_path, "night", [("BUY", 1)])
    (tmp_path / "fills_broken.csv").write_text("not,a,ledger\n1,2,3\n",
                                               encoding="utf-8")
    assert account_net_position(tmp_path) is None


# ---- what it does to the rail ---------------------------------------------

def _rails():
    return SafetyRails(RailsConfig(), simulation=True,
                       policy=SessionPolicy.night())


def test_the_two_deployment_case_no_longer_halts():
    # Tech: this deployment holds +1; the other also holds +1; the venue says +2.
    # Why:  §14, the exact failure. Comparing 1 against 2 halts both deployments
    #       for a disagreement that is not a desync. Comparing OUR total (2)
    #       against the venue's 2 is like-for-like and passes.
    rails = _rails()
    rails.on_reconcile(mirror=1, venue=2)          # the old operand
    assert rails.halted, "precondition: the old comparison halts"

    rails = _rails()
    rails.on_reconcile(mirror=2, venue=2)          # the §16 operand
    assert not rails.halted


def test_a_genuine_desync_still_halts():
    """§16 must not weaken the rail — a fill we never recorded is exactly what
    it exists to catch, and it still must stop the loop."""
    rails = _rails()
    rails.on_reconcile(mirror=2, venue=3)
    assert rails.halted
    assert rails.halt_action is HaltAction.STOP


# ---- the fallback: a run with no ledger behaves exactly as before -----------

@pytest.mark.parametrize("declared,own,expected_mirror", [
    (None, 1, 1),      # no ledger -> this deployment's own position
    (2, 1, 2),         # ledger present -> the account total
    (0, 1, 0),         # ledger says flat -> flat, even though we hold 1
])
def test_the_supplier_falls_back_to_own_position(declared, own, expected_mirror):
    # Tech: reproduce the loop's selection logic on its own.
    # Why:  the third row is the important one — a ledger sum of 0 must be used,
    #       not treated as falsy and discarded. `if declared:` instead of
    #       `if declared is not None:` would silently fall back exactly when the
    #       account is flat, which is most of the time.
    mirror = own
    if declared is not None:
        mirror = declared
    assert mirror == expected_mirror


# ---- through a real LiveLoop, not a reproduction of its logic ---------------

from datetime import datetime, timedelta                       # noqa: E402

from forecast_eval.events import (FillEvent, Forecast,          # noqa: E402
                                   MarketEvent)
from forecast_eval.execution import Execution                   # noqa: E402
from forecast_eval.forecaster.base import Forecaster            # noqa: E402
from forecast_eval.live.broker.sim import SimBroker             # noqa: E402
from forecast_eval.live.loop import LiveLoop                    # noqa: E402
from forecast_eval.portfolio import Portfolio                   # noqa: E402
from forecast_eval.strategy.base import SignalEmitter           # noqa: E402
from forecast_eval.trader import Trader                         # noqa: E402

T0 = datetime(2026, 9, 2, 15, 0)


class _Hold(SignalEmitter):
    def emit(self, forecast):
        return "HOLD"


class _Flat(Forecaster):
    warmup_bars = 0
    forecast_stride_bars = 1
    forecast_horizon_bars = 1

    def forecast(self, history_df):
        return Forecast(timestamp=history_df["timestamp"].iloc[-1],
                        horizon_bars=1, payload={"predicted_return": 0.0})


class _VenueSays(SimBroker):
    """A SimBroker that reports a fixed venue position, as a shared account would."""

    def __init__(self, execution, venue):
        super().__init__(execution)
        self._venue = venue

    def broker_position(self):
        return self._venue


def _loop(rails, venue, expected_position=None):
    execution = Execution(fee_rate=0.0)
    broker = _VenueSays(execution, venue)
    trader = Trader(execution=broker, tick_size=1.0, aggression_ticks=0)
    return LiveLoop(
        forecaster=_Flat(), emitter=_Hold(), broker=broker, trader=trader,
        portfolio=Portfolio(contract_multiplier=1.0), rails=rails,
        expected_position=expected_position,
    )


def test_the_loop_halts_without_the_sum_and_survives_with_it(tmp_path):
    # Tech: this deployment is flat; the OTHER one holds +1, so the venue says
    #       +1. Run one bar with and without the ledger-sum supplier.
    # Why:  the end-to-end form of §14/§16, through the real `LiveLoop` rather
    #       than a reproduction of its branch. Without the supplier the loop
    #       compares 0 against 1 and halts — a deployment stopped by another
    #       deployment merely existing. With it, our record also says 1.
    rails = SafetyRails(RailsConfig(), simulation=True)
    _loop(rails, venue=1).on_bar(MarketEvent(T0, 100.0, 1, "NIGHT"))
    assert rails.halted, "the old operand halts on another deployment's position"

    _ledger(tmp_path, "day", [("BUY", 1)])
    rails = SafetyRails(RailsConfig(), simulation=True)
    loop = _loop(rails, venue=1,
                 expected_position=lambda: account_net_position(tmp_path))
    loop.on_bar(MarketEvent(T0 + timedelta(minutes=1), 100.0, 1, "NIGHT"))
    assert not rails.halted, "the ledger sum must match the venue"


def test_the_loop_still_halts_on_a_real_desync(tmp_path):
    """§16 must not weaken the rail: the venue holding a lot no ledger knows
    about is the state it exists to refuse to trade through."""
    _ledger(tmp_path, "day", [("BUY", 1)])
    rails = SafetyRails(RailsConfig(), simulation=True)
    loop = _loop(rails, venue=2,
                 expected_position=lambda: account_net_position(tmp_path))
    loop.on_bar(MarketEvent(T0, 100.0, 1, "NIGHT"))
    assert rails.halted


def test_a_missing_ledger_reproduces_the_previous_behaviour(tmp_path):
    """The supplier returning None must leave the loop on its own position —
    every single-deployment run and every offline test depends on it."""
    rails = SafetyRails(RailsConfig(), simulation=True)
    loop = _loop(rails, venue=0,
                 expected_position=lambda: account_net_position(tmp_path))
    loop.on_bar(MarketEvent(T0, 100.0, 1, "NIGHT"))
    assert not rails.halted, "flat mirror vs flat venue"


# ---- the two things the tests above ASSUME rather than prove ----------------

def test_what_FillLedger_writes_is_what_the_sum_reads(tmp_path):
    # Tech: write fills through the real `FillLedger.sync`, then read them back
    #       with `account_net_position` — no hand-built CSV anywhere.
    # Why:  every test above writes the ledger itself, using `LEDGER_COLUMNS`.
    #       That shares a constant with the writer but not the WRITER, so a
    #       change to how `sync` spells `side` (or a column reorder) would leave
    #       all of them green while production silently summed to zero. This is
    #       the round trip, and it is the only test here that would catch that.
    from forecast_eval.live.ledger import FillLedger, ledger_path
    from forecast_eval.live.sessions import SessionPolicy

    path = ledger_path(tmp_path, SessionPolicy.night())
    ledger = FillLedger(path, soak_id="epoch-1")
    fills = [
        FillEvent(timestamp=T0, side="BUY", fill_price=46000.0, quantity=1, fee=1.2),
        FillEvent(timestamp=T0 + timedelta(minutes=1), side="BUY",
                  fill_price=46010.0, quantity=2, fee=1.2),
        FillEvent(timestamp=T0 + timedelta(minutes=2), side="SELL",
                  fill_price=46020.0, quantity=1, fee=1.2),
    ]
    assert ledger.sync(fills) == 3
    assert path.exists()
    # +1 +2 -1 = +2, read back through the production reader.
    assert account_net_position(tmp_path) == 2
    # ...and it agrees with the in-memory helper the resume path uses.
    from forecast_eval.live.ledger import net_position
    assert account_net_position(tmp_path) == net_position(fills)


def test_the_directory_cli_passes_is_the_one_the_ledgers_land_in():
    # Tech: `cli.py` writes to `ledger_path(out.parent, policy)` and passes
    #       `ledger_dir=out.parent`. Assert those two agree for every deployment.
    # Why:  the whole fix is inert if the sum reads a directory the ledgers are
    #       not written into — and it would be inert SILENTLY, because an empty
    #       directory returns None and falls back to the old behaviour. Nothing
    #       else in the suite connects those two arguments.
    from pathlib import Path

    from forecast_eval.live.config import CONFIGS, config_named
    from forecast_eval.live.ledger import ledger_path

    out_parent = Path("outputs")
    for name in CONFIGS:
        policy = config_named(name).policy
        assert ledger_path(out_parent, policy).parent == out_parent, name

    # The three deployments must also land on three DIFFERENT files, or the sum
    # would count one deployment twice and miss another.
    names = {ledger_path(out_parent, config_named(n).policy).name
             for n in CONFIGS}
    assert len(names) == len(CONFIGS), names


def test_the_glob_matches_every_deployment_filename():
    # Tech: `account_net_position` globs `fills_*.csv`; check each deployment's
    #       real filename matches that pattern.
    # Why:  a deployment whose ledger the glob misses is a deployment whose
    #       position is invisible to the rail — the exact silent under-count the
    #       fix exists to prevent, reintroduced by a naming change.
    import fnmatch
    from pathlib import Path

    from forecast_eval.live.config import CONFIGS, config_named
    from forecast_eval.live.ledger import ledger_path

    for name in CONFIGS:
        filename = ledger_path(Path("outputs"), config_named(name).policy).name
        assert fnmatch.fnmatch(filename, "fills_*.csv"), (name, filename)
