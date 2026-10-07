"""§2.24 re-login probe — the parts that can be checked without a venue.

A probe is an instrument, and an instrument that is wrong produces confident
wrong answers — which on this project has cost a market window (§2.18) and a
crashed soak (§2.22b). These tests cover the two things that can go wrong
offline: the verdicts it draws from what it saw, and the promise that off-hours
mode touches nothing.
"""
import pathlib
import sys
import types

import pytest

from forecast_eval.live import relogin_probe as probe


# ---- the fake venue --------------------------------------------------------


class FakeAccount:
    account_id = "1234567"
    person_id = "A123456789"
    broker_id = "F002000"
    account_type = "F"
    signed = True


class FakeUsage:
    def __init__(self, connections=1):
        self.connections = connections
        self.remaining_bytes = 500_000_000


class FakeApi:
    """Records every call, so a test can assert what was *not* reached."""

    def __init__(self, simulation=True, usage_after_logout=None,
                 connections_per_login=0):
        self.simulation = simulation
        self.futopt_account = FakeAccount()
        self.calls = []
        self.logged_in = False
        self.connections = 0
        self._usage_after_logout = usage_after_logout
        self._per_login = connections_per_login
        self._event_cb = None

    def _note(self, name):
        self.calls.append(name)

    def on_event(self, cb):
        self._event_cb = cb

    def login(self, **kw):
        self._note("login_force" if kw.get("force_refresh") else "login")
        self.logged_in = True
        self.connections += self._per_login or (1 if self.connections == 0 else 0)
        if self._event_cb is not None:
            self._event_cb(0, 0, "host x", "Session up")
        return [FakeAccount()]

    def logout(self):
        self._note("logout")
        self.logged_in = False

    def usage(self):
        self._note("usage")
        if not self.logged_in:
            if self._usage_after_logout is None:
                raise RuntimeError("AuthError: Not authenticated")
            return self._usage_after_logout
        return FakeUsage(connections=self.connections)

    def list_positions(self, *a, **kw):
        self._note("list_positions")
        if not self.logged_in:
            raise RuntimeError("TokenError: Token is expired")
        return []

    # Anything below must never be reached in off-hours mode.
    def subscribe(self, *a, **kw):
        self._note("subscribe")

    def place_order(self, *a, **kw):
        self._note("place_order")
        raise AssertionError("off-hours mode must never order")

    def snapshots(self, *a, **kw):
        self._note("snapshots")
        return []


def _install_fake(monkeypatch, api):
    fake = types.ModuleType("shioaji")
    fake.__version__ = "1.7.2-fake"
    fake.Shioaji = lambda **kw: api
    monkeypatch.setitem(sys.modules, "shioaji", fake)


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("SHIOAJI_API_KEY", "key")
    monkeypatch.setenv("SHIOAJI_SECRET_KEY", "secret")


# ---- verdicts --------------------------------------------------------------


def test_q3_verdict_names_the_dangerous_case():
    """A duplicated order callback double-counts every fill — the finding the
    whole probe exists to protect against."""
    assert "DUPLICATED" in probe._q3_verdict(1, 2)
    assert "DROPPED" in probe._q3_verdict(1, 0)
    assert probe._q3_verdict(1, 1) == "survives, not duplicated"


def test_q3_verdict_admits_when_it_cannot_tell():
    """No baseline means no comparison; saying so beats inventing a verdict."""
    assert "inconclusive" in probe._q3_verdict(0, 0)
    assert "inconclusive" in probe._q3_verdict(None, 3)


def test_call_returns_the_exception_as_an_answer(capsys):
    """An exception here *is* a measurement, not a probe failure — letting one
    propagate would abandon the remaining questions and waste the session."""
    ok, value = probe.call("boom", lambda: (_ for _ in ()).throw(
        RuntimeError("Token is expired")))
    assert ok is False
    assert "RuntimeError" in value and "Token is expired" in value
    assert "RAISED" in capsys.readouterr().out


def test_call_reports_success_without_dumping_the_object(capsys):
    probe.call("big", lambda: list(range(500)))
    assert len(capsys.readouterr().out.splitlines()[0]) < 140


def test_brief_truncates():
    assert probe._brief(list(range(500))).endswith("…")
    assert probe._brief("short") == "'short'"


# ---- the leak, and its regression test -------------------------------------


