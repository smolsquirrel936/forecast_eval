# [strategy/day/pct_stop.py](../../../strategy/day/pct_stop.py)

**Role:** EntryPercentStop — exit when the print moves a fixed *fraction of the entry price* against the position.
**Pipeline:** a concrete [ExitRule](../../exits/base.md) built by [strategy/day/wiring.py](wiring.md) when `stop_pct` is set.
**Depends on:** [events.py](../../events.md), [exits/base.py](../../exits/base.md)   **Used by:** [strategy/day/wiring.py](wiring.md), [run.py](../../run.md) Step 2

## Responsibility
Closes the position once price crosses `entry_price * (1 ∓ stop_pct)` — a hard
stop whose **width is a percent of the entry price, anchored at the entry
fill** and never moved.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `EntryPercentStop(stop_pct)` | class | the fraction-of-entry stop |
| `should_exit(position, market, bar_idx)` | method | `True` at/beyond the stop level |

## Key points / gotchas
- **Percent, not points.** A fixed point budget means different fractions of
  the contract at different price levels — the day tape's entries span
  6 952 to 26 000, and the trail sweep measured 3 600 points reading as
  19–54 % of price in the train era but ~8 % in 2026. `stop_pct` keeps the
  *fraction* constant: 3 % is ~209 points at a 2011 entry, ~780 at a 2026 one.
- **Entry-anchored, not peak-anchored.** The reference is the entry fill, not
  the best print since entry (that is `TrailingStop`, discarded in the
  day_trail post-mortem: fires at V-bottoms). Open profit on a winner is never
  given back by this rule; only the initial `stop_pct` band is at risk.
- **Stateless.** `should_exit` reads only the `PositionState` snapshot the run
  loop rebuilds every tick, so a re-entry after a stop fires needs no arming or
  re-keying machinery — the new position carries its own entry price.
- **Exact threshold fires** (`<=` / `>=`), matching `FixedStopLoss`; the fill
  then follows SPEC §4.5's placement-time classification like any other order.
- **Flatten-and-continue.** On fire, run.py Step 2 submits a closing limit and
  the loop keeps trading (later signals may re-enter) — the semantics the live
  deployment wants, which is what makes the backtest a faithful measurement of
  it.
- Knob `stop_pct` defaults to `None` (OFF) in [wiring.py](wiring.md) `PARAMS`,
  so the unedited package still reproduces every recorded run; cells opt in
  via `--param stop_pct=...`.

## Related
[exits/stop_loss.md](../../exits/stop_loss.md) · [exits/base.md](../../exits/base.md) · [strategy/day/wiring.md](wiring.md) · [run.md](../../run.md) Step 2
