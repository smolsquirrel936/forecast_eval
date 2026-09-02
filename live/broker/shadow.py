"""ShadowBroker — run the offline fill model beside the real one and diff them.

This is V4's deliverable. The backtest's fill model is optimistic by
construction: at `aggression_ticks: 0` an entry limit sits *exactly* at the last
print, so `Execution` classifies every order marketable and fills it at the next
print, guaranteed (§2.1). In a real book that limit is joining the queue, and it
fills only when someone crosses to you — which is the adverse-selection case.

So every number in `results.tsv` rests on an assumption nothing has measured.
This class measures it: the live broker and a private `Execution` see the *same*
orders, and the difference between what each reports is the fill-model gap.

**The shadow can never influence trading.** Only the live broker's fills are
returned from `poll_fills`, so only they reach `Trader` and `Portfolio`; the
shadow's are recorded and nothing else. Same rule the recorder follows — an
observer that can change the thing it observes is not measuring it.

### What is measured, and what deliberately is not

Well-defined, and reported:

  * **fill rate**, live and shadow, over identical orders;
  * **slippage** on orders *both* filled, signed so positive always means live
    did worse;
  * **divergences** — one filled and the other did not, split by direction.

**Not** reported: a full sim-versus-live PnL. The moment one side fills and the
other does not, their positions differ, and every later order — generated from
the *live* position by `Trader` — is meaningless for the shadow (it would be
"closing" something the shadow never opened). Producing a PnL number from that
would be fabricating a counterfactual, so the slippage total is given instead:
the part of the gap attributable to execution *price*, which is real, plus the
divergence counts, which are the part that cannot be priced without assuming a
strategy the shadow never ran.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, List, Optional

from ...events import FillEvent, MarketEvent, OrderEvent
from ...execution import Execution
from .base import Broker, HeldPosition


@dataclass
class OrderGap:
    """One order's life, as each side of the comparison saw it."""

    timestamp: Any
    side: str
    intent: str
    quantity: int
    limit_price: float
    current_at_placement: float
    bars: int = 0
    live_price: Optional[float] = None
    live_quantity: int = 0
    live_bars: Optional[int] = None
    shadow_price: Optional[float] = None
    shadow_bars: Optional[int] = None

    @property
    def live_filled(self) -> bool:
        return self.live_price is not None

    @property
    def shadow_filled(self) -> bool:
        return self.shadow_price is not None

    @property
    def slippage_points(self) -> Optional[float]:
        # Tech: signed difference between the two fill prices, oriented so that
        #       positive always means the live fill was worse.
        # Why:  a raw subtraction flips meaning with side — paying 2 points more
        #       on a BUY and receiving 2 points less on a SELL are the same
        #       outcome, and averaging them unsigned would cancel a real cost to
        #       zero. Only meaningful when both sides filled.
        if not (self.live_filled and self.shadow_filled):
            return None
        if self.side == "BUY":
            return float(self.live_price) - float(self.shadow_price)
        return float(self.shadow_price) - float(self.live_price)

    def as_record(self) -> dict:
        return {
            "timestamp": self.timestamp, "side": self.side,
            "intent": self.intent, "quantity": self.quantity,
            "limit_price": self.limit_price,
            "current_at_placement": self.current_at_placement,
            "bars_working": self.bars,
            "live_filled": self.live_filled, "live_price": self.live_price,
            "live_bars": self.live_bars,
            "shadow_filled": self.shadow_filled,
            "shadow_price": self.shadow_price, "shadow_bars": self.shadow_bars,
            "slippage_points": self.slippage_points,
        }