class LeakyAccount:
    def __repr__(self):
        return ("FutureAccount(person_id='L125551606', broker_id='F002000', "
                "account_id='1234567', signed=True)")


def test_identifiers_never_reach_the_console():
    """REGRESSION — the first live run printed a Taiwanese national ID in full.

    `cli.py` digests `person_id` and never even masks it, on the grounds that a
    partial national ID is more than an audit trail needs to carry. A probe whose
    output is meant to be pasted into a chat defeated that in one line.
    """
    text = probe._brief(LeakyAccount(), limit=500)
    assert "L125551606" not in text
    assert "1234567" not in text
    assert "person_id=<redacted>" in text
    assert "broker_id='F002000'" in text          # non-identifying, still useful


def test_redaction_happens_before_truncation():
    """Truncating first could cut mid-value and leak a prefix."""
    assert "L125551606" not in probe._brief(LeakyAccount(), limit=30)


def test_a_bare_national_id_in_free_text_is_redacted():
    """REGRESSION 2 — field-name redaction was not enough. The SDK embeds the ID
    *bare* inside a session-event string, where there is no `person_id=` to
    match on, and run 2 leaked it again through exactly that route."""
    leaked = ("#P2P/v:bcsolace01/Oryfkqqm/PYAPI/L125551606/0815/165616/"
              "792488600/49.213.252.147/>")
    assert "L125551606" not in probe._redact(leaked)


def test_exception_text_is_redacted_too(capsys):
    """The failure path can quote the object that caused it."""
    ok, value = probe.call("boom", lambda: (_ for _ in ()).throw(
        RuntimeError("rejected for person_id='L125551606'")))
    assert "L125551606" not in value
    assert "L125551606" not in capsys.readouterr().out


def test_summarise_contains_every_finding():
    text = probe.summarise({"Q6_login_now": "GRANTED", "Q7_x": "raised"})
    assert "Q6_login_now" in text and "GRANTED" in text
    assert "PASTE THIS BACK" in text


# ---- off-hours mode --------------------------------------------------------


def test_offhours_mode_never_subscribes_or_orders(monkeypatch, creds):
    """The safety promise in the docstring, asserted rather than trusted."""
    api = FakeApi()
    _install_fake(monkeypatch, api)
    assert probe.main(["--mode", "offhours", "--settle-seconds", "0"]) == 0
    assert "subscribe" not in api.calls
    assert "place_order" not in api.calls
    assert "snapshots" not in api.calls


def test_offhours_answers_q6_and_q7(monkeypatch, creds, capsys):
    api = FakeApi()
    _install_fake(monkeypatch, api)
    probe.main(["--mode", "offhours", "--settle-seconds", "0"])
    out = capsys.readouterr().out
    assert "GRANTED" in out                       # Q6: a session was granted
    assert "GOOD, usage() is a valid check" in out  # Q7: it raised when dead


def test_q8_verdict_on_the_real_measured_deltas():
    """MEASURED 2026-08-16 run 2: plain +0, force_refresh +1, logout+login +1,
    and logout itself releasing nothing (baseline 1, still 2 after logout)."""
    verdict = probe._q8_verdict(
        {"Q1_plain": 0, "Q1_force_refresh": 1, "Q1_logout_then_login": 1},
        baseline=1, after_logout=2)
    assert "plain login is a NO-OP" in verdict
    assert "USE THIS FORM" in verdict
    assert "logout did NOT release" in verdict


def test_q8_verdict_does_not_advise_logout_when_logout_frees_nothing():
    """REGRESSION — the first verdict said 'MUST logout first', which the run
    then falsified: logout released no connection at all."""
    verdict = probe._q8_verdict({"Q1_plain": 0, "Q1_force_refresh": 1},
                                baseline=1, after_logout=2)
    assert "MUST logout first" not in verdict


def test_q8_verdict_credits_a_logout_that_does_release():
    verdict = probe._q8_verdict({"Q1_plain": 0, "Q1_force_refresh": 1},
                                baseline=2, after_logout=1)
    assert "released down to 1" in verdict


