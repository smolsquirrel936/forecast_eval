"""Flatten — the arithmetic and the cancel-first rule, without a login.

This is the tool that runs *after* something has already gone wrong, so its
failure modes are the expensive ones: closing the wrong way, or stacking a second
close on top of a resting one and turning a long into a short.
"""
from types import SimpleNamespace

from forecast_eval.live.flatten import (
    CLOSE_AGGRESSION_POINTS, cancel_working, close_price, is_working,
    net_position,
)

CODES = {"TXFR1", "TXFH6"}


def _position(code="TXFH6", quantity=1, direction="Buy"):
    return SimpleNamespace(code=code, quantity=quantity, direction=direction)


# ---- reading the position ---------------------------------------------------

def test_a_long_reads_positive_and_a_short_negative():
    assert net_position([_position(direction="Buy")], CODES) == 1
    assert net_position([_position(direction="Sell")], CODES) == -1


def test_positions_are_matched_on_the_resolved_code_not_the_alias():
    """§7.2 again — positions come back stamped `TXFH6` while the alias reports
    `TXFR1`. Filtering on the alias alone finds nothing and reports flat while
    holding, which is the one reading this tool must never produce."""
    assert net_position([_position(code="TXFH6")], CODES) == 1
    assert net_position([_position(code="TXFH6")], {"TXFR1"}) == 0


def test_other_contracts_are_ignored():
    assert net_position([_position(code="MXFH6")], CODES) == 0


def test_an_empty_account_is_flat():
    assert net_position([], CODES) == 0
    assert net_position(None, CODES) == 0


# ---- pricing the close ------------------------------------------------------

def test_the_close_is_priced_through_the_touch_in_the_right_direction():
    """A BUY closing a short must be willing to pay *more*. The wrong sign
    prices the close further from the market — the opposite of urgent, and it
    would look like it worked."""
    assert close_price(45_000.0, "BUY") == 45_000.0 + CLOSE_AGGRESSION_POINTS
    assert close_price(45_000.0, "SELL") == 45_000.0 - CLOSE_AGGRESSION_POINTS


def test_the_close_stays_a_bounded_limit():
    """Aggressive, but the worst price is still knowable before sending — which
    is exactly what an unbounded MKT gives up."""
    assert close_price(45_000.0, "BUY") - 45_000.0 <= 100.0


def test_the_close_lands_on_the_tick_grid():
    assert close_price(45_000.5, "SELL", tick_size=1.0) % 1.0 == 0.0


# ---- cancel before closing --------------------------------------------------

def _trade(code="TXFH6", status="Submitted"):
    return SimpleNamespace(contract=SimpleNamespace(code=code),
                           status=SimpleNamespace(status=status))


class _Api:
    def __init__(self, trades):
        self._trades, self.cancelled = trades, []

    def update_status(self, account):
        pass

    def list_trades(self):
        return self._trades

    def cancel_order(self, trade):
        self.cancelled.append(trade)


def test_a_resting_order_is_cancelled_before_a_new_close_is_sent():
    """ROD rests where IOC expired (§2.19), so a close from a killed process can
    still be live. Placing a second Cover on top can fill twice against one lot
    and turn a long into a short — a worse state than the one being repaired."""
    api = _Api([_trade()])
    assert cancel_working(api, None, CODES) == 1
    assert len(api.cancelled) == 1


def test_filled_and_cancelled_orders_are_left_alone():
    api = _Api([_trade(status="Filled"), _trade(status="Cancelled")])
    assert cancel_working(api, None, CODES) == 0


def test_orders_on_other_contracts_are_untouched():
    api = _Api([_trade(code="MXFH6")])
    assert cancel_working(api, None, CODES) == 0


def test_an_unrecognised_status_counts_as_not_working():
    """The concrete literals have been wrong before, so the worst case has to be
    a cancel we skip — never a position we mistake for flat."""
    assert not is_working(_trade(status="SomeNewStatus"))


def test_the_enum_prefixed_form_is_still_recognised():
    """`str(enum)` renders as `Status.Submitted`; matching the bare name would
    silently stop cancelling anything."""
    assert is_working(_trade(status="Status.Submitted"))


def test_a_failure_to_list_orders_does_not_abort_the_flatten():
    class _Broken(_Api):
        def list_trades(self):
            raise RuntimeError("no permission")

    assert cancel_working(_Broken([]), None, CODES) == 0


def test_one_stubborn_order_does_not_stop_the_others():
    class _PartlyBroken(_Api):
        def cancel_order(self, trade):
            if getattr(trade.contract, "code") == "TXFH6" and not self.cancelled:
                self.cancelled.append(trade)
                raise RuntimeError("busy")
            self.cancelled.append(trade)

    api = _PartlyBroken([_trade(), _trade()])
    cancel_working(api, None, CODES)
    assert len(api.cancelled) == 2, "the second order must still be attempted"
