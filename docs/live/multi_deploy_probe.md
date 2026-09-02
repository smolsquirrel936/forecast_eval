# [live/multi_deploy_probe.py](../../live/multi_deploy_probe.py)

**Role:** STRATEGY_SPLIT_PLAN S7/S8 — verify the three deployments really are on three accounts.
**Pipeline:** a standalone probe, not part of a run. Decided S8; now guards it.
**Depends on:** [live/ioc_probe.py](ioc_probe.md) (`redact`), `shioaji`   **Used by:** a person, by hand

## Responsibility
S8 is a choice between **(c) separate accounts** — zero code, every rail
unchanged — and **(b) a shared position broker**, the largest single piece of
work in the split. Nothing in the repo can decide it: it turns on facts about the
venue and the credential. `Shioaji` is a compiled `_core.pyd` with no source and
no docstrings on the calls that matter, so this measures instead of reading.

## What introspection already settled (2026-09-02), so the probe does not re-ask
| fact | consequence |
|---|---|
| `login(..., subscribe_trade=True, ...)` | the deal stream is subscribed **per account**, and every account is subscribed by default |
| `subscribe_trade(account)` / `unsubscribe_trade(account)` exist | a session *can* in principle be narrowed to one account |
| `list_positions(account=None, ...)` | position queries are already account-scoped; [broker/shioaji.py](broker/shioaji.md) already passes one |
| `usage() -> UsageOut.connections: int` | simultaneous connections are directly observable, not inferred |

## S8 is decided — what the probe asks now
The first run (2026-09-02) measured **one futures account** on the original
credential, so option (c) was not reachable *on it*. The answer was to obtain
**one API credential per deployment** — which is (c) after all, by a route the
code cannot arrange. See [live/credentials.py](credentials.md).

So the live questions are no longer "can one credential be split"; they are the
ones the new design **rests on**.

| | Question | Mode | Why it matters |
|---|---|---|---|
| **Q0** | Do the three credentials reach three **distinct** accounts? | `accounts` | **Load-bearing.** Three keys against one account fixes nothing — every §7.1 halt returns, invisibly, until two deployments hold at once |
| **Q4** | Does a deal on A's session reach B's callback? | `session` | separate accounts *should* isolate this; measured, not assumed |
| **Q5** | Does a position opened by A appear in B's `list_positions()`? | `session` | same assumption from the other side — it is what `on_reconcile`'s correctness now rests on |

### Retired, and not worth re-running
| | | answer |
|---|---|---|
| Q1 | accounts on one credential | **1.** Moot — there are three credentials now |
| Q2 | do two simultaneous logins coexist? | **Yes** — `connections` 1 → 2, first session stayed usable. Moot: different credentials |
| Q3 | can a session opt out of an account's deal stream? | call accepted; moot, each credential sees only its own account |

`--mode offhours` still runs Q1–Q3, kept for vetting a brand-new credential.

## Modes
```bash
# Q0. Reads config/secrets.toml through the same loader cli.py uses.
# No market, no orders, ~1 minute. START HERE, and after any credential change.
uv run python -m forecast_eval.live.multi_deploy_probe --mode accounts

# Q4-Q5. Places ONE resting order 500 points from the market, inside a session.
uv run python -m forecast_eval.live.multi_deploy_probe --mode session \n    --i-understand-this-places-orders
```

Exit codes: `0` separation holds · `1` **two deployments share an account** ·
`2` nothing configured.

## Key points / gotchas
- **Run `accounts` first, and after every credential change.** It needs no
  market and takes about a minute. If two deployments share an account, nothing
  downstream can save them — and it exits **1** so a launch script can refuse.
- **Q0 measures identity, not netting.** Equal `account_id`s across credentials
  do not establish that positions net — Shioaji may track a simulated book per
  API key. That is Q5, in `--mode session`. There is no account field to fill in:
  [cli.py](cli.md)'s existing fingerprint already watches for a moved account.
- **The first run of this probe was wrong, and that is instructive.** Its Q1 said
  "FUTOPT accounts 0" for a credential trading TXF nightly, because the test was
  `"Future" in str(account_type)` and `str(AccountType.Future)` is `'F'`. The
  classifier now compares the enum member and prints every account's raw type
  beside the count — a number with no evidence under it is how that passed.
- **A quiet Q4 is not proof of isolation.** A resting order that never fills may
  produce no deal event for anyone. The probe says so in its own output rather
  than letting silence read as a pass.
- **Order safety:** `--mode session` requires
  `--i-understand-this-places-orders`, the same acknowledgement
  [cli.py](cli.md) demands, and exits 5 without it. The order rests 500 points
  below the market so it cannot fill by accident, and is cancelled before exit —
  if the cancel fails the probe says so loudly.
- **Redaction happens in a PARENT process, not in this one.** The probe re-runs
  itself as a child with stderr folded into stdout and filters every line through
  [ioc_probe](ioc_probe.md)'s `redact`. Shioaji's compiled `_core.pyd` writes a
  connection banner (`Response Code: ... PYAPI/<id>/...`) straight to the console
  from C, where `<id>` is a Taiwanese national ID — `redirect_stdout` and every
  print-level guard miss it. A child's stdout is a pipe by construction, so the
  SDK, Python prints and tracebacks all arrive in one filterable stream.
- **The first fix for this was wrong, and the failure is worth knowing.** It
  swapped file descriptor 1 for a pipe in-process. That works when stdout is
  already a pipe and **crashes on an interactive Windows console** with
  `OSError: [WinError 1] Incorrect function`, because Python uses
  `_WindowsConsoleIO` there and `WriteConsoleW` cannot write to a pipe. Anything
  that reassigns fd 1 in this repo will hit the same wall. The child-process form
  has no such failure mode: the parent's console handle is never touched.

## Related
[live/ioc_probe.py](ioc_probe.md) · [live/relogin_probe.py](relogin_probe.md) · [live/broker/shioaji.py](broker/shioaji.md) · [live/state.py](state.md) · [STRATEGY_SPLIT_PLAN.md](../../STRATEGY_SPLIT_PLAN.md)