def test_session_mode_defaults_to_force_refresh(capsys):
    """REGRESSION — session mode once used a plain `login()`, which Q8 later
    proved is a no-op. Q2/Q3 would then have measured whether a subscription
    survives a call that does nothing, reported a confident YES, and sent us to
    wire a closure that breaks the first time it runs for real.

    The plain form is now the deliberate Q10 control, reachable only by opting
    in — never the default.
    """
    with pytest.raises(SystemExit):
        probe.main(["--help"])
    helptext = capsys.readouterr().out
    assert "--relogin-form {force_refresh,plain}" in helptext

    src = pathlib.Path(probe.__file__).read_text(encoding="utf-8")
    assert 'default="force_refresh"' in src
    body = src.split("def _session_questions")[1].split("def _q10_verdict")[0]
    assert '"force_refresh": True' in body
    assert 'call("login #2 (plain)"' not in body


def test_q10_verdict_forks_on_the_mechanism():
    """The control's whole purpose: a no-op login that disturbs nothing means
    callbacks die with the connection object; one that breaks them anyway means
    every `login()` is destructive and the story is wrong."""
    survived = probe._q10_verdict(172, 165, 2, 2)
    assert "disturbs NOTHING" in survived
    assert "die with the CONNECTION" in survived

    broke = probe._q10_verdict(172, 0, 2, 0)
    assert "story is WRONG" in broke
    assert "re-assert unconditionally" in broke


def test_q10_treats_a_skipped_order_probe_as_no_evidence():
    """--no-order leaves the callback counts unset; that must not read as a
    dropped callback."""
    assert "disturbs NOTHING" in probe._q10_verdict(172, 165, None, None)


def test_q9_names_both_outcomes():
    findings = {}
    api = FakeApi()
    api.logged_in = True
    api.connections = 3
    probe.watch_connections(api, minutes=0.0, every=0, findings=findings)
    assert "Q9_verdict" not in findings          # no samples, no claim


def test_q8_exercises_the_logout_then_login_form(monkeypatch, creds):
    """Q1 specified three forms; the first run only ever ran two."""
    api = FakeApi()
    _install_fake(monkeypatch, api)
    probe.main(["--mode", "offhours", "--settle-seconds", "0"])
    # login, plain, force_refresh, logout, login  — the 4th login is the form
    # that is unambiguously a fresh session.
    assert api.calls.count("logout") >= 2
    assert api.calls.count("login") >= 3


def test_q7_flags_a_usage_that_returns_quietly_on_a_dead_token(
        monkeypatch, creds, capsys):
    """The outcome that would silently disarm the verify window: `usage()`
    answering instead of raising once the token is dead."""
    api = FakeApi(usage_after_logout={"remaining_bytes": 1})
    _install_fake(monkeypatch, api)
    probe.main(["--mode", "offhours", "--settle-seconds", "0"])
    assert "INERT, use list_positions instead" in capsys.readouterr().out


def test_q7_runs_last_because_it_destroys_the_session(monkeypatch, creds):
    """Logout manufactures a dead token in seconds instead of waiting 24 h — but
    nothing can run after it, so its position in the sequence is load-bearing."""
    api = FakeApi()
    _install_fake(monkeypatch, api)
    probe.main(["--mode", "offhours", "--settle-seconds", "0"])
    first_logout = api.calls.index("logout")
    assert "usage" in api.calls[first_logout:], "Q7 must probe after the logout"
    assert "list_positions" in api.calls[:first_logout], (
        "the live-token questions must all be answered before the logout")


def test_a_refused_login_is_reported_not_raised(monkeypatch, creds, capsys):
    """Q6's whole point: a weekend refusal is an answer, and it must end the
    probe cleanly rather than as a traceback."""
    api = FakeApi()

    def refuse(**kw):
        raise RuntimeError("AuthError: not a trading day")

    api.login = refuse
    _install_fake(monkeypatch, api)
    assert probe.main(["--mode", "offhours", "--settle-seconds", "0"]) == 0
    out = capsys.readouterr().out
    assert "REFUSED" in out
    assert "PASTE THIS BACK" in out


def test_missing_credentials_refuse_before_touching_the_sdk(monkeypatch):
    monkeypatch.delenv("SHIOAJI_API_KEY", raising=False)
    monkeypatch.delenv("SHIOAJI_SECRET_KEY", raising=False)
    assert probe.main(["--mode", "offhours", "--settle-seconds", "0"]) == 2


def test_a_non_simulation_client_aborts(monkeypatch, creds):
    """`simulation=True` is asserted, not assumed — the §7.2 rule, unchanged."""
    api = FakeApi(simulation=False)
    _install_fake(monkeypatch, api)
    assert probe.main(["--mode", "offhours", "--settle-seconds", "0"]) == 2
    assert "login" not in api.calls


