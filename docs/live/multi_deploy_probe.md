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

# X1. Opens +1 and tries to cross it to -1, first with one order and then with
# a sized pair. Places up to six orders, flattening between the phases and at
# the end. Refuses unless the account is already flat.
uv run python -m forecast_eval.live.multi_deploy_probe --mode crossing --i-understand-this-places-orders
```

```bash
# X2. Times deal -> list_positions. Alternates Buy 1 / Sell 1 (default 4
# samples), so it ends flat.
uv run python -m forecast_eval.live.multi_deploy_probe --mode lag --i-understand-this-places-orders
```

## `--mode sessions` — X3, does option (b)'s shape fit?

```bash
uv run python -m forecast_eval.live.multi_deploy_probe --mode sessions
```

No market, no orders, no acknowledgement. About a minute.

[Option (b)](../../STRATEGY_SPLIT_PLAN.md) runs the owner **plus** three
deployments, and each deployment still logs in for the market feed — only orders
are delegated. That is **four** sessions where §14b Q2 measured **two**.

It opens `--sessions` logins (default 4), prints `connections` after each, then
checks the **first** session still answers with every session open. That last
check is the point: Q2's real finding was not that a second login succeeds but
that the first *survives* it, and a login that quietly evicts an earlier session
looks identical to one that coexists — right up to the moment the owner stops
hearing deals.

**Three concurrent logins are already known to work**, incidentally: `--mode
accounts` holds every session until its `finally`, so the 2026-09-02 Q0 run had
three open at once. What it never showed is `connections`, or whether the first
was still usable.

**It also prints the byte quota**, which nothing here has ever read. `usage()` is
called in three places in this repo and only ever as a liveness check, so
`limit_bytes` / `remaining_bytes` have gone unexamined. Four sessions subscribe
to the same TXF tick stream and download it four times against one credential's
allowance — and a quota exhausted mid-session stops a deployment forecasting
while it is still holding a position, which is a worse failure than one at
start-up. The figure to watch is what **one** deployment consumes over a full
session; this run only logs in.

Exit **1** if a login is refused or the first session is dead.

## `--mode lag` — X2, the number the rail needs

[STRATEGY_SPLIT_PLAN §18.9](../../STRATEGY_SPLIT_PLAN.md) established that the
venue's position read trails its own deal stream, but never timed it. This does.

Per sample: place a marketable Buy 1 (or Sell 1), timestamp the **deal
callback** — the earliest instant this process can know a fill happened, and
exactly when `ShioajiBroker._match` moves the mirror — then poll
`list_positions` every 0.25 s until it reports the new net. The interval between
those is the lag.

The reported numbers are **ceilings at 0.25 s granularity**: the position may
have settled at any point since the previous poll. That is the right direction to
err for a rail tolerance. 0.25 s is 4 reads/second, under the 25-per-5-s
accounting limit [broker/shioaji.py](broker/shioaji.md) documents.

**Why it matters:** `broker_position()` polls `list_positions` live every bar and
[state.py](state.md)'s `on_reconcile` halts on **any** mismatch with no tolerance
and no debounce. A fill landing within the lag of a bar boundary gives a correct
mirror, a stale venue read, and a spurious `STOP` — on the bar right after an
entry or exit. The probe prints the exposed window and stops there: whether that
becomes a tolerance, a re-read, or nothing is a plan decision, not a probe's.

Exit codes: `0` separation holds, `crossing` reached a verdict and got flat, or
`lag` measured at least one sample ·
`1` **two deployments share an account** · `2` nothing configured · `3`
`crossing` refused a non-flat account · `4` `crossing` could not open the +1 it
needs · `5` the order acknowledgement was missing · `6` **`crossing` could not
flatten what it opened — the account is still holding**.

## `--mode crossing` — X1, and what it decides

Added for **S9b**, option (b), the owning process
([STRATEGY_SPLIT_PLAN §18](../../STRATEGY_SPLIT_PLAN.md)).

Under (b) one process owns the account, so it maps `octype` from the **account**
net rather than from a deployment's intent — which is what makes
Cover-with-nothing-to-cover unconstructible. The case that mapping *cannot*
express is an order crossing **through** flat: the account holds +1, the
deployments' intents net to −1, so the resulting Sell 2 is `Cover` for its first
lot and `New` for its second.

The probe asks that directly, in **two phases** against one session.

**Phase A — the single call** (what the owner must *not* do):

| step | order | reads |
|---|---|---|
| A1 | `New` Buy 1, marketable | must leave **+1**, else exit 4 — there is nothing to cross |
| A2 | `Cover` Sell 2, settled, position sampled 3× | **−1** ⇒ *ONE CALL, octype=Cover* · **0** ⇒ *COVER CLAMPS AT FLAT* · anything else ⇒ *SINGLE CALL LANDS WRONG* |
| A3 | `New` Sell 2, only if A2 left +1 | **−1** ⇒ *ONE CALL, octype=New*; otherwise *MUST SPLIT* |

**Phase B — the split**, which is the path the owner will actually issue:

| step | order | reads |
|---|---|---|
| — | flatten, then re-open +1 | skipped rather than guessed if either fails |
| B2 | `Cover` Sell **1** — exactly the lots held | must reach **0**, else the New leg is not sent |
| B3 | `New` Sell 1 | **−1** ⇒ *SPLIT PAIR CONFIRMED* |

Phase B is the one that matters. Phase A only establishes what does not work; a
failing remedy discovered while writing the broker is far more expensive than one
discovered here. It also answers what Phase A cannot — whether the `New` leg
opens correctly immediately after a `Cover` leg with no settling gap.

### MEASURED 2026-09-07: both forms work — and the position read lags

Final answer, `simulation=True`, DAY session, every position sampled 3× over 6 s:

```
A1  New   Buy  1  -> Filled                          position +1
A2  Cover Sell 2  -> Filled, TWO deals 1 @ 47403 + 1 @ 47402 (9s apart)
                                                     position -1  (-1, -1, -1)
