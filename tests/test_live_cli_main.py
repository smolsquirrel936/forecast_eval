"""`live/cli.py:main` — the setup half, actually executed.

Three bugs shipped in `main` on 2026-09-09 (missing imports, then two
UnboundLocalErrors), every one of them found by a user running the CLI rather
than by the suite, because **nothing here called `main`**. It logs in, makes
directories and talks to a venue, so it had been left to integration-by-operator.

The venue, the credentials and the runtime are stubbed. The run is stopped the
moment `LiveRuntime` would be constructed — by then every line this file cares
about has executed: argument resolution, the restart guard, the strategy
resolution, `--param` coercion, the recorder's `meta` header and `params.json`.
The trading loop is out of scope and covered by `test_live_runtime.py`.

The sentinel derives from `BaseException` on purpose: `main` wraps its run in
`except Exception`, which would otherwise swallow it and turn a proof into an
exit code.
"""
import json
import sys
import types

import pytest

from forecast_eval.live import cli


class _ReachedRuntime(BaseException):
    """Raised in place of constructing a LiveRuntime."""

    def __init__(self, kwargs):
        super().__init__("reached LiveRuntime")
        self.kwargs = kwargs


class _FakeAccount:
    account_id = "1234567"
    broker_id = "F002000"
    account_type = "F"
    signed = True


class _FakeApi:
    def __init__(self, *a, **kw):
        self.futopt_account = _FakeAccount()

    def login(self, **kw):
        return None

    def usage(self):
        return types.SimpleNamespace(connections=1, bytes=0, limit_bytes=10**9)

    def logout(self):
        return None


@pytest.fixture
def stub_venue(monkeypatch):
    """Everything between `main` and the outside world."""
    fake_sj = types.ModuleType("shioaji")
    fake_sj.Shioaji = _FakeApi
    fake_sj.QuoteType = types.SimpleNamespace(Tick="tick")
    monkeypatch.setitem(sys.modules, "shioaji", fake_sj)

    monkeypatch.setattr(cli.credentials, "load",
                        lambda name: types.SimpleNamespace(api_key="k", secret_key="s"))
    monkeypatch.setattr(cli, "resolve_txf",
                        lambda api, name: (types.SimpleNamespace(code="TXF202609"),
                                           "code=TXF202609 (stub)"))
    monkeypatch.setattr(cli, "check_account_fingerprint", lambda fp, path: None)

    captured = {}

    class _StubRuntime:
        """Constructed for real, then stops the run at `adopt_position`.

        The cut is deliberately AFTER construction: `main` prints the rails
        banner, the broker banner, `describe()` and the `--param` warning from
        the runtime's own attributes, so cutting at construction would leave the
        console output untested. `adopt_position` is the first thing that needs a
        venue.
        """

        def __init__(self, cfg, **kwargs):
            captured.update(kwargs)
            self.rails = types.SimpleNamespace(banner=lambda: "[SIMULATION] stub")
            self.broker = types.SimpleNamespace(
                broker_position_detail=lambda: None)

        def adopt_position(self, *a, **kw):
            raise _ReachedRuntime(captured)

        # `main`'s `finally` runs even on the sentinel, and an AttributeError in
        # it would mask the sentinel entirely (that is how the first draft of
        # this file failed). The teardown reads `shutdown`, `summary`,
        # `roll_fills` and `loop.fills`, and `ArtifactWriter.write(rt)` walks the
        # runtime, so artifact writing is disabled rather than satisfied.
        def shutdown(self, *a, **kw):
            return {}

        def summary(self):
            return {}

        roll_fills = None
        loop = types.SimpleNamespace(fills=[])

    monkeypatch.setattr(cli, "LiveRuntime", _StubRuntime)

    # Tech: neutralise the teardown's artifact write.
    # Why:  `ArtifactWriter.write(rt, report=True)` renders a full report off the
    #       runtime, which a stub cannot supply and which this file does not
    #       test. `params.json` -- the thing these cases DO assert on -- is
    #       written earlier by `write_params`, well before the sentinel.
    monkeypatch.setattr(cli.ArtifactWriter, "write", lambda self, *a, **kw: None)
    return captured


