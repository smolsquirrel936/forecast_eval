"""The deployment's end of the wire to the owning process (§18.2).

`ShioajiBroker` talks to a venue. This talks to the **owner**, which talks to the
venue on behalf of all three deployments. It implements the same six-method
`Broker` interface, so `Trader` and `LiveLoop` are unchanged — the swap happens
at `cli.py`'s single broker construction site.

**Why an owner at all.** MEASURED (§14): deals are delivered per session but the
position book is **shared**, so three deployments on one account see each other's
positions and none of each other's fills. One process owning the session gives
one book, one `_match`, and no foreign deals.

**What this class is responsible for, and what it is not.** It serialises intents
and drains events. It does **not** decide `octype`, size a crossing order, or
know the account's net — those belong to the owner, which is the only thing that
can see all three deployments (§18.9: `cover_qty = min(intent, |held|)`, then
`New` for the remainder). This end knows only its own orders and its own fills,
which is exactly what `Trader` already assumes.

**Silence is not consent.** The transport is files, so a dead owner looks like a
quiet one (`intents.py`). If the owner has not spoken within `owner_timeout`,
this broker stops answering questions about the venue and raises the anomaly that
halts the rails — a deployment that cannot reach the owner must not keep trading
against a book it can no longer see (§18.3 item 6).
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Callable, Iterator, List, Optional

from ...events import FillEvent, MarketEvent, OrderEvent
from ..intents import JsonlStream, event_path, intent_path
from .base import Broker, HeldPosition

# Tech: how long the owner may stay silent before this deployment treats it as
#       gone.
# Why:  the owner heartbeats far more often than a bar, so anything approaching a
#       bar's worth of silence is already abnormal. 90 s is comfortably longer
#       than any single owner operation (a fill took up to 12.8 s to arrive,
#       §18.10) and comfortably shorter than the time it takes a stale book to
#       matter. It is generous on purpose: a false "owner is gone" halts a
#       healthy deployment, which is its own kind of outage.
DEFAULT_OWNER_TIMEOUT = 90.0

# Tech: how often a running deployment tells the owner it is alive.
# Why:  the owner reports a deployment silent for `DEFAULT_DEPLOYMENT_TIMEOUT`
#       (180 s). Until 2026-10-07 nothing called `heartbeat()`, so the owner
#       timed a deployment from its last ORDER, and fullday was "silent for
#       180s" every 30 s from the moment its 10-06 BUY went quiet. 30 s gives
#       six beats per timeout: one late or lost beat never reads as gone.
DEFAULT_HEARTBEAT_SECONDS = 30.0


class ClientBroker(Broker):
    """Submits intents to the owning process; receives fills back."""

    # Tech: there IS a venue behind the owner, so a None from
    #       `broker_position()` means "the query failed", not "there is nothing
    #       to ask".
    # Why:  `base.Broker` gives those two meanings opposite handling — an offline
    #       broker's None must SKIP the reconciliation, a failed query must
    #       REFUSE. An unreachable owner is the second: the account exists and is
    #       holding something, we simply cannot see it.
    has_venue = True

    def __init__(
        self,
        directory: Any,
        slug: str,
        *,
        rails: Any = None,
        on_anomaly: Optional[Callable[[str], None]] = None,
        owner_timeout: float = DEFAULT_OWNER_TIMEOUT,
        heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
        contract_code: Optional[str] = None,
    ) -> None:
        # Tech: open both halves of the wire, resume the outbound sequence past
        #       anything already on disk, and hold the optional observers.
        # Why:  `resume_seq` matters on a restart — reissuing sequence numbers
        #       the owner has already seen would make a continuation look like a
        #       duplicate stream (`intents.py`). The rails/on_anomaly pair is the
        #       same shape `ShioajiBroker` takes, so both brokers halt a run the
        #       same way rather than inventing a second mechanism.
        self.slug = slug
        self._out = JsonlStream(intent_path(directory, slug))
        self._in = JsonlStream(event_path(directory, slug))
        self._out.resume_seq()
        self.rails = rails
        self.on_anomaly = on_anomaly
        self.owner_timeout = owner_timeout
        self.heartbeat_seconds = heartbeat_seconds
        self._last_beat: Optional[float] = None
        # Tech: the dated month this deployment trades, stamped on every submit.
        # Why:  2026-10-20 (simtrade_operations §11) — the owner placed night's
        #       and fullday's roll re-opens on October because it only knew the
        #       month IT had resolved at start-up. Deployments roll at different
        #       instants, so the month an order is for is the deployment's to
        #       say. None sends no field, which an owner reads as its own month.
        self.contract_code = contract_code

        # Tech: this deployment's view of its own working order, its undelivered
        #       fills, and the last position the owner reported for it.
        # Why:  `Trader` asks `has_pending()` before every decision and the loop
        #       drains `poll_fills` every bar, so both must be answerable without
        #       a round trip. `_venue_position` is a cache of the owner's last
        #       word rather than a live query for the same reason: the wire is
        #       asynchronous, and a bar cannot block waiting for an answer.
        self._pending: Optional[OrderEvent] = None
        self._fills: List[FillEvent] = []
        self._venue_position: Optional[int] = None
        self._venue_detail: Optional[HeldPosition] = None
        self._urgent = False
        self.skipped_events = self._skip_history()

    def _skip_history(self) -> int:
        # Tech: consume every event already on disk, apply only the newest
        #       `position`, and return how many were skipped.
        # Why:  a fresh stream reads from byte 0, so a restarted deployment would
        #       re-book every `fill` the owner ever sent it — on top of the
        #       ledger replay `--resume` already does, doubling the position —
        #       and halt on any old `rejected`. Its history is the ledger's job.
        #       The last `position` is kept because `adopt_position` asks for it
        #       before the owner's next once-a-second report; liveness reads the
        #       file's newest `ts` (`_owner_is_gone`), so a stale report still
        #       reads as a silent owner. A corrupt line halts here, as `_drain`
        #       would: `read_new` has already moved past it, so swallowing the
        #       error would lose the only report of it.
        try:
            history = self._in.read_new()
        except ValueError as exc:
            self._anomaly(str(exc))
            return 0
        positions = [e for e in history if e.get("kind") == "position"]
        if positions:
            self._apply_position(positions[-1])
        return len(history)

    # ---- Broker interface --------------------------------------------------

    def submit(self, order: OrderEvent, *, current_price: float) -> None:
        # Tech: write a `submit` intent carrying the order and the price observed
        #       at submission, and record it as this deployment's working order.
        # Why:  the signature matches `Execution.submit` exactly, so `Trader`
        #       drives this and the offline sim without a shim. `current_price`
        #       travels because the owner prices a FLATTEN or a roll leg through
        #       the touch and needs the reference the deployment saw. `intent` is
        #       carried rather than an `octype`: OPEN/CLOSE is what the deployment
        #       means, and only the owner knows what that becomes against the
        #       account's net (§18.9).
        self._out.append(
            "submit",
            side=order.side,
            limit_price=order.limit_price,
            quantity=order.quantity,
            intent=order.intent,
            timestamp=order.timestamp.isoformat(),
            current_price=current_price,
            urgent=self._urgent,
            **({"contract": self.contract_code} if self.contract_code else {}),
        )
        self._pending = order

    def switch_contract(self, contract: Any) -> None:
        """Stamp later intents with a new month after a roll (§2.6)."""
        # Tech: replace the stamped code with the new contract's dated code.
        # Why:  same role as `ShioajiBroker.switch_contract` — the roll moves
        #       every holder of the contract in one place (`cli._apply_roll`).
        #       Here the holder is the field the owner places by.
        from ..contracts import month_code
        self.contract_code = month_code(contract) or None

    def cancel(self) -> None:
        # Tech: write a `cancel` intent; leave `_pending` set.
        # Why:  `base.Broker` is explicit that a live cancel is asynchronous, so
        #       this means "cancel requested" and `has_pending()` stays True until
        #       the owner confirms. Clearing it here would let §4.4's
        #       cancel-on-every-signal issue a second order while the first is
        #       still working at the venue.
        if self._pending is not None:
            self._out.append("cancel")

    def has_pending(self) -> bool:
        return self._pending is not None

    def pending_order(self) -> Optional[OrderEvent]:
        return self._pending

    def poll_fills(self, market: MarketEvent) -> List[FillEvent]:
        # Tech: drain the wire first, then hand over whatever fills arrived and
        #       forget them.
        # Why:  draining rather than peeking is the contract in `base.Broker`.
        #       `market` is ignored — the offline sim resolves a fill *from* the
        #       incoming print, a live broker merely reports what already
        #       happened, and one signature serves both.
        self._drain()
        out, self._fills = self._fills, []
        return out

    @contextmanager
    def urgent(self) -> Iterator[None]:
        # Tech: mark intents written inside the block as urgent.
        # Why:  a FLATTEN halt must actually close, and the submit is issued by
        #       `Trader.submit_exit`, which is not ours to change. The flag rides
        #       on the intent so the *owner* prices it through the touch — the
        #       aggression belongs where the order is actually placed. A context
        #       manager so an exception mid-submit cannot leave it stuck on.
        self._urgent = True
        try:
            yield
        finally:
            self._urgent = False

    def broker_position(self) -> Optional[int]:
        # Tech: drain, then return the owner's last reported net for this
        #       deployment — or None if the owner has gone quiet.
        # Why:  §16/§18.4 — the rail compares what we believe against what the
        #       account holds, and only the owner can say. None when the owner is
        #       silent is the same degradation `ShioajiBroker` uses when the venue
        #       will not answer: refuse to compare rather than compare against a
        #       stale number, which would halt on a disagreement that is our own
        #       staleness rather than a real desync.
        self._drain()
        if self._owner_is_gone():
            return None
        return self._venue_position

    def broker_position_detail(self) -> Optional[HeldPosition]:
        # Tech: the same answer with the average entry price the owner reported.
        # Why:  `adopt_position` needs "at what price" as well as "how much"
        #       (`base.HeldPosition`) — a resumed run that invented an entry would
        #       book a fictional number on the closing fill.
        self._drain()
        if self._owner_is_gone():
            return None
        return self._venue_detail

    # ---- the wire ----------------------------------------------------------

    def _drain(self) -> None:
        # Tech: read every event the owner has written since last time and apply
        #       it; a corrupt record propagates as an anomaly rather than an
        #       exception out of the bar loop.
        # Why:  `JsonlStream.read_new` raises on a complete-but-unparseable line
        #       because dropping a record would drop a fill (`intents.py`). That
        #       is the right behaviour for the wire and the wrong shape for the
        #       loop, which needs a halt rather than a traceback — so it is
        #       translated here, at the boundary, exactly once.
        try:
            events = self._in.read_new()
        except ValueError as exc:
            self._anomaly(str(exc))
            return
        for event in events:
            self._apply(event)

    def _apply(self, event: dict) -> None:
        # Tech: dispatch one event by kind.
        # Why:  the four kinds are the whole protocol this end understands. An
        #       unknown kind is ignored rather than fatal, so the owner can add a
        #       message type without every deployment needing to be redeployed
        #       first; a *malformed* known kind is an anomaly, because that is a
        #       bug rather than a version skew.
        kind = event.get("kind")
        if kind == "fill":
            self._apply_fill(event)
        elif kind == "done":
            # Tech: the owner reports the working order is finished — filled,
            #       cancelled or rejected.
            # Why:  this is the only thing that clears `_pending`, which is why
            #       `cancel()` does not. The owner owns the order's lifetime
            #       because the owner is what talks to the venue.
            self._pending = None
        elif kind == "position":
            self._apply_position(event)
        elif kind == "rejected":
            self._pending = None
            self._anomaly(f"owner rejected the order: {event.get('reason')!r}")

    def _apply_fill(self, event: dict) -> None:
        # Tech: turn a fill event into a `FillEvent` and queue it for the loop.
        # Why:  the deployment books only its OWN fills, which is correct because
        #       the owner routes each deal to the one deployment whose order it
        #       matched (§18.3 item 2). Partial fills arrive as several events —
        #       MEASURED (§18.9), a 2-lot order filled as two 1-lot deals nine
        #       seconds apart — so this appends rather than replaces, and
        #       `_pending` is cleared by `done`, never by the arrival of a fill.
        import pandas as pd
        try:
            self._fills.append(FillEvent(
                timestamp=pd.Timestamp(event["timestamp"]),
                side=event["side"],
                fill_price=float(event["fill_price"]),
                quantity=int(event["quantity"]),
                fee=float(event.get("fee", 0.0)),
            ))
        except (KeyError, TypeError, ValueError) as exc:
            self._anomaly(f"malformed fill event: {event!r} ({exc!r})")

    def _apply_position(self, event: dict) -> None:
        # Tech: cache the owner's report of what THIS deployment holds.
        # Why:  the owner sends the deployment's own signed size, not the
        #       account's — §16's operand question settled inside the one process
        #       that can answer it. `avg_price` is optional because a flat
        #       position has none.
        try:
            signed = int(event["signed_qty"])
        except (KeyError, TypeError, ValueError) as exc:
            self._anomaly(f"malformed position event: {event!r} ({exc!r})")
            return
        self._venue_position = signed
        price = event.get("avg_price")
        self._venue_detail = (
            None if signed == 0 or price is None
            else HeldPosition(signed_qty=signed, avg_price=float(price)))

    def heartbeat(self) -> None:
        # Tech: tell the owner this deployment is still alive.
        # Why:  the owner has the same silence problem in reverse — a deployment
        #       that died holding a position is not one whose intents should be
        #       waited for. Separate from `submit` so a deployment that is flat
        #       and quiet still counts as present; `maybe_heartbeat` paces it.
        self._out.append("heartbeat")

    def maybe_heartbeat(self, now: float) -> bool:
        """Write a heartbeat if `heartbeat_seconds` have passed since the last."""
        # Tech: beat on the first call, then whenever `now` (seconds, any
        #       monotonic-enough clock) is `heartbeat_seconds` past the last
        #       beat. Returns whether it wrote.
        # Why:  `live/cli.py` calls this from its once-a-second clock block,
        #       which runs inside and outside sessions — so the owner hears a
        #       live deployment through the dead hours too, and only a process
        #       that stopped (crashed, halted, killed) goes quiet. Pacing lives
        #       here, not in the loop, so it is testable without one.
        if (self._last_beat is not None
                and now - self._last_beat < self.heartbeat_seconds):
            return False
        self.heartbeat()
        self._last_beat = now
        return True

    def _owner_is_gone(self) -> bool:
        # Tech: True when the owner has never spoken, or not within the timeout.
        # Why:  both are "we cannot see the book". Never having spoken is the
        #       start-up case and must not read as fresh — `age_seconds()`
        #       returns None for it precisely so the two cannot be confused
        #       (`intents.py`). Raising the anomaly here rather than at the call
        #       sites keeps the halt on the one condition, however it is noticed.
        age = self._in.age_seconds()
        if age is None:
            self._anomaly("owner has not spoken at all; cannot see the book")
            return True
        if age > self.owner_timeout:
            self._anomaly(
                f"owner silent for {age:.0f}s (limit {self.owner_timeout:.0f}s); "
                f"cannot see the book")
            return True
        return False

    def _anomaly(self, reason: str) -> None:
        # Tech: report to whichever observers were supplied, halting the rails.
        # Why:  identical to `ShioajiBroker._anomaly`. A broker that notices
        #       something impossible must stop the run rather than log and carry
        #       on, and using the same mechanism means one place to look when a
        #       live run halts.
        if self.on_anomaly is not None:
            self.on_anomaly(reason)
        if self.rails is not None:
            from ..state import HaltAction
            self.rails.halt(f"client broker: {reason}", HaltAction.STOP)
