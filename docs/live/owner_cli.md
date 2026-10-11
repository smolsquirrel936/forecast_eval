# [live/owner_cli.py](../../live/owner_cli.py)

**Role:** the entry point for the owning process — the one process that places orders.
**Pipeline:** login → seed from the ledgers → reconcile → serve (`poll` / re-login / `reconcile`) until stopped.
**Depends on:** [live/owner_process.py](owner_process.md), [live/ledger.py](ledger.md), [live/contracts.py](contracts.md), [live/credentials.py](credentials.md), [live/relogin.py](relogin.md)   **Used by:** the operator

```bash
# 1. the owner FIRST
uv run --no-sync python -m forecast_eval.live.owner_cli --out outputs/live_soak \
    --i-understand-this-places-orders

# 2. then each deployment, beside it
uv run --no-sync python -m forecast_eval.live.cli --config day --broker client \
    --out outputs/live_soak/day --resume --i-understand-this-places-orders
```

## Start it before the deployments

A [ClientBroker](broker/client.md) treats a stream that has never spoken as
*"cannot see the book"* and halts — deliberately, so a missing owner is never
mistaken for a flat account. A deployment started first will refuse until the
owner publishes its opening `position` event.

## It seeds from the ledgers, then checks that against the venue

On a shared book `list_positions` gives the account **total** with no way to
split it, so a restart cannot ask the venue who holds what. It reads each
deployment's own `fills_<slug>.csv` ([ledger.py](ledger.md)) instead, and a slug
with no ledger seeds **flat** — a deployment that has never traded is normal, not
unknown.

**`seed_positions` seeds the entry price as well as the size**, using
`ledger.open_position`, the same reducer `--resume` replays with. The owner
publishes both, and `adopt_position` seeds the Trader and the Portfolio from
them. Until 2026-10-07 the seed was a bare count (`seed_nets`), so a held lot
was published at `avg_price` 0.0. Fullday's `--resume` that morning adopted its
+1 "@ 0.00"; closing it would have booked about +50 000 points.

**Then it reconciles the sum against the venue before serving anything, and
refuses on a disagreement.** The owner's whole authority rests on `account_net`
being right: it is what `plan_legs` sizes every `Cover` against
([§18.9](../../STRATEGY_SPLIT_PLAN.md)). Starting to place orders on a seed the
venue disputes turns one stale ledger into a stream of wrongly-sized legs.
Refusing costs a restart; not refusing costs positions. A venue that will not
answer is a warning, not a refusal — *cannot compare* is not *disagrees*.

## `subscribe_trade` is explicit, and checked (§18.14)

**No account fingerprint here.** [cli.py](cli.md) fingerprints the account behind
each deployment; the owner deliberately does not. It was built and removed the
same day at the user's direction — not important for this use case — and the
check is a warning with nothing to configure, so its absence costs nothing. `login()` defaults it to `True`,
so the owner worked by relying on a default. This is the **one** process that
must hear deals: under option (b) it is the only `_match` there is, so a silent
unsubscribe means every deployment's fills stop arriving while their orders keep
filling. §14b Q3 measured the call returns `True`, so a `False` here is a real
answer rather than an unknown — and it **refuses to start** (exit 7). It is
called *after* login rather than instead of it: re-subscribing an already
subscribed account is harmless, and a default that changes in a future SDK
release would otherwise fail silently.

Resolving which account to subscribe goes through `futopt_account` first and the
`login()` list second, matched on the **enum member**. §13 is the record of what
one wrong spelling costs — "0 futures accounts" reported for a credential trading
TXF nightly, because `str(AccountType.Future)` is `'F'`. Writing this function
reproduced the same class of mistake one level up (`sj.constant.AccountType`,
which does not exist in 1.7.2); its own test caught it.

## It re-logs in before every session open (§2.24)

A Shioaji token dies **24 h after login** and the SDK cannot refresh one — the
[relogin](relogin.md) page has the soak that died proving it. Until 2026-09-29
the owner logged in once and never again, while every deployment re-logged in on
schedule. Past 24 h the deployments kept trading through an owner whose
`list_positions` raised (logged as an anomaly, not a stop) and whose orders
failed.

It now runs the same `ReloginScheduler`, driven once per serve pass:

| | Owner | Deployments ([cli.py](cli.md)) |
|---|---|---|
| re-login window | open − 8 min → open − 6 min (08:37, 14:52) | open − 5 min → open − 1 min (08:40, 14:55) |
| verify | `api.usage()` — raises on a dead token | `api.usage()` + a tick since the re-login |

**Earlier on purpose.** The three deployments re-login in the same second, and a
`force_refresh=True` login opens a new connection before the old one drops, ~30 s
later (relogin_probe Q9), against a cap of **5 per `person_id`** (§2.8b) with four
already open. Going three minutes first means the owner's spike has cleared
before theirs begin — it adds nothing to that collision — and the process every
deployment depends on is refreshed before any of them.

**What the re-login re-takes.** `logout()`, a plain `login()`, then
`subscribe_trade(api.futopt_account)` with the new session's account, and
`OwnerProcess.attach()`. Until 2026-10-08 it was `login(force_refresh=True)` +
`subscribe_trade(account)` + `attach()`, and on 2026-10-07 that left the owner
deaf from its 14:52 re-login on: the 22:16 night fill was never booked and the
owner froze at 22:18. `relogin_probe --mode reassert` then measured that **no**
re-assertion restores the order callback after a `force_refresh` login (0
callbacks for each of `set_order_callback`, `subscribe_trade(old account)` and
`subscribe_trade(new account)`), while logout + plain login + both was heard (2
of 2). The logout is best-effort, so a token that is already dead still gets
replaced. A plain `login()` *without* the logout is still the measured no-op
(Q1/Q8). A refused `subscribe_trade` raises,
so the scheduler counts a failed attempt and retries inside the window rather
than settling on a session that cannot hear fills. There is no tick half to the
verify: the owner holds no quote feed.

