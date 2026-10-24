"""Contract resolution — regression for the live start-up failure of 2026-08-11.

`api.Contracts` is deprecated in favour of `api.contracts`, and the switch was
made after checking that `sj.Contracts` exposes `Futures`. It does — but
`api.contracts` returns a **`ContractsApi`**, a different type with no `Futures`
at all, and the run died at start-up on a market window that could not be re-run:

    AttributeError: 'builtins.ContractsApi' object has no attribute 'Futures'

The fakes below are shaped from the real `dir()` output of each type, so the
tests fail the same way production did.
"""
import pytest

from forecast_eval.live.contracts import (
    contract_codes, describe_contract_match, resolve_in, resolve_txf, txf_root,
)


class _TXF:
    """A futures root: attribute access for aliases, keys for explicit months."""

    def __init__(self):
        self.TXFR1 = _Contract("TXFH6", "202608")
        self._by_key = {"TXF202608": _Contract("TXFH6", "202608")}

    def __getitem__(self, key):
        # The real container raises KeyError from __getattr__ too — see below.
        return self._by_key[key]


class _Contract:
    def __init__(self, code, month):
        self.code, self.delivery_month = code, month


class _V1Api:
    """The deprecated shape: api.Contracts.Futures.TXF — proven in four runs."""

    def __init__(self):
        self.Contracts = type("C", (), {"Futures": type("F", (), {"TXF": _TXF()})})


class _FuturesRoots:
    """`ContractsApi.futures` — a container *of* roots, keyed by root code."""

    def __init__(self):
        self.TXF = _TXF()

    def __getitem__(self, key):
        if key == "TXF":
            return self.TXF
        raise KeyError(key)


class _ContractsApi:
    """The v2 shape, from the real `dir()`: futures/get/list, and NO `Futures`.

    The *inner* layout is a guess — `FuturesContractDict` exposes
    get/items/keys/values, but whether it is keyed by root (`TXF`) or by contract
    code has never been observed on a live session. That is exactly why the v2
    routes are fallbacks: their job is "do not die, try the next one", and the
    route that actually runs today is the proven deprecated one.
    """

    def __init__(self):
        self.futures = _FuturesRoots()

    def get(self, code):
        return None


class _V2OnlyApi:
    """`api.contracts` present, `api.Contracts` gone — the future SDK."""

    def __init__(self):
        self.contracts = _ContractsApi()

    def __getattr__(self, name):
        raise AttributeError(name)


class _BrokenApi:
    def __getattr__(self, name):
        raise AttributeError(name)


# ---- the failure that happened ---------------------------------------------

def test_a_contracts_api_without_Futures_does_not_kill_resolution():
    """The exact production failure: `api.contracts` resolves fine but has no
    `Futures`. Reaching for it must fall through to a working route, not raise."""
    api = _V2OnlyApi()
    with pytest.raises(AttributeError):
        api.contracts.Futures            # the call that died live
    contract, note = resolve_txf(api, "TXFR1")
    assert contract.code == "TXFH6"
    assert "api.contracts.futures.TXF" in note


def test_the_proven_route_is_tried_first():
    """Route order inverts the usual 'prefer the new API' instinct on purpose:
    the deprecated path is the only one with live evidence behind it, and a
    deprecation warning is noise where a failed start-up is a lost session."""
    _, route = txf_root(_V1Api())
    assert route == "api.Contracts.Futures.TXF"


def test_every_route_failing_names_all_of_them():
    """The error has to carry what was tried, so the next fix needs no live run."""
    with pytest.raises(RuntimeError) as exc:
        txf_root(_BrokenApi())
    message = str(exc.value)
    assert "api.Contracts.Futures.TXF" in message
    assert "api.contracts.futures.TXF" in message


# ---- resolving a contract within the root ----------------------------------

def test_alias_is_found_by_attribute_and_month_by_key():
    api = _V1Api()
    assert resolve_txf(api, "TXFR1")[0].code == "TXFH6"
    assert resolve_txf(api, "TXF202608")[0].delivery_month == "202608"


def test_a_container_whose_getattr_raises_KeyError_is_survived():
    """MEASURED — the real container's `__getattr__` is a dict lookup raising
    **KeyError**, which `getattr(x, name, default)` does NOT suppress. The
    three-argument form propagates and crashes."""

    class _KeyErrorContainer:
        def __getattr__(self, name):
            raise KeyError(name)

        def __getitem__(self, key):
            if key == "TXFR1":
                return _Contract("TXFH6", "202608")
            raise KeyError(key)

    assert resolve_in(_KeyErrorContainer(), "TXFR1").code == "TXFH6"
    assert resolve_in(_KeyErrorContainer(), "NOPE") is None


def test_a_missing_contract_is_a_clear_error_naming_the_route():
    with pytest.raises(RuntimeError, match="not found via"):
        resolve_txf(_V1Api(), "TXFNOPE")


# ---- what the caller prints -------------------------------------------------

# ---- alias vs resolved code (the false MISMATCH of 2026-08-11) -------------

class _Alias:
    """The alias object as the SDK actually presents it: `code` is the alias,
    `target_code` is the month the venue stamps on ticks and fills."""
    code, target_code = "TXFR1", "TXFH6"


def test_an_alias_matches_a_capture_stamped_with_its_resolved_month():
    """The false alarm: `TXFR1` reports `code='TXFR1'` while every captured tick
    is stamped `TXFH6`, so comparing on `code` alone declared a contract
    different from *itself* — and labelled a valid V1 diff 'meaningless'."""
    note, mismatched = describe_contract_match(_Alias(), ["TXFH6"])
    assert not mismatched, note
    assert "MISMATCH" not in note


def test_a_genuinely_different_contract_is_still_caught():
    """Non-vacuity: the check must still fire across a real rollover, which is
    the case it exists for (§2.6)."""
    _, mismatched = describe_contract_match(_Alias(), ["TXFE6"])
    assert mismatched


def test_an_empty_capture_is_not_called_a_mismatch():
    _, mismatched = describe_contract_match(_Alias(), [])
    assert not mismatched


def test_contract_codes_is_the_single_source_the_broker_also_uses():
    """`ShioajiBroker` seeds its accepted-code set from this function; two copies
    of the alias rule would drift, and the drift halts the loop on a real fill."""
    assert contract_codes(_Alias()) == {"TXFR1", "TXFH6"}


def test_the_description_carries_the_resolved_code_not_just_the_alias():
    """`TXFR1` is an alias; fills and positions come back stamped `TXFH6`.
    Surfacing both at start-up is what made that mismatch visible (§7.2)."""
    _, note = resolve_txf(_V1Api(), "TXFR1")
    assert "TXFR1" in note and "TXFH6" in note and "202608" in note


# ---- reading the month off a dated code -------------------------------------

from datetime import date as _date

from forecast_eval.live.contracts import code_month


@pytest.mark.parametrize("code, month", [
    ("TXFJ6", "202610"), ("TXFK6", "202611"), ("TXFH6", "202608"),
    ("TXFA7", "202701"), ("TXFL6", "202612"),
])
def test_code_month_reads_the_letter_and_the_year(code, month):
    assert code_month(code, _date(2026, 10, 24)) == month


def test_code_month_takes_the_nearest_decade():
    assert code_month("TXFC9", _date(2030, 1, 5)) == "202903"
    assert code_month("TXFA1", _date(2030, 12, 5)) == "203101"


@pytest.mark.parametrize("code", ["TXFR1", "TXFR2", "", None, "TXF202611"])
def test_code_month_is_empty_for_anything_not_dated(code):
    assert code_month(code, _date(2026, 10, 24)) == ""
