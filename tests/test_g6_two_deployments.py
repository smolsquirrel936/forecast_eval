"""G6 — two deployments holding at once, through the whole stack (§18).

    two deployments hold non-flat positions across a full session; neither rail
    halts; the venue's reported position equals the SUM of the deployments' own
    fill ledgers; and each deployment's PnL is still attributed to it alone.

This is the scenario §14 measured as a guaranteed double halt on one shared
account, and §7.1 predicted before that. Everything here is real except the
venue: two `LiveLoop`s, two `ClientBroker`s, one `OwnerProcess`, one
`OrderBook`, and the real `FillLedger` writer. A reproduction of the wiring
would prove nothing — the wiring is what is on trial.

The live half of G6 (a real session, real latency) is `tools/g6_live.py`.
"""
from datetime import datetime, timedelta

import pytest

from forecast_eval.events import Forecast, MarketEvent
from forecast_eval.execution import Execution
from forecast_eval.forecaster.base import Forecaster
from forecast_eval.live.broker.client import ClientBroker
from forecast_eval.live.ledger import (FillLedger, account_net_position,
                                       per_deployment_nets)
from forecast_eval.live.loop import LiveLoop
from forecast_eval.live.owner_process import OwnerProcess
from forecast_eval.live.state import RailsConfig, SafetyRails
from forecast_eval.portfolio import Portfolio
from forecast_eval.strategy.base import SignalEmitter
from forecast_eval.trader import Trader

from .test_live_owner_process import _Contract, _FakeSJ, _Row

T0 = datetime(2026, 9, 7, 15, 0)


# ---- a venue that actually fills -------------------------------------------

class _Venue:
    """Accepts orders, fills them on demand, and reports the resulting net.

    Deliberately not the `_FakeAPI` used elsewhere: those tests inject deals by
    hand, which is right for testing attribution and wrong here. G6 is about the
    loop closing — intent, order, fill, event, mirror, ledger, venue — so the
    venue has to move its own position and answer `list_positions` from it.
    """

    def __init__(self):
        self.net = 0
        self.callback = None
        self.placed = []
        self.cancelled = []
        self._queued = []
        self._n = 0

    def set_order_callback(self, cb):
        self.callback = cb

    def place_order(self, contract, order):
        self._n += 1
        ordno, oid = f"NO{self._n}", f"OID{self._n}"
        self.placed.append(order)
        self._queued.append((ordno, str(order.action), order.quantity,
                             float(order.price)))
        return type("T", (), {
            "order": type("o", (), {"id": oid, "ordno": ordno})()})()

    def cancel_order(self, trade):
        self.cancelled.append(trade)

    def list_positions(self, account=None):
        if self.net == 0:
            return []
        return [_Row(abs(self.net), "Buy" if self.net > 0 else "Sell")]

    def fill_all(self, at_price=None):
        """Fill every working order, move the book, and report each deal."""
        queued, self._queued = self._queued, []
        for ordno, action, qty, price in queued:
            self.net += qty if action.endswith("Buy") else -qty
            self.callback(type("S", (), {"name": "FuturesDeal"})(), {
                "ordno": ordno, "trade_id": None, "exchange_seq": "1",
                "action": "Buy" if action.endswith("Buy") else "Sell",
                "price": at_price if at_price is not None else price,
                "quantity": qty, "ts": T0.timestamp(),
            })


# ---- deployments -----------------------------------------------------------

class _Scripted(SignalEmitter):
    """Emits a scripted sequence, then HOLD forever."""

    def __init__(self, *signals):
        self._signals = list(signals)

    def emit(self, forecast):
        return self._signals.pop(0) if self._signals else "HOLD"


class _Flat(Forecaster):
    warmup_bars = 0
    forecast_stride_bars = 1
    forecast_horizon_bars = 1

    def forecast(self, history_df):
        return Forecast(timestamp=history_df["timestamp"].iloc[-1],
                        horizon_bars=1, payload={"predicted_return": 0.0})


