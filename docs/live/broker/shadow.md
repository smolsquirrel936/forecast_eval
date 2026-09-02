# [live/broker/shadow.py](../../../live/broker/shadow.py)

**Role:** Run the offline fill model beside the real one and diff them — V4's deliverable.
**Pipeline:** LiveLoop → **ShadowBroker** → [ShioajiBroker](shioaji.md) (authoritative) + a private [Execution](../../execution.md) (observed).
**Depends on:** [live/broker/base.py](base.md), [execution.py](../../execution.md)   **Used by:** [live/cli.py](../cli.md) when `--broker shioaji`

## Responsibility
The backtest's fill model is optimistic by construction. At `aggression_ticks: 0`
an entry limit sits *exactly* at the last print, so `Execution` classifies every
order marketable and fills it at the next print, guaranteed (§2.1). In a real
book that limit is joining the queue and fills only when someone crosses to
you — the adverse-selection case.

Every number in `results.tsv` therefore rests on an assumption nothing has
measured. This class measures it: both fill models see the *same* orders, and
the difference between what each reports is the gap.

> **The shadow can never influence trading.** Only the live broker's fills leave
> `poll_fills`, so only they reach `Trader` and `Portfolio`. Same rule the
> [recorder](../recorder.md) follows — an observer that can change the thing it
> observes is not measuring it.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `ShadowBroker(live, *, fee_rate=0.0, recorder=None)` | class | wraps a real broker; builds its own `Execution` |
| `gap_summary()` | method | fill rates, slippage, divergence counts |
| `format_summary()` | method | the console block printed at shutdown |
| `close()` | method | score the final episode; call once at shutdown |
| `has_venue` | property | whatever the wrapped live broker is |
| `gaps` | attribute | every scored `OrderGap` |
| `OrderGap` | dataclass | one order's life as each side saw it |

## What is measured — and what deliberately is not
**Reported:** fill rate (live and shadow, over identical orders); **slippage** on
orders *both* filled, signed so positive always means live did worse; and
**divergences** — one filled and the other did not, split by direction.

**Not reported: a full sim-versus-live PnL.** The moment one side fills and the
other does not, their positions differ, and every later order — generated from
the *live* position by `Trader` — is meaningless for the shadow, which would be
"closing" something it never opened. A PnL number from that is a fabricated
counterfactual. The slippage total is given instead: the part of the gap
attributable to execution *price*, which is real, plus the divergence counts,
which are the part that cannot be priced without assuming a strategy the shadow
never ran.

## Key invariants / gotchas
- **An episode runs from submit to cancel/replacement, and is scored on close —
  not on fill.** That is what makes "neither side filled" and "only one side
  filled" representable at all; a log written only when something fills cannot
  compute a fill *rate*.
- **Slippage is signed by side.** Paying 2 points more on a BUY and receiving 2
  points less on a SELL are the same outcome; an unsigned difference would let
  them cancel to zero.
- **Slippage statistics cover matched episodes only.** Averaging over unmatched
  ones would treat "did not fill" as "filled at no cost" — the opposite of the
  truth, since a missed entry is exactly the adverse-selection case being hunted.
- **`has_pending` / `pending_order` / `broker_position` / `broker_position_detail`
  all answer from the venue.** Those gate §4.4, the flatten retry and the §2.20
  resume; answering from the shadow would let a simulated fill convince the loop
  that a real working order had gone away — or report flat at start-up and adopt
  nothing while the account was holding. **`has_venue` delegates for the same
  reason**: it describes the broker those queries actually reach, and answering
  `False` here would tell the ledger reconciliation there is nothing to check
  against while a real account sits one call away ([base](base.md)).
- **The shadow is stepped on every bar**, not only when the venue reports. A
  passive order fills on some *later* print, and skipping bars would understate
  its fill rate.
- **The `Execution` is constructed here, not injected.** A shared one would
  already be carrying the loop's own orders, so the comparison would be against a
  fill model that had seen a different order stream.
- **Urgency is forwarded to the venue only.** [The urgent scope](shioaji.md)
  changes the price *sent*, which is the thing being measured; the shadow keeps
  pricing the Trader's original limit, so an urgent close shows up as the
  slippage it actually cost.

## Related
[live/broker/shioaji.py](shioaji.md) · [live/broker/sim.py](sim.md) · [execution.py](../../execution.md) · [live/cli.py](../cli.md) · [SHIOAJI_PLAN.md V4](../../../SHIOAJI_PLAN.md)
