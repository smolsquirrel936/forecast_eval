"""Per-deployment credentials (STRATEGY_SPLIT_PLAN S8).

Storing live trading keys on disk was a deliberate choice over env-var names, so
the guards around that choice are the point of this file, not a formality:

  * a `secrets.toml` that git has started tracking must be REFUSED, not read —
    that is the one way this design leaks three live credentials at once;
  * the committed example's placeholders must never reach the broker;
  * no key material in a `repr`, which is what travels through tracebacks;

There is deliberately no expected-account check here: `cli.py`'s
`account_fingerprint` / `check_account_fingerprint` already detect a moved
account, per deployment, with nothing to configure (STRATEGY_SPLIT_PLAN §13).
"""
import textwrap

import pytest

from forecast_eval.live import credentials
from forecast_eval.live.credentials import Credential


@pytest.fixture
def secrets(tmp_path, monkeypatch):
    """Point the module at a throwaway secrets file."""
    path = tmp_path / "secrets.toml"
    monkeypatch.setattr(credentials, "SECRETS_FILE", path)
    monkeypatch.delenv("SHIOAJI_API_KEY", raising=False)
    monkeypatch.delenv("SHIOAJI_SECRET_KEY", raising=False)
    return path


def test_each_deployment_gets_its_own_credential(secrets):
    secrets.write_text(textwrap.dedent("""
        [night]
        api_key = "night-key"
        secret_key = "night-secret"

        [day]
        api_key = "day-key"
        secret_key = "day-secret"
    """), encoding="utf-8")

    night, day = credentials.load("night"), credentials.load("day")
    assert (night.api_key, night.secret_key) == ("night-key", "night-secret")
    assert (day.api_key, day.secret_key) == ("day-key", "day-secret")
    # The whole point: two deployments, two distinct credentials.
    assert night.api_key != day.api_key


def test_a_missing_deployment_falls_back_to_the_environment(secrets, monkeypatch):
    """Every existing single-credential run — the NIGHT soak included — must
    keep working with no config file at all."""
    monkeypatch.setenv("SHIOAJI_API_KEY", "env-key")
    monkeypatch.setenv("SHIOAJI_SECRET_KEY", "env-secret")
    cred = credentials.load("fullday")
    assert cred.api_key == "env-key"
    assert "SHIOAJI_API_KEY" in cred.source


def test_no_file_and_no_environment_is_None_not_an_exception(secrets):
    """`cli.py` turns None into its own `no_credentials` fatal, through its own
    alerter — that message belongs there, not here."""
    assert credentials.load("night") is None


def test_a_tracked_secrets_file_is_refused(secrets, monkeypatch):
    # Tech: force the git-tracking check True and assert `load` raises with a
    #       message naming `git rm --cached` and rotation.
    # Why:  the single failure mode of keys-on-disk. Reading on regardless is how
    #       three live trading credentials reach a public diff, and the refusal
    #       has to say "rotate" — a key that was committed is disclosed whether
    #       or not the commit was pushed.
    secrets.write_text('[night]\napi_key="k"\nsecret_key="s"\n', encoding="utf-8")
    monkeypatch.setattr(credentials, "_is_tracked_by_git", lambda p: True)
    with pytest.raises(RuntimeError, match="TRACKED BY GIT"):
        credentials.load("night")
    with pytest.raises(RuntimeError, match="ROTATE"):
        credentials.load("night")


def test_the_example_placeholder_is_refused(secrets):
    """Copying the template and forgetting one entry is the expected mistake.
    Sent as-is it is a login failure that looks like a network problem."""
    secrets.write_text(
        '[day]\napi_key = "REPLACE_ME"\nsecret_key = "real"\n', encoding="utf-8")
    with pytest.raises(RuntimeError, match="placeholder"):
        credentials.load("day")


def test_the_repr_never_carries_key_material():
    cred = Credential("night", "SUPERSECRETKEY", "SUPERSECRETVALUE")
    text = repr(cred)
    assert "SUPERSECRETKEY" not in text
    assert "SUPERSECRETVALUE" not in text
    assert "<redacted>" in text
