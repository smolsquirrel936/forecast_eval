# [live/owner.py](../../live/owner.py)

**Role:** the owning process's order book — N working orders across three deployments on one account, and the rule that sizes each one.
**Pipeline:** intent → `plan_legs` → owner places → `OrderBook.register` → venue deal → `on_deal` → `(slug, FillEvent)` → that deployment's `events_<slug>.jsonl`.
**Depends on:** [events.py](../events.md)   **Used by:** the owner process (S9b, in progress)

## Why it is not `ShioajiBroker`

[broker/shioaji.py](broker/shioaji.md) tracks exactly **one** live order
(`self._live`). That is right for a deployment and wrong for the owner: three
deployments can each have an order working, and §18.9's crossing rule turns a
single intent into a **pair** of legs, so even one deployment can hold two.

It is also the file the live soak runs on tonight, and its place path sets
`octype` from the deployment's own OPEN/CLOSE intent — which
[STRATEGY_SPLIT_PLAN §18.9](../../STRATEGY_SPLIT_PLAN.md) measured is the
owner's call, not the deployment's. So this is a new module and `ShioajiBroker`
is untouched.

**It is deliberately venue-free.** No `place_order`, no session, no `shioaji`
import. It is the accounting: which legs an intent becomes, which order a deal
belongs to, whose fill it is, and what the account therefore holds. Every rule
below is testable without a market — which matters, because they were measured
the expensive way.

## `plan_legs` — the rule §18.12 paid for

```python
plan_legs("SELL", 2, account_net=1)  ->  [Leg(Cover, 1), Leg(New, 1)]
plan_legs("SELL", 1, account_net=3)  ->  [Leg(Cover, 1)]
plan_legs("BUY",  1, account_net=3)  ->  [Leg(New, 1)]
```

The lots that oppose `account_net` become one `Cover` leg; the rest become one
`New` leg; **Cover first**, the order Phase B measured end to end with no
settling gap between the legs.

**Why not one order.** MEASURED (§18.12): an over-sized `Cover` crossed +1 to −1
once and clamped at flat twice, with no difference in how it was issued — and
**when it clamped the deal still reported the full order quantity**. That would
move `Trader.position` by two against a book that moved one. Sizing every Cover
to `min(quantity, |account_net|)` makes it unreachable rather than unlikely: a
leg that can always fully execute cannot partially fail, so it cannot
over-report. A property test sweeps every net from −4 to +4 and asserts no Cover
ever exceeds what is held.

**"Closing" is a property of the ACCOUNT, not of the intent.** A deployment
closing its own long while a peer is short is *opening* at the venue. That case
is exactly what §18.1 was written to find, and it is why `octype` cannot be
decided at the [client](broker/client.md) end.

## Routing: whose fill is it?

`Working` carries a **`slug`** — the field `ShioajiBroker._Live` has no need for
and the owner cannot work without. It routes each deal back to the one deployment
whose order it was, which makes per-deployment attribution exact rather than
apportioned (§18.3 item 2).

Matching is by `ordno` first, then by the id returned at placement — two keys
because a deal can arrive before the order event that carries `ordno`.

**There is no sole-order fallback.** `ShioajiBroker._match` ends with *"fall back
to the sole live order when neither key is known"*, which is safe when there is
only ever one. Here it would attribute one deployment's fill to another, so an
unattributable deal raises an anomaly instead. A test pins that a lone working
order is still not assumed.

## Fills accumulate; the first deal does not finish an order

MEASURED (§18.9): a 2-lot order filled as two 1-lot deals nine seconds apart. So
`filled` accumulates against a leg and the leg is finished when its own quantity
is met.

Three guards, each with a test:

| | |
|---|---|
| dedup on `(ordno, exchange_seq)` | duplicate deal reports are documented; a double-booked fill is a position that does not exist |
| `filled + qty > quantity` → anomaly | catches a venue over-report (§18.12) *before* the mirror moves further than the account did |
| `qty <= 0` → anomaly | a deal that moves nothing is a message we cannot explain |

Nothing is booked when a report is refused — not the fill, not the net.

## `account_net` comes from booked fills, not from the venue

It is what `plan_legs` sizes the next Cover against, so it has to be right **the
instant a fill is booked** — §18.10 measured `list_positions` catching up in
~0.1 s, which is fast but is still after a decision the owner may already be
making.

`seed_net()` sets it without booking a fill: a restart adopts from §16's ledger
sum, and adoption is not a trade. A fill would reach a deployment's `poll_fills`
and book a phantom trade into its `Portfolio`.

## Cancel keeps the leg

`mark_cancel_requested` flags and returns the legs; it does not drop them. §2.2 —
a cancelled order can still fill in the race window and that fill is real money.
`ShioajiBroker` moves such orders to `_retiring` for the same reason; here the
flag serves, because `_match` already searches every leg rather than a single
slot. A test books a fill arriving after the cancel request.

`forget_dead()` is explicit rather than automatic, so a just-completed leg can
still be inspected — the fill and the leg's disappearance are two different
moments, and conflating them turns a late duplicate into "unknown order" instead
of a dedup hit.

## Related
[live/broker/client.py](broker/client.md) · [live/broker/shioaji.py](broker/shioaji.md) · [live/intents.py](intents.md) · [live/ledger.py](ledger.md) · [STRATEGY_SPLIT_PLAN.md](../../STRATEGY_SPLIT_PLAN.md)