class _Deployment:
    """One deployment: its own loop, rails, portfolio, ledger and slug."""

    def __init__(self, tmp_path, slug, *signals):
        self.slug = slug
        self.rails = SafetyRails(RailsConfig(), simulation=True)
        self.broker = ClientBroker(tmp_path, slug, rails=self.rails)
        self.portfolio = Portfolio(contract_multiplier=1.0)
        self.trader = Trader(execution=self.broker, tick_size=1.0,
                             aggression_ticks=0)
        self.loop = LiveLoop(
            forecaster=_Flat(), emitter=_Scripted(*signals), broker=self.broker,
            trader=self.trader, portfolio=self.portfolio, rails=self.rails,
        )
        # The real writer, at the path `ledger_path` produces for this slug.
        self.ledger = FillLedger(tmp_path / f"fills_{slug}.csv", soak_id="g6")

    def bar(self, market):
        self.loop.on_bar(market)

    def flush(self):
        self.ledger.sync(self.loop.fills)


def _run(tmp_path, day_signals, night_signals, bars=6):
    """Drive both deployments and the owner through `bars` bars."""
    venue = _Venue()
    owner = OwnerProcess(venue, _Contract(), directory=tmp_path,
                         slugs=("day", "night"), account="ACC", sj=_FakeSJ,
                         fee_rate=0.0)
    owner.start()

    day = _Deployment(tmp_path, "day", *day_signals)
    night = _Deployment(tmp_path, "night", *night_signals)

    for i in range(bars):
        market = MarketEvent(T0 + timedelta(minutes=i), 100.0 + i, 1, "NIGHT")
        day.bar(market)
        night.bar(market)
        owner.poll()        # drain intents, place
        venue.fill_all()    # the venue does its half
        owner.poll()        # route the fills back, republish positions

    day.flush()
    night.flush()
    return owner, venue, day, night


# ---- G6, clause by clause --------------------------------------------------

def test_opposite_positions_net_at_the_venue_and_nobody_halts(tmp_path):
    """§14's exact scenario: a BUY from one and a SELL from the other.

    Measured on the real account, this flattened the position and halted BOTH
    deployments on `mirror != venue`. Under the owner it must not.
    """
    owner, venue, day, night = _run(tmp_path, ["BUY"], ["SELL"])

    assert day.trader.position == 1
    assert night.trader.position == -1
    assert venue.net == 0, "the account really is flat — netting is real"
    assert not day.rails.halted
    assert not night.rails.halted


def test_the_same_direction_leaves_the_account_holding_two(tmp_path):
    """The non-trivial reconcile: venue 2 against two ledgers of 1."""
    owner, venue, day, night = _run(tmp_path, ["BUY"], ["BUY"])

    assert day.trader.position == 1 and night.trader.position == 1
    assert venue.net == 2
    assert owner.book.account_net == 2
    assert owner.reconcile() is True
    assert not day.rails.halted and not night.rails.halted


def test_the_venue_equals_the_sum_of_the_deployments_own_ledgers(tmp_path):
    """G6's third clause, through the real FillLedger writer and reader."""
    owner, venue, day, night = _run(tmp_path, ["BUY"], ["BUY"])

    assert per_deployment_nets(tmp_path) == {"day": 1, "night": 1}
    assert account_net_position(tmp_path) == venue.net == 2


def test_the_ledgers_sum_correctly_when_the_two_oppose(tmp_path):
    owner, venue, day, night = _run(tmp_path, ["BUY"], ["SELL"])

    assert per_deployment_nets(tmp_path) == {"day": 1, "night": -1}
    assert account_net_position(tmp_path) == venue.net == 0


def test_each_deployment_books_only_its_own_fills(tmp_path):
    """G6's fourth clause. A peer's fill must not reach this Portfolio."""
    owner, venue, day, night = _run(tmp_path, ["BUY"], ["SELL"])

    assert [f.side for f in day.loop.fills] == ["BUY"]
    assert [f.side for f in night.loop.fills] == ["SELL"]
    assert len(day.loop.fills) == 1 and len(night.loop.fills) == 1