B2  Cover Sell 1  -> Filled                          position +0  (+0, +0, +0)
B3  New   Sell 1  -> Filled                          position -1  (-1, -1, -1)
```

**A single `Cover` crosses through flat, and so does the split pair.** Both are
available; [STRATEGY_SPLIT_PLAN §18.9](../../STRATEGY_SPLIT_PLAN.md) records why
the owner uses the split anyway.

**Two earlier runs said otherwise, and they were misread.** Each read the position
**once**, a second or two after the last deal, and each came back exactly one lot
short of the arithmetic — read first as a `list_positions` lag, then (wrongly) as
`Cover` clamping at flat. Sampling repeatedly settled it: **the venue's position
read trails its own deal stream.** `_stable_net` exists for exactly this, and a
lag settles where a clamp would not.

**Why that matters far beyond this probe.** `broker_position()` polls
`list_positions` live every bar ([broker/shioaji.py](broker/shioaji.md)) and
`on_reconcile` halts on **any** mismatch, with no tolerance and no debounce
([state.py](state.md)). A fill landing shortly before a bar boundary therefore
gives a correct mirror, a stale venue read, and a **spurious `STOP`** — on the bar
right after an entry or exit. The once-a-minute cadence absorbs a few seconds of
lag, and a median 4.5-day hold offers few fills to catch, which is most likely why
the soak has not hit it. That is cadence, not a guard. **The lag's duration is
unmeasured and the rail is unchanged** — a tolerance on a safety rail deserves its
own decision.

**Partial fills are real here.** A2's Cover-2 arrived as two one-lot deals nine
seconds apart, which is why `poll_fills` returns a *list*
([broker/base.py](broker/base.md)) and why anything tracking orders must
accumulate partials rather than treat the first deal as the fill.

**The verdict is an input to `broker/shioaji.py`'s place path** — one
`place_order`, or a pair with an ordering guarantee between them (§18.3 item 1)
— which is why it runs *before* the N-order book is written rather than after.

Every order is **marketable by construction** (buys above the market, sells
below) and `LMT`+`ROD`. That is deliberate, and it is the opposite of `--mode
session`'s resting order: a resting order measures nothing, which is the mistake
§14 records — the first `--mode session` run compared "0 positions vs 0" and
drew a conclusion from it. Only a fill says what the venue did with an `octype`.

### Run it INSIDE a session

Every order `crossing` places is marketable, and the whole measurement is what
the venue did with a **fill**. Outside TXF hours there is nothing to fill
against: A1 times out, is cancelled, and the probe exits **4** having measured
nothing — the same empty result §14 records for a resting order.

Windows are **DAY 08:45–13:45** and **NIGHT 15:00–05:00**, Taipei
([environment.py](../../environment.py)'s `classify_session`; the 13:45–15:00
gap and 05:00–08:45 are closed). The probe classifies the wall clock at start-up
and prints `local time ... session=DAY|NIGHT|CLOSED`. A closed session **warns
rather than refuses**, because the simulation venue's off-hours behaviour is
itself unmeasured and a refusal would block the run that would measure it.

Both measured runs (§18.6, §18.8) were mid-NIGHT, around 18:20 Taipei.

**Do not run it while a deployment is trading the same account.** It opens and
closes positions of its own, which is exactly what
[STRATEGY_SPLIT_PLAN §15](../../STRATEGY_SPLIT_PLAN.md) forbids two writers from
doing. The probe is `simulation=True`, so a real-money soak is unaffected — a
*simulated* one is not.

### The position is never read against a working order

**This mode's first run (2026-09-02) got a wrong answer this way, so the rule is
load-bearing.** It slept a fixed 5 seconds after placing the `Cover` Sell 2, read
the status as `Submitted` — *working, not finished* — saw the position still at
+1, recorded "Cover did not cross", and placed step 3 on top of it. The Cover
then filled in full, two lots, and the two orders' fills were no longer
separable. The verdict it printed is contradicted by the deals in its own
output. See [STRATEGY_SPLIT_PLAN §18.6](../../STRATEGY_SPLIT_PLAN.md).

So:

- **`_settle()` polls `update_status` to a TERMINAL status** — `Filled`,
  `Cancelled` or `Failed` — before any position is read. `_status_name` compares
  the enum's `.value`, not `str(enum)`, because `str()` on a Shioaji enum is not
  its name (the `AccountType.Future` → `'F'` trap, above).
- **An order that will not settle within `--fill-timeout` (30 s) is cancelled**,
  not abandoned. A working order left behind is what filled underneath the next
  step.
- **The exit flatten retries** up to `FLATTEN_ATTEMPTS` (3), re-reading the
  position and re-snapshotting the price each attempt — the rule
  [flatten.py](flatten.md) already carries. The first run's single-shot flatten
  placed a Buy 2 against a −2 book, filled one lot, and left the account short.
  The probe now **exits 6** when it cannot get flat.

A related observation from that run, **carried and not closed**:
`list_positions` appeared to lag the deal stream — four lots sold from +1 read
back as −2 with every deal already printed. `broker_position()` is what the
reconciliation rail compares against ([loop.py](loop.md)), so a position read
that trails its own fills is a concern well past this probe.

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
- **Order safety:** `--mode session` and `--mode crossing` both require
  `--i-understand-this-places-orders`, the same acknowledgement
  [cli.py](cli.md) demands, and exit 5 without it. In `session` the order rests
  500 points below the market so it cannot fill by accident, and is cancelled
  before exit — if the cancel fails the probe says so loudly.
- **`crossing` refuses a non-flat account, and flattens what it opens.** Every
  step is read as a *delta* from a known position, so starting on someone else's
  lot would make step 2's result unattributable and the closing flatten would
  close a position the probe did not open. It exits **3** rather than guess. On
  the way out it closes whatever it holds with `octype=Auto` — deliberately not
  the explicit octype the steps measure, because the flatten is not part of the
  measurement and has to work from whatever state the steps left, including one
  where `Cover` was just refused. If the account is still holding it says so
  loudly and names [flatten.py](flatten.md).
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
