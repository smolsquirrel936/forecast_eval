# [strategy/fullday/exits.py](../../../strategy/fullday/exits.py)

**Role:** FULL-DAY's own exit rules — profit protection (reversal loop decisions 9–11).
**Pipeline:** concrete [ExitRule](../../exits/base.md) implementations, owned by the FULL-DAY package.
**Depends on:** [events.py](../../events.md), [exits/base.py](../../exits/base.md), [exits/pct_stop.py](../../exits/pct_stop.md)   **Used by:** [strategy/fullday/wiring.py](wiring.md)

## Responsibility
Model-free exit rules that read the tape, not the forecast: a break-even lock
(G1), a give-back trail with a 2% arm (G2), and the any-of composition seam
that lets them compose with the shared percent stop.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `BreakEvenLock(lock_after_pct)` | class | G1: once best price is `lock_after_pct` in favour, exit at a return to entry |
| `GivebackTrail(giveback_frac)` | class | G2: once 2% in favour, exit when the open gain falls to `(1-frac)` of its max |
| `ChannelExit(channel_exit_bars)` | class | H: once N bars old, exit on a break of the last N in-position bars' extreme |
| `AnyOf(rules)` | class | fire when any wrapped rule fires (decision 9's composition seam) |
| `EntryPercentStop` | re-export | the shared percent stop, for composition |
| `GIVEBACK_ARM_PCT` | constant | the give-back trail's arm level (0.02) |

## Key points / gotchas
- **Stateful, therefore `adoptable = False`.** Both protection rules track the
  best price since entry, so a live `--resume` that adopts a held lot
  (`live/runtime.py` `adopt_position`) is refused for them — the best price
  belongs to the dead process and must not be invented.
- **Their state survives a live roll.** The rules key their state on the entry's
  `(entry_bar_idx, entry_price, side)` and restart on a new one. A roll closes
  and re-opens the position, but the fullday [Trader](trader.md) carries the
  original entry onto the roll's re-open (`candidate_fullday_iter34`). So the
  lock keeps its best price and arming, as it does on the backtest's spliced
  tape. Any other new entry restarts the rules.
- **`run.py` queries the rule only while a position is open** (Step 2), so the
  rules never see flat bars; per-tick state updates cost nothing while flat.
- **The G2 arm is the day_trail lesson**: a fixed-width trail fires at
  V-bottoms. `GivebackTrail` cannot fire on a trade that never earned 2%, and
  its width grows with the gain banked. At the arming print
  `gain == max_gain`, so it never fires on the arm tick itself.
- **`ChannelExit`'s window excludes the current print** — the N bars are the
  in-position prints *before* the current one, so a long breaking below their
  minimum is a well-defined event (the current print can never be its own
  extreme). The rule is inert until the position is at least N bars old
  (N prior prints seen), which is what makes 1140 bars mean "one fullday
  trading day" (300 day + 840 night bars).
- **`AnyOf` short-circuits** — the first rule to fire wins the tick, and later
  rules' state updates are skipped. Safe: the close is already submitted, and
  every stateful rule resets on the next trade's new entry identity. The
  composite's `adoptable` is the AND of its members'.
- **Widths are validated in the constructors** (`0 < lock_after_pct < 1`,
  `0 < giveback_frac < 1`), which `wiring.make_strategy` surfaces as
  `ValueError`.
- **The control is inert by construction**: with `lock_after_pct` and
  `giveback_frac` both `None`, wiring returns the bare `EntryPercentStop` —
  byte-identical to the pre-knob package.

## Related
[strategy/fullday/wiring.md](wiring.md) · [exits/base.md](../../exits/base.md) · [exits/pct_stop.md](../../exits/pct_stop.md) · [FULLDAY_PCTSTOP_POSTMORTEM.md](../../../FULLDAY_PCTSTOP_POSTMORTEM.md)
