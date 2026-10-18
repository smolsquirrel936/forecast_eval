# [live/relogin.py](../../live/relogin.py)

**Role:** Replace the session token before its 24-hour wall, in a window where nothing real is trading.
**Pipeline:** [cli.py](cli.md) owns the API object and drives this from its clock tick.
**Depends on:** [environment.py](../environment.md) for the session opens — no `shioaji` import   **Used by:** [live/cli.py](cli.md), [live/owner_cli.py](owner_cli.md) (its own, earlier anchors), [tests/test_live_relogin.py](../tests.md)

## Responsibility
The session token expires **24 hours after login**, and the SDK has no refresh
method — re-login is the only lever. A 24-hour soak died proving it
(`outputs/live_20260813_191326`, §2.24):

```
24h 0m 36s in:  list_positions raised
                TokenError('StatusCode: 401, Detail: Token is expired')
```

The quote feed was still delivering prints five seconds before the halt. It is
the **REST** token that dies, so the failure surfaces on the reconciliation path
rather than as silence — which is why no feed-side rail can catch it.

Since re-login is forced, *when* is the entire design. Each session open gets two
sequential windows in the quiet before it:

```
open − 5 min ............ open − 1 min ...... open
└── re-login, retried ──┘ └── verify ──┘
    08:40 → 08:44           08:44 → 08:45
    14:55 → 14:59           14:59 → 15:00
```

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `ReloginScheduler(relogin, verify=None, anchors=…, recheck_grace=20s, base_backoff_seconds=60.0, max_backoff_seconds=900.0, recorder=None)` | class | both actions injected as callables |
| `note_login(now)` | method | record when a token was issued; the CLI's startup login must call it |
| `token_age(now)` | method | how old the current token is, or `None` before the first login |
| `check(now)` | method | act if a window is live; returns an `Outcome` or `None` |
| `summary()` | method | the counters the CLI prints and records |
| `Outcome` | enum | `RELOGGED` / `FAILED` / `ABANDONED` / `VERIFIED` / `VERIFY_FAILED` |
| `session_open_anchors(relogin_lead=5min, verify_lead=1min)` | function | `((08:40, 08:44, 08:45), (14:55, 14:59, 15:00))`, derived from `DAY_OPEN`/`NIGHT_OPEN` |
| `in_window(now)` | method | whether a re-login or verify window is live; the owner's lost-session watch stands aside then |
| `deployment_relogin_anchors(deployment)` | function | the anchors with the re-login start moved `stagger_slot × 40 s` later; verify unchanged |
| `stagger_slot(deployment)` / `DEPLOYMENT_STAGGER` / `STAGGER_ORDER` | function / constants | day 0, night 1, fullday 2 (anything else 0); 40 s; the order |

## The deployments are staggered (since 2026-10-18)
`force_refresh=True` opens its new connection ~30 s before the old one drops
(Q9 below). With the owner and three deployments connected, three refreshes in
the same second briefly made **7** against the cap of 5, and what Shioaji does
above 5 has never been measured. Each deployment now starts its re-login window
`stagger_slot × 40 s` late (08:40:00 / 08:40:40 / 08:41:20, and 14:55 likewise),
so each spike clears before the next and the peak is 5. Later, never earlier,
so the owner's 08:37–08:39 window stays clear. Verify stays at open − 1 min for
all three. The cost is retry room: fullday's window is 2 min 40 s, which still
fits two attempts at the default 60 s backoff (tested). The same slot staggers
FeedWatch's lost-session re-login ([feedwatch.md](feedwatch.md)).

## The verify window
One minute before the open, an injected health check runs — **an authenticated
REST call on the token that dies**, decided as `api.usage()`. A feed-side check
would have passed: prints were still arriving five seconds before the halt.

| Check | Then |
|---|---|
| answers | `VERIFIED`, token age recorded |
| raises | **one** recovery re-login → `RELOGGED`, then **one re-check** 20 s later → `VERIFIED` or `VERIFY_FAILED` |

