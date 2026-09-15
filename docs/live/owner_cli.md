# [live/owner_cli.py](../../live/owner_cli.py)

**Role:** the entry point for the owning process — the one process that places orders.
**Pipeline:** login → seed from the ledgers → reconcile → serve (`poll` / `reconcile`) until stopped.
**Depends on:** [live/owner_process.py](owner_process.md), [live/ledger.py](ledger.md), [live/contracts.py](contracts.md), [live/credentials.py](credentials.md)   **Used by:** the operator

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

## The serve loop

| | |
|---|---|
| `--poll-seconds` (1.0) | drain intents, place, cancel, republish positions |
| `--reconcile-seconds` (30.0) | the venue call, on a slower cadence because it costs one |

**A failed reconciliation stops the owner** rather than logging and continuing —
the same rule [state.py](state.md)'s `on_reconcile` follows, and for the same
reason it never auto-corrects: "close the position" is undefined when the size is
in dispute. Here it is sharper still, because the disputed number is what sizes
every subsequent leg.

**A stale deployment is reported, not acted on.** What to do about an absent
deployment holding a lot is a risk decision (§7.4), not a mechanical one —
closing its position would be trading on someone else's behalf.

`SIGINT`/`SIGTERM` set a flag checked *between* passes, so a stop never lands
mid-placement — the one moment an order exists at the venue but not yet in the
book.

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