class ShadowBroker(Broker):
    def __init__(self, live: Broker, *, fee_rate: float = 0.0,
                 recorder: Any = None):
        # Tech: hold the real broker, a private Execution as the shadow, the
        #       episode currently working, and every episode already scored.
        # Why:  the shadow is constructed here rather than injected because it
        #       must be *ours* — a shared Execution would already be carrying the
        #       loop's own orders, and the comparison would be against a fill
        #       model that had seen a different order stream.
        self.live = live
        self.shadow = Execution(fee_rate=fee_rate)
        self.recorder = recorder
        self.gaps: List[OrderGap] = []
        self._open: Optional[OrderGap] = None

    # ---- Broker interface --------------------------------------------------

    def submit(self, order: OrderEvent, *, current_price: float) -> None:
        # Tech: close any episode still open, then send the order to both sides
        #       and start a new episode.
        # Why:  §4.4 replaces a working order on every signal, so a submit is
        #       also the end of the previous order's life — that is the moment
        #       its outcome is final and can be scored.
        self._score_open()
        self.live.submit(order, current_price=current_price)
        self.shadow.submit(order, current_price=current_price)
        self._open = OrderGap(
            timestamp=order.timestamp, side=order.side, intent=order.intent,
            quantity=order.quantity, limit_price=order.limit_price,
            current_at_placement=current_price,
        )

    def cancel(self) -> None:
        self.live.cancel()
        self.shadow.cancel()
        self._score_open()

    def has_pending(self) -> bool:
        # Tech: the live broker's answer, never the shadow's.
        # Why:  this drives the loop's own gating (§4.4, and the flatten retry).
        #       Answering from the shadow would let a simulated fill convince the
        #       loop that a real working order had gone away.
        return self.live.has_pending()

    def pending_order(self) -> Optional[OrderEvent]:
        return self.live.pending_order()

    def poll_fills(self, market: MarketEvent) -> List[FillEvent]:
        # Tech: drain the live broker, step the shadow against the same bar,
        #       attribute both to the open episode — and return only the live
        #       fills.
        # Why:  the return value is what reaches `Trader` and `Portfolio`, so it
        #       must contain nothing the venue did not actually report. The
        #       shadow is stepped every bar regardless, because a passive order
        #       fills on some *later* print and skipping bars would understate
        #       its fill rate.
        live_fills = self.live.poll_fills(market)
        shadow_fill = self.shadow.check_fill(market)

        gap = self._open
        if gap is not None:
            gap.bars += 1
            for fill in live_fills:
                gap.live_price = float(fill.fill_price)
                gap.live_quantity += int(fill.quantity)
                if gap.live_bars is None:
                    gap.live_bars = gap.bars
            if shadow_fill is not None and gap.shadow_price is None:
                gap.shadow_price = float(shadow_fill.fill_price)
                gap.shadow_bars = gap.bars

        return live_fills

    @property
    def has_venue(self) -> bool:
        # Tech: whatever the wrapped live broker is.
        # Why:  every position query here passes through to it, so answering
        #       differently would describe a broker this class never asks.
        return self.live.has_venue

    def broker_position(self) -> Optional[int]:
        return self.live.broker_position()

    def broker_position_detail(self) -> Optional[HeldPosition]:
        # Tech: pass through to the venue; the shadow holds no position of its own.
        # Why:  the shadow exists to price a parallel fill model, not to own state.
        #       Answering from `Execution` here would report the offline mirror —
        #       zero at start-up — and a resumed run would adopt nothing.
        return self.live.broker_position_detail()

    @contextmanager
    def urgent(self):
        # Tech: open the live broker's urgent scope; the shadow has no notion of
        #       urgency and needs none.
        # Why:  urgency changes the *price sent to the venue*, which is exactly
        #       the thing being measured. The shadow keeps pricing the Trader's
        #       original limit, so an urgent close shows up in the comparison as
        #       the slippage it actually cost.
        with self.live.urgent():
            yield

    # ---- the measurement ---------------------------------------------------

    def close(self) -> None:
        """Score the final episode; call once at shutdown."""
        self._score_open()

    def _score_open(self) -> None:
        # Tech: move the open episode into `gaps` and record it.
        # Why:  scoring on close rather than on fill is what makes "neither side
        #       filled" and "only one side filled" representable — an episode
        #       that is only recorded when something fills cannot measure a fill
        #       *rate*.
        gap, self._open = self._open, None
        if gap is None:
            return
        self.gaps.append(gap)
        if self.recorder is not None:
            self.recorder.record("fill_gap_order", **gap.as_record())

    def gap_summary(self) -> dict:
        """The V4 numbers: fill rates, slippage, and where the two diverged."""
        # Tech: aggregate every scored episode; slippage statistics come only
        #       from episodes where both sides filled.
        # Why:  averaging slippage over unmatched episodes would silently treat
        #       "did not fill" as "filled at no cost", which is the opposite of
        #       the truth — a missed entry is the adverse-selection case §2.1 is
        #       about.
        n = len(self.gaps)
        live = [g for g in self.gaps if g.live_filled]
        shadow = [g for g in self.gaps if g.shadow_filled]
        matched = [g for g in self.gaps if g.live_filled and g.shadow_filled]
        slips = [g.slippage_points for g in matched]
        return {
            "orders": n,
            "live_fills": len(live),
            "shadow_fills": len(shadow),
            "live_fill_rate": len(live) / n if n else None,
            "shadow_fill_rate": len(shadow) / n if n else None,
            "matched": len(matched),
            "live_only": sum(1 for g in self.gaps
                             if g.live_filled and not g.shadow_filled),
            "shadow_only": sum(1 for g in self.gaps
                               if g.shadow_filled and not g.live_filled),
            "neither": sum(1 for g in self.gaps
                           if not g.live_filled and not g.shadow_filled),
            "slippage_total_points": sum(slips) if slips else 0.0,
            "slippage_mean_points": (sum(slips) / len(slips)) if slips else None,
            "slippage_worst_points": max(slips) if slips else None,
            "mean_bars_to_fill_live": _mean(
                [g.live_bars for g in live if g.live_bars is not None]),
            "mean_bars_to_fill_shadow": _mean(
                [g.shadow_bars for g in shadow if g.shadow_bars is not None]),
        }

    def format_summary(self) -> str:
        s = self.gap_summary()
        if not s["orders"]:
            return "fill gap: no orders this session"
        def pct(x):
            return "n/a" if x is None else f"{100 * x:.1f}%"
        def pts(x):
            return "n/a" if x is None else f"{x:+.2f}"
        return "\n".join([
            "",
            "--- fill-model gap (V4) " + "-" * 46,
            f"  orders                {s['orders']}",
            f"  fill rate  live/sim   {pct(s['live_fill_rate'])} / "
            f"{pct(s['shadow_fill_rate'])}",
            f"  both filled           {s['matched']}",
            f"  live only / sim only  {s['live_only']} / {s['shadow_only']}",
            f"  neither               {s['neither']}",
            f"  slippage mean/worst   {pts(s['slippage_mean_points'])} / "
            f"{pts(s['slippage_worst_points'])} points (+ = live worse)",
            f"  slippage total        {pts(s['slippage_total_points'])} points",
            "-" * 70,
        ])


def _mean(values: List[Any]) -> Optional[float]:
    return (sum(values) / len(values)) if values else None