## The serve loop

| | |
|---|---|
| `--poll-seconds` (1.0) | drain intents, place, cancel, republish positions |
| `--reconcile-seconds` (30.0) | the venue call, on a slower cadence because it costs one |

**A failed reconciliation freezes the owner** instead of logging and
continuing. That is the same rule [state.py](state.md)'s `on_reconcile` follows,
and for the same reason it never auto-corrects: "close the position" is
undefined when the size is in dispute. Here it is sharper still, because the
disputed number is what sizes every subsequent leg. Frozen, it places nothing
and rejects every submit, which halts the deployment that asked. It keeps
booking deals and publishing positions until Ctrl-C, then exits **6**. It prints
the banner once, and once more if the venue and the book come to agree; that
does not unfreeze it.

**Until 2026-10-07 it returned instead**, and the logout cut the deal stream.
On 2026-10-06 that stranded fullday's fill: the reconcile ran 2.5 s after the
fill, before its deal arrived. A difference that orders in flight explain is
now *pending* for up to 120 s, printed as `reconcile pending: …`
([owner_process.md](owner_process.md), "A fill in flight is not a desync").

**The start-up check still refuses** (exit 6): a fresh owner has nothing in
flight. It also reports `intents already on disk, not replayed: {…}` when the
streams hold history, which it no longer places.

**The owner keeps a trail**: `owner_session.jsonl` in `--out`, appended across
runs, with a `meta` line at each start and `owner_stop` at each exit. Before
that file existed, its anomalies went only to the console.

**A stale deployment is reported, not acted on.** What to do about an absent
deployment holding a lot is a risk decision (§7.4), not a mechanical one —
closing its position would be trading on someone else's behalf.

`SIGINT`/`SIGTERM` set a flag checked *between* passes, so a stop never lands
mid-placement — the one moment an order exists at the venue but not yet in the
book.

## Alerts, and Telegram (since 2026-10-12)

The owner builds an [Alerter](alerts.md) and attaches [Telegram](telegram.md) as
`[owner]`, on the same `telegram_level.json` as the deployments, so one
`/settings` press steers all four. Every existing console line is unchanged; the
alerts are additions beside them.

| Level | Kind | Raised when |
|---|---|---|
| FATAL | `owner_frozen` | a running reconcile disagreed and the owner froze |
| FATAL | `startup_refused` | exit 7 (`gate=subscribe_trade`) or exit 6 (`gate=reconcile`) |
| FATAL | `no_credentials`, `crashed`, `session_unverified` | as in [cli.py](cli.md); `crashed` re-raises unchanged |
| WARN | `deployment_silent`, `traffic_low` | **on a change only**: the console still repeats them every 30 s |
| WARN | `venue_unanswered`, `relogin_failed`, `relogin_abandoned` | start-up reconcile got no answer; a re-login went wrong |
| INFO | `relogin`, `owner_agrees_again` | a fresh token; the venue matches again while frozen |

Before this, a freeze reached only the owner's console, and the phone heard
about it about two minutes later as three `owner silent` halts.

**No `fatal.jsonl`.** The status page attributes that file's entries to
deployment soaks by timing ([status_report.md](status_report.md)), so an owner
entry would be pinned on whichever soak it fell inside. The owner's alerts are
recorded in `owner_session.jsonl` as `alert` events instead. The ack and
`--out` refusals stay console-only: they are typos at a terminal someone is
looking at. `--no-telegram` turns Telegram off.

## Two things to know before running it

**Four sessions on one credential — MEASURED, they coexist.** This process logs
in, and so does each deployment: they still need the market feed to build bars,
and only *orders* are delegated. §18.13's `--mode sessions` opened four,
`connections` went 1→4, and the first session still answered with all four up.

**What four subscriptions COST is still open.** The allowance is 500 MiB/day and
`RestartGuard.check_usage` refuses to start below 50 MB — over quota Shioaji
returns EMPTY history rather than erroring, which would silently produce an empty
model context. That is a **start-up** gate, so this process also samples the
headroom on its reconcile cadence and warns below `--min-remaining-mb`. It warns
rather than stops: the owner stopping halts every deployment at once, and a thin
data allowance is not a reason to abandon open positions. `live/cli.py` now
reports what one session consumed, which is the number that settles it.

**The console prints the account's `person_id`** — a Taiwanese national ID — from
Shioaji's compiled layer, exactly as [cli.py](cli.md) does. Unlike
[multi_deploy_probe.py](multi_deploy_probe.md), which re-runs itself as a
filtered child, this does not redact. Read the output before pasting it anywhere.

`simulation=True` is hardcoded, not a flag — §7.2 measured that `api.simulation`
reads back as `None`, so the constructor argument is the only guarantee there is,
and an entry point with no reachable path to a production account cannot be
argued into one.

## Related
[live/owner_process.py](owner_process.md) · [live/owner.py](owner.md) · [live/broker/client.py](broker/client.md) · [live/cli.py](cli.md) · [STRATEGY_SPLIT_PLAN.md](../../STRATEGY_SPLIT_PLAN.md)