# ---- reassert mode ---------------------------------------------------------


class ReassertApi(FakeApi):
    """Order callbacks die on a `force_refresh` login and come back only on the
    rung named by `restore_on` — so a test can say which fix the probe finds."""

    def __init__(self, restore_on=None):
        super().__init__(connections_per_login=1)
        self.restore_on = restore_on
        self._order_cb = None
        self.deaf = False

    def login(self, **kw):
        out = super().login(**kw)
        if kw.get("force_refresh"):
            self.deaf = True
            self.futopt_account = FakeAccount()     # a new object, same account
        elif self.restore_on == "fresh" and "logout" in self.calls:
            self.deaf = False
        return out

    def set_order_callback(self, cb):
        self._note("set_order_callback")
        self._order_cb = cb
        if self.restore_on == "callback":
            self.deaf = False

    def subscribe_trade(self, account):
        self._note("subscribe_trade")
        if self.restore_on == "new_account" and account is self.futopt_account:
            self.deaf = False
        return True

    def snapshots(self, *a, **kw):
        self._note("snapshots")
        return [types.SimpleNamespace(close=20000.0)]

    def place_order(self, contract, order):
        self._note("place_order")
        if not self.deaf and self._order_cb is not None:
            self._order_cb("FORDER", {})            # New
            self._order_cb("FORDER", {})            # Cancel
        return object()

    def update_status(self, *a, **kw):
        self._note("update_status")

    def cancel_order(self, trade):
        self._note("cancel_order")

    def list_trades(self):
        return []


def _run_reassert(monkeypatch, api, capsys):
    fake = types.ModuleType("shioaji")
    fake.__version__ = "1.7.2-fake"
    fake.Shioaji = lambda **kw: api
    fake.FuturesOrder = lambda **kw: kw
    fake.Action = types.SimpleNamespace(Buy="Buy")
    fake.FuturesPriceType = types.SimpleNamespace(LMT="LMT")
    fake.OrderType = types.SimpleNamespace(ROD="ROD")
    fake.FuturesOCType = types.SimpleNamespace(New="New")
    monkeypatch.setitem(sys.modules, "shioaji", fake)
    monkeypatch.setattr(probe, "resolve_txf", lambda api, name: (
        types.SimpleNamespace(code="TXFR1", target_code="TXFJ6"), "TXFR1"))
    monkeypatch.setattr(probe.time, "sleep", lambda s: None)
    assert probe.main(["--mode", "reassert", "--settle-seconds", "0"]) == 0
    return capsys.readouterr().out


def test_reassert_stops_at_the_first_rung_that_restores_the_callback(
        monkeypatch, creds, capsys):
    """The minimal fix is the first rung heard — later rungs must not run, or
    R4's logout would be credited with what R3 already did."""
    api = ReassertApi(restore_on="new_account")
    out = _run_reassert(monkeypatch, api, capsys)
    assert "R3_subscribe_trade_new_account is the first step" in out
    assert api.calls.count("place_order") == 4   # baseline, R1, R2, R3
    last_order = len(api.calls) - 1 - api.calls[::-1].index("place_order")
    assert "logout" not in api.calls[:last_order], "R4's logout ran"


def test_reassert_says_so_when_nothing_restores_it(monkeypatch, creds, capsys):
    out = _run_reassert(monkeypatch, ReassertApi(restore_on=None), capsys)
    assert "NOTHING restores the order callback in-process" in out


def test_reassert_credits_a_fresh_session(monkeypatch, creds, capsys):
    out = _run_reassert(monkeypatch, ReassertApi(restore_on="fresh"), capsys)
    assert "R4_logout_then_login is the first step" in out


def test_reassert_without_a_baseline_claims_nothing(monkeypatch, creds, capsys):
    """Closed market: no baseline callbacks, so no rung can be judged."""
    api = ReassertApi(restore_on="new_account")
    api.place_order = lambda contract, order: object()   # never heard
    out = _run_reassert(monkeypatch, api, capsys)
    assert "inconclusive — no baseline callbacks" in out
    assert "login_force" not in api.calls


def test_r_verdict_flags_a_duplicating_rung():
    assert "DUPLICATED" in probe._r_verdict("R1_set_order_callback", 2, 4)