def test_pnl_is_attributed_to_the_deployment_that_earned_it(tmp_path):
    """Opposite positions: one wins what the other loses, and each books its own."""
    venue = _Venue()
    owner = OwnerProcess(venue, _Contract(), directory=tmp_path,
                         slugs=("day", "night"), account="ACC", sj=_FakeSJ,
                         fee_rate=0.0)
    owner.start()
    day = _Deployment(tmp_path, "day", "BUY")
    night = _Deployment(tmp_path, "night", "SELL")

    # Bar 0: both open at 100.
    m0 = MarketEvent(T0, 100.0, 1, "NIGHT")
    day.bar(m0)
    night.bar(m0)
    owner.poll()
    venue.fill_all(at_price=100.0)
    owner.poll()

    # Bars 1-3: the market rallies to 130 and both simply hold.
    for i, price in enumerate((110.0, 120.0, 130.0), start=1):
        market = MarketEvent(T0 + timedelta(minutes=i), price, 1, "NIGHT")
        day.bar(market)
        night.bar(market)
        owner.poll()
        venue.fill_all()
        owner.poll()

    # Tech: mark each Portfolio to the last price and compare.
    # Why:  the account is FLAT, so the venue shows no exposure and a single
    #       account-level PnL would be zero. The two deployments' PnL is
    #       emphatically not zero, and it is opposite — which is the whole point
    #       of attributing per deployment rather than reading the account.
    day_pnl = day.portfolio.unrealized_pnl(130.0)
    night_pnl = night.portfolio.unrealized_pnl(130.0)
    assert day_pnl == pytest.approx(30.0)
    assert night_pnl == pytest.approx(-30.0)
    assert venue.net == 0, "the account shows nothing; the deployments show ±30"


def test_neither_rail_halts_across_a_long_hold(tmp_path):
    """G6 says 'across a full session'. Reconciliation runs on every bar."""
    owner, venue, day, night = _run(tmp_path, ["BUY"], ["SELL"], bars=120)

    assert not day.rails.halted and not night.rails.halted
    assert day.trader.position == 1 and night.trader.position == -1
    assert owner.anomalies == []


def test_a_crossing_intent_still_lands_with_a_peer_holding(tmp_path):
    """§18.9's split, exercised where it actually matters.

    night holds -1, so when day closes its long the ACCOUNT crosses through
    flat. The owner must issue Cover-then-New sized from the account, not from
    day's own intent.
    """
    venue = _Venue()
    owner = OwnerProcess(venue, _Contract(), directory=tmp_path,
                         slugs=("day", "night"), account="ACC", sj=_FakeSJ,
                         fee_rate=0.0)
    owner.start()
    day = _Deployment(tmp_path, "day", "BUY")
    night = _Deployment(tmp_path, "night", "HOLD")

    # Tech: two bars, not one. A deployment drains its fills at the START of a
    #       bar, so the fill placed during bar 0 reaches its mirror on bar 1.
    # Why:  the loop resolves fills before it decides anything (§ step 1), which
    #       is what makes the decision see a settled position rather than a
    #       pending one. Asserting after a single bar tests the venue, not the
    #       deployment.
    for i in range(2):
        market = MarketEvent(T0 + timedelta(minutes=i), 100.0, 1, "NIGHT")
        day.bar(market)
        night.bar(market)
        owner.poll()
        venue.fill_all()
        owner.poll()
    assert venue.net == 1 and day.trader.position == 1

    # Every leg the owner sent stayed inside what the account held.
    covers = [o for o in venue.placed if o.octype == "Cover"]
    assert all(c.quantity <= 1 for c in covers)
    assert not day.rails.halted and not night.rails.halted


