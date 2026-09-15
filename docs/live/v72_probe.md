# [live/v72_probe.py](../../live/v72_probe.py)

**Role:** One-shot probe: does TXF futures `place_order` work under `simulation=True`?
**Run:** `uv run --no-sync python -m forecast_eval.live.v72_probe --dry-run` then without the flag.
**Depends on:** — (`shioaji` at runtime)   **Used by:** humans, before V3

## Responsibility
The gate on V3/V4. The docs list the order APIs without product qualification and
never mention 期貨/選擇權/TXF, and the only stated exclusion (興櫃, 零股) is
equity-specific — so the question could only be settled empirically.

**Answered 2026-08-10: yes.** `place_order` → `op_code='00'`,
`PendingSubmit` → `Submitted`; `cancel_order` → `op_code='00'`, `Cancelled`;
positions flat afterwards.

## This script places an order
Everything about it is built so that order cannot do anything:

- `simulation=True` is **asserted**, not assumed;
- **one** lot, never more;
- priced far below the market (default 500 points), so it is **passive by
  construction** — `probe_limit_price` *floors* rather than rounds, so the tick
  snap can only move it further from the touch;
- cancelled in a `finally`, with a second attempt on any failure path;
- position printed at the end.

The 500-point default is bounded on both sides: far enough not to fill, near
enough to stay inside TAIFEX's ±10 % daily band — an order outside that band is
rejected, which would masquerade as "futures unsupported in simulation". Both
bounds are tested.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `probe_limit_price(reference, offset_points, tick_size)` | function | the passive-by-construction limit (the only testable logic) |
| `main(argv)` | function | the probe; `--dry-run` places nothing |

## What it also harvested
Three things V2 had to guess, none documented:

- **`OrderState` literals** — `FuturesOrder` / `value='FORDER'`; the SDK also
  defines `FuturesDeal='FDEAL'`. The adapter's name-based dispatch was correct,
  and has since been hardened to upper-case and test DEAL first.
- **`Trade` object shape** — `order.id` / `seqno` / `ordno`.
- **The order-callback payload**, field by field — which revealed that ordering
  through the `TXFR1` alias reports the **resolved** code `TXFH6`, the bug fixed
  in [broker/shioaji.py](broker/shioaji.md).

## Key points / gotchas
- **Run it during a trading session.** Outside one a rejection is ambiguous
  between "unsupported" and "market closed"; the probe prints `op_msg` verbatim so
  the two can be told apart.
- **`api.simulation` is `None`** — the SDK does not expose the flag, so the
  abort-if-`False` guard can never actually fire. The constructor argument is the
  only guarantee. This is why [SafetyRails](state.md) requires an explicit
  `simulation` argument rather than asking the SDK.

## Related
[live/broker/shioaji.py](broker/shioaji.md) · [live/state.py](state.md) · [live/v1c_check.py](v1c_check.md) · [SHIOAJI_PLAN.md §7.2](../../SHIOAJI_PLAN.md)
