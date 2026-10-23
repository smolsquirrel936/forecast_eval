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
| here, `reconcile()` | `OrderBook.net_in(code)` vs `list_positions`, per month the owner knows | a real desync with the venue |
| each deployment | its `Trader.position` vs `net_for(slug)`, sent as a `position` event | an event was lost on the wire |

§16 moved the first comparison onto the sum of the fill ledgers because no single
deployment could make it. With an owner it is made in the one process that can,
**in memory** — which is what retires §16's freshness race.

A venue that will not answer degrades to `None` — *cannot compare* — never to a
confident zero, the same rule `broker_position()` follows. On a mismatch the
anomaly names the per-deployment split, because "mirror 2 != venue 3" without
knowing who holds what is a message you cannot act on.

### A fill in flight is not a desync — `DEFAULT_RECONCILE_GRACE` (120 s)

**MEASURED 2026-10-06** (`simtrade_0929`): fullday's BUY `14AD46` filled at
09:14:04.5. The reconcile at about 09:14:07 read the venue at +1 against a book
still at 0, and the owner stopped. Its logout cut the deal stream, so the fill
never reached `fills_day_night.csv`, and the next start-up refused with
"account mirror 0 != venue 1". `list_positions` catches up in about 0.1 s
(§18.10), but deal callbacks have taken 12 s and 42 s to book (§23.7). Any
check inside that window sees the venue ahead of the book.

`reconcile()` therefore returns True, with `reconcile_note` set, for a
difference that orders in flight can explain:

| venue vs book | explained by |
|---|---|
| venue **ahead** | the unbooked remainder of working legs on that side (the 10-06 case) |
| venue **behind** | deals booked within the grace on the other side (`list_positions` still catching up) |

The wait is bounded at `reconcile_grace`, inclusive. After that it fails,
because a callback that never comes *is* a desync (§23.6). A difference
neither of those can explain fails at once, as before. A fresh owner has nothing
in flight, so the **start-up check is unchanged**. `clock` is injectable for
the tests.

### `freeze()` — stop placing, keep listening

On a failed reconcile the CLI calls `freeze(reason)`. From then on every submit
is answered with `rejected`, which halts the deployment that asked. Cancels
still reach the venue, because they can only reduce what is working. Deals are
still booked and forwarded, and positions are still published. That is the
difference from the old exit: the fill being disputed still lands. The first
reason is latched, and nothing unfreezes in-process; as with
[state.py](state.md)'s rails, a dispute is settled by a person and a restart.

## Each order goes to the month its intent names (2026-10-20)

The owner resolved `--contract` once and placed every order on it. At the
October → November roll the deployments switched to `TXFK6`, but the owner kept
placing on `TXFJ6`. Night's and fullday's re-opens were therefore October trades
(night's BUY closed fullday's SELL at the venue), and the summed reconcile read
a flat account as agreeing ([simtrade_operations §11](../simtrade_operations.md)).

- **Placement.** `_submit` reads the intent's `contract`
  ([ClientBroker](broker/client.md)) and places on that month's contract.
  `resolve` (from `owner_cli`, `resolve_txf(api, code)`) is called once per new
  month and cached. An intent with no `contract` uses the start-up month. A
  month that cannot be resolved is answered `rejected` and not placed, because
  trading the wrong month is the defect itself.
- **Sizing.** `plan_legs` uses `net_in(code)`, so a November order never
  `Cover`s an October lot.
- **Reconcile.** Each known month is compared on its own, and the in-flight
  grace only counts legs and recent deals in that month. Messages name the
  month (`... in TXFJ6`) only once the owner knows more than one, so
  single-month alerts read as before.
- **Seeding.** The ledgers carry no month, so `seed_from` books the whole seed
  to the start-up month. `--contract` must still name the month the deployments
  hold. A wrong one is refused by the start-up reconcile, not traded on.

## A restart does not replay the intent streams

A fresh `JsonlStream` reads from byte 0, so a restarted owner used to place
**every intent a deployment had ever sent**. After 10-06 it would have re-sent
fullday's 09:13 BUY on its first pass. The constructor now consumes what is
already on disk without acting on it and keeps the count in `skipped_intents`.
An intent the owner did not act on while it ran is stale: its deployment
halted on the silence and re-decides on `--resume`. A corrupt history line is
reported as the same anomaly `poll` raises. The deployment side does the same
([broker/client.py](broker/client.md)).

## The trail

With a `recorder`, the owner writes `owner_intent`, `owner_placed` (slug, side,
price, octype, `order_id`, `ordno`), `owner_order_event`, `owner_deal` (from
the book), `owner_reconcile_pending` / `_settled`, `owner_frozen`,
`owner_intents_skipped` and every anomaly. `owner_placed` is the record that
was missing on 10-06: nothing on disk said whose order `14AD46` was.

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

**A live deployment heartbeats every 30 s**, inside and outside its sessions
([broker/client.md](broker/client.md), `maybe_heartbeat`). 180 s of silence is
therefore six missed beats: the process has stopped. Until 2026-10-07 nothing
called `heartbeat()`, so a deployment was timed from its last *order*. From 10-06
on, fullday was reported as silent every 30 s while running normally.

## Related
[live/owner.py](owner.md) · [live/broker/client.py](broker/client.md) · [live/intents.py](intents.md) · [live/broker/shioaji.py](broker/shioaji.md) · [STRATEGY_SPLIT_PLAN.md](../../STRATEGY_SPLIT_PLAN.md)
