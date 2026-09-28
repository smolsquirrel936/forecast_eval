"""night_iter02: the Trader's `long_only` entry veto.

The knob this file guards was added at `night_iter02` (the night autoresearch
loop): `strategy/night/trader.py` diverged from its shared frozen copy with a
`long_only` flag that vetoes SELL entries while flat. Each case pins one
observable of the veto so a regression in either direction (veto broken /
veto leaking into closes or into the shared default) is caught by name.
"""
import pandas as pd
import pytest

from forecast_eval.events import MarketEvent
from forecast_eval.execution import Execution
from forecast_eval.strategy.night import wiring as night_wiring
from forecast_eval.strategy.night.trader import Trader


def _market(price: float, offset_s: int = 0) -> MarketEvent:
    return MarketEvent(
        timestamp=pd.Timestamp("2026-01-05 16:00:00") + pd.Timedelta(seconds=offset_s),
        price=price,
        volume=1,
        session="NIGHT",
    )


def _fill(ex: Execution, price: float, offset_s: int = 1):
    f = ex.check_fill(_market(price, offset_s=offset_s))
    assert f is not None
    return f


def test_sell_while_flat_places_no_order_when_long_only():
    ex = Execution(fee_rate=0.0)
    tr = Trader(ex, tick_size=1.0, aggression_ticks=0, long_only=True)

    assert tr.on_signal("SELL", _market(17500)) is None
    assert tr.position == 0
    assert not ex.has_pending(), "the vetoed SELL must not leave a resting order"


def test_sell_while_flat_still_cancels_a_pending_order():
    """A vetoed SELL behaves like HOLD: cancel first (SPEC §4.4), then nothing."""
    ex = Execution(fee_rate=0.0)
    tr = Trader(ex, tick_size=1.0, aggression_ticks=0, long_only=True)

    # A passive BUY rests (limit 6 ticks below a 17500 print at placement).
    assert tr.on_signal("BUY", _market(17500)) is not None
    assert ex.has_pending()

    assert tr.on_signal("SELL", _market(17490)) is None
    assert not ex.has_pending(), "the vetoed SELL must still cancel the resting BUY"
    assert tr.position == 0


def test_sell_while_long_still_closes_when_long_only():
    ex = Execution(fee_rate=0.0)
    tr = Trader(ex, tick_size=1.0, aggression_ticks=0, long_only=True)

    assert tr.on_signal("BUY", _market(17500)) is not None
    f = _fill(ex, 17500)
    tr.on_fill(f.side, f.quantity, fill_price=f.fill_price, bar_idx=1,
               timestamp=f.timestamp)
    assert tr.position == 1

    order = tr.on_signal("SELL", _market(17600))
    assert order is not None
    assert order.intent == "CLOSE"
    f = _fill(ex, 17600)
    tr.on_fill(f.side, f.quantity, fill_price=f.fill_price, bar_idx=2,
               timestamp=f.timestamp)
    assert tr.position == 0, "long_only must not block closing a long"


def test_default_still_opens_shorts():
    """long_only=False (the default) is byte-identical to the shared copy."""
    ex = Execution(fee_rate=0.0)
    tr = Trader(ex, tick_size=1.0, aggression_ticks=0)

    order = tr.on_signal("SELL", _market(17500))
    assert order is not None
    assert order.intent == "OPEN"
    f = _fill(ex, 17500)
    tr.on_fill(f.side, f.quantity, fill_price=f.fill_price, bar_idx=1,
               timestamp=f.timestamp)
    assert tr.position == -1


def test_wiring_params_sets_long_only_and_override_reaches_the_trader():
    # Tech: resolve the package's own stack twice — once on PARAMS defaults,
    #       once with the documented escape hatch --param long_only=False —
    #       and read the knob off the Trader each factory builds.
    # Why:  PARAMS is the only place a search may pin a session-declared knob,
    #       so the wiring must actually thread it through `make_trader`; and
    #       the override must raise nothing (the knob is declared, D6).
    for overrides, expected in (({}, True), ({"long_only": False}, False)):
        stack = night_wiring.make_strategy(warmup_bars=0, **overrides)
        ex = Execution(fee_rate=0.0)
        tr = stack["make_trader"](ex)
        assert tr.long_only is expected
        assert stack["params"]["long_only"] is expected


def test_wiring_rejects_a_knob_the_package_does_not_declare():
    with pytest.raises(ValueError, match="unknown strategy override"):
        night_wiring.make_strategy(warmup_bars=0, short_veto=True)
