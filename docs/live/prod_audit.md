# [live/prod_audit.py](../../live/prod_audit.py)

**Role:** Did anything we did in "simulation" reach the real account?
**Pipeline:** standalone — a **production** session, read-only, outside the trading process.
**Depends on:** — (duck-typed against the SDK)   **Used by:** run by hand, when the API key changes

```powershell
uv run python -m forecast_eval.live.prod_audit --i-am-querying-production
```

> **The only module in the repo that passes `simulation=False`.** That exception
> is named in [tests/test_live_simulation_guard.py](../tests.md), which also
> asserts every *other* construction is `simulation=True` and that this module
> cannot reach any order API.

## Responsibility
Simulation mode cannot be verified from inside a simulation session: the SDK
exposes no flag (§7.2 — `api.simulation` is absent before login and `None`
after), so `[SIMULATION]` in every log line records *intent*, not evidence. The
only independent check is the production account itself.

## Two outcomes, and the first is better
**1 — the login is refused for lack of production permission.** MEASURED
2026-08-11: `Token doesn't have production permission.` This is the strongest
result available. The credential is scoped to simulation **by the venue**, so no
order placed with it can reach a real account regardless of what the client
passes; it holds for every session rather than one tested window, and it makes
the comparison below unnecessary. Reported as a pass, exit 0.

**2 — the login succeeds.** Then the key *can* reach production and the
client-side `simulation=` argument is the only thing keeping orders out of it.
Use the before/after pair:

```powershell
uv run python -m forecast_eval.live.prod_audit --i-am-querying-production --baseline
# ... run the simulated session ...
uv run python -m forecast_eval.live.prod_audit --i-am-querying-production
```

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `snapshot(api, account, *, days=7)` | function | positions, trades and settled P&L, each failing independently |
| `compare(before, after)` | function | findings; an errored section is `UNKNOWN`, never "unchanged" |
| `is_simulation_only_token(error)` | function | was the login refused *because the credential is scoped*? |

## Exit codes
| Code | Meaning |
|---|---|
| `0` | credential cannot reach production, **or** production unchanged |
| `1` | production **changed** — investigate before trading again |
| `2` | refused (no `--i-am-querying-production`) or no credentials |
| `3` | no baseline — this is a snapshot, not evidence |

## Key invariants / gotchas
- **A position on its own proves nothing.** The account may hold positions for
  reasons unrelated to us; only a *change across* a simulated session is
  diagnostic. Without a baseline the tool refuses a verdict rather than
  presenting a snapshot as one.
- **Positions alone are not enough either.** An order that opened *and closed*
  leaves a net position of zero, so trades and settled P&L are compared too —
  those record the event, not its residue.
- **A failed query is `UNKNOWN`, never "unchanged".** That is the one genuinely
  dangerous answer this module could give: it would clear the production account
  on no evidence. Each section is queried independently so a permission gap on
  one cannot hide the others.
- **Only a permission refusal counts as the good outcome.** A network drop, a bad
  secret or a generic "insufficient permission" must not be read as "safely
  scoped" — there is a test for each.
- **The guarantee now lives in the credential, not the code.** Issuing a
  production-capable key into the same `SHIOAJI_API_KEY` would remove it with no
  code change and nothing in the suite to catch it. Re-run this whenever the key
  is reissued or its permissions change.
- **Deliberately not wired into the live CLI's start-up.** Catching that case
  automatically would put a production code path inside the trading process,
  which is exactly the structure being kept out.

## Related
[live/cli.py](cli.md) · [live/v4_probe.py](v4_probe.md) · [live/v72_probe.py](v72_probe.md) · [SHIOAJI_PLAN.md §7.2](../../SHIOAJI_PLAN.md)
