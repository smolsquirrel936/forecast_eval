# [live/relogin_probe.py](../../live/relogin_probe.py)

**Role:** Measure what a second `login()` actually does, so [relogin.py](relogin.md)'s injected callables can be written from evidence.
**Pipeline:** standalone script — run by the operator, output pasted back.
**Depends on:** [contracts.py](contracts.md), [cli.py](cli.md) (`account_fingerprint`), [v72_probe.py](v72_probe.md) (`probe_limit_price`)   **Used by:** nothing — it is an instrument

## Responsibility
[ReloginScheduler](relogin.md) decides *when* to re-login. This decides *what*
the `relogin` and `verify` callables must do — and it is the only instrument
left. `Shioaji` comes from a compiled `_core.pyd`, and `login` / `logout` /
`subscribe` carry **no source and no docstrings** (introspected 2026-08-15).
Reading is exhausted.

## The seven questions
| # | Question | Why it matters |
|---|---|---|
| Q1 | Does `login()` on a live session succeed — plain, `force_refresh=True`, `logout()`+`login()`? | determines the shape of the `relogin` closure |
| Q2 | Does the **tick subscription** survive? | if not, the closure must resubscribe — and [carefully](feedwatch.md), since a doubled stream corrupts bars |
| Q3 | Does the **order callback** survive, or duplicate? | **the dangerous one.** A duplicate double-counts every fill, corrupting the position mirror and PnL at once, and it would look like a venue bug rather than ours |
| Q4 | Does `list_positions()` work on the new token, same answer? | the call that actually died in the soak |
| Q5 | Does the account fingerprint change? | a re-login that lands on a different account must never go unnoticed |
| Q6 | Does Shioaji grant a session on a **non-trading day**? | the anchors are wall-clock and fire Saturday and Sunday (§2.25) |
| Q7 | Does **`api.usage()` raise on a dead token**? | if it returns quietly instead, the verify window is **inert** — passing forever while the token is already gone |

## Two modes
| Mode | Answers | Needs | Cost |
|---|---|---|---|
| `--mode offhours` | Q6, Q7 (+Q1 partially, Q4, Q5) | nothing — any time, including a weekend | ~10 s |
| `--mode session` | all seven | live ticks | ~6 min |

Session mode does **not** need the night session specifically: it tests SDK
mechanics, not strategy, so the day session (08:45–13:45) serves as well and
comes sooner.

```powershell
$env:SHIOAJI_API_KEY="..."; $env:SHIOAJI_SECRET_KEY="..."
uv run python -m forecast_eval.live.relogin_probe --mode offhours
uv run python -m forecast_eval.live.relogin_probe --mode session
```

## Design notes
- **Q7 is destructive and therefore last.** It calls `logout()` to invalidate the
  token, which is the whole point — a dead token in seconds instead of a
  24-hour wait. Nothing can run after it, so the sequence is load-bearing and
  `test_q7_runs_last_because_it_destroys_the_session` pins it.
- **An exception is an answer, not a probe failure.** Every venue call goes
  through `call()`, which catches, prints and records. Letting one propagate
  would abandon the remaining questions and waste the session — which is exactly
  how §2.18 cost a market window.
- **A refused login ends the probe cleanly**, because that *is* Q6's answer.
- **Off-hours mode touches nothing.** No subscription, no snapshot, no order —
  asserted by test against a fake that raises if `place_order` is reached.
- **The order form is the adapter's**, `sj.FuturesOrder` with the top-level
  enums, not `api.Order` with `sj.constant.*`. Those literals were confirmed
  against the venue by the §7.2 probe and the adapter has since placed real
  simulation orders with them; a probe that invented its own form would measure
  a path the live system never takes.
- **The order safety argument is [v72_probe](v72_probe.md)'s, reused not
  restated:** `simulation=True` asserted, one lot, priced far below the touch so
  it is passive by construction, cancelled in a `finally`.
- **Output ends in a paste-back block.** The operator runs this and reports; a
  probe whose output must be interpreted before it can be reported produces
  arguments instead of measurements.

## The answers — all seven, measured
Four runs, 2026-08-16 (off-hours ×3) and 2026-08-17 (day session).

| Q | Answer | Consequence |
|---|---|---|
| **Q1/Q8** | plain `login()` is a **NO-OP** (+0 connections); `force_refresh=True` is **+1** | the closure **must** use `force_refresh=True`, or the fix is inert while printing healthy counters |
| **Q2** | ticks **172 → 0** | the subscription does **not** survive — re-subscribe |
| **Q3** | callbacks **2 → 0** — **DROPPED** | re-register `set_order_callback`. (DUPLICATED would have been far worse) |
| **Q4** | `list_positions()` fine either side | — |
| **Q5** | fingerprint unchanged | a re-login stays on the same account |
| **Q6** | **GRANTED** on a Sunday | wall-clock anchors need no non-trading-day special case |
| **Q7** | `usage()` **raises** on a dead token (`AuthError`) | `api.usage()` is a valid verify call |
| **Q9** | connection spike **clears to 1 in ~30 s**, flat for 15 min | §2.8b's cap of 5 is never approached |
| **Q10** | the control: `plain` login → **+0 connections, ticks and callbacks both survive** | callbacks are bound to the **connection object**. Re-assert after session-replacing logins, and only those |

**The headline:** `force_refresh=True` rebuilds the session object, so all five
start-up registrations must be redone — subscription, tick callback, order
callback, `on_session_down`, `on_event`. See [relogin.md](relogin.md).

## Two defects the runs found in the probe itself
- **It leaked a national ID, twice, by two routes** — first as a `person_id=`
  field in an SDK repr, then bare inside a session-event string where no field
  name existed to match on. Redaction is now both field- and shape-based. Note
  the SDK *also* prints that string on its own stdout, outside any code here.
- **Session mode re-logged in with the plain form**, which Q8 had just proven to
  be a no-op — it would have measured whether a subscription survives a call
  that does nothing and reported a confident "YES". Caught before the run;
  session mode now uses `force_refresh=True` and asserts the connection delta so
  the test cannot silently degrade back into a no-op.

## Related
[live/relogin.py](relogin.md) · [live/v72_probe.py](v72_probe.md) · [live/cli.py](cli.md) · [SHIOAJI_PLAN.md §2.24](../../SHIOAJI_PLAN.md)
