"""The owning process's order book — N working orders, one venue, three deployments.

STRATEGY_SPLIT_PLAN §18.3 item 1. `ShioajiBroker` tracks exactly **one** live
order (`self._live`), which is right for a deployment and wrong for the owner:
three deployments can each have an order working, and §18.9's crossing rule turns
a single intent into a **pair** of legs, so even one deployment can hold two.

**This module is deliberately venue-free.** It does no `place_order`, holds no
session, imports no `shioaji`. It is the accounting: which legs an intent becomes,
which order a deal belongs to, whose fill it is, and what the account therefore
holds. That makes every rule below testable without a market, which matters
because the rules were measured the expensive way.

**Why the owner exists at all.** MEASURED (§14): on one account, deals are
delivered per session but the position book is **shared**. Three deployments
placing their own orders see each other's positions and none of each other's
fills, so the reconciliation rail compares one against the other and both halt.
One process owning the session gives one book and one `_match`.

**The two measured facts this encodes.**

* §18.9/§18.12 — an **over-sized `Cover` is not reliable**. It crossed +1 to -1
  once and clamped at flat twice, with no difference in how it was issued, and
  when it clamped *the deal still reported the full order quantity*. So a leg is
  never larger than it can fully execute: `plan_legs` sizes the Cover to the lots
  that actually close and opens the rest as `New`.
* §18.9 — **partial fills are real**. A 2-lot order filled as two 1-lot deals
  nine seconds apart. Fills accumulate against a leg; a leg is finished when its
  own quantity is met, never on the first deal.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from ..events import FillEvent, OrderEvent

# Tech: the two `octype` spellings this book issues.
# Why:  strings rather than the `shioaji` enum so this module stays importable
#       without the package — the same reason `ShioajiBroker` imports `sj`
#       lazily. The owner maps these to `sj.FuturesOCType` at the placement
#       boundary, which is the only place the venue's vocabulary belongs.
COVER = "Cover"
NEW = "New"


@dataclass(frozen=True)
class Leg:
    # Tech: one order to place — what it does at the venue, and how big it is.
    # Why:  an intent is not always one order (§18.9), so the place path takes a
    #       list. Keeping `octype` here rather than deriving it later means the
    #       decision is made once, against the account net that justified it.
    octype: str      # COVER (closes existing lots) or NEW (opens)
    quantity: int


def plan_legs(side: str, quantity: int, account_net: int) -> List[Leg]:
    """Split an intent into legs no larger than each can fully execute (§18.9).

    Tech: the lots that reduce `account_net` become one `Cover` leg, the rest
          become one `New` leg, Cover first.
    Why:  MEASURED (§18.12) — an over-sized `Cover` clamped at flat twice and
          crossed once, and when it clamped the deal still reported the full
          order quantity, which would move `Trader.position` by two against a
          book that moved one. Sizing the Cover to `min(quantity, |account_net|)`
          makes that unreachable rather than merely unlikely: a leg that can
          always fully execute cannot partially fail, so it cannot over-report.
          Cover-first because that is the order Phase B measured end to end, with
          no settling gap needed between the legs.
    """
    if quantity <= 0:
        return []
    # Tech: signed direction of the intent, then the lots of it that oppose what
    #       the account currently holds.
    # Why:  "closing" is not a property of the deployment's OPEN/CLOSE intent —
    #       it is a property of the ACCOUNT. A deployment closing its own long
    #       while a peer is short may be opening at the venue, which is exactly
    #       the case §18.1 was written to find.
    direction = 1 if side == "BUY" else -1
    opposing = account_net if (account_net * direction) < 0 else 0
    cover = min(quantity, abs(opposing))
    legs: List[Leg] = []
    if cover:
        legs.append(Leg(COVER, cover))
    if quantity - cover:
        legs.append(Leg(NEW, quantity - cover))
    return legs


@dataclass
class Working:
    # Tech: one placed leg and everything needed to reconcile its reports — whose
    #       it is, both correlation ids, and running fill/cancel state.
    # Why:  `slug` is the addition that `ShioajiBroker._Live` has no need for and
    #       the owner cannot work without: it is what routes a deal back to the
    #       one deployment whose order it was (§18.3 item 2), which is what makes
    #       per-deployment attribution exact rather than apportioned. `filled`
    #       bounds total fills against the leg's own quantity — the last defence
    #       against a duplicate or stray deal inventing a position.
    slug: str
    order: OrderEvent
    octype: str
    quantity: int
    trade: Any = None
    code: str = ""
    order_id: Optional[str] = None
    ordno: Optional[str] = None
    filled: int = 0
    cancel_requested: bool = False
    dead: bool = False

    @property
    def remaining(self) -> int:
        return self.quantity - self.filled


class OrderBook:
    """N working orders across three deployments, on one account.

    Every mutating method takes the lock: deals arrive on the venue's callback
    thread while the owner's own loop places and cancels, exactly as
    `ShioajiBroker` documents.
    """

    def __init__(self, *, on_anomaly: Optional[Callable[[str], None]] = None,
                 recorder: Any = None, default_code: str = "") -> None:
        # Tech: the working legs, the dedup set, the running account net, and the
        #       optional observers.
        # Why:  a list rather than a per-slug slot because §18.9's split gives one
        #       deployment two legs at once, so "one order per deployment" was
        #       never the right shape. `_net` is maintained from BOOKED fills
        #       rather than read from the venue: it is what `plan_legs` must size
        #       against, and it has to be correct the instant a fill is booked,
        #       not one `list_positions` call later.
        self._lock = threading.RLock()
        self._working: List[Working] = []
        self._seen_deals: Set[Tuple[Any, Any]] = set()
        self._net = 0
        # Tech: the account net split by dated month code. A caller that names
        #       no month means `default_code`, the owner's start-up month.
        # Why:  2026-10-20 (simtrade_operations §11) — between the deployments'
        #       roll instants the account holds two months at once, and a lot on
        #       one cannot cover a lot on the other. `plan_legs` must size a
        #       Cover against the month the order is for, which the total cannot
        #       say; `_net` stays the total because that is what the account
        #       holds across months.
        self._net_by_code: Dict[str, int] = {}
        self.default_code = default_code
        # Tech: each deployment's own signed size, from the fills routed to it.
        # Why:  two different reconciliations need two different operands. The
        #       OWNER compares `account_net` against `list_positions` — that is
        #       the venue check. A DEPLOYMENT compares its `Trader.position`
        #       against this — both sides are then "the fills this deployment
        #       had", so a disagreement means an event lost on the wire rather
        #       than a peer holding a position, which is the category error §16
        #       exists to remove.
        self._slug_net: Dict[str, int] = {}
        # Tech: each deployment's size-weighted average entry price.
        # Why:  `adopt_position` needs "at what price" as well as "how much"
        #       (`broker/base.HeldPosition`) — it is what an ExitRule measures
        #       from and what the Portfolio realizes PnL against, so a resumed run
        #       that invented one would book a fictional number on the closing
        #       fill. The venue reports an average for the ACCOUNT, which is the
        #       wrong number on a shared book, so the owner has to keep its own
        #       per deployment.
        self._slug_entry: Dict[str, float] = {}
        self.anomalies: List[str] = []
        self.on_anomaly = on_anomaly
        self.recorder = recorder

    # ---- state ------------------------------------------------------------

    @property
    def account_net(self) -> int:
        """What this book believes the ACCOUNT holds, from its own booked fills."""
        with self._lock:
            return self._net

    def net_in(self, code: str = "") -> int:
        """What this book believes the account holds in ONE month."""
        with self._lock:
            return self._net_by_code.get(code or self.default_code, 0)

    def codes(self) -> List[str]:
        """Every month this book has seeded, placed or booked in."""
        with self._lock:
            return list(self._net_by_code)

    def net_for(self, slug: str) -> int:
        """What THIS deployment holds, from the fills routed to it."""
        with self._lock:
            return self._slug_net.get(slug, 0)

    def entry_for(self, slug: str) -> float:
        """This deployment's size-weighted entry, or 0.0 when it is flat."""
        with self._lock:
            return self._slug_entry.get(slug, 0.0)

    def seed_slug_entry(self, slug: str, price: float) -> None:
        with self._lock:
            self._slug_entry[slug] = float(price)

    def seed_slug_net(self, slug: str, signed_qty: int) -> None:
        # Tech: set one deployment's net without booking a fill.
        # Why:  a restart reads each deployment's own `fills_<slug>.csv`
        #       (`ledger.net_position`), and the account seed is their sum. Same
        #       rule as `seed_net`: adoption is not a trade.
        with self._lock:
            self._slug_net[slug] = int(signed_qty)

    def seed_net(self, signed_qty: int, code: str = "") -> None:
        # Tech: set the account net without booking a fill.
        # Why:  the owner restarts into an account that is already holding, and
        #       §16's `account_net_position` (the sum of the deployments' fill
        #       ledgers) is what it restarts from. Seeding is not a fill: it must
        #       not reach any deployment's `poll_fills`, or a restart would book a
        #       phantom trade into a Portfolio.
        with self._lock:
            self._net = int(signed_qty)
            self._net_by_code = {code or self.default_code: int(signed_qty)}

    def working_for(self, slug: str) -> List[Working]:
        with self._lock:
            return [w for w in self._working if w.slug == slug and not w.dead]

    def has_pending(self, slug: str) -> bool:
        return bool(self.working_for(slug))

    def all_working(self) -> List[Working]:
        with self._lock:
            return [w for w in self._working if not w.dead]

    # ---- placing ----------------------------------------------------------

    def register(self, slug: str, order: OrderEvent, leg: Leg, *,
                 trade: Any = None, order_id: Optional[str] = None,
                 ordno: Optional[str] = None, code: str = "") -> Working:
        # Tech: start tracking one placed leg.
        # Why:  registration is separate from placement because this module never
        #       talks to the venue — the owner places, then hands back whatever
        #       correlation ids the venue returned. Splitting it this way is also
        #       what lets every rule here be tested without a market.
        with self._lock:
            working = Working(slug=slug, order=order, octype=leg.octype,
                              quantity=leg.quantity, trade=trade,
                              order_id=order_id, ordno=ordno,
                              code=code or self.default_code)
            self._working.append(working)
            self._net_by_code.setdefault(working.code, 0)
            return working

    def mark_cancel_requested(self, slug: str) -> List[Working]:
        # Tech: flag this deployment's legs as cancel-requested and return them;
        #       they stay tracked.
        # Why:  §2.2 — a cancel is a request, not an outcome. A cancelled order
        #       can still fill in the race window and that fill is real money, so
        #       dropping the leg here would lose a live position. `ShioajiBroker`
        #       moves such orders to `_retiring` for the same reason; here the
        #       flag serves, because `_match` already searches every leg rather
        #       than a single slot.
        with self._lock:
            legs = [w for w in self._working if w.slug == slug and not w.dead]
            for leg in legs:
                leg.cancel_requested = True
            return legs

    # ---- venue reports ----------------------------------------------------

    def match(self, trade_id: Any, ordno: Any) -> Optional[Working]:
        """Find a tracked leg by either correlation id, taking the lock."""
        # Tech: the locked public form of `_match`.
        # Why:  `_match` documents "caller holds the lock" because `on_deal`
        #       already does. The owner's order-event handler has no lock of its
        #       own, and reaching for the private form from another module would
        #       read the working list while a deal mutates it.
        with self._lock:
            return self._match(trade_id, ordno)

    def learn_ordno(self, order_id: Any, ordno: Any) -> None:
        # Tech: attach a venue order number to the leg placed under `order_id`.
        # Why:  a deal can arrive before the order event that carries `ordno`, so
        #       the id returned at placement is the only handle in that window.
        #       Learning it when it does arrive is what makes later deals match on
        #       the stronger key.
        if not order_id or not ordno:
            return
        with self._lock:
            for working in self._working:
                if working.order_id and working.order_id == order_id:
                    working.ordno = str(ordno)
                    return

    def on_deal(self, *, ordno: Any, trade_id: Any, exchange_seq: Any,
                side: str, price: float, quantity: int,
                timestamp: Any, fee: float = 0.0
                ) -> Optional[Tuple[str, FillEvent]]:
        """Book one venue deal. Returns `(slug, fill)`, or None if it was not booked."""
        with self._lock:
            # Tech: one venue deal produces exactly one fill, however often it is
            #       reported.
            # Why:  duplicate deal reports are documented behaviour, and a
            #       double-booked fill is a position that does not exist. Same key
            #       `ShioajiBroker` dedups on.
            key = (ordno, exchange_seq)
            if key in self._seen_deals:
                return None

            working = self._match(trade_id, ordno)
            if working is None:
                # Tech: a deal that belongs to no tracked leg.
                # Why:  with one owner there are no foreign deals — §14 measured
                #       that a session hears only its own — so this is a fill for
                #       an order we placed and lost track of, or one placed
                #       outside this system. Either way the account moved and we
                #       cannot say whose it was, which is precisely what must not
                #       be guessed.
                self._anomaly(
                    f"deal for an unknown order: ordno={ordno!r} "
                    f"trade_id={trade_id!r} seq={exchange_seq!r}")
                return None

            if quantity <= 0:
                self._anomaly(f"deal with non-positive quantity: {quantity!r}")
                return None

            if working.filled + quantity > working.quantity:
                # Tech: refuse a deal that would fill a leg beyond its own size.
                # Why:  §18.12 measured the venue reporting a 2-lot deal for an
                #       order that moved the book by one. `plan_legs` makes that
                #       unreachable for a Cover by never over-sizing one — this
                #       guard is what catches it if the venue over-reports
                #       anyway, before `Trader.position` moves further than the
                #       account did.
                self._anomaly(
                    f"deal overfills order: {working.filled}+{quantity} > "
                    f"{working.quantity} (slug={working.slug} ordno={ordno!r})")
                return None

            self._seen_deals.add(key)
            working.filled += quantity
            # Tech: move the account net by the signed size of this deal.
            # Why:  this is the number `plan_legs` sizes the next Cover against,
            #       so it must be right the instant a fill is booked — §18.10
            #       measured `list_positions` catching up in ~0.1 s, which is fast
            #       but is still after the decision the owner may already be
            #       making.
            signed = quantity if side == "BUY" else -quantity
            self._net += signed
            self._net_by_code[working.code] = (
                self._net_by_code.get(working.code, 0) + signed)
            self._update_entry(working.slug, signed, quantity, float(price))
            self._slug_net[working.slug] = (
                self._slug_net.get(working.slug, 0) + signed)
            if working.remaining <= 0:
                working.dead = True

            if self.recorder is not None:
                self.recorder.record(
                    "owner_deal", slug=working.slug, ordno=ordno,
                    exchange_seq=exchange_seq, side=side, price=price,
                    quantity=quantity, octype=working.octype,
                    code=working.code, account_net=self._net)

            return working.slug, FillEvent(
                timestamp=timestamp, side=side, fill_price=float(price),
                quantity=int(quantity), fee=float(fee))

    def finish(self, working: Working) -> None:
        # Tech: mark a leg finished and stop tracking it.
        # Why:  called when the venue reports the order cancelled, rejected or
        #       filled. Removal is separate from `dead` because a fully-filled leg
        #       is marked dead the moment its last deal is booked, while an order
        #       the venue has merely *told* us about may still have a deal in
        #       flight behind the message.
        with self._lock:
            working.dead = True
            if working in self._working:
                self._working.remove(working)

    def forget_dead(self) -> None:
        # Tech: drop every leg already marked dead.
        # Why:  `_working` would otherwise grow for the life of the process. Kept
        #       explicit rather than automatic so a caller can still inspect a
        #       just-completed leg — the fill and the leg's disappearance are two
        #       different moments, and conflating them is how a late duplicate
        #       deal becomes "unknown order" instead of a dedup hit.
        with self._lock:
            self._working = [w for w in self._working if not w.dead]

    # ---- helpers ----------------------------------------------------------

    def _update_entry(self, slug: str, signed: int, quantity: int,
                      price: float) -> None:
        # Tech: roll this deployment's average entry forward for one fill. Called
        #       BEFORE `_slug_net` is updated, so `prev` is the size going in.
        # Why:  four cases, and only one of them is an average. Opening from flat
        #       and crossing through flat both START a position, so the entry is
        #       simply this fill's price — carrying the old side's average across
        #       a cross would price a short at what a long cost. Adding to a side
        #       is the weighted case. REDUCING keeps the entry unchanged: the lots
        #       that remain were bought at what they were bought at, and
        #       re-averaging on a partial close is how a closing fill silently
        #       rewrites the PnL of the lots it did not touch.
        prev = self._slug_net.get(slug, 0)
        new = prev + signed
        prev_entry = self._slug_entry.get(slug, 0.0)
        if new == 0:
            self._slug_entry[slug] = 0.0
        elif prev == 0 or (prev > 0) != (new > 0):
            self._slug_entry[slug] = price
        elif (signed > 0) == (prev > 0):
            self._slug_entry[slug] = (
                (abs(prev) * prev_entry + quantity * price) / abs(new))
        else:
            self._slug_entry[slug] = prev_entry

    def _match(self, trade_id: Any, ordno: Any) -> Optional[Working]:
        # Tech: find a tracked leg by venue order number, then by the id returned
        #       at placement. Caller holds the lock.
        # Why:  two keys because a deal may arrive before the order event that
        #       would have taught us `ordno`. **No sole-order fallback**, unlike
        #       `ShioajiBroker._match`: that one may assume the single live order
        #       is the right one, and here it would attribute one deployment's
        #       fill to another. An unattributable deal must halt, not be
        #       assigned to whoever happens to be first.
        for working in self._working:
            if ordno and working.ordno and working.ordno == ordno:
                return working
        for working in self._working:
            if trade_id and working.order_id and working.order_id == trade_id:
                return working
        return None

    def _anomaly(self, message: str) -> None:
        # Tech: record, notify, and let the caller halt. Caller holds the lock.
        # Why:  same contract as `ShioajiBroker._anomaly` — every branch reaching
        #       here is a message we could not account for, and losing one
        #       silently means losing track of a position. The owner wires
        #       `on_anomaly` to the halt, so there is one place to look when a
        #       live run stops.
        self.anomalies.append(message)
        if self.recorder is not None:
            self.recorder.record("owner_anomaly", message=message)
        if self.on_anomaly is not None:
            self.on_anomaly(message)
