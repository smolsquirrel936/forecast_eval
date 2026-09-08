"""ShioajiBroker — the Broker seam over Sinopac's Shioaji futures API.

Where `SimBroker` *infers* fills from trade prints, this one *receives* them on a
callback thread and hands them to the loop. Everything hard about it comes from
that inversion:

  * **Deals can arrive before the order event.** The docs say so explicitly, so
    correlation cannot rely on having seen `ordno` first — a deal message carries
    its own `ordno`, and we also match on `trade_id` ↔ the order `id` returned by
    `place_order`.
  * **Duplicate deals.** Dedup is on `(ordno, exchange_seq)`; one venue deal must
    produce exactly one `FillEvent` no matter how often it is reported.
  * **Cancellation is asynchronous** (§2.2). `cancel()` is a *request*; the order
    is not gone until the venue says so. A cancelled order can still fill in the
    race window, and that fill is real money — so it is booked, not discarded.
  * **Callbacks run on Shioaji's thread.** All mutable state is under a lock and
    fills are queued; `poll_fills` drains them from the loop's thread. Nothing
    slow ever runs in a callback.

Anything this adapter cannot explain — an unrecognised callback, a deal it cannot
attribute, a fill exceeding the order's own quantity — is surfaced as an anomaly
that halts the loop. Silently dropping a message here means silently losing a
position.

`shioaji` is imported lazily and can be substituted, so this module (and its
tests) run with the package absent.
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from ...events import FillEvent, MarketEvent, OrderEvent
from ..contracts import contract_codes
from .base import Broker, HeldPosition


# Tech: how long an IOC may stay unresolved before this broker stops counting
#       it as pending, in seconds.
# Why:  an IOC either fills or expires *immediately* — there is nothing for it to
#       rest on. Whether the venue then reports that expiry is **UNMEASURED**:
#       §2.19 saw an expired IOC leave `has_pending()` True, and the probe that
#       would settle it needs a live session. So the ladder cannot depend on the
#       report arriving. Three seconds is far longer than an immediate-or-cancel
#       decision can honestly take and far shorter than the ladder's retry
#       interval, which is what keeps the two from racing. If the probe later
#       shows the venue does report, this becomes a backstop rather than the
#       mechanism — and nothing about the ladder changes.
IOC_TTL_SECONDS = 3.0


def _load_sj() -> Any:
    # Tech: import the `shioaji` package on first use.
    # Why:  keeps the import off module load so the live package can be imported,
    #       and its tests run, on a machine where shioaji is not installed.
    import shioaji as sj
    return sj


@dataclass
class _Live:
    # Tech: one submitted order and everything needed to reconcile its reports —
    #       the venue Trade handle, both correlation ids, the placement price, and
    #       running fill/cancel state.
    # Why:  `filled` is what bounds total fills against the order's own quantity,
    #       which is the last line of defence against a duplicate or stray deal
    #       inventing a position. `current_at_placement` is kept for the V4
    #       fill-gap measurement — it is what the sim would have priced against.
    order: OrderEvent
    trade: Any
    order_id: Optional[str]
    ordno: Optional[str]
    current_at_placement: float
    filled: int = 0
    cancel_requested: bool = False
    dead: bool = False


class ShioajiBroker(Broker):
    def __init__(
        self,
        api: Any,
        contract: Any,
        *,
        account: Any = None,
        fee_rate: float = 0.0002,
        sj: Any = None,
        rails: Any = None,
        recorder: Any = None,
        on_anomaly: Optional[Callable[[str], None]] = None,
        tick_size: float = 1.0,
        flatten_aggression_ticks: int = 5,
    ):
        # Tech: hold the injected api/contract/account, the fee rate, and the
        #       optional observers; register the order callback.
        # Why:  `api` is injected rather than constructed so login/session
        #       management stays outside this class and a fake can be substituted
        #       wholesale. `sj` is the enum/constructor namespace — defaulted
        #       lazily so importing this module never requires the package.
        self.api = api
        self.contract = contract
        self.account = account if account is not None else getattr(
            api, "futopt_account", None
        )
        self.fee_rate = fee_rate
        self._sj = sj
        self.rails = rails
        self.recorder = recorder
        self._on_anomaly = on_anomaly

        # Tech: how far through the touch an urgent (FLATTEN) order is priced,
        #       and the flag `urgent()` raises for the duration of that submit.
        # Why:  a resting LMT+ROD flatten can silently fail — ROD is cancelled at
        #       session end, so a close fired near the bell leaves the position on
        #       with the loop blind until the next session (the
        #       `flatten_incomplete` failure `shutdown()` already reports).
        #
        #       **Aggression is what fills a close; the time-in-force only says
        #       what happens when it cannot.** MEASURED 2026-08-12 across seven
        #       probe orders: an IOC close priced 5 ticks through a *stationary*
        #       market went unfilled for a full 20 s window with the limit
        #       reachable throughout — the venue matches on prints, and in a
        #       quiet minute there is nothing to match against. Worse, the
        #       expiry left `has_pending()` **True**, and the loop's flatten
        #       retry is gated on exactly that: the position would never have
        #       been closed. ROD rests instead and fills on the next print.
        #
        #       MKT and MKP were both rejected: MKT is unbounded in a thin book
        #       (a flatten that fills at any price is its own kind of accident),
        #       and MKP's band semantics are unverified — §7.2's history says do
        #       not ship an unverified Shioaji behaviour into the risk path.
        self.tick_size = tick_size
        self.flatten_aggression_ticks = flatten_aggression_ticks
        self._urgent = False
        # Tech: how many ticks through the touch a roll leg is priced, `None`
        #       when no roll leg is being submitted; and the moment after which an
        #       unresolved IOC stops counting as pending.
        # Why:  §2.6's roll ladder is the only path that sends IOC. It is kept
        #       separate from `_urgent` because the two want opposite things: a
        #       FLATTEN wants to *rest* until the next print (ROD, measured
        #       §2.19), while a roll leg wants to expire so the ladder can
        #       re-price it. `_ioc_deadline` exists because whether the venue
        #       reports that expiry is **unmeasured** — see `has_pending`.
        self._rolling_ticks = None
        self._ioc_deadline = None

        # Tech: lock guarding every mutable field below, a queue of fills awaiting
        #       collection, the current order, the orders being retired, and the
        #       set of deal keys already booked.
        # Why:  callbacks arrive on Shioaji's thread while the loop reads from its
        #       own; the lock is what makes that safe. `_seen_deals` never shrinks
        #       within a session, which is the point — dedup must hold for the
        #       whole run, and one key per fill is trivial memory.
        self._lock = threading.Lock()
        self._fills: List[FillEvent] = []
        self._live: Optional[_Live] = None
        self._retiring: List[_Live] = []
        self._seen_deals: Set[Tuple[Any, Any]] = set()
        self.anomalies: List[str] = []

        # Tech: every contract code that counts as "ours" — the object's own
        #       `code`, its `target_code` if present, and any code learned from a
        #       callback.
        # Why:  MEASURED 2026-08-10. Ordering through the `TXFR1` alias produces
        #       fills, callbacks and positions stamped with the **resolved**
        #       code `TXFH6`, while the alias object reports `code='TXFR1'`.
        #       Matching positions on the alias code alone therefore filters out
        #       our own position, `broker_position()` returns 0 while the mirror
        #       says 1, and the reconciliation rail halts the loop with a false
        #       desync the moment anything is held. Accepting a *set* of codes,
        #       and widening it from what the venue actually reports, removes the
        #       whole class of alias-vs-concrete mismatch.
        #       Seeded from `live.contracts.contract_codes` so this rule lives in
        #       one place — `v1_check` needs the identical comparison, and two
        #       copies would drift.
        self._codes: Set[str] = set(contract_codes(contract))

        self.attach()

    def switch_contract(self, contract: Any) -> None:
        """Point this broker at a new contract after a roll (§2.6)."""
        # Tech: replace the contract and **rebuild** the accepted-code set from
        #       it, discarding the codes learned for the old month.
        # Why:  `_codes` is what `_held_lots` filters the venue's positions with.
        #       Carrying the expired month's code forward would let a stale
        #       position on a settled contract keep counting toward
        #       `broker_position()`, so the reconciliation rail would compare a
        #       mirror that holds one month against a venue sum across two — a
        #       false desync, and one that resolves itself hours later when the
        #       venue drops the settled leg, which is the worst kind. The roll is
        #       the one moment where forgetting is the correct behaviour.
        self.contract = contract
        self._codes = set(contract_codes(contract))
        if self.recorder is not None:
            self.recorder.record("broker_contract_switched",
                                 code=str(getattr(contract, "code", "?")),
                                 delivery_month=str(
                                     getattr(contract, "delivery_month", "?")),
                                 codes=sorted(self._codes))

    def attach(self) -> None:
        # Tech: clear any existing order callback, then register ours.
        # Why:  MEASURED 2026-08-17 — `login(force_refresh=True)` replaces the
        #       session object and every callback registered against the old one
        #       stops firing (order callbacks went 2 -> 0 across a re-login;
        #       §2.24). So this has to be re-callable, not a one-shot in
        #       `__init__`. Clearing first is the same reasoning as the CLI's
        #       unsubscribe-before-subscribe: if registration ever *appends*
        #       rather than replaces, a repeated repair would deliver every fill
        #       twice, and a double-counted fill corrupts the position mirror and
        #       the PnL together.
        try:
            self.api.clear_order_callback()
        except Exception:                                 # pragma: no cover
            # Tech: a missing or throwing `clear` must not stop the register.
            # Why:  losing the callback entirely is far worse than the duplicate
            #       risk it guards against — without it no fill is ever seen.
            pass
        self.api.set_order_callback(self._on_order_state)

    # Tech: this broker has a real account behind it.
    # Why:  so a None from `broker_position_detail` reads as "the query
    #       failed" rather than "there is no venue" — see `base.Broker`.
    has_venue = True

    # ---- Broker interface --------------------------------------------------

    def submit(self, order: OrderEvent, *, current_price: float) -> None:
        # Tech: translate the OrderEvent into a Shioaji futures order, place it,
        #       and record the returned Trade as the live order.
        # Why:  intent maps to octype explicitly (OPEN→New, CLOSE→Cover) rather
        #       than using `Auto` — the no-flip invariant is built on knowing which
        #       one we meant, and `Auto` would mask a mirror desync instead of
        #       surfacing it (§2.3). LMT+ROD matches the backtest's resting-limit
        #       semantics; §4.4's cancel-on-every-signal is what bounds its life.
        sj = self._sj or _load_sj()
        urgent = self._urgent
        rolling = self._rolling_ticks
        # Tech: three pricings, one order type each — ordinary, FLATTEN, roll leg.
        # Why:  a roll leg is LMT+**IOC** and everything else is LMT+ROD (§2.6).
        #       IOC is right *only* because the ladder retries: §2.19 measured that
        #       an IOC alone expires into nothing, and ROD was chosen for the
        #       flatten precisely because nothing retried it. With a retry the
        #       trade-off inverts — IOC leaves nothing resting, so re-pricing needs
        #       no cancel and §2.2's asynchronous cancel/replace race never
        #       happens. It stays a **bounded limit** either way: the worst fill is
        #       knowable before the order is sent, which is what MKT gives up.
        if rolling is not None:
            price = self.through_price(order.side, order.limit_price, rolling)
        elif urgent:
            price = self.urgent_price(order.side, order.limit_price)
        else:
            price = order.limit_price
        fo = sj.FuturesOrder(
            action=(sj.Action.Buy if order.side == "BUY" else sj.Action.Sell),
            price=price,
            quantity=order.quantity,
            price_type=sj.FuturesPriceType.LMT,
            order_type=(sj.OrderType.IOC if rolling is not None
                        else sj.OrderType.ROD),
            octype=(sj.FuturesOCType.New if order.intent == "OPEN"
                    else sj.FuturesOCType.Cover),
            account=self.account,
        )
        # Tech: an IOC gets a local deadline at submit time.
        # Why:  see `IOC_TTL_SECONDS` — the venue's expiry report is unmeasured,
        #       and a ladder blocked by a pending order that no longer exists is
        #       inert while logging beautifully.
        if rolling is not None:
            self._ioc_deadline = time.monotonic() + IOC_TTL_SECONDS
        trade = self.api.place_order(self.contract, fo)
        with self._lock:
            self._live = _Live(
                order=order,
                trade=trade,
                order_id=_dig(trade, "order", "id"),
                ordno=_dig(trade, "order", "ordno") or None,
                current_at_placement=current_price,
            )
        if self.recorder is not None:
            self.recorder.record(
                "broker_submit", side=order.side, limit_price=order.limit_price,
                sent_price=price, quantity=order.quantity, intent=order.intent,
                # Tech: read the time-in-force back off the order object that was
                #       actually sent, never from a second expression.
                # Why:  it *was* a duplicated conditional, and when the sent value
                #       changed IOC→ROD the log kept saying IOC. A trail that
                #       describes an order the venue never received is worse than
                #       no trail: it is the input to every later diagnosis, and it
                #       cost one here before being caught.
                urgent=urgent,
                rolling_ticks=rolling,
                order_type=str(getattr(fo.order_type, "value", fo.order_type)),
                current_at_placement=current_price,
                order_id=self._live.order_id if self._live else None,
            )

    @contextmanager
    def urgent(self):
        # Tech: mark submits inside this block as urgent, restoring the previous
        #       value on the way out even if the body raises.
        # Why:  the loop opens this scope around a FLATTEN close (§5). The flag is
        #       set and read on the loop thread only — callbacks never touch it —
        #       so it needs no lock. Restoring rather than clearing keeps it
        #       correct if the scope is ever nested.
        previous, self._urgent = self._urgent, True
        try:
            yield
        finally:
            self._urgent = previous

    @contextmanager
    def rolling(self, aggression_ticks: int):
        # Tech: mark submits inside this block as roll legs priced
        #       `aggression_ticks` through the touch, and sent IOC.
        # Why:  the same seam as `urgent()`, for the same reason — set and read on
        #       the loop thread only, restored rather than cleared so nesting
        #       cannot strand the flag. The ladder passes a *different* number on
        #       every attempt, which is what makes escalation possible without the
        #       broker holding ladder state it has no way to reason about.
        previous, self._rolling_ticks = self._rolling_ticks, int(aggression_ticks)
        try:
            yield
        finally:
            self._rolling_ticks = previous

    def through_price(self, side: str, limit_price: float,
                      ticks: float) -> float:
        # Tech: move the limit `ticks` through the touch — up for a BUY, down for
        #       a SELL — snapped to the tick grid.
        # Why:  factored out of `urgent_price` so the FLATTEN and the roll ladder
        #       cannot drift apart on the one thing that actually fills an order.
        #       The direction is what makes it aggressive: a BUY closing a short
        #       must be willing to pay *more*, not less.
        offset = ticks * self.tick_size
        raw = limit_price + offset if side == "BUY" else limit_price - offset
        return round(raw / self.tick_size) * self.tick_size

    def urgent_price(self, side: str, limit_price: float) -> float:
        # Tech: move the limit `flatten_aggression_ticks` through the touch — up
        #       for a BUY, down for a SELL — snapped to the tick grid.
        # Why:  the direction is what makes it aggressive: a BUY that closes a
        #       short must be willing to pay *more*, not less. Rounding (rather
        #       than flooring) keeps the price on the grid without ever landing
        #       back on the passive side, and the result is still a bounded limit —
        #       the worst fill is knowable before the order is sent, which is what
        #       an unbounded MKT gives up.
        return self.through_price(side, limit_price,
                                  self.flatten_aggression_ticks)

    def cancel(self) -> None:
        # Tech: ask the venue to cancel the live order and move it to the retiring
        #       list; it stops counting as pending immediately.
        # Why:  the *request* is what §4.4 requires on every signal, and the loop
        #       must be free to submit a replacement at once — waiting for the
        #       confirmation would stall the decision. The order stays tracked in
        #       `_retiring` because it can still fill in the race window, and that
        #       fill is real: dropping it would lose a live position.
        with self._lock:
            live = self._live
            if live is None or live.dead:
                self._live = None
                return
            live.cancel_requested = True
            self._retiring.append(live)
            self._live = None
        try:
            self.api.cancel_order(live.trade)
        except Exception as exc:                      # pragma: no cover
            self._anomaly(f"cancel_order raised: {exc!r}")

    def has_pending(self) -> bool:
        # Tech: True only while an order is live and not yet fully resolved —
        #       and an IOC past its local deadline is treated as resolved.
        # Why:  retiring orders deliberately do not count: the loop has already
        #       disowned them, and treating them as pending would block the
        #       replacement order §4.4 expects to place immediately. The IOC
        #       deadline is the same argument against a different cause — an IOC
        #       that expired without a report would otherwise block the roll
        #       ladder for the whole window (§2.6). The order is *not* forgotten,
        #       only un-pended: it stays tracked so a fill arriving in the race
        #       window is still booked, exactly as `cancel()` does.
        with self._lock:
            live = self._live
            if live is None or live.dead:
                return False
            if (self._ioc_deadline is not None
                    and time.monotonic() > self._ioc_deadline):
                self._ioc_expired()
                return False
            return True

    def _ioc_expired(self) -> None:
        # Tech: move the timed-out IOC to `_retiring` and record it. Caller holds
        #       the lock.
        # Why:  retiring rather than dropping is what keeps a late fill bookable —
        #       the venue's answer may simply be slower than our deadline, and a
        #       fill we asked for and then forgot would be a position nobody owns.
        live = self._live
        self._live = None
        self._ioc_deadline = None
        if live is not None:
            self._retiring.append(live)
            if self.recorder is not None:
                self.recorder.record(
                    "ioc_timed_out", ttl_seconds=IOC_TTL_SECONDS,
                    order_id=getattr(live, "order_id", None),
                    note="no venue report within the local deadline; "
                         "un-pended so the roll ladder can re-price")

    def pending_order(self) -> Optional[OrderEvent]:
        with self._lock:
            return self._live.order if self._live else None

    def poll_fills(self, market: MarketEvent) -> List[FillEvent]:
        # Tech: drain and return every fill booked since the last call; `market`
        #       is unused.
        # Why:  fills here are reported by the venue, not derived from the incoming
        #       print, so the bar carries no information — the argument exists only
        #       because `SimBroker` genuinely needs it.
        with self._lock:
            out, self._fills = self._fills, []
        return out

    def _held_lots(self) -> Optional[List[Tuple[int, float]]]:
        # Tech: query the venue's positions for our contract and return one
        #       (signed_qty, entry_price) pair per row; None if it cannot answer.
        # Why:  both position accessors read the same rows, and duplicating the
        #       parse is how the two would drift — the code filter especially,
        #       which is what stops the `TXFR1` alias reporting flat while holding
        #       (§2.18b). `price` is the average entry: field verified by
        #       introspecting `sj.FuturePosition` (2026-08-12), not from the docs,
        #       which have now been wrong four times.
        try:
            positions = self.api.list_positions(self.account)
        except Exception as exc:
            self._anomaly(f"list_positions raised: {exc!r}")
            return None
        if positions is None:
            return None
        lots: List[Tuple[int, float]] = []
        for p in positions:
            code = getattr(p, "code", None)
            if code is not None and self._codes and str(code) not in self._codes:
                continue
            qty = int(getattr(p, "quantity", 0))
            direction = str(getattr(p, "direction", "Buy"))
            signed = qty if direction.endswith("Buy") else -qty
            lots.append((signed, float(getattr(p, "price", 0.0) or 0.0)))
        return lots

    def broker_position(self) -> Optional[int]:
        # Tech: sum the venue's signed position for this contract; None if the
        #       query fails or the contract is absent.
        # Why:  the venue is the source of truth the mirror is reconciled against
        #       (§2.9). Called once per bar (1/min), far under the accounting rate
        #       limit of 25 per 5 s. Returning None on error means a transient
        #       query failure degrades to "cannot compare" rather than fabricating
        #       a mismatch and halting a healthy loop.
        lots = self._held_lots()
        if lots is None:
            return None
        return sum(signed for signed, _ in lots)

    def broker_position_detail(self) -> Optional[HeldPosition]:
        # Tech: the same rows as `broker_position`, plus a size-weighted average
        #       entry price across them.
        # Why:  called once at start-up so a restart can adopt what it is already
        #       holding (§2.20). The weighting is defined over *all* rows, which
        #       is only meaningful when they sit on one side — a netted futures
        #       account gives at most one row per side, and adoption refuses
        #       anything above `max_position` anyway, so the ambiguous case cannot
        #       reach the Trader.
        lots = self._held_lots()
        if lots is None:
            return None
        total = sum(signed for signed, _ in lots)
        gross = sum(abs(signed) for signed, _ in lots)
        if total == 0 or gross == 0:
            return HeldPosition(0, 0.0)
        avg = sum(abs(signed) * price for signed, price in lots) / gross
        return HeldPosition(total, avg)

    # ---- callback handling (Shioaji's thread) ------------------------------

    def _on_order_state(self, state: Any, msg: Dict[str, Any]) -> None:
        # Tech: dispatch on the callback's state name; anything unrecognised is an
        #       anomaly rather than a no-op.
        # Why:  matching on the enum *name* (`FuturesOrder` / `FuturesDeal`) is what
        #       the docs display; the underlying literals are unverified until first
        #       contact with the real API. Treating an unknown state as an anomaly
        #       means a wrong guess shows up loudly on day one instead of silently
        #       discarding every fill.
        kind = str(getattr(state, "name", None) or state).upper()
        try:
            # Tech: test for DEAL before ORDER, case-insensitively.
            # Why:  MEASURED — the enum is `OrderState.FuturesDeal` with name
            #       'FuturesDeal' and value 'FDEAL'. Upper-casing makes all three
            #       spellings (name, value, str) match. The DEAL-first ordering is
            #       load-bearing, not stylistic: `str(state)` is
            #       "OrderState.FuturesDeal", which contains ORDER as well — so
            #       testing ORDER first would route every deal into the order
            #       handler and silently drop every fill.
            if "DEAL" in kind:
                self._handle_deal(msg)
            elif "ORDER" in kind:
                self._handle_order(msg)
            else:
                self._anomaly(f"unrecognised order-callback state: {kind!r}")
        except Exception as exc:                      # pragma: no cover
            self._anomaly(f"order callback raised: {exc!r}")

    def _handle_order(self, msg: Dict[str, Any]) -> None:
        # Tech: read the operation block; a non-"00" code is a rejection, a
        #       successful New records the venue order number, a successful Cancel
        #       retires the order once nothing more can fill.
        # Why:  op_code is the venue's accept/reject verdict — anything but "00"
        #       must stop the loop for review rather than be retried, since a retry
        #       loop is how a rejection becomes a suspension (§2.8b).
        self._learn_code((msg.get("contract") or {}).get("code"))
        operation = msg.get("operation") or {}
        op_code = str(operation.get("op_code", "00"))
        op_type = str(operation.get("op_type", ""))
        order = msg.get("order") or {}
        ordno = order.get("ordno")
        oid = order.get("id")

        if op_code != "00":
            self._anomaly(
                f"order rejected: op_type={op_type} op_code={op_code} "
                f"{operation.get('op_msg', '')}".strip()
            )
            if self.rails is not None:
                self.rails.on_order_report(
                    op_code=op_code, op_msg=str(operation.get("op_msg", ""))
                )
            with self._lock:
                live = self._match(oid, ordno)
                if live is not None:
                    live.dead = True
                    self._forget(live)
            return

        with self._lock:
            live = self._match(oid, ordno)
            if live is None:
                return
            if ordno:
                live.ordno = ordno
            if op_type == "Cancel":
                # Tech: a confirmed cancel kills the order only when it cannot
                #       fill further.
                # Why:  a partially filled order that is then cancelled leaves a
                #       real position behind; marking it dead is about refusing
                #       *future* deals, not about erasing the ones already booked.
                live.dead = True
                self._forget(live)

    def _handle_deal(self, msg: Dict[str, Any]) -> None:
        # Tech: dedup on (ordno, exchange_seq), correlate to a tracked order,
        #       bound the fill against that order's quantity, then queue a
        #       FillEvent.
        # Why:  each guard covers a documented or plausible venue behaviour —
        #       duplicate reports, deal-before-order, and a stray deal after the
        #       order is done. The quantity bound is the backstop that stops any
        #       of them from inventing contracts we do not hold.
        self._learn_code(msg.get("code"))
        ordno = msg.get("ordno")
        seq = msg.get("exchange_seq")
        key = (ordno, seq)

        with self._lock:
            if key in self._seen_deals:
                return                                  # duplicate report
            live = self._match(msg.get("trade_id"), ordno)
            if live is None:
                self._anomaly(
                    f"deal for an unknown order: ordno={ordno!r} "
                    f"trade_id={msg.get('trade_id')!r} seq={seq!r}"
                )
                return
            qty = int(msg.get("quantity", 0))
            if qty <= 0:
                self._anomaly(f"deal with non-positive quantity: {qty!r}")
                return
            if live.filled + qty > live.order.quantity:
                self._anomaly(
                    f"deal overfills order: {live.filled}+{qty} > "
                    f"{live.order.quantity} (ordno={ordno!r})"
                )
                return

            self._seen_deals.add(key)
            live.filled += qty
            price = float(msg.get("price"))
            action = str(msg.get("action", ""))
            side = "BUY" if action.endswith("Buy") else "SELL"
            fill = FillEvent(
                timestamp=msg.get("ts"),
                side=side,
                fill_price=price,
                quantity=qty,
                fee=price * self.fee_rate * qty,
            )
            self._fills.append(fill)
            if live.filled >= live.order.quantity:
                live.dead = True
                self._forget(live)

        if self.recorder is not None:
            self.recorder.record(
                "broker_deal", ordno=ordno, exchange_seq=seq, side=side,
                price=price, quantity=qty,
                current_at_placement=live.current_at_placement,
            )

    # ---- helpers (call under the lock) -------------------------------------

    def _match(self, oid: Any, ordno: Any) -> Optional[_Live]:
        # Tech: find a tracked order by venue order number or by the id returned
        #       at placement, searching the live order then the retiring ones.
        # Why:  two keys because a deal may arrive before the order event that
        #       would have taught us `ordno` — `trade_id`/`id` is the only handle
        #       available in that window. Retiring orders are searched too, since
        #       a cancel that lost the race still produces a real fill.
        candidates = ([self._live] if self._live else []) + self._retiring
        for live in candidates:
            if ordno and live.ordno and live.ordno == ordno:
                return live
            if oid and live.order_id and live.order_id == oid:
                return live
        # Fall back to the sole live order when neither key is known yet.
        if self._live is not None and self._live.ordno is None and not ordno:
            return self._live
        return None

    def _learn_code(self, code: Any) -> None:
        # Tech: widen the accepted-code set with a code the venue reported.
        # Why:  the alias object may not expose `target_code`, so the concrete
        #       contract (`TXFH6` behind `TXFR1`) is sometimes only knowable from
        #       traffic. Learning it the first time an order or deal is reported
        #       means position reconciliation matches from then on. Widening is
        #       safe: we only ever trade the one contract this broker was
        #       constructed for, so anything the venue attributes to our own
        #       orders is by definition ours.
        if code:
            self._codes.add(str(code))

    def _forget(self, live: _Live) -> None:
        if live in self._retiring:
            self._retiring.remove(live)
        if self._live is live:
            self._live = None

    def _anomaly(self, message: str) -> None:
        # Tech: record the anomaly, notify the optional hook, and halt the rails.
        # Why:  every branch that reaches here represents a message we could not
        #       account for. Losing one silently means losing track of a position,
        #       so the default is to stop the loop and let a human look (§2.9).
        self.anomalies.append(message)
        if self.recorder is not None:
            self.recorder.record("broker_anomaly", message=message)
        if self._on_anomaly is not None:
            self._on_anomaly(message)
        if self.rails is not None:
            from ..state import HaltAction
            self.rails.halt(f"broker anomaly: {message}", HaltAction.STOP)


def _dig(obj: Any, *path: str) -> Any:
    # Tech: walk attribute-or-key access down a path, returning None on a miss.
    # Why:  Shioaji's Trade is an object while the callback payloads are dicts;
    #       one accessor keeps the adapter from caring which it was handed.
    cur = obj
    for name in path:
        if cur is None:
            return None
        cur = getattr(cur, name, None) if not isinstance(cur, dict) else cur.get(name)
    return cur