def _run(tmp_path, stub_venue, *extra):
    argv = ["--strategy", "fullday", "--broker", "dryrun", "--resume",
            "--out", str(tmp_path / "run"),
            "--state-file", str(tmp_path / "state.json"), *extra]
    with pytest.raises(_ReachedRuntime) as excinfo:
        cli.main(argv)
    return excinfo.value.kwargs, json.loads(
        (tmp_path / "run" / "params.json").read_text(encoding="utf-8"))


def test_main_reaches_the_runtime_with_the_strategy_resolved(tmp_path, stub_venue):
    """The regression test for all three shipped bugs at once.

    Getting here means every name `main` touches on the way is imported and
    bound: a missing import or a use-before-assignment raises long before the
    sentinel does.
    """
    kwargs, params = _run(tmp_path, stub_venue)

    assert kwargs["strategy_overrides"] == {}
    assert params["strategy_package"] == "fullday"
    assert params["strategy_overridden"] is False
    assert set(params["strategy_origins"].values()) == {"fullday"}


def test_an_untuned_deployment_is_no_longer_refused(tmp_path, stub_venue):
    """D1, end to end rather than by inspection. `fullday` is unprovenanced and
    used to return 5 here; reaching the runtime at all is the proof."""
    _, params = _run(tmp_path, stub_venue)
    assert params["source_commit"] is None and params["source_calmar"] is None


def test_param_reaches_the_runtime_and_the_artifact(tmp_path, stub_venue):
    """D7. A typed knob has to survive into the object that trades AND into the
    record, or the run is unreproducible."""
    kwargs, params = _run(tmp_path, stub_venue,
                          "--param", "min_prob=0.42",
                          "--param", "aggression_ticks=2")

    assert kwargs["strategy_overrides"] == {"min_prob": 0.42, "aggression_ticks": 2}
    assert params["min_prob"] == 0.42 and params["aggression_ticks"] == 2
    assert params["strategy_overridden"] is True
    assert params["strategy_origins"]["min_prob"] == "param"
    assert params["strategy_origins"]["buy_threshold"] == "fullday"


def test_an_overridden_run_says_so_on_the_console(tmp_path, stub_venue, capsys):
    """`describe()` renders the resolved values, so an overridden run's banner
    looks exactly like a tuned one. The extra line is what stops that from
    hiding the fact that the numbers were typed."""
    _run(tmp_path, stub_venue, "--param", "min_prob=0.42")

    out = capsys.readouterr().out
    assert "min_prob=0.42" in out, "the banner renders the RUN's knobs"
    assert "--param OVERRIDES" in out
    assert "NOT comparable" in out


def test_a_misspelled_param_is_refused_before_the_guard_and_the_login(
        tmp_path, stub_venue, capsys):
    """Found by writing this file: the typo used to survive until `LiveRuntime`
    constructed it — after `guard.start()` marked a run in flight and after a
    login had been spent, so a slip cost a phantom restart failure and a venue
    round trip. It is now an argument check, exit 5, before either.
    """
    argv = ["--strategy", "fullday", "--broker", "dryrun", "--resume",
            "--out", str(tmp_path / "run"),
            "--state-file", str(tmp_path / "state.json"),
            "--param", "min_prb=0.42"]
    assert cli.main(argv) == 5

    err = capsys.readouterr().err
    assert "min_prb" in err and "min_prob" in err, "name the typo and the real set"
    assert not (tmp_path / "run").exists(), "refused before the run directory"


def test_an_uncoercible_param_is_refused_the_same_way(tmp_path, stub_venue, capsys):
    """`--param context_length=3008.5` would truncate silently if coerced
    loosely; `param_override` refuses it, and the CLI turns that into exit 5
    rather than a traceback."""
    argv = ["--strategy", "fullday", "--broker", "dryrun", "--resume",
            "--out", str(tmp_path / "run"),
            "--state-file", str(tmp_path / "state.json"),
            "--param", "context_length=3008.5"]
    assert cli.main(argv) == 5
    assert "expected an integer" in capsys.readouterr().err


def test_config_is_still_accepted_as_an_alias(tmp_path, stub_venue):
    """D5 — recorded soak commands spell it `--config`."""
    argv = ["--config", "fullday", "--broker", "dryrun", "--resume",
            "--out", str(tmp_path / "run"),
            "--state-file", str(tmp_path / "state.json")]
    with pytest.raises(_ReachedRuntime):
        cli.main(argv)
