# [live/owner_process.py](../../live/owner_process.py)

**Role:** the venue-facing half of the owner — one session, one callback, three deployments' orders.
**Pipeline:** `intents_<slug>.jsonl` → `poll()` → `plan_legs` → `place_order`; venue deal → `_on_event` → [OrderBook](owner.md) → `events_<slug>.jsonl`.
**Depends on:** [live/owner.py](owner.md), [live/intents.py](intents.md), [live/flatten.py](flatten.md), [live/contracts.py](contracts.md)   **Used by:** the owner entry point (S9b, in progress)

## The split with `owner.py`

[owner.py](owner.md) is the accounting and holds no session: it decides which
legs an intent becomes, which order a deal belongs to, and what the account
holds. This module is the half that touches the venue. The seam is deliberate —
it is what lets every rule in the book be tested without a market, and it is why
`Leg.octype` is a plain string that only becomes `sj.FuturesOCType` **here**, at
the placement boundary.

`shioaji` is imported lazily and can be substituted, so this module and its tests
run with the package absent — the same arrangement
[broker/shioaji.py](broker/shioaji.md) uses.

## One callback, no foreign deals

MEASURED ([§14](../../STRATEGY_SPLIT_PLAN.md)): deals are delivered per session
and the position book is shared, so three deployments placing their own orders
see each other's positions and none of each other's fills. With one owner there
is one session, and every deal it receives belongs to an order it placed — §7.2
dissolves rather than needing a fix.

**DEAL is tested before ORDER, and that ordering is load-bearing.**
`str(state)` is `"OrderState.FuturesDeal"`, which contains `ORDER` as well — so
testing ORDER first routes every deal into the order handler and silently drops
every fill. `ShioajiBroker` records the same trap; a test here uses a state with
no `name`, so the dispatch falls back to `str(state)` and the ambiguity is real.

## Two reconciliations, and they are not the same check

| where | compares | a disagreement means |
|---|---|---|
| here, `reconcile()` | `OrderBook.account_net` vs `list_positions` | a real desync with the venue |
| each deployment | its `Trader.position` vs `net_for(slug)`, sent as a `position` event | an event was lost on the wire |

§16 moved the first comparison onto the sum of the fill ledgers because no single
deployment could make it. With an owner it is made in the one process that can,
**in memory** — which is what retires §16's freshness race.

A venue that will not answer degrades to `None` — *cannot compare* — never to a
confident zero, the same rule `broker_position()` follows. On a mismatch the
anomaly names the per-deployment split, because "mirror 2 != venue 3" without
knowing who holds what is a message you cannot act on.

**Each deployment is told its OWN size, never the account's.** That is the
category error §14 measured and §16 removed, and a test pins it: day +1 and
night −1 are reported as +1 and −1 while the account is flat.

## `done` arrives once per intent, not once per leg

A deployment submits **one** order and must see **one** `done` — even when
§18.9's split turned it into two legs. [ClientBroker](broker/client.md) clears
`_pending` on `done`, so a `done` after the first leg would let §4.4's
cancel-on-every-signal place a replacement while the second leg is still working.
`_maybe_done` fires only when the slug has no working legs left.

## Cancel touches one deployment's legs

A shared session is not a shared order book. Cancelling a peer's resting order
because our signal changed is the exact interference the split exists to prevent,
so `_cancel` only flags and cancels the named slug's legs — and they stay tracked,
because §2.2 says a cancelled order can still fill in the race window and that
fill is real money. A test books a fill arriving after the cancel request.

## Isolation between deployments

One deployment's problem must not become another's. Three places enforce it, each
with a test:

- a **corrupt intent stream** is an anomaly for that slug and the others are
  still drained in the same `poll()`;
- a **place failure** emits `rejected` to the deployment that asked, not to all
  three;
- a **venue rejection** (`op_code != "00"`) is routed to the owning leg's slug.

## Two ordering bugs worth knowing about

**The observers are assigned before the book is constructed.** The default
`OrderBook` is handed `self._anomaly` as its callback, and that method reads
`self.anomalies` and `self.recorder`. Building the book first leaves a live
callback pointing at attributes that do not exist yet — which only fails on the
first anomaly, i.e. exactly when it is least welcome.

**`OrderBook.match()` is the public, lock-taking form.** `_match` documents
*"caller holds the lock"* because `on_deal` already does; the order-event handler
here has no lock of its own, and reaching for the private form would read the
working list while a deal mutates it.

## Threads

Fills are written from the venue's callback thread while positions and heartbeats
are written from the owner's own loop, so every outbound write takes one shared
lock. Per-file locks would be finer, but the writes are short and the cost of
getting one wrong is an interleaved line — which
[JsonlStream](intents.md)'s reader would correctly refuse to parse, halting a
healthy deployment.

## `stale_deployments()` reports and stops

A deployment that died holding a position is not one whose intents should be
waited for, and the owner is the only thing that can notice. **Nothing is closed
on this signal** — deciding what to do about an absent deployment holding a lot is
a risk call (§7.4), not a mechanical one. A deployment that has *never* spoken is
not stale: absent at start-up is not the same as having gone away.

## Related
[live/owner.py](owner.md) · [live/broker/client.py](broker/client.md) · [live/intents.py](intents.md) · [live/broker/shioaji.py](broker/shioaji.md) · [STRATEGY_SPLIT_PLAN.md](../../STRATEGY_SPLIT_PLAN.md)