It gets one shot at each, not a backoff ladder: a check still retrying when the
session opens has failed at its only job. The recovery attempt is worth taking
because we are still in the quiet, and the alternative is trading on a session
we have just proven is broken.

**The re-check exists because the recovery used to settle the window on its own
— and that hid the exact failure this window was built to catch.** MEASURED
2026-08-19 (§2.6): a soak whose contract had rolled had a live token and a dead
feed. The check caught it at 14:59, the recovery re-login succeeded, and the
window settled on *that* — reporting `RELOGGED`/INFO for a session that was
unusable. The run bled to a staleness halt sixteen minutes later and the FATAL
never fired. **A login proves a login; only the check proves the session.**

The 20 s grace is not padding. The check's tick half asserts a print has arrived
*since* the last re-login, and the recovery re-login resets that baseline — so
re-checking immediately would fail by construction, every time, turning every
recovery into a FATAL. 20 s is long enough for a subscription to deliver on a
tape that is printing, and leaves the verdict ~40 s ahead of the open. A re-check
still owed when the window closes is itself a `VERIFY_FAILED`: nothing may leave
the window unsettled.

**This replaced an age-based backstop** ("re-login anywhere if age > 20 h"),
which was designed and then rejected. A backstop fires on a clock reading,
mid-session, on suspicion. This fires on evidence, in the quiet, at the last
moment it is still free — and it is what stands behind an abandoned re-login
window.

## Why those two moments
`simtrade` prints occupy exactly two short pre-open windows a day, measured and
then reproduced on an independent day:

| Window | 2026-08-12 | 2026-08-14 | First genuine print |
|---|---|---|---|
| Day pre-open | 08:30:05 .. 08:44:55 | 08:30:10 .. 08:44:55 | 08:45:00 |
| Night pre-open | 14:50:10 .. 14:59:55 | 14:50:15 .. 14:59:55 | 15:00:00 |

Both anchors sit inside a trial-print-only window with five minutes of clearance
before real trading. `login()` blocks the consumer thread for seconds, and this
is the only time of day when that costs nothing.

| | |
|---|---|
| Intervals | 6 h 15 m and 17 h 45 m |
| Token age ceiling | **17 h 45 m** against a 24 h wall |
| Login cost | 2/day against a documented 1 000/day cap (§2.8b) |

## Design notes
- **Anchors ignore [SessionPolicy](sessions.md), deliberately.** A night-only
  deployment still re-logins at the *day* session's anchor, because one anchor a
  day means re-logging in every exactly 24 h — a dead heat with the wall, every
  day. The policy says what we *trade*; it does not get to say when a credential
  is refreshed. This is also what makes the day-session port free: the same two
  anchors, no code change.
- **The window is the retry budget.** There is no attempt counter. A re-login
  window runs only from its start to the verify window, and 60 s exponential
  backoff fits three attempts in four minutes (+0 s, +60 s, +180 s; the fourth
  would be +420 s). A structural bound, and it bounds the thing that matters —
  nothing chases a login onto a live bar. Six attempts a day is three orders of
  magnitude under the login cap §2.8b warns about.
- **The two windows are adjacent and never overlap.** That is what guarantees a
  retry is never still in flight when the health check runs, so the check reads
  a settled state rather than racing one.
- **Every window must end settled.** A window that closes mid-retry abandons the
  attempt and clears `_pending`, `_attempts` and the backoff deadline. Found by
  test: without it the next day's window inherited a spent budget and a deadline
  set yesterday.
- **No token-age floor.** An earlier draft skipped an anchor when the token was
  under two hours old, to avoid a "wasted" login. It stranded the first anchor
  for any process started between 13:00 and 15:00 and pushed the ceiling from
  17 h 45 m to **18 h 36 m** — caught by the week-long property test. A login is
  free; margin is not.
- **Any exception is a failed attempt.** A re-login can fail as `TokenError`, a
  timeout, or something the SDK has not shown us yet, and the response is the
  same for all three. Narrowing the catch would let an unfamiliar error reach the
  CLI's crash handler and end a run still holding a valid token.
