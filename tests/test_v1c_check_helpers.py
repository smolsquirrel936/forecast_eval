"""Contract resolution in the V1c checker.

Regression for a live crash (2026-08-10): `--contract TXF202605` raised
`KeyError: 'TXF202605'` from inside `getattr(txf, name, None)`. The Shioaji
Contracts container implements `__getattr__` as a dict lookup that raises
**KeyError**, and getattr's three-argument default only suppresses
AttributeError — so the default never applied and the script died instead of
printing the available contracts.
"""
import types

from forecast_eval.live.v1c_check import _contract_codes, _resolve_contract


class _KeyErrorContainer:
    """Mimics the real container: attribute access raises KeyError on a miss."""

    def __init__(self, mapping):
        self.__dict__["_d"] = mapping

    def __getattr__(self, name):
        try:
            return self.__dict__["_d"][name]
        except KeyError:
            raise KeyError(name)

    def __getitem__(self, key):
        return self.__dict__["_d"][key]

    def __iter__(self):
        return iter(self.__dict__["_d"].values())


def _c(code, month):
    return types.SimpleNamespace(code=code, delivery_month=month)


def _container():
    return _KeyErrorContainer({
        "TXFR1": _c("TXFR1", "202608"),
        "TXFQ6": _c("TXFQ6", "202608"),
        "TXFU6": _c("TXFU6", "202609"),
    })


def test_missing_contract_returns_none_instead_of_raising_keyerror():
    """The exact live failure: this must not propagate KeyError."""
    assert _resolve_contract(_container(), "TXF202605") is None


def test_resolves_an_alias_by_attribute():
    assert _resolve_contract(_container(), "TXFR1").code == "TXFR1"


def test_resolves_an_explicit_month_by_key():
    assert _resolve_contract(_container(), "TXFQ6").code == "TXFQ6"


def test_lists_codes_with_delivery_months():
    """Listing must show delivery_month — that is what answers whether an
    expired month is still offered."""
    assert _contract_codes(_container()) == [
        ("TXFQ6", "202608"), ("TXFR1", "202608"), ("TXFU6", "202609")]


def test_listing_falls_back_when_the_container_is_not_iterable():
    class _NotIterable:
        TXFR1 = _c("TXFR1", "202608")

    assert ("TXFR1", "202608") in _contract_codes(_NotIterable())
