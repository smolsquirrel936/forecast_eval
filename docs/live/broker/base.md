# [live/broker/base.py](../../../live/broker/base.py)

**Role:** The `Broker` ABC — the seam between order *intent* and order *reality*.
**Pipeline:** Trader → **Broker** → (sim fill model | real venue).
**Depends on:** [events.py](../../events.md)   **Used by:** [live/broker/sim.py](sim.md), [live/broker/dryrun.py](dryrun.md), [live/broker/shadow.py](shadow.md), [live/broker/shioaji.py](shioaji.md), [live/broker/client.py](client.md), [live/loop.py](../loop.md)

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
| `has_venue` | attribute | does this implementation have an account to ask? (`False` by default) |
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

## `has_venue` — because `None` means two different things

`broker_position_detail()` returns `None` both when an offline broker has **no
venue to ask** and when [ShioajiBroker](shioaji.md)'s `list_positions` **raised**.
The two need opposite handling, and no inspection of the returned `None` can tell
them apart:

| | meaning | correct response |
|---|---|---|
| `has_venue = False` | there is no account behind this broker | **skip** — there is nothing to reconcile against |
| `has_venue = True`, `None` returned | the query failed; the answer is unknown | **refuse** — assuming flat would seed an unchecked history |

MEASURED 2026-09-02 on `live_night_20260902_082640`. The ledger reconciliation in
[live/cli.py](../cli.md) read both as "the venue is flat", so a `--broker dryrun`
run's ledger — three real fills netting +1 — was compared against a venue that was
never asked and refused on every start (`"replays to +1 lot(s) but the venue holds
+0"`). The run then wrote an empty `fills.csv` and `trades.csv`, and its report
drew a soak-wide price line with no fills on it and an empty PnL panel.

Which case applies is a property of the *implementation*, not of a call's
outcome, so it is a class attribute. [ShadowBroker](shadow.md) delegates it to
the broker it wraps, since it delegates every position query there too.

## The five implementations

| | what is on the other side |
|---|---|
| [SimBroker](sim.md) | the offline fill model — infers fills from prints |
| [DryRunBroker](dryrun.md) | the same model, with every intended order recorded |
| [ShadowBroker](shadow.md) | a real venue *and* the sim model, run beside it, for V4's fill-gap |
| [ShioajiBroker](shioaji.md) | the venue itself |
| [ClientBroker](client.md) | the **owning process**, which then talks to the venue (§18.2) |

`ClientBroker` is the one that is not a venue at all, and `has_venue = True` is
what makes that safe: a `None` from it means *the query failed*, so the
reconciliation **refuses** rather than skipping. An unreachable owner is exactly
that case — the account exists and may be holding, we simply cannot see it.

It also shows why `poll_fills` returns a **list**: MEASURED (§18.9) a 2-lot order
filling as two 1-lot deals nine seconds apart, so a fill does not end an order
and anything tracking one must accumulate partials.