def test_a_deployment_restarting_adopts_only_its_own_lot(tmp_path):
    """The restart case §7.1 named, end to end rather than in isolation."""
    owner, venue, day, night = _run(tmp_path, ["BUY"], ["BUY"])
    assert venue.net == 2

    # A fresh ClientBroker, as a restarted process would build.
    restarted = ClientBroker(tmp_path, "day")
    held = restarted.broker_position_detail()
    assert held is not None
    assert held.signed_qty == 1, "the account holds 2; this deployment holds 1"


# ---- the live script's verdict (§22.7) --------------------------------------
#
# The live half cannot be run from a test — it needs a session. Its VERDICT can,
# and must be: on 2026-09-07 it printed `ledgers on disk {}` / `ledger sum None`
# and still announced "G6 PASSED ... every fill reached exactly one deployment",
# on evidence it had just shown was absent. G6 is the gate that lifts §15, so a
# verdict asserting an unchecked clause retires a safety rule on a measurement
# nobody made.

import csv

from forecast_eval.live.ledger import LEDGER_COLUMNS


class _VerdictBook:
    def __init__(self, nets):
        self._nets = nets

    def net_for(self, slug):
        return self._nets[slug]

    @property
    def account_net(self):
        return sum(self._nets.values())


class _VerdictOwner:
    """Just enough owner for `_verdict`: a book, and a venue it can be asked."""

    def __init__(self, nets, venue):
        self.book = _VerdictBook(nets)
        self.anomalies = []
        self.account = "A"
        self.contract = type("C", (), {"code": "TXFI6"})()
        self._venue = venue
        self.api = self

    def list_positions(self, *a, **k):
        if self._venue is None:
            return None                       # "cannot ask", not "flat"
        if not self._venue:
            return []
        return [type("P", (), {
            "code": "TXFI6", "quantity": abs(self._venue),
            "direction": "Buy" if self._venue > 0 else "Sell"})()]


def _write_ledger(directory, slug, rows):
    path = directory / f"fills_{slug}.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(LEDGER_COLUMNS)
        for side, quantity in rows:
            writer.writerow(["2026-09-07T16:10:00", side, 47454.0, quantity,
                             0.0, ""])


def _verdict_for(tmp_path, ledgers=None, venue=0, nets=None):
    from forecast_eval.tools import g6_live

    if ledgers:
        for slug, rows in ledgers.items():
            _write_ledger(tmp_path, slug, rows)
    args = type("A", (), {"out": str(tmp_path)})()
    owner = _VerdictOwner(nets or {"day": 1, "night": -1}, venue)
    return g6_live._verdict(owner, {}, [], args)


def test_no_ledger_on_disk_is_not_a_pass(tmp_path):
    """The 2026-09-07 run, exactly: both holding, nothing written."""
    assert _verdict_for(tmp_path) == 4


def test_matching_ledgers_and_an_agreeing_venue_pass(tmp_path):
    assert _verdict_for(tmp_path, ledgers={"day": [("BUY", 1)],
                                           "night": [("SELL", 1)]}) == 0


def test_ledgers_that_disagree_with_the_book_fail(tmp_path):
    """A fill attributed to the wrong deployment — the thing G6 exists for."""
    assert _verdict_for(tmp_path, ledgers={"day": [("BUY", 1)],
                                           "night": [("BUY", 1)]}) == 1


def test_a_venue_disagreeing_with_the_ledger_sum_fails(tmp_path):
    assert _verdict_for(tmp_path, ledgers={"day": [("BUY", 1)],
                                           "night": [("SELL", 1)]},
                        venue=2) == 1


def test_a_venue_that_will_not_answer_is_inconclusive_not_a_pass(tmp_path):
    """"Cannot compare" is not "agrees" — the rule `reconcile` already follows."""
    assert _verdict_for(tmp_path, ledgers={"day": [("BUY", 1)],
                                           "night": [("SELL", 1)]},
                        venue=None) == 4
