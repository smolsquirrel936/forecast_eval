# [live/broker/client.py](../../../live/broker/client.py)

**Role:** the deployment's end of the wire to the owning process — submits intents, receives fills, and halts when the owner goes quiet.
**Pipeline:** `Trader` → `ClientBroker.submit` → `intents_<slug>.jsonl` → owner → venue; fills return via `events_<slug>.jsonl` → `poll_fills`.
**Depends on:** [live/intents.py](../intents.md), [live/broker/base.py](base.md), [live/state.py](../state.md)   **Used by:** [live/cli.py](../cli.md) (S9b, in progress)

## Why an owner at all

MEASURED ([§14](../../../STRATEGY_SPLIT_PLAN.md)): on one account, deals are
delivered **per session** but the position book is **shared**. So three
deployments see each other's positions and none of each other's fills — the rail
compares one against the other and both halt. One process owning the session
gives one book, one `_match`, and no foreign deals.

`ShioajiBroker` talks to a venue; this talks to the owner. It implements the same
six-method [Broker](base.md) interface, so `Trader` and `LiveLoop` are unchanged
and the swap happens at `cli.py`'s single construction site.

## What it does not do

It serialises intents and drains events. It does **not** decide `octype`, size a
crossing order, or know the account's net. Those belong to the owner, the only
thing that can see all three deployments — and §18.9 measured why it matters:

```
cover_qty = min(intent_qty, abs(account_net))   # the lots that close
new_qty   = intent_qty - cover_qty              # the lots that open
```

So a `submit` intent carries **`intent` (OPEN/CLOSE), never an `octype`**. What
the deployment *means* is OPEN or CLOSE; what that becomes at the venue depends
on a net this end cannot see. A test pins the absence of `octype`.

## Silence is not consent

The transport is files, so a dead owner looks exactly like a quiet one. Three
rules follow, each with a test:

- **Never having spoken ≠ flat.** `age_seconds()` returns `None` for a stream
  with no records ([intents.py](../intents.md)) precisely so the two cannot be
  confused. `broker_position()` returns `None` and the rails halt.
- **A stale number is never compared.** Past `owner_timeout` (default 90 s),
  `broker_position()` returns `None` rather than the last value it saw. Comparing
  a stale venue read against a fresh mirror would halt on *our* staleness and
  call it a desync.
- **`has_venue = True`.** [base.py](base.md) gives `None` two opposite meanings —
  an offline broker has nothing to ask (SKIP), a failed query means the answer is
  unknown (REFUSE). An unreachable owner is the second: the account exists and may
  be holding, we simply cannot see it.

90 s is generous on purpose. A false "owner is gone" halts a healthy deployment,
which is its own outage — and §18.10 measured a fill taking up to **12.8 s** to
arrive, so short timeouts here would be wrong.

## Order lifetime belongs to the owner

`cancel()` writes an intent and **leaves `_pending` set**. [base.py](base.md) is
explicit that a live cancel is asynchronous, so this means *cancel requested*;
`has_pending()` stays `True` until the owner says `done`. Clearing it early would
let §4.4's cancel-on-every-signal issue a second order while the first is still
working at the venue.

**A fill does not end an order either.** §18.9 measured a 2-lot order filling as
two 1-lot deals nine seconds apart, so fills accumulate and only `done` clears
the pending state.

## The four event kinds

| kind | effect |
|---|---|
| `fill` | append a `FillEvent` for `poll_fills` to drain |
| `done` | the working order is finished — the only thing that clears `_pending` |
| `position` | cache this deployment's own signed size (and entry, for `adopt_position`) |
| `rejected` | clear `_pending` and halt |

**An unknown kind is ignored; a malformed known kind halts.** The first is
version skew — the owner may add a message type before the deployments are
redeployed. The second is a bug, and booking a fill with a missing price would be
worse than stopping.

## Corruption is translated exactly once

`JsonlStream.read_new` **raises** on a complete-but-unparseable line, because
dropping a record would drop a fill. That is right for the wire and wrong for the
bar loop, which needs a halt rather than a traceback. `_drain` catches it at the
boundary and routes it through `_anomaly` — the same mechanism
[broker/shioaji.py](shioaji.md) uses, so there is one place to look when a live
run halts.

## Related
[live/intents.py](../intents.md) · [live/broker/base.py](base.md) · [live/broker/shioaji.py](shioaji.md) · [live/state.py](../state.md) · [STRATEGY_SPLIT_PLAN.md](../../../STRATEGY_SPLIT_PLAN.md)
