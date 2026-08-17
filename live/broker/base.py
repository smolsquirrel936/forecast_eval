"""Broker interface — the seam between order intent and order reality.

Deliberately narrow: the first four methods are exactly the surface `Trader`
already calls on `Execution` (`submit` / `cancel` / `has_pending` /
`pending_order`), so `Trader` needs no change to drive a real broker. The two
additions are what a live venue forces on us:

  * `poll_fills` — fills are *reported* asynchronously, not derived inline, so
    the loop drains whatever arrived instead of taking a single return value.
  * `broker_position` — the venue, not our mirror, is the source of truth for
    position; `None` means the implementation cannot answer (offline sim).
"""
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, List, Optional

from ...events import FillEvent, MarketEvent, OrderEvent


@dataclass(frozen=True)
class HeldPosition:
    # Tech: what the venue holds for our contract — signed lots and the average
    #       price they were opened at.
    # Why:  `broker_position()` answers "how much", which is all the per-bar
    #       reconciliation needs. Resuming into an existing position needs "at
    #       what price" as well: the entry is what an ExitRule measures from and
    #       what the Portfolio realizes PnL against, and a resumed run that
    #       invented one would book a fictional number on the closing fill.
    signed_qty: int          # +long / -short, in contracts
    avg_price: float         # size-weighted entry, in points


class Broker(ABC):
    @abstractmethod
    def submit(self, order: OrderEvent, *, current_price: float) -> None:
        # Tech: send `order` to the venue, recording the price observed at the
        #       moment of submission.
        # Why:  signature matches `Execution.submit` exactly so `Trader` is
        #       duck-typed against both without a shim. current_price is what the
        #       offline fill model classifies marketable-vs-passive from (SPEC
        #       §4.5); a real broker ignores it but logs it for the V4 fill-gap
        #       measurement (SHIOAJI_PLAN.md §2.1).
        ...

    @abstractmethod
    def cancel(self) -> None:
        # Tech: withdraw any resting order.
        # Why:  §4.4 gives a pending order a lifetime of exactly one forecast
        #       boundary, and `Trader` calls this on *every* signal including
        #       HOLD. Against a real venue the cancel is asynchronous, so
        #       implementations must treat this as "cancel requested", not
        #       "cancelled" — `has_pending` stays True until the venue confirms.
        ...

    @abstractmethod
    def has_pending(self) -> bool:
        # Tech: True while an order is working (including one whose cancel has
        #       been requested but not yet confirmed).
        # Why:  lets the loop and the safety rails see book state without
        #       reaching into implementation internals.
        ...

    @abstractmethod
    def pending_order(self) -> Optional[OrderEvent]:
        # Tech: the working OrderEvent, or None when the book is empty.
        # Why:  read-only accessor for logging and assertions.
        ...

    @abstractmethod
    def poll_fills(self, market: MarketEvent) -> List[FillEvent]:
        # Tech: return every fill that has become known since the last call, as
        #       of `market`, and forget them (draining, not peeking).
        # Why:  the offline sim resolves a fill *from* the incoming print, so it
        #       needs `market`; a live broker receives fills on a callback thread
        #       and merely drains its queue here, ignoring the argument. One
        #       signature serves both, and returning a list (rather than
        #       `Optional[FillEvent]`) is what lets a live venue report the
        #       partial fills the print-only sim cannot produce.
        ...

    @contextmanager
    def urgent(self) -> Iterator[None]:
        # Tech: orders submitted inside this block should prioritise *filling*
        #       over price. Default: a no-op, so every existing Broker behaves
        #       exactly as before.
        # Why:  a `FLATTEN` halt has to actually close the position, but the
        #       submit is issued by `Trader.submit_exit` — and `trader.py` is not
        #       ours to modify (the seam is duck-typed by design). A scoped marker
        #       on the *broker* lets the loop say "this one is urgent" without a
        #       new argument threaded through code the backtest shares.
        #
        #       A context manager rather than a flag so it cannot leak: an
        #       exception mid-submit still restores normal pricing, and the block
        #       makes the urgent region visible at the call site.
        #
        #       No-op by default is what keeps V0 parity intact — `SimBroker` and
        #       `DryRunBroker` price a flatten exactly as they always did, so the
        #       offline comparison is unchanged by this addition.
        yield

    def broker_position(self) -> Optional[int]:
        # Tech: the venue's own signed position, or None if unavailable.
        # Why:  `Trader.position` is a mirror; live it must be reconciled against
        #       the venue after every fill (SHIOAJI_PLAN.md §2.9). Defaulting to
        #       None means offline implementations opt out and the reconciler
        #       skips them rather than every Broker being forced to fake it.
        return None

    def broker_position_detail(self) -> Optional[HeldPosition]:
        # Tech: the venue's held position including its entry price, or None when
        #       the implementation cannot answer.
        # Why:  read exactly once, at start-up, so a restart can adopt a position
        #       it did not open (§2.20) — the per-bar path stays on
        #       `broker_position()` and gains no extra query. Defaulting to None
        #       means the offline brokers, which always start flat, opt out by
        #       inheritance and the adopt step becomes a no-op for them.
        return None
