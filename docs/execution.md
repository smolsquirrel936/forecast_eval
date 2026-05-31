# [execution.py](../execution.py)

**Role:** Fill simulator for trade-print-only data (SPEC §4.5–4.6).
**Pipeline:** Trader → **Execution** → Portfolio.
**Depends on:** [events.py](events.md)   **Used by:** [trader.py](trader.md), [run.py](run.md)

## Responsibility
Decides whether a resting limit order fills against an incoming print, at what
price, and charges the per-side fee. Because the data is trade-prints-only (no
order book), fills are *inferred* from subsequent prints. It owns exactly one
pending order at a time (v1).

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `Execution(fee_rate)` | class | holds the fee rate and the single pending order |
| `submit(order, current_price)` | method | park an order, freezing its placement price |
| `cancel()` | method | drop any resting order |
| `has_pending()` / `pending_order()` | method | inspect the book |
| `check_fill(market)` | method | resolve the pending order against a new print |

## The two fill rules (set at placement time)
- **Marketable** (BUY limit ≥ current, or SELL limit ≤ current): fills at the
  **next** print, at *that print's* price (assumes it crossed the spread).
- **Passive** (otherwise): fills only when a later print **strictly** crosses the
  limit (strictly below for BUY, strictly above for SELL); fill price = the limit
  (no price improvement — the order is assumed behind the unseen queue).

Worked examples are mirrored verbatim from SPEC §4.5 in the golden tests.

## Key invariants / gotchas
- **Same-tick guard:** `market.timestamp == placed_at_timestamp` → no fill. An
  order can't fill against the tick it was placed on (SPEC §4.1 step 1).
- The marketable/passive classification is **frozen at placement** (it reads
  `current_at_placement`), never re-evaluated as the market moves.
- A fill clears `_pending` before returning, preventing a double fill next tick.
- Fee = `fill_price * fee_rate`, charged on *every* leg (open and close).

## Related
[trader.py](trader.md) · [events.py](events.md) · [portfolio.py](portfolio.md) · [tests/test_execution.py](tests.md) · [SPEC §4.5](../SPEC.md)
