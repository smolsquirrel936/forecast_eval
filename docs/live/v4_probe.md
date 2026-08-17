# [live/v4_probe.py](../../live/v4_probe.py)

**Role:** Drive one order through the real V4 stack on demand, instead of waiting a night for a signal.
**Pipeline:** **v4_probe** → [ShadowBroker](broker/shadow.md) → [ShioajiBroker](broker/shioaji.md) → the venue.
**Depends on:** [live/broker/shadow.py](broker/shadow.md), [live/broker/shioaji.py](broker/shioaji.md), [live/contracts.py](contracts.md), [live/config.py](config.md)   **Used by:** run by hand

```powershell
uv run python -m forecast_eval.live.v4_probe                # passive, cannot fill
uv run python -m forecast_eval.live.v4_probe --allow-fill   # round trip
```

## Responsibility
The strategy signals roughly **once a night** — 28 stride boundaries × 3.74 %
non-HOLD — so waiting for it to exercise the order path costs an evening per
attempt, and the first V4 session duly placed zero orders. Everything below
`LiveLoop` was therefore still untested against the venue: `submit`, the order
and deal callbacks, fill dedup, the urgent flatten, and the whole gap
measurement. This places the order deliberately.

> **Not [v72_probe](v72_probe.md).** That one calls `api.place_order` directly
> and answers "does the venue accept a futures order in simulation?". This one
> goes through `ShadowBroker → ShioajiBroker`, so what it tests is *our* code.

## Two modes
| Mode | What it reaches | Exposure |
|---|---|---|
| default (passive) | submit → order callback → cancel → the "neither filled" gap path | **none** — the limit cannot fill |
| `--allow-fill` | additionally: deal callback, dedup, `FillEvent`, slippage, the urgent close | one lot, for seconds |

`--allow-fill` is the only way to reach the components most likely to be wrong,
because none of them runs without a real fill. It closes through the **urgent**
path — the one place a fill is actually wanted, which makes it the natural test
of the flatten order type.

## Key invariants / gotchas
- **The passive limit floors, never rounds.** The no-fill guarantee is that the
  price is strictly below the market; flooring means the tick snap can only move
  it further away, never back towards a fill. Same argument and the same
  500-point default as [v72_probe](v72_probe.md).
- **500 points is bounded on both sides.** Far enough not to fill in the seconds
  before the cancel, near enough to stay inside TAIFEX's ±10 % daily band —
  outside which the order is *rejected*, and the rejection would masquerade as
  "the venue refused our order", the exact thing being tested.
- **A pre-existing position refuses the run.** The probe cannot tell its own
  order from one that was already there, so it declines rather than measure
  something it cannot attribute.
- **"Submitted a close" is not "flat".** `--allow-fill` verifies the position
  afterwards and exits **4** if anything is still held, with the manual-close
  instruction — the same failure `LiveRuntime.shutdown` reports, and it must
  never read as success.
- **Fills are polled, not awaited.** They arrive on Shioaji's callback thread and
  the docs warn a deal event can precede its order event, so the probe polls for
  a window exactly as the live loop does per bar.
- **A passive order that fills is an error, not a surprise.** It fails the run,
  because it would mean the no-fill argument above is wrong.

## Related
[live/broker/shadow.py](broker/shadow.md) · [live/broker/shioaji.py](broker/shioaji.md) · [live/v72_probe.py](v72_probe.md) · [live/cli.py](cli.md) · [SHIOAJI_PLAN.md V4](../../SHIOAJI_PLAN.md)
