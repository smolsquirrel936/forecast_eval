# [tools/g6_live.py](../../tools/g6_live.py)

**Role:** the live half of G6 — two deployments holding opposed positions on one real account, watched for a halt that should not come.
**Pipeline:** login → reconcile (must be flat) → owner starts → two `ClientBroker`s open ±1 → watch → flatten.
**Depends on:** [live/owner_process.py](../live/owner_process.md), [live/broker/client.py](../live/broker/client.md), [live/ledger.py](../live/ledger.md)   **Used by:** the operator, once

```bash
uv run python -m forecast_eval.tools.g6_live --out outputs/g6 \
    --i-understand-this-places-orders --minutes 10
```

## What G6 is

[STRATEGY_SPLIT_PLAN §8](../../STRATEGY_SPLIT_PLAN.md), restated after §14:

> two deployments hold non-flat positions across a full session; neither rail
> halts; the venue's reported position equals the **sum of the deployments' own
> fill ledgers**; and each deployment's PnL is still attributed to it alone.

## What this does *not* test

**Three of the four clauses are already proven offline** by
`tests/test_g6_two_deployments.py`, which drives two real `LiveLoop`s, two real
`ClientBroker`s, one real `OwnerProcess` and the real `FillLedger` writer through
a venue that fills. The arithmetic, the attribution and the ledger sum are
settled there, deterministically and in a second.

What that cannot prove is the half needing a market: **that nothing halts against
real latency, real partial fills and a real `list_positions`.** So this script
does not re-check the arithmetic — it opens the §14 scenario for real and
watches.

## The scenario, deliberately minimal

Two deployments, one lot each, **opposite directions** — the exact pairing §14
measured as a guaranteed double halt when each placed its own orders. The account
nets flat while both deployments hold, which is the case that has no
account-level signal at all: `list_positions` shows nothing, and only the
per-deployment books know anything is open.

Every 30 seconds it reports each deployment's position, the account net and
`reconcile()`. It fails fast on either rail halting or a reconcile disagreement.

## Run it inside a session, with room to close

It needs **fills**, so off-hours it measures nothing and exits 4. Windows are
**DAY 08:45–13:45** and **NIGHT 15:00–05:00**, Taipei. Either works — the slugs
`day` and `night` are deployment *identities*, not schedules, so running both
during the night session is exactly right. NIGHT is the longer window.

**It also checks the run FITS before the session close**, which matters more than
the first check: this script **flattens** at the end, and a close needs a market
too. A run that reaches the session boundary still holding cannot close, leaving
the account with two opposed lots overnight — the one outcome it must not
produce. It warns when `--minutes` plus a margin exceeds the time remaining.

The night session's wrap past midnight is handled: at 23:00 there are six hours
left, not minus eighteen.

## It fails fast when nothing fills

Before the watch starts it waits up to 90 s for **both** positions to appear, and
exits 4 if they do not. Otherwise a run that was already decided in its first
minute would spend the whole `--minutes` observing nothing — burning the session
window. §18.10 measured a fill taking up to 12.8 s, so 90 s is generous rather
than tight.

## Three refusals before it opens anything

| | |
|---|---|
| no `--i-understand-this-places-orders` | exit 5 |
| `--out` already holds `fills_*.csv` | exit 2 — a stale ledger would be counted into the sum while matching no position the venue still holds, failing G6 for a reason unrelated to G6 |
| the account is not flat, or disagrees with the empty ledgers | exit 3 — every number reported is a delta from a known start |

## Exit codes

`0` passed · `1` **failed** (a halt, a reconcile disagreement, ledgers that
disagree with the owner's book, or a venue that disagrees with the ledger sum) ·
`2` the directory was not fresh · `3` the account was not flat · `4`
inconclusive — one or both never filled, **or no ledger was written, or the
venue would not answer** · `5` no acknowledgement.

**A pass names the numbers it checked, and refuses when it has none**
(STRATEGY_SPLIT_PLAN §22.7). The 2026-09-07 run printed `ledgers on disk {}` and
`ledger sum None` and announced `G6 PASSED ... every fill reached exactly one
deployment` — asserting the one clause it had just shown it could not check.
`ClientBroker` writes no ledger and this script runs no `LiveLoop`, so the live
half could never check its own central clause in *any* run; it now keeps one
`FillLedger` per deployment, and the verdict requires the files to exist, to
match the owner's book per deployment, and to sum to what the venue reports.
"Cannot ask the venue" is inconclusive, never agreement.

**Inconclusive is its own code, not a pass.** §14 records what a run that
measured nothing and reported a conclusion costs; a G6 where neither deployment
filled has observed exactly nothing about concurrent holding.

## It flattens through the owner, not through `flatten.py`

[flatten.py](../live/flatten.md) closes the account's whole net and cannot update
any deployment's ledger (§18.3 item 8). Closing through the owner keeps each
deployment's record correct, which matters because those records are the thing
G6 just finished checking. If it cannot get flat it says so loudly and names
`flatten.py` as the manual fallback.

## §15 and what a pass means

**§15 — one instance at a time — is suspended for the duration of this run and by
this run only.** That is the point of it: G6 passing is what lifts §15
permanently, for the shape it tested. A pass on two opposed one-lot positions is
evidence about two opposed one-lot positions; three deployments, or larger sizes,
are a bigger claim than this run makes.

## Related
[live/owner_cli.py](../live/owner_cli.md) · [live/owner_process.py](../live/owner_process.md) · [live/broker/client.py](../live/broker/client.md) · [live/multi_deploy_probe.py](../live/multi_deploy_probe.md) · [STRATEGY_SPLIT_PLAN.md](../../STRATEGY_SPLIT_PLAN.md)