- **Alerting is the caller's job.** This returns an outcome and records; the CLI
  maps that to INFO/WARN. Same seam as [FeedWatch](feedwatch.md), and it is what
  keeps every branch testable with no transport.

## Proven in the soak — 2026-08-18
`outputs/live_20260817_134515` crossed the wall and kept going: started
2026-08-17 13:45:15, past 24 h at 2026-08-18 13:45:15, still running at
2026-08-19 01:29 (35 h 44 m). The §2.24 soak died at 24 h 0 m 36 s.

| | |
|---|---|
| Windows fired | 14:55/14:59, 08:40/08:44, 14:55/14:59 — all six |
| Re-logins / verifies | 3 / 3, `attempts=1` each |
| Failed / abandoned / verify-failed | 0 / 0 / 0 |
| Peak token age | **17 h 45 m 00 s** — the designed ceiling, to the second |
| Alerts | the three INFO re-logins, nothing else at any severity |

**The evidence is the REST calls made after the wall, not the uptime.**
`api.usage()` answered at 2026-08-18 14:59:00, 25 h 13 m 45 s after the login
that issued the first token, and the feed check's `api.snapshots()` answered a
minute later. `usage()` is the call that raised `TokenError` in the soak, so a
run that crosses 24 h *and* still answers it is what separates "`force_refresh`
resets the clock" from "`force_refresh` looked like it worked" — the distinction
this module was built around.

## The remaining gap
**A wholly lost window still reaches the wall — but it now takes two independent
failures.** All three re-login attempts must fail *and* the health check a minute
before the open must fail too; only then is the next successful login one anchor
later, putting token age at exactly 24 h. Documented by
`test_a_lost_window_still_reaches_the_wall_when_verify_fails_too` rather than
left implicit.

**The 24 h lifetime rests on one data point.** The 6 h 15 m margin absorbs a real
lifetime as short as ~18 h; below that the anchors alone fail.

**Both injected bodies are decided and wired** (`cli.py:586-626`), from three
off-hours probe runs (2026-08-16) and then proven by the soak above. The SDK
**cannot be read**: `Shioaji` comes from a compiled `_core.pyd`, and `login` /
`logout` / `subscribe` carry no source and no docstrings, so all of this is
measurement.

### `relogin` = `api.login(..., force_refresh=True)`

**A plain `login()` on a live session is a NO-OP.** Measured by connection count:

| Form | Δ connections | |
|---|---|---|
| `login()` | **+0** | returns a cached session — **do not use** |
| `login(force_refresh=True)` | **+1** | opens a real connection — **this one** |
| `logout()` + `login()` | +1 | works, but adds a step for nothing |

This is the finding that mattered most. Had the closure used a plain `login()`,
the scheduler would have fired at 08:40 and 14:55, written `relogin` records and
printed healthy counters — while the token expired on schedule. **Inert while
looking like it worked.**

The connection spike is transient: it returns to 1 within ~30 s and stays there
(30 samples over 15 min). §2.8b caps connections at 5 per `person_id`, and two
re-logins a day six hours apart never approach it *per process*; four processes
refreshing together did, which is what the stagger above is for.

*No longer circumstantial:* a new connection with the old one torn down was only
strong evidence of a fresh session — "the token's 24 h clock reset" needed a soak
crossing 24 h, and the 2026-08-18 run above is it.

### `verify` = `api.usage()`

Both it and `list_positions` are REST calls on the token that dies; `usage()` is
cheaper, carries no position semantics, and is already the call
[startup.py](startup.md) duck-types for its headroom check. **Confirmed to raise
on a dead token** (`AuthError: Not authenticated`), so the window is live rather
than inert. Two things to get right when wiring it:

- **Do not pass `cli.py`'s `_usage(api)` helper.** It swallows every exception
  and returns `None`, which would make the check pass forever and disarm the
  window silently — the same failure shape [feedwatch.py](feedwatch.md) guards
  against with `SANE_AFTER`, where a malformed answer reading as "healthy" would
  have been worse than the crash it replaced. Call `api.usage()` directly.
