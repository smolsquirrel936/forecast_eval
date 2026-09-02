"""Per-deployment Shioaji credentials — one API key per session.

STRATEGY_SPLIT_PLAN S8. Each deployment gets its **own API credential**, so the
three strategies do not submit orders through one session — the user's reason
being that a BUY from one strategy and a SELL from another must not flatten each
other. This module is how a deployment says which credential it uses.

**What is measured, and what is not.** The three credentials in use report the
**same `account_id`**; whether their positions actually net against each other is
a separate question that `account_id` cannot answer, and it is **unmeasured**
(STRATEGY_SPLIT_PLAN §13, and the probe's `--mode session` Q5). Shioaji may well
track a simulated book per API key, in which case the separation works as
intended. Do not read this module as proof of separation — it is the plumbing,
not the evidence.

Every rail is already account-scoped, which is why this costs so little:
`list_positions(account=...)` takes one, `subscribe_trade(account)` subscribes
one, and `broker/shioaji.py` already passes the account it was constructed with.

**Where the values live.** `config/secrets.toml`, which is **gitignored** and
never committed; `config/secrets.example.toml` is the committed template.
Storing keys on disk was a deliberate choice (the alternative offered was
env-var names); the guards below are what makes that choice as safe as it can be:

  * `load` refuses to read a `secrets.toml` that **git is tracking**, because the
    one failure mode of this design is a `.gitignore` that stopped matching and
    nobody noticing until three live trading credentials are in a public diff;
  * placeholder values from the example file are rejected rather than sent to the
    broker, which would otherwise look like a credential failure at the venue;
  * nothing here ever puts key material in an exception message, a log line or a
    `repr` — the `SecretStr`-ish discipline is manual but total.

**Backwards compatibility.** With no `config/secrets.toml`, or no entry for the
requested deployment, this falls back to `SHIOAJI_API_KEY` / `SHIOAJI_SECRET_KEY`
— the single-credential path every existing run and every soak uses. Nothing that
works today stops working.
"""
from __future__ import annotations

import os
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Tech: the config directory, resolved from this file rather than the CWD.
# Why:  `live/cli.py` documents that `--out` is relative and resolves against the
#       directory you launch from; credentials must NOT behave that way. A run
#       started from the wrong folder picking up a different key set — or none —
#       is a failure that looks like a broker outage.
CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
SECRETS_FILE = CONFIG_DIR / "secrets.toml"

# Tech: values the committed example carries, which must never reach the venue.
# Why:  copying the template and forgetting one entry is the expected mistake.
#       Sent as-is it is a login failure at the broker, three seconds of
#       confusion, and a diagnostic pointing at the network. Caught here it names
#       the line to edit.
_PLACEHOLDERS = {"", "REPLACE_ME", "your-api-key", "your-secret-key"}


@dataclass(frozen=True)
class Credential:
    # Tech: one deployment's login material. `__repr__` is overridden so the key
    #       cannot leak by accident.
    # Why:  a dataclass repr prints every field, and this object travels through
    #       tracebacks, `--verbose` dumps and debugger sessions.
    #
    #       There is deliberately NO expected-account field. `cli.py` already
    #       carries `account_fingerprint` / `check_account_fingerprint`, which
    #       digests the account behind each run, stores it per deployment in
    #       `live_account.json` and compares it run-to-run with nothing to
    #       maintain by hand. A second, manually-configured copy of that check
    #       was redundant — and it REFUSED where the established one warns, on
    #       purpose: "an account change can be legitimate (a new sub-account)".
    deployment: str
    api_key: str
    secret_key: str
    source: str = "config/secrets.toml"

    def __repr__(self) -> str:
        return (f"Credential(deployment={self.deployment!r}, key=<redacted>, "
                f"source={self.source!r})")


def _is_tracked_by_git(path: Path) -> bool:
    # Tech: ask git whether it has this path in the index; any failure (no git,
    #       not a repo, git missing) answers "no".
    # Why:  this is a safety check, and a safety check that CRASHES when the
    #       environment is unusual is worse than one that declines to fire — it
    #       would make the module unimportable on a machine without git. The
    #       failure it exists to catch is a `.gitignore` that stopped matching,
    #       which only happens inside a working repo where this call succeeds.
    try:
        out = subprocess.run(
            ["git", "ls-files", "--error-unmatch", str(path)],
            cwd=str(path.parent), capture_output=True, timeout=10,
        )
        return out.returncode == 0
    except Exception:                                   # noqa: BLE001 — see Why
        return False


def _read_secrets() -> dict:
    # Tech: parse `config/secrets.toml`, refusing it if git is tracking it.
    # Why:  see the module docstring. The refusal is loud and names the fix,
    #       because the alternative — carrying on — is how live trading keys end
    #       up in a commit.
    if not SECRETS_FILE.exists():
        return {}
    if _is_tracked_by_git(SECRETS_FILE):
        raise RuntimeError(
            f"{SECRETS_FILE} is TRACKED BY GIT and holds live trading keys.\n"
            f"  Refusing to read it. Fix, in this order:\n"
            f"    git rm --cached forecast_eval/config/secrets.toml\n"
            f"    confirm 'config/secrets.toml' is in .gitignore\n"
            f"    ROTATE the keys — assume anything committed is disclosed"
        )
    with SECRETS_FILE.open("rb") as fh:
        return tomllib.load(fh)


def load(deployment: str) -> Optional[Credential]:
    """This deployment's credential, or None when nothing is configured.

    Order: `config/secrets.toml` first, then the legacy
    `SHIOAJI_API_KEY`/`SHIOAJI_SECRET_KEY` environment pair.
    """
    # Tech: look the deployment up as a table in the secrets file; fall back to
    #       the environment pair; return None when neither is present.
    # Why:  returning None rather than raising lets the caller decide — `cli.py`
    #       turns it into a `no_credentials` fatal with its own alerter, which is
    #       where that message belongs. The env fallback is what keeps every
    #       existing single-credential run working unchanged.
    table = _read_secrets().get(deployment)
    if isinstance(table, dict):
        api_key = str(table.get("api_key", "")).strip()
        secret_key = str(table.get("secret_key", "")).strip()
        if api_key in _PLACEHOLDERS or secret_key in _PLACEHOLDERS:
            raise RuntimeError(
                f"[{deployment}] in {SECRETS_FILE} still holds the example "
                f"placeholder. Fill in the real api_key/secret_key, or delete "
                f"the section to fall back to the environment."
            )
        return Credential(deployment, api_key, secret_key)

    api_key = os.environ.get("SHIOAJI_API_KEY")
    secret_key = os.environ.get("SHIOAJI_SECRET_KEY")
    if api_key and secret_key:
        return Credential(deployment, api_key, secret_key,
                          source="SHIOAJI_API_KEY/SHIOAJI_SECRET_KEY")
    return None
