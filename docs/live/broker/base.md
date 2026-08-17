# [live/broker/base.py](../../../live/broker/base.py)

**Role:** The `Broker` ABC — the seam between order *intent* and order *reality*.
**Pipeline:** Trader → **Broker** → (sim fill model | real venue).
**Depends on:** [events.py](../../events.md)   **Used by:** [live/broker/sim.py](sim.md), [live/broker/shioaji.py](shioaji.md), [live/loop.py](../loop.md)

## Responsibility
Defines the smallest interface that can stand in for both the offline fill
simulator and a live venue. Deliberately narrow: the first four methods are
*exactly* the surface [trader.py](../../trader.md) already calls on
[execution.py](../../execution.md), so `Trader` drives a real broker with **no
change and no shim** — it is duck-typed against this ABC.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `submit(order, *, current_price)` | abstract | send an order, recording the price at submission |
| `cancel()` | abstract | withdraw the resting order (a *request*, live) |
| `has_pending()` | abstract | is an order working? |
| `pending_order()` | abstract | the working `OrderEvent`, or `None` |
| `poll_fills(market)` | abstract | drain fills known since the last call |
| `broker_position()` | method | the venue's signed position; `None` = can't answer |
| `broker_position_detail()` | method | the same position **with its entry price** (`HeldPosition`); `None` = can't answer |
| `HeldPosition(signed_qty, avg_price)` | dataclass | what the venue holds, size and size-weighted entry |

## Why the last two exist
The first four mirror `Execution`. The additions are what a live venue forces:

- **`poll_fills`** — fills are *reported* asynchronously, not derived inline, so
  the loop drains a list rather than taking a single return value. Returning a
  list (not `Optional[FillEvent]`) is also what lets a venue report the **partial
  fills** the print-only sim cannot produce. The `market` argument exists only
  because [SimBroker](sim.md) genuinely needs it; a live broker ignores it.
- **`broker_position`** — the venue, not our mirror, is the source of truth
  (SHIOAJI_PLAN §2.9). Defaulting to `None` means offline implementations opt out
  and the reconciler skips them, rather than every Broker faking a number.
- **`broker_position_detail`** — the same question plus "at what price", asked
  **once, at start-up**, so a restart can adopt a position it did not open
  (§2.20). Separate from `broker_position` so the per-bar reconciliation path
  gains no extra field or query; also defaults to `None`, which is how the
  offline brokers — always flat at start — opt out by inheritance.

## Key points / gotchas
- **`cancel()` is "cancel requested", not "cancelled."** Against a real venue it
  is asynchronous; implementations must not pretend otherwise. See
  [shioaji.py](shioaji.md) for how the race is handled.
- `current_price` on `submit` is what the offline model classifies
  marketable-vs-passive from (SPEC §4.5). A live broker only logs it — for the
  V4 fill-gap measurement.

## Related
[live/broker/sim.py](sim.md) · [live/broker/shioaji.py](shioaji.md) · [trader.py](../../trader.md) · [execution.py](../../execution.md) · [live/loop.py](../loop.md)
