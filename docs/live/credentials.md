# [live/credentials.py](../../live/credentials.py)

**Role:** One Shioaji API credential per deployment — the plumbing for S8, not the evidence for it.
**Pipeline:** read once by [live/cli.py](cli.md) before login, and by [multi_deploy_probe](multi_deploy_probe.md).
**Depends on:** `tomllib` (stdlib), `config/secrets.toml`   **Used by:** [live/cli.py](cli.md)

## Responsibility
Each deployment gets its **own API credential**, so the three strategies do not
submit orders through one session — a BUY from one and a SELL from another must
not flatten each other. This module is how a deployment says which credential it
uses.

**What is measured, and what is not.** The three credentials report the **same
`account_id`**. Whether their positions actually net is a separate question that
`account_id` cannot answer, and it is **unmeasured** — see STRATEGY_SPLIT_PLAN
§13 and the probe's `--mode session` (Q5). Shioaji may well track a simulated
book per API key, in which case the separation works as intended. This module is
the plumbing, not the proof.

It costs very little because every rail is already account-scoped:
`list_positions(account=...)` takes one, `subscribe_trade(account)` subscribes
one, and [broker/shioaji.py](broker/shioaji.md) already passes the account it was
built with.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `CONFIG_DIR` / `SECRETS_FILE` | constants | `forecast_eval/config/` and `config/secrets.toml`, resolved from **this file**, never the CWD |
| `Credential` | frozen dataclass | `deployment`, `api_key`, `secret_key`, `source`; `__repr__` redacts |
| `load(deployment)` | function | that deployment's credential, or `None` when nothing is configured |

## Where the values live
`config/secrets.toml` — **gitignored**, never committed.
`config/secrets.example.toml` — the committed template.

```toml
[night]
api_key = "..."
secret_key = "..."
```

Keys-on-disk was chosen deliberately over env-var names. These are the guards
that make that choice as safe as it can be:

- **A `secrets.toml` that git is tracking is REFUSED, not read.** That is the one
  way this design leaks three live trading credentials at once, and the refusal
  names the fix in order — `git rm --cached`, check `.gitignore`, then **rotate**,
  because a key that was committed is disclosed whether or not it was pushed.
- **The example's placeholders are rejected** rather than sent to the broker,
  where they would look like a network failure.
- **No key material in any `repr`**, which is what travels through tracebacks and
  debugger sessions. The account id is masked too, as [cli.py](cli.md) does.

## There is deliberately no account field
An earlier version of this module carried an `expected_account` that was checked
at login. It was removed on 2026-09-02: [cli.py](cli.md) **already** has
`account_fingerprint` / `check_account_fingerprint`, which digests the account
behind every run, stores it per deployment in `live_account.json` and compares it
run-to-run — with nothing to keep in step by hand.

The duplicate was also worse in kind. It **refused** (exit 5) where the
established check deliberately **warns**, and that check's own comment says why:

> *"An account change can be legitimate (a new sub-account) … so the honest
> action is to make the change impossible to miss, not to block on it."*

Which is exactly the situation here — obtaining sub-accounts is the expected next
step, and a hard refusal would have blocked it. See STRATEGY_SPLIT_PLAN §13.

## Key points / gotchas
- **Paths resolve from this file, not the CWD.** `--out` is deliberately relative
  and resolves against the launch directory; credentials must not behave that
  way, or a run started from the wrong folder silently picks up a different key
  set — a failure that looks like a broker outage.
- **The environment is still the fallback.** With no `config/secrets.toml`, or no
  section for the requested deployment, `load` returns the legacy
  `SHIOAJI_API_KEY` / `SHIOAJI_SECRET_KEY` pair. Every existing single-credential
  run, the NIGHT soak included, works unchanged.
- **`load` returns `None` rather than raising** when nothing is configured, so
  `cli.py` can turn it into a `no_credentials` fatal through its own alerter —
  where that message belongs. It *does* raise for a tracked file or a leftover
  placeholder, which are mistakes, not absences.
- **The git check fails open.** No git, not a repo, git not installed ⇒ "not
  tracked". A safety check that crashes in an unusual environment is worse than
  one that declines to fire, and the failure it guards against only happens
  inside a working repo.

## Related
[live/cli.py](cli.md) · [live/multi_deploy_probe.py](multi_deploy_probe.md) · [live/config.py](config.md) · [live/state.py](state.md) · [STRATEGY_SPLIT_PLAN.md](../../STRATEGY_SPLIT_PLAN.md)
