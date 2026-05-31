"""Portfolio: position, cash, realized & unrealized PnL with session tagging.

Units convention (Phase 1):
  * realized_pnl, total_fees are in price-points (per contract).
  * To get NT$, multiply by `contract_multiplier` (e.g. 200 for TXF).
  * Fees are taken from FillEvent.fee, which Execution computes as
    fill_price * fee_rate (SPEC §4.6, literal).
"""
from dataclasses import dataclass, field
from typing import Dict, List

from .events import FillEvent


@dataclass
class Portfolio:
    # Tech: the full accounting state — multiplier, signed position, average entry,
    #       running realized PnL and fees, the fill log, and per-session buckets.
    # Why:  one dataclass holds everything metrics/reporting need after a run; the
    #       per-session dicts exist so forced-close mode can attribute PnL to the
    #       DAY or NIGHT it was earned in (SPEC §4.7) without a second pass.
    contract_multiplier: float = 1.0
    position: int = 0           # signed contracts; +long / -short
    avg_entry_price: float = 0.0
    realized_pnl: float = 0.0
    total_fees: float = 0.0
    fills: List[FillEvent] = field(default_factory=list)
    realized_pnl_by_session: Dict[str, float] = field(
        default_factory=lambda: {"DAY": 0.0, "NIGHT": 0.0}
    )
    fees_by_session: Dict[str, float] = field(
        default_factory=lambda: {"DAY": 0.0, "NIGHT": 0.0}
    )

    def apply_fill(self, fill: FillEvent, session: str) -> None:
        # Tech: record the fill and compute the new signed position from the old.
        # Why:  signed arithmetic (+1 BUY / -1 SELL) lets one code path cover long
        #       and short; keeping prev/new explicit makes the open/add/close
        #       branches below readable.
        self.fills.append(fill)
        signed = 1 if fill.side == "BUY" else -1
        prev_pos = self.position
        delta = signed * fill.quantity
        new_pos = prev_pos + delta

        if prev_pos == 0:
            # Tech: opening from flat — the fill price becomes the average entry.
            # Why:  with no prior exposure there's nothing to average against, so
            #       entry is simply this fill.
            self.avg_entry_price = fill.fill_price
        elif (prev_pos > 0 and signed > 0) or (prev_pos < 0 and signed < 0):
            # Tech: same-direction add (pyramiding) — recompute a size-weighted
            #       average entry across the old and new lots.
            # Why:  a correct average cost is needed for unrealized PnL once
            #       max_position > 1; v1 caps at 1 so this is defensive, not hot.
            total_cost = (
                self.avg_entry_price * abs(prev_pos)
                + fill.fill_price * fill.quantity
            )
            self.avg_entry_price = total_cost / abs(new_pos)
        else:
            # Tech: opposite-direction fill — realize PnL on the closed quantity
            #       ((exit-entry) signed by trade direction) and book it both to the
            #       global total and the session bucket.
            # Why:  this is where money is actually made/lost; v1's Trader prevents
            #       flips, but we still clamp close_qty and handle a flip cleanly so
            #       the accounting can never go wrong if that invariant is relaxed.
            close_qty = min(abs(prev_pos), fill.quantity)
            direction = 1 if prev_pos > 0 else -1
            pnl_pts = (fill.fill_price - self.avg_entry_price) * direction * close_qty
            self.realized_pnl += pnl_pts
            self.realized_pnl_by_session[session] = (
                self.realized_pnl_by_session.get(session, 0.0) + pnl_pts
            )
            # Tech: reset entry to flat on a full close, or to the new fill on a flip.
            # Why:  a stale avg_entry_price after returning to flat would poison the
            #       next trade's PnL; the flip branch (new*prev < 0) shouldn't fire
            #       in v1 but is handled so the state stays consistent regardless.
            if new_pos == 0:
                self.avg_entry_price = 0.0
            elif new_pos * prev_pos < 0:
                # Flipped (shouldn't happen in v1).
                self.avg_entry_price = fill.fill_price

        # Tech: accrue the fee to the global total and the session bucket, then
        #       commit the new position.
        # Why:  fees are charged on every leg (SPEC §4.6) regardless of which branch
        #       above ran, so this is done once at the end; position is set last so
        #       the branches above could read the pre-fill `prev_pos` cleanly.
        self.total_fees += fill.fee
        self.fees_by_session[session] = (
            self.fees_by_session.get(session, 0.0) + fill.fee
        )
        self.position = new_pos

    def unrealized_pnl(self, mark_price: float) -> float:
        # Tech: mark-to-market of the open position; 0 when flat.
        # Why:  (mark - entry) * signed position works for both long and short
        #       without a branch; flat returns 0 to avoid a spurious value off a
        #       leftover avg_entry_price.
        if self.position == 0:
            return 0.0
        return (mark_price - self.avg_entry_price) * self.position

    def net_pnl(self) -> float:
        # Tech: realized PnL minus all fees paid.
        # Why:  "net" is the number that actually matters for evaluation — gross
        #       edge is meaningless until execution cost is subtracted.
        return self.realized_pnl - self.total_fees

    def summary(self) -> Dict[str, float]:
        # Tech: assemble the headline numbers in both points and NT$ (points ×
        #       contract_multiplier), plus the fill count.
        # Why:  callers want points for model comparison and NT$ for real money;
        #       computing both here keeps the conversion in one place and avoids
        #       every demo re-deriving the multiplier.
        return {
            "position": self.position,
            "realized_pnl_points": self.realized_pnl,
            "total_fees_points": self.total_fees,
            "net_pnl_points": self.net_pnl(),
            "realized_pnl_ntd": self.realized_pnl * self.contract_multiplier,
            "total_fees_ntd": self.total_fees * self.contract_multiplier,
            "net_pnl_ntd": self.net_pnl() * self.contract_multiplier,
            "n_fills": len(self.fills),
        }
