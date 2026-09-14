"""§18.3 item 7 — restarting a deployment while a PEER holds a position.

STRATEGY_SPLIT_PLAN §7.1 named `adopt_position` as broken under a shared book:
*"a restarting deployment adopts the COMBINED venue position as its own."* That
is true of `ShioajiBroker`, whose `broker_position_detail()` sums the account.

It is **not** true through a `ClientBroker`, because the owner sends each
deployment its own signed size. So the defect is fixed by the seam rather than by
an edit to `runtime.py` — and that is exactly the kind of claim that must be
tested rather than asserted, since it is the difference between "no change
needed" and "we forgot".

These tests drive the real `OwnerProcess` and the real `ClientBroker`.
"""
import pandas as pd
import pytest

from forecast_eval.events import OrderEvent
from forecast_eval.live.broker.client import ClientBroker
from forecast_eval.live.owner_process import OwnerProcess
from forecast_eval.live.runtime import LiveRuntime

from .test_live_owner_process import _Contract, _FakeAPI, _FakeSJ, _deal
from .test_live_runtime import _config

TS = pd.Timestamp("2026-09-07 10:00:00")
NOW = TS.to_pydatetime()


# These build a `LiveRuntime`, which assembles from the session package since
# LIVE_STRATEGY_ROUTING_PLAN D2. `_config()` is night-policy, and night's real
# package builds a Toto2 forecaster (a checkpoint load). These cases are about
# WHOSE lot is adopted, not about the strategy, so they use the shared cheap
# strategy the other assembly tests use. (Adopted when night's package still
# returned a `TimeStop`, which `adopt_position` refuses — D10; that exit was
# removed by DAYNIGHT_TOTO2_RESET_PLAN.)
pytestmark = pytest.mark.usefixtures("cheap_strategy")


def _owner_and_clients(tmp_path, api=None):
    api = api or _FakeAPI()
    owner = OwnerProcess(api, _Contract(), directory=tmp_path,
                         slugs=("day", "night"), account="ACC", sj=_FakeSJ)
    return owner, api


def _runtime(tmp_path, slug, **kw):
    return LiveRuntime(_config(), broker=ClientBroker(tmp_path, slug), **kw)


# ---- the defect §7.1 predicted, and why it does not occur here -------------

def test_a_restart_adopts_its_own_lot_not_the_accounts(tmp_path):
    """day holds +1 and night holds +1; a restarting day must adopt +1, not +2."""
    owner, _ = _owner_and_clients(tmp_path)
    owner.seed_from({"day": 1, "night": 1})
    owner.publish_positions()
    assert owner.book.account_net == 2

    rt = _runtime(tmp_path, "day")
    adopted = rt.adopt_position(timestamp=NOW)

    assert adopted is not None
    assert adopted["position"] == 1, "the account holds 2; this deployment holds 1"
    assert rt.trader.position == 1


def test_a_deployment_flat_while_a_peer_holds_adopts_nothing(tmp_path):
    """The account is non-flat, but this deployment opened none of it."""
    owner, _ = _owner_and_clients(tmp_path)
    owner.seed_from({"day": 0, "night": 2})
    owner.publish_positions()

    rt = _runtime(tmp_path, "day")
    assert rt.adopt_position(timestamp=NOW) is None
    assert rt.trader.position == 0


def test_opposite_peers_do_not_cancel_into_a_false_flat(tmp_path):
    """day +1 and night -1 nets the ACCOUNT flat; neither deployment is flat."""
    owner, _ = _owner_and_clients(tmp_path)
    owner.seed_from({"day": 1, "night": -1})
    owner.publish_positions()
    assert owner.book.account_net == 0

    day = _runtime(tmp_path, "day")
    night = _runtime(tmp_path, "night")
    assert day.adopt_position(timestamp=NOW)["position"] == 1
    assert night.adopt_position(timestamp=NOW)["position"] == -1


def test_adoption_refuses_when_the_owner_cannot_be_seen(tmp_path):
    """A silent owner must not read as a flat account (§18.3 item 6)."""
    rt = _runtime(tmp_path, "day")          # the owner has never spoken
    assert rt.adopt_position(timestamp=NOW) is None
    assert rt.venue_position is None, "None means unknown, never zero"


def test_the_entry_price_survives_the_wire(tmp_path):
    """An adoption that invented an entry would flatter every restart of a loser."""
    owner, api = _owner_and_clients(tmp_path)
    day = ClientBroker(tmp_path, "day")
    day.submit(OrderEvent(timestamp=TS, side="BUY", limit_price=17480.0,
                          intent="OPEN", quantity=1), current_price=17480.0)
    owner.poll()
    _deal(owner, "NO1", side="Buy", qty=1, price=17480.0)
    owner.publish_positions()

    # The owner reports size; the entry comes with it when there is one.
    from forecast_eval.live.intents import JsonlStream, event_path
    positions = [r for r in JsonlStream(event_path(tmp_path, "day")).read_new()
                 if r["kind"] == "position"]
    assert positions[-1]["signed_qty"] == 1


# ---- the --fresh-start gate reads the same operand -------------------------

def test_fresh_start_sees_its_own_position_not_the_accounts(tmp_path):
    """`cli.py`'s gate calls broker_position_detail(), so it inherits the fix.

    A day deployment that is genuinely flat must be allowed to start fresh even
    while night holds — under `ShioajiBroker` it would have been refused.
    """
    owner, _ = _owner_and_clients(tmp_path)
    owner.seed_from({"day": 0, "night": 3})
    owner.publish_positions()

    held = ClientBroker(tmp_path, "day").broker_position_detail()
    assert held is None, "the gate refuses only on a non-zero signed_qty"


def test_fresh_start_still_refuses_when_this_deployment_holds(tmp_path):
    owner, _ = _owner_and_clients(tmp_path)
    owner.seed_from({"day": 1, "night": 0})
    owner.publish_positions()

    held = ClientBroker(tmp_path, "day").broker_position_detail()
    assert held is not None and held.signed_qty == 1