- **`usage()` needs authentication**, so it cannot report on a session that is
  already gone — it raises rather than answering. That is exactly what makes it
  a valid check, and it is also why the probe cannot measure connections after a
  `logout()`.

### A re-login rebuilds the client — re-assert everything
**Measured 2026-08-17, day session:** across a confirmed re-login, ticks went
**172 → 0** and order callbacks **2 → 0**. `force_refresh=True` replaces the
session object, and every registration made against the old one is gone.

So the `relogin` closure is:

```
api.login(..., force_refresh=True)     # +1 connection, clears in ~30 s
api.subscribe(contract, QuoteType.Tick)
@api.on_tick_fop_v1()                  # re-register
api.set_order_callback(...)            # re-register  <- broker/shioaji.py:155
@api.on_session_down                   # re-register
@api.on_event                          # re-register
```

- **DROPPED was the good outcome.** The alternative, DUPLICATED, would have
  double-counted every fill — corrupting the position mirror and the PnL
  together, and looking like a venue bug rather than ours.
- **Do not reuse `cli.py`'s `_resubscribe`.** Its unsubscribe-first step is right
  for repairing a *live* subscription ([feedwatch](feedwatch.md) §2.22 — a second
  subscribe on top of a live one risks a doubled tick stream and inflated bar
  volume), but after a re-login there is nothing to unsubscribe from.
- **Zero ticks does not distinguish** a dropped subscription from a dropped tick
  callback. The probe cannot tell them apart and does not need to — the closure
  re-does both.
- **Callbacks are bound to the connection object.** A controlled run confirmed
  it: a `plain` login (+0 connections) left ticks and callbacks **untouched**,
  while `force_refresh` (+1 connection) killed both. So re-assertion is needed
  after session-replacing logins and only those — and there is **no cheap safe
  re-login**, since the plain form is harmless *and* useless.
- **This makes an SDK reconnect a live risk.** Any replacement of the connection
  drops handlers, and a reconnect after a dropped socket creates one by
  definition. Unless the SDK re-attaches internally — unmeasured, since the 24 h
  soak never dropped a socket — a network blip leaves the process alive,
  subscribed, and **deaf**. That is why the re-assertion belongs in
  [FeedWatch](feedwatch.md)'s repair path too, not just after our own re-login:
  its subscription-only `_resubscribe` cannot fix a dropped callback however
  many times it retries.
- **Seen live 2026-10-15: the SDK did not reconnect at all.** The session died
  silently at 09:52 (`SessionNotEstablished` on every call, no `session_down`)
  and stayed dead for 5 h; only the 14:55 `force_refresh` re-login revived it.
  Re-asserting callbacks is therefore not enough — the repair has to re-login.
  **Since 2026-10-18 FeedWatch does**: 3 consecutive raised snapshots in
  session call this same `_relogin` ([feedwatch.md](feedwatch.md)).

### `usage()` proves the token, not the feed — so the check does both
A silently failed re-subscribe would still pass a token-only verification at
open−1 min, and the first evidence would be [FeedWatch](feedwatch.md) ~60 s into
the session — it is gated on `policy.includes()`, and 14:59 is outside. The
session would open blind for about a minute before healing.

Decided by taking the second half the anchors already make available: `_verify`
asserts a tick has arrived *since* the re-login (`cli.py:623`), counting trial
prints deliberately, because the windows sit inside the measured simtrade
sessions precisely so there is something to confirm against. It measures the
transport, not the market — which is why it is skipped on non-trading days
(§2.25), where a wall-clock anchor fires onto a tape that was never going to
print.

## Related
[live/cli.py](cli.md) · [live/feedwatch.py](feedwatch.md) · [live/sessions.py](sessions.md) · [live/startup.py](startup.md) · [SHIOAJI_PLAN.md §2.24](../../SHIOAJI_PLAN.md)
