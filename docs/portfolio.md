# [portfolio.py](../portfolio.py)

**Role:** Position, average entry, and realized/unrealized PnL with session tagging.
**Pipeline:** Execution → `FillEvent` → **Portfolio**.
**Depends on:** [events.py](events.md)   **Used by:** [run.py](run.md), [metrics.py](metrics.md)

## Responsibility
The accounting ledger. Applies each fill to the signed position, computes realized
PnL on closes, accrues fees, and buckets both PnL and fees by session (for
forced-close mode). It owns the money math — nothing about order placement.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `Portfolio(contract_multiplier=1.0)` | dataclass | full accounting state |
| `apply_fill(fill, session)` | method | fold a fill in; realize PnL on a close |
| `unrealized_pnl(mark_price)` | method | mark-to-market the open position |
| `net_pnl()` | method | realized PnL − total fees |
| `summary()` | method | headline numbers in both points and NT$ |

## Key points
- **Units:** PnL and fees are in price-**points**; multiply by `contract_multiplier`
  (e.g. 200 for TXF) for NT$. `summary()` reports both.
- **Signed position** (+long / −short) lets one code path cover long and short.
- `apply_fill` branches on open-from-flat / same-direction add (pyramiding) /
  opposite-direction close. v1 caps at one contract, so the pyramiding/flip
  branches are defensive but kept correct.
- **Session tagging:** `realized_pnl_by_session` / `fees_by_session` hold DAY/NIGHT
  buckets; the run loop passes the session a close's PnL should be booked to
  (which, for a forced close, is the *prior* session — see [run.py](run.md)).

## Related
[events.py](events.md) · [run.py](run.md) · [metrics.py](metrics.md) · [SPEC §4.6–4.7](../SPEC.md)
