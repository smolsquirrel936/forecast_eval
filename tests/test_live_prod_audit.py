"""Production audit — the comparison logic, without a production login.

The one wrong answer this module can give is "unchanged" when it does not know,
because that clears the real account on no evidence. Most of what follows is
about refusing to say that.
"""
from forecast_eval.live.prod_audit import (
    build_parser, compare, is_simulation_only_token, snapshot,
)


# ---- the best outcome: the credential cannot reach production at all --------

def test_a_production_permission_refusal_is_recognised():
    """MEASURED 2026-08-11. `simulation=False` returned
    `BadRequestError: StatusCode: 400, Detail: Token doesn't have production
    permission.` — which is the strongest result this tool can report, not a
    failure: a token the venue will not admit to production cannot place a
    production order, whatever the client passes."""
    exc = RuntimeError("StatusCode: 400, Detail: Token doesn't have "
                       "production permission.")
    assert is_simulation_only_token(exc)


def test_other_login_failures_are_not_mistaken_for_it():
    """A network drop or a bad secret must not be read as 'safely scoped' — that
    would report a guarantee nobody established."""
    for message in ("Connection refused", "invalid secret key",
                    "StatusCode: 500, Detail: internal error",
                    "insufficient permission"):
        assert not is_simulation_only_token(RuntimeError(message)), message


class _Api:
    """A read-only API double. Any query can be made to fail independently."""

    def __init__(self, positions=(), trades=(), profit_loss=(), fail=()):
        self._positions, self._trades = list(positions), list(trades)
        self._pl, self._fail = list(profit_loss), set(fail)

    def _maybe_fail(self, name):
        if name in self._fail:
            raise RuntimeError(f"{name} not permitted")

    def list_positions(self, account):
        self._maybe_fail("positions")
        return self._positions

    def list_trades(self):
        self._maybe_fail("trades")
        return self._trades

    def list_profit_loss(self, account, start, end):
        self._maybe_fail("profit_loss")
        return self._pl


# ---- snapshot ---------------------------------------------------------------

def test_a_clean_account_snapshots_as_three_empty_sections():
    s = snapshot(_Api(), None)
    assert s["positions"] == [] and s["trades"] == [] and s["profit_loss"] == []


def test_rows_are_coerced_to_plain_dicts():
    class _Row:
        def __init__(self):
            self.code, self.quantity = "TXFH6", 1
    s = snapshot(_Api(positions=[_Row()]), None)
    assert s["positions"] == [{"code": "TXFH6", "quantity": "1"}]


def test_one_failing_query_does_not_hide_the_others():
    """An audit that dies on `list_profit_loss` reports nothing about positions —
    which is the answer we came for."""
    s = snapshot(_Api(positions=[{"code": "TXFH6"}], fail={"profit_loss"}), None)
    assert s["positions"] == [{"code": "TXFH6"}]
    assert "error" in s["profit_loss"]


# ---- compare ----------------------------------------------------------------

def test_no_change_is_reported_as_no_findings():
    before = snapshot(_Api(), None)
    after = snapshot(_Api(), None)
    assert compare(before, after) == []


def test_a_new_position_is_flagged():
    before = snapshot(_Api(), None)
    after = snapshot(_Api(positions=[{"code": "TXFH6", "quantity": 1}]), None)
    findings = compare(before, after)
    assert findings and "positions" in findings[0] and "CHANGED" in findings[0]


def test_a_trade_that_opened_and_closed_is_still_caught():
    """Positions alone are not enough: an order that opened *and* closed leaves a
    net position of zero, so trades and P&L are compared too."""
    before = snapshot(_Api(), None)
    after = snapshot(_Api(trades=[{"id": "x"}]), None)   # position still flat
    findings = compare(before, after)
    assert findings and "trades" in findings[0]


def test_a_failed_query_is_unknown_and_never_unchanged():
    """The one wrong answer: reading 'no change' off a query that never ran would
    clear the production account on no evidence at all."""
    before = snapshot(_Api(), None)
    after = snapshot(_Api(fail={"positions"}), None)
    findings = compare(before, after)
    assert any("UNKNOWN" in f for f in findings)
    assert findings != []


def test_a_failure_in_the_baseline_is_unknown_too():
    before = snapshot(_Api(fail={"trades"}), None)
    after = snapshot(_Api(), None)
    assert any("UNKNOWN" in f and "trades" in f for f in compare(before, after))


# ---- the flag ---------------------------------------------------------------

def test_production_access_needs_an_explicit_flag():
    assert build_parser().parse_args([]).i_am_querying_production is False
    assert build_parser().parse_args(
        ["--i-am-querying-production"]).i_am_querying_production is True


def test_main_refuses_without_the_flag(capsys):
    from forecast_eval.live import prod_audit
    assert prod_audit.main([]) == 2
    assert "PRODUCTION" in capsys.readouterr().err
