"""The owning process — one session, one account, three deployments (§18.2).

[owner.py](owner.py) is the accounting and holds no session. This is the half
that touches the venue: it registers the single order callback, places the legs
`plan_legs` decided on, drains three `intents_<slug>.jsonl`, and writes back
three `events_<slug>.jsonl`.

**One callback, one `_match`, no foreign deals.** MEASURED (§14): deals are
delivered per session and the position book is shared, so three deployments
placing their own orders see each other's positions and none of each other's
fills. With one owner there is one session, and every deal it receives belongs to
an order it placed — §7.2 dissolves rather than needing a fix.

**Two reconciliations, two operands, and they are not the same check.**

* here: `OrderBook.account_net` against `list_positions` — does the venue agree
  with the sum of everything we have booked? A disagreement is a real desync.
* at each deployment: its `Trader.position` against `OrderBook.net_for(slug)`,
  sent as a `position` event. Both sides are "the fills this deployment had", so
  a disagreement means an event was lost on the wire.

§16 moved the first comparison onto the ledger sum because no single deployment
could make it. With an owner it is made in the one process that can, in memory,
which is also what retires §16's freshness race.

**`shioaji` is imported lazily and can be substituted**, so this module and its
tests run with the package absent — the same arrangement `ShioajiBroker` uses.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from datetime import datetime, timedelta
from typing import Any, Callable, Deque, Dict, Iterable, List, Optional, Tuple

import pandas as pd

from ..events import OrderEvent
from .intents import JsonlStream, event_path, intent_path
from .owner import COVER, Leg, OrderBook, plan_legs

# Tech: how long a deployment may stay silent before the owner stops expecting
#       intents from it.
# Why:  the mirror image of `ClientBroker`'s owner timeout, and generous for the
#       same reason — a deployment that is flat and quiet still heartbeats every
#       30 s (`ClientBroker.maybe_heartbeat`), so 180 s is six missed beats, but
#       a false "it is gone"
#       must not strand a position nobody is managing. Nothing is closed on this
#       signal: it is reported, because deciding what to do about an absent
#       deployment holding a lot is a risk call, not a mechanical one.
DEFAULT_DEPLOYMENT_TIMEOUT = 180.0

# Tech: how long a venue-vs-book difference that orders in flight can explain
#       may last before the running reconcile calls it a failure.
# Why:  MEASURED 2026-10-06 (simtrade_0929): order 14AD46 filled at 09:14:04.5,
#       the reconcile at ~09:14:07 read the venue at +1 against a book still at
#       0, and the owner stopped before the deal callback arrived — stranding a
#       lot no ledger held. `list_positions` catches up in ~0.1 s (§18.10) but
#       deal callbacks have taken 12 s and 42 s to book (§23.7), so any check
#       inside that window sees the venue ahead. 120 s is about 3x the slowest
#       booking seen; an unexplainable difference still fails at once.
DEFAULT_RECONCILE_GRACE = 120.0

# Tech: the venue clock's offset from UTC, added to a deal's epoch `ts`.
# Why:  MEASURED 2026-10-08 (simtrade_0929): a deal's `ts` is a UTC epoch, while
#       every other timestamp here — bars, orders, ticks — is naive Taipei time.
#       Unconverted, night's SELL ordered at 22:15 was booked at 14:16:52, between
#       sessions and eight hours before its own order, which mislabels the
#       trade's session through `classify_session`. TAIFEX is Asia/Taipei, UTC+8
#       all year with no DST, so the offset is a constant — the same one
#       `tools/session_artifacts.VENUE_UTC_OFFSET` applies to recorded deals.
VENUE_UTC_OFFSET = timedelta(hours=8)


def _load_sj() -> Any:
    # Tech: import `shioaji` on first use.
    # Why:  keeps the import off module load, so the live package imports and its
    #       tests run on a machine without the package.
    import shioaji as sj
    return sj


def _dig(obj: Any, *path: str) -> Any:
    # Tech: walk attribute-or-key access down a path, returning None on a miss.
    # Why:  Shioaji's `Trade` is an object while the callback payloads are dicts;
    #       one accessor keeps this from caring which it was handed. Same helper
    #       `ShioajiBroker` carries, for the same reason.
    cur = obj
    for name in path:
        if cur is None:
            return None
        cur = getattr(cur, name, None) if not isinstance(cur, dict) else cur.get(name)
    return cur


class OwnerProcess:
    """Places every order for every deployment, and routes every deal back."""

    def __init__(
        self,
        api: Any,
        contract: Any,
        *,
        directory: Any,
        slugs: Iterable[str],
        account: Any = None,
        sj: Any = None,
        book: Optional[OrderBook] = None,
        fee_rate: float = 0.0002,
        tick_size: float = 1.0,
        flatten_aggression_ticks: int = 5,
        deployment_timeout: float = DEFAULT_DEPLOYMENT_TIMEOUT,
        reconcile_grace: float = DEFAULT_RECONCILE_GRACE,
        clock: Callable[[], float] = time.monotonic,
        recorder: Any = None,
        on_anomaly: Optional[Callable[[str], None]] = None,
        resolve: Optional[Callable[[str], Any]] = None,
        wall_clock: Callable[[], datetime] = datetime.now,
    ) -> None:
        # Tech: hold the injected session objects, open both halves of the wire
        #       for every deployment, and resume each outbound sequence.
        # Why:  `api` is injected rather than constructed so login stays outside
        #       this class and a fake can be substituted wholesale — the same
        #       reason `ShioajiBroker` takes one. `resume_seq` matters on a
        #       restart: reissuing numbers a deployment has already seen would
        #       make a continuation look like a duplicate stream (`intents.py`).
        self.api = api
        self.contract = contract
        # Tech: every month this owner can place on, keyed by dated code, seeded
        #       with the start-up contract; `resolve` adds one on first use.
        # Why:  2026-10-20 (simtrade_operations §11) — the owner resolved
        #       `--contract` once and placed every order on it, so night's and
        #       fullday's roll re-opens went to October while both deployments
        #       had rolled to November. Deployments roll at different instants
        #       (day and fullday 12:45, night 15:30), so between them the
        #       account holds two months and no single owner contract is right;
        #       each submit names its month (`ClientBroker.contract_code`).
        from .contracts import month_code
        self.code = month_code(contract)
        self._contracts: Dict[str, Any] = {self.code: contract}
        self._resolve = resolve
        self.account = account if account is not None else getattr(
            api, "futopt_account", None)
        self._sj = sj
        self.slugs = list(slugs)
        self.fee_rate = fee_rate
        self.tick_size = tick_size
        self.flatten_aggression_ticks = flatten_aggression_ticks
        self.deployment_timeout = deployment_timeout
        # Tech: the reconcile's grace state — when the current in-flight
        #       difference was first seen, the deals booked recently, the line
        #       the CLI prints while waiting, and the freeze reason.
        # Why:  see `reconcile` and `freeze`. `_recent` is a deque because the
        #       venue's callback thread appends while the main loop prunes and
        #       reads; append/popleft are atomic, and a reader takes a snapshot.
        self.reconcile_grace = reconcile_grace
        self._clock = clock
        # Tech: Taipei wall time, for telling which months have expired.
        # Why:  expiry is a calendar instant (`roll.month_expired`), which the
        #       monotonic `clock` cannot express. Injected so a test can stand
        #       on either side of one.
        self._wall_clock = wall_clock
        self._mismatch_since: Optional[float] = None
        self._recent: Deque[Tuple[float, int, str]] = deque()
        self.reconcile_note: Optional[str] = None
        self.frozen: Optional[str] = None
        # Tech: the observers are assigned BEFORE the book is constructed.
        # Why:  the default book is given `self._anomaly` as its callback, and
        #       that method reads `self.anomalies` and `self.recorder`. Building
        #       the book first would leave a live callback pointing at attributes
        #       that do not exist yet — which only fails on the first anomaly,
        #       i.e. exactly when it is least welcome.
        self.recorder = recorder
        self.on_anomaly = on_anomaly
        self.anomalies: List[str] = []
        # Tech: how many callbacks of each kind this process has received.
        # Why:  see `_on_event` — §23.6. Written from the venue's thread and read
        #       from the main one; a dict of small ints under the GIL is enough
        #       for a diagnostic that only ever needs to distinguish 2 from 4.
        self.events_seen: Dict[str, int] = {}
        self.book = book if book is not None else OrderBook(
            on_anomaly=self._anomaly, recorder=recorder,
            default_code=self.code)

        self._in: Dict[str, JsonlStream] = {
            slug: JsonlStream(intent_path(directory, slug))
            for slug in self.slugs}
        self._out: Dict[str, JsonlStream] = {
            slug: JsonlStream(event_path(directory, slug))
            for slug in self.slugs}
        for stream in self._out.values():
            stream.resume_seq()
        # Tech: consume every intent already on disk without acting on it, and
        #       keep the count per deployment.
        # Why:  a fresh stream reads from byte 0, so a restarted owner used to
        #       place every intent a deployment had EVER sent — after 2026-10-06
        #       that would have re-sent fullday's 09:13 BUY on the first pass.
        #       An intent the owner did not act on while it was running is stale:
        #       its deployment has halted on the silence (`ClientBroker`) and
        #       re-decides on `--resume`. A corrupt history line is reported as
        #       the same anomaly `poll` raises: `read_new` has already moved
        #       past it, so swallowing the error would lose the only report.
        self.skipped_intents: Dict[str, int] = {}
        for slug, stream in self._in.items():
            try:
                self.skipped_intents[slug] = len(stream.read_new())
            except ValueError as exc:
                self.skipped_intents[slug] = 0
                self._anomaly(f"{slug}: {exc}")
        if self.recorder is not None:
            self.recorder.record("owner_intents_skipped",
                                 skipped=dict(self.skipped_intents))
        # Tech: one lock over every outbound stream.
        # Why:  fills are written from the venue's callback thread while
        #       positions and heartbeats are written from the owner's own loop.
        #       Per-file locks would be finer, but the writes are short and the
        #       cost of getting one wrong is an interleaved line — which
        #       `JsonlStream`'s reader would correctly refuse to parse, halting a
        #       deployment for no reason.
        self._out_lock = threading.Lock()

    # ---- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        # Tech: register the one order callback and announce a starting position
        #       to every deployment.
        # Why:  a `ClientBroker` treats a stream that has never spoken as "cannot
        #       see the book" and halts — deliberately, so start-up is not
        #       mistaken for a flat account. The opening `position` event is what
        #       lets a deployment begin.
        self.attach()
        self.publish_positions()

    def attach(self) -> None:
        # Tech: (re-)register the owner's one order callback on the api handle.
        # Why:  callbacks are bound to the connection object (§2.24, MEASURED
        #       2026-08-17): a `force_refresh=True` re-login drops them, so the
        #       owner's re-login must re-attach or it keeps placing orders while
        #       hearing none of their deals. Split out of `start` because a
        #       re-login must not re-announce positions.
        self.api.set_order_callback(self._on_event)

    def seed_from(self, nets: Dict[str, Any]) -> None:
        # Tech: seed each deployment's net, by month, and the account total from
        #       a mapping `{slug: held}` or `{slug: {code: held}}`.
        # Why:  a restart adopts from §16's per-deployment ledgers
        #       (`owner_cli.seed_positions`), and the account is their sum by
        #       construction. Seeding rather than replaying fills is what keeps a
        #       restart from booking phantom trades into three Portfolios.
        months: Dict[Tuple[str, str], int] = {}
        for slug, held in nets.items():
            # Tech: a held value with no month, and a month of `''`, go to the
            #       start-up month.
            # Why:  ledger rows from before the `contract` column carry no
            #       month, and for those `--contract` is still the assumption a
            #       restart makes (simtrade_operations §6.5). A wrong one is
            #       refused by the start-up reconcile rather than traded on.
            by_code = held if isinstance(held, dict) else {"": held}
            slug_net, entry = 0, 0.0
            for code, lot in by_code.items():
                code = code or self.code
                # Tech: accept a bare signed size or anything carrying
                #       `signed_qty`/`avg_price`.
                # Why:  `ledger.open_position` already returns the second shape,
                #       so the restart path hands its answer straight in — and a
                #       bare int stays usable for a seed where no entry is known.
                net = int(getattr(lot, "signed_qty", lot))
                if net and self._expired(code):
                    # Tech: a lot on an expired month is not seeded.
                    # Why:  the exchange settled it at expiry and no deal said
                    #       so, so a ledger whose deployment was not running to
                    #       write the settlement still holds it.
                    if self.recorder is not None:
                        self.recorder.record("owner_seed_expired", slug=slug,
                                             code=code, net=net)
                    continue
                if net:
                    entry = float(getattr(lot, "avg_price", 0.0) or 0.0)
                months[(slug, code)] = months.get((slug, code), 0) + net
                slug_net += net
            self.book.seed_slug_net(slug, slug_net)
            self.book.seed_slug_entry(slug, entry if slug_net else 0.0)
        self.book.seed_months(months)

    # ---- the intent side ---------------------------------------------------

    def poll(self) -> None:
        """One pass: act on everything the deployments have said, then report."""
        # Tech: drain each deployment's intent stream in turn, then publish.
        # Why:  publishing after acting means a deployment's next bar sees the
        #       consequences of its own last intent rather than the state before
        #       it. Draining per slug rather than merging keeps one deployment's
        #       malformed stream from stalling the others.
        for slug in self.slugs:
            try:
                intents = self._in[slug].read_new()
            except ValueError as exc:
                self._anomaly(f"{slug}: {exc}")
                continue
            for intent in intents:
                self._act(slug, intent)
        self.publish_positions()

    def _act(self, slug: str, intent: Dict[str, Any]) -> None:
        # Tech: dispatch one intent by kind.
        # Why:  `heartbeat` is deliberately a no-op here — its whole content is
        #       the timestamp `JsonlStream.age_seconds` reads, so receiving it is
        #       the effect. An unknown kind is ignored rather than fatal, so a
        #       newer deployment can speak to an older owner.
        kind = intent.get("kind")
        if kind == "submit":
            self._submit(slug, intent)
        elif kind == "cancel":
            self._cancel(slug)
        elif kind == "settle":
            self._settle(slug, intent)

    def _submit(self, slug: str, intent: Dict[str, Any]) -> None:
        # Tech: rebuild the deployment's order, split it against the account net,
        #       and place each leg.
        # Why:  the split is computed HERE and not at the deployment because it
        #       depends on the account's net, which no deployment can see
        #       (§18.9). Rebuilding an `OrderEvent` keeps the leg carrying what
        #       the deployment actually asked for, so a fill can be reported
        #       against the intent rather than against the leg it became.
        if self.recorder is not None:
            self.recorder.record("owner_intent", slug=slug, intent=intent)
        # Tech: a frozen owner answers every submit with `rejected`.
        # Why:  see `freeze`. The rejection is what halts the asking deployment
        #       (`ClientBroker`), so a deployment stops when it next tries to
        #       trade — not before, while it is merely holding.
        if self.frozen is not None:
            self._emit(slug, "rejected", reason=f"owner frozen: {self.frozen}")
            return
        try:
            order = OrderEvent(
                timestamp=pd.Timestamp(intent["timestamp"]),
                side=intent["side"],
                limit_price=float(intent["limit_price"]),
                intent=intent.get("intent", "OPEN"),
                quantity=int(intent.get("quantity", 1)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            self._anomaly(f"{slug}: malformed submit intent: {intent!r} ({exc!r})")
            return

        # Tech: the month this order is for — the intent's, or ours when it
        #       names none — and the contract object that trades it.
        # Why:  see `_contracts`. A month that cannot be resolved is refused,
        #       not placed on ours: trading the wrong month is the 10-20 defect,
        #       and the rejection halts the deployment where a person sees it.
        code = str(intent.get("contract") or self.code)
        contract = self._contract_for(code)
        if contract is None:
            self._emit(slug, "rejected",
                       reason=f"owner cannot resolve contract {code!r}")
            return

        # Tech: size the legs against the net in THIS month.
        # Why:  §18.9's crossing rule is about lots the order can close, and a
        #       November order cannot close an October lot.
        legs = plan_legs(order.side, order.quantity, self.book.net_in(code))
        if not legs:
            self._anomaly(f"{slug}: submit planned no legs: {intent!r}")
            return

        urgent = bool(intent.get("urgent"))
        for leg in legs:
            self._place(slug, order, leg, urgent=urgent,
                        contract=contract, code=code)

    def _contract_for(self, code: str) -> Optional[Any]:
        # Tech: the contract object for a dated code — cached, else resolved once
        #       through `resolve` and kept; None when it cannot be.
        # Why:  the roll is the only time a new month appears, and it appears in
        #       a deployment's intent before the owner has heard of it. A failed
        #       resolve is recorded and answered with None rather than raised, so
        #       one bad intent cannot take down the owner of three deployments.
        if code in self._contracts:
            return self._contracts[code]
        if self._resolve is None:
            if self.recorder is not None:
                self.recorder.record("owner_contract_unknown", code=code)
            return None
        try:
            contract = self._resolve(code)
        except Exception as exc:                        # noqa: BLE001
            if self.recorder is not None:
                self.recorder.record("owner_contract_unresolved", code=code,
                                     error=repr(exc))
            return None
        self._contracts[code] = contract
        if self.recorder is not None:
            self.recorder.record("owner_contract_added", code=code)
        return contract

    def _place(self, slug: str, order: OrderEvent, leg: Leg, *,
               urgent: bool, contract: Any = None, code: str = "") -> None:
        # Tech: build the venue order for one leg, send it, and register whatever
        #       correlation ids came back.
        # Why:  `Leg.octype` is mapped onto the venue's enum here and nowhere else
        #       — `owner.py` stays importable without `shioaji` precisely because
        #       the vocabulary conversion lives at this boundary. LMT+ROD matches
        #       the backtest's resting-limit semantics, and a bounded limit keeps
        #       the worst fill knowable, which MKT gives up.
        sj = self._sj or _load_sj()
        price = (self.urgent_price(order.side, order.limit_price) if urgent
                 else order.limit_price)
        venue_order = sj.FuturesOrder(
            action=(sj.Action.Buy if order.side == "BUY" else sj.Action.Sell),
            price=price,
            quantity=leg.quantity,
            price_type=sj.FuturesPriceType.LMT,
            order_type=sj.OrderType.ROD,
            octype=(sj.FuturesOCType.Cover if leg.octype == COVER
                    else sj.FuturesOCType.New),
            account=self.account,
        )
        try:
            trade = self.api.place_order(
                self.contract if contract is None else contract, venue_order)
        except Exception as exc:                        # noqa: BLE001
            self._anomaly(f"{slug}: place_order raised: {exc!r}")
            self._emit(slug, "rejected", reason=f"place_order raised: {exc!r}")
            return
        working = self.book.register(
            slug, order, leg, trade=trade,
            order_id=_dig(trade, "order", "id"),
            ordno=_dig(trade, "order", "ordno"),
            code=code or self.code,
        )
        # Tech: record which deployment each venue order belongs to.
        # Why:  `ordno -> slug` lived only in memory, so when the owner stopped
        #       on 2026-10-06 nothing on disk said whose order 14AD46 was — it
        #       had to be inferred from the one intent stream that had spoken.
        if self.recorder is not None:
            self.recorder.record(
                "owner_placed", slug=slug, side=order.side, price=price,
                octype=leg.octype, quantity=leg.quantity,
                contract=code or self.code,
                order_id=working.order_id, ordno=working.ordno)

    def _cancel(self, slug: str) -> None:
        # Tech: flag this deployment's legs and ask the venue to cancel each.
        # Why:  §2.2 — the request is what §4.4 requires on every signal, and the
        #       leg stays tracked because it can still fill in the race window.
        #       Only this deployment's legs are touched: a shared session is not a
        #       shared order book, and cancelling a peer's resting order because
        #       our signal changed is the exact interference the split exists to
        #       prevent.
        for working in self.book.mark_cancel_requested(slug):
            try:
                self.api.cancel_order(working.trade)
            except Exception as exc:                    # noqa: BLE001
                self._anomaly(f"{slug}: cancel_order raised: {exc!r}")

    def _settle(self, slug: str, intent: Dict[str, Any]) -> None:
        # Tech: take the deployment's lots in the named month off the book, and
        #       answer `settled` with the size taken; a size that differs from
        #       the deployment's is an anomaly.
        # Why:  a missed scheduled roll leaves `RollWatch` to catch the expiry,
        #       and the exchange then cash-settles the lot with no deal — so the
        #       owner's book kept it on the expired month, and the next
        #       reconcile flagged a desync for a person to sort out.
        #       The deployment settles its own mirror at that moment
        #       (`LiveRuntime.settle_rolled_leg`) and says so here, in the same
        #       stream as its next submit, so the re-open that follows is sized
        #       on a book that already agrees. Allowed while frozen: it places
        #       nothing. Not refused for a month the calendar still calls live —
        #       the calendar has no holidays (`roll.roll_at`), the alias does.
        if self.recorder is not None:
            self.recorder.record("owner_intent", slug=slug, intent=intent)
        code = str(intent.get("contract") or self.code)
        try:
            claimed = int(intent.get("signed_qty", 0))
        except (TypeError, ValueError):
            claimed = None
        held = self.book.settle(slug, code)
        if held != claimed:
            self._anomaly(f"{slug}: settled {held:+d} in {code}, but the "
                          f"deployment settled {intent.get('signed_qty')!r}")
        if not self._expired(code) and self.recorder is not None:
            self.recorder.record("owner_settle_unexpired", slug=slug, code=code)
        self._emit(slug, "settled", contract=code, signed_qty=held)

    def _expired(self, code: str) -> bool:
        # Tech: whether the month a dated code names has stopped trading.
        # Why:  read off the code, not a contract object — an expired month may
        #       already be gone from the contract table (`contracts.code_month`).
        from .contracts import code_month
        from .roll import month_expired
        now = self._wall_clock()
        return month_expired(code_month(code, now), now)

    # ---- the venue side ----------------------------------------------------

    def _on_event(self, *msg: Any) -> None:
        # Tech: the single order callback — dispatch on the state name, DEAL
        #       before ORDER.
        # Why:  the DEAL-first ordering is load-bearing, not stylistic:
        #       `str(state)` is "OrderState.FuturesDeal", which contains ORDER as
        #       well, so testing ORDER first would route every deal into the order
        #       handler and silently drop every fill. `ShioajiBroker` records the
        #       same trap. Nothing slow runs here — this is the venue's thread.
        try:
            state, payload = (msg + (None, None))[:2]
            kind = str(getattr(state, "name", None) or state).upper()
            # Tech: count every callback by kind, before any handling.
            # Why:  §23.6 — the closes FILLED at the venue and were never
            #       booked, and no anomaly fired, which means `on_deal` was not
            #       reached rather than reached and confused. Those need
            #       opposite fixes and nothing recorded which had happened: a
            #       callback that never arrives and one that arrives and is
            #       dropped look identical from the book. Counting is
            #       observation only — it changes no behaviour and is the
            #       cheapest thing that distinguishes them.
            self.events_seen[kind] = self.events_seen.get(kind, 0) + 1
            # Tech: accept any payload that answers `.get`, rather than
            #       requiring `isinstance(payload, dict)`.
            # Why:  MEASURED 2026-09-07 night (§22) — the compiled layer hands
            #       out a mapping that renders as a dict but is not a `dict`
            #       instance, so the isinstance guard rejected every FORDER and
            #       FDEAL and the owner booked no fill for two orders the venue
            #       had actually filled. `ShioajiBroker._on_order_state` has
            #       never had this guard: it passes the payload straight to
            #       handlers that only call `.get`, which is why the soak saw
            #       fills and the owner did not. Duck-typing restores parity
            #       with the one implementation proven against a live venue.
            if not hasattr(payload, "get"):
                self._anomaly(
                    f"order callback payload has no .get(): "
                    f"type={type(payload).__name__} {msg!r}")
                return
            if "DEAL" in kind:
                self._handle_deal(payload)
            elif "ORDER" in kind:
                self._handle_order(payload)
            else:
                self._anomaly(f"unrecognised order-callback state: {kind!r}")
        except Exception as exc:                        # noqa: BLE001
            self._anomaly(f"order callback raised: {exc!r}")

    def _handle_order(self, msg: Dict[str, Any]) -> None:
        # Tech: read the operation block — a non-"00" code is a rejection, a
        #       successful New teaches the venue order number, a confirmed Cancel
        #       finishes the leg.
        # Why:  op_code is the venue's accept/reject verdict, and anything but
        #       "00" must stop rather than be retried — a retry loop is how a
        #       rejection becomes a suspension (§2.8b). The rejection is reported
        #       to the deployment that asked for it, not to all three.
        operation = msg.get("operation") or {}
        op_code = str(operation.get("op_code", "00"))
        op_type = str(operation.get("op_type", ""))
        order = msg.get("order") or {}
        ordno = order.get("ordno")
        oid = order.get("id")
        if self.recorder is not None:
            self.recorder.record("owner_order_event", op_type=op_type,
                                 op_code=op_code, ordno=ordno, order_id=oid)

        working = self.book.match(oid, ordno)
        if op_code != "00":
            reason = (f"order rejected: op_type={op_type} op_code={op_code} "
                      f"{operation.get('op_msg', '')}").strip()
            self._anomaly(reason)
            if working is not None:
                self._emit(working.slug, "rejected", reason=reason)
                self.book.finish(working)
            return

        if working is None:
            return
        if ordno:
            self.book.learn_ordno(oid, ordno)
        if op_type == "Cancel":
            # Tech: a confirmed cancel finishes the leg.
            # Why:  a partially filled order that is then cancelled leaves a real
            #       position behind; finishing it is about refusing FUTURE deals,
            #       not about erasing the ones already booked.
            self.book.finish(working)
            self._maybe_done(working.slug)

    def _handle_deal(self, msg: Dict[str, Any]) -> None:
        # Tech: book the deal, then forward the resulting fill to the one
        #       deployment whose order it was.
        # Why:  `OrderBook.on_deal` owns dedup, attribution and the overfill
        #       guard; this only carries the answer across the wire. Returning
        #       None means the book refused it and already raised the anomaly, so
        #       nothing is forwarded — a refused report must not reach a
        #       Portfolio.
        price = float(msg.get("price", 0.0))
        quantity = int(msg.get("quantity", 0))
        action = str(msg.get("action", ""))
        # Tech: the month of the leg this deal is for, read before booking.
        # Why:  `_recent` keeps the race per month (`_in_flight_explains`), and
        #       a fully filled leg is finished by `on_deal` itself.
        leg = self.book.match(msg.get("trade_id"), msg.get("ordno"))
        code = leg.code if leg is not None and leg.code else self.code
        booked = self.book.on_deal(
            ordno=msg.get("ordno"),
            trade_id=msg.get("trade_id"),
            exchange_seq=msg.get("exchange_seq"),
            side="BUY" if action.endswith("Buy") else "SELL",
            price=price,
            quantity=quantity,
            timestamp=pd.Timestamp(msg["ts"], unit="s") + VENUE_UTC_OFFSET
            if msg.get("ts") else pd.Timestamp.now(),
            fee=price * self.fee_rate * max(quantity, 0),
        )
        if booked is None:
            return
        slug, fill = booked
        # Tech: remember when this deal moved the book, and by how much.
        # Why:  the other side of the race `reconcile` tolerates — a deal booked
        #       a moment before `list_positions` catches up puts the book AHEAD
        #       of the venue, and the leg is already finished by then.
        self._recent.append((self._clock(),
                             fill.quantity if fill.side == "BUY"
                             else -fill.quantity, code))
        self._emit(slug, "fill", timestamp=fill.timestamp.isoformat(),
                   side=fill.side, fill_price=fill.fill_price,
                   quantity=fill.quantity, fee=fill.fee)
        self._maybe_done(slug)

    def _maybe_done(self, slug: str) -> None:
        # Tech: tell the deployment its order is finished once it has no working
        #       legs left.
        # Why:  the deployment submitted ONE order and must see ONE `done`, even
        #       when §18.9's split turned it into two legs — `ClientBroker` clears
        #       `_pending` on `done`, and a `done` after the first leg would let
        #       §4.4 place a replacement while the second leg is still working.
        if not self.book.has_pending(slug):
            self._emit(slug, "done")

    # ---- reporting ---------------------------------------------------------

    def publish_positions(self) -> None:
        # Tech: send each deployment its OWN signed size.
        # Why:  its own, never the account's — that is the category error §14
        #       measured and §16 removed. The deployment's rail then compares its
        #       `Trader.position` against the fills the owner routed to it, which
        #       is a like-for-like check on the wire rather than on the venue.
        for slug in self.slugs:
            # Tech: send the size, and the entry price whenever there is one.
            # Why:  `adopt_position` needs both, and `ClientBroker` builds a
            #       `HeldPosition` only when it has a price — so omitting the
            #       entry would make a held deployment look unadoptable and
            #       refuse every restart (`broker/base.HeldPosition`).
            self._emit(slug, "position",
                       signed_qty=self.book.net_for(slug),
                       avg_price=self.book.entry_for(slug))

    def reconcile(self) -> Optional[bool]:
        """Compare the account's booked net against the venue. None = cannot ask.

        True also covers a difference that orders still in flight explain, for
        up to `reconcile_grace` seconds; `reconcile_note` then says so.
        """
        # Tech: read `list_positions`, compare each month we know against
        #       `OrderBook.net_in`, and raise an anomaly on a disagreement.
        # Why:  this is the check §16 could only approximate from three files on
        #       disk. Made here it is exact and current, which is what retires
        #       §16's freshness race. A venue that will not answer degrades to
        #       None — "cannot compare" — rather than to a confident zero, the
        #       same rule `broker_position()` follows.
        from .flatten import net_position
        from .contracts import contract_codes
        try:
            positions = self.api.list_positions(self.account)
        except Exception as exc:                        # noqa: BLE001
            self._anomaly(f"list_positions raised: {exc!r}")
            return None
        if positions is None:
            return None
        # Tech: compare each month this owner knows on its own — the venue's
        #       rows for that month against the book's net in it.
        # Why:  2026-10-20 — summed across months, night's October BUY against
        #       fullday's October SELL read as an agreeing 0 while both were in
        #       the wrong month. Per month, a lot in the wrong one is a desync.
        split = {s: self.book.net_for(s) for s in self.slugs}
        # Tech: every month the owner can place on or the book holds — a month
        #       seeded from the ledgers may not have been resolved — matched on
        #       its contract's codes when there is a contract, else on the code.
        codes = list(dict.fromkeys(list(self._contracts) + self.book.codes()))
        many = len(codes) > 1
        off: Dict[str, Tuple[int, int]] = {}
        for code in codes:
            contract = self._contracts.get(code)
            venue = net_position(positions, contract_codes(contract)
                                 if contract is not None else {code})
            booked = self.book.net_in(code)
            # Tech: an expired month is recorded, not compared.
            # Why:  the exchange settles it with no deal, so the book holds its
            #       lots until the deployment's `settle` intent arrives, and
            #       when the venue stops reporting them is unmeasured. Either
            #       side of that gap would freeze an owner for a settlement that
            #       is going exactly as planned; no order can touch the month.
            if self._expired(code):
                if (venue or booked) and self.recorder is not None:
                    self.recorder.record("owner_reconcile_expired", contract=code,
                                         booked=booked, venue=venue)
                continue
            if venue != booked:
                off[code] = (booked, venue)

        def _where(code: str) -> str:
            # Tech: name the month only when there is more than one.
            # Why:  the single-month messages stay exactly what operators and
            #       the alert history already read.
            return f" in {code}" if many else ""

        if not off:
            if self._mismatch_since is not None and self.recorder is not None:
                self.recorder.record("owner_reconcile_settled",
                                     net=self.book.account_net)
            self._mismatch_since = None
            self.reconcile_note = None
            return True

        # Tech: a difference that orders in flight can explain is "pending" until
        #       it has lasted `reconcile_grace`; anything else fails at once.
        # Why:  DEFAULT_RECONCILE_GRACE — the venue reports a fill before its
        #       deal callback books it, so a check in that window is not a
        #       desync. The grace is bounded because a callback that never comes
        #       IS one (§23.6 measured closes that filled and were never booked).
        now = self._clock()
        if all(self._in_flight_explains(venue - booked, now, code)
               for code, (booked, venue) in off.items()):
            if self._mismatch_since is None:
                self._mismatch_since = now
            waited = now - self._mismatch_since
            if waited <= self.reconcile_grace:
                self.reconcile_note = "; ".join(
                    f"account mirror {booked} != venue {venue}{_where(code)}, "
                    f"explained by orders in flight — waiting {waited:.0f}s of "
                    f"{self.reconcile_grace:.0f}s"
                    for code, (booked, venue) in off.items())
                if self.recorder is not None:
                    for code, (booked, venue) in off.items():
                        self.recorder.record("owner_reconcile_pending",
                                             booked=booked, venue=venue,
                                             contract=code, waited=waited,
                                             per_deployment=split)
                return True
            for code, (booked, venue) in off.items():
                self._anomaly(
                    f"account mirror {booked} != venue {venue}{_where(code)} "
                    f"for {waited:.0f}s, past the {self.reconcile_grace:.0f}s "
                    f"grace for fills in flight (per deployment: {split})")
            return False
        for code, (booked, venue) in off.items():
            self._anomaly(
                f"account mirror {booked} != venue {venue}{_where(code)} "
                f"(per deployment: {split})")
        return False

    def _in_flight_explains(self, diff: int, now: float,
                            code: Optional[str] = None) -> bool:
        # Tech: True when `diff` (venue minus book) fits inside the unbooked
        #       remainder of working legs on its side, plus deals booked within
        #       the grace on the opposite side.
        # Why:  the two halves of one race. Venue AHEAD: a working leg filled
        #       and its deal has not arrived — 2026-10-06's +1 against a BUY leg
        #       with 1 remaining. Venue BEHIND: a deal was booked and
        #       `list_positions` has not caught up (~0.1 s, §18.10). A
        #       difference larger than both together is something no order of
        #       ours explains, and must not be waited out.
        while self._recent and now - self._recent[0][0] > self.reconcile_grace:
            self._recent.popleft()
        code = self.code if code is None else code
        sign = 1 if diff > 0 else -1
        in_flight = sum(w.remaining for w in self.book.all_working()
                        if (1 if w.order.side == "BUY" else -1) == sign
                        and (w.code or self.code) == code)
        lagging = sum(abs(s) for _, s, c in list(self._recent)
                      if (1 if s > 0 else -1) == -sign and c == code)
        return abs(diff) <= in_flight + lagging

    def freeze(self, reason: str) -> None:
        """Stop placing orders; keep hearing deals and publishing positions."""
        # Tech: latch the first reason; every later submit is answered with
        #       `rejected` (`_submit`). Cancels still reach the venue.
        # Why:  2026-10-06 — the owner EXITED on a failed reconcile, which
        #       logged out the only session that could hear the fill it was
        #       arguing about. Freezing keeps the old rule (no order is sized
        #       against a disputed net) without dropping the deal stream. Cancels
        #       pass because they can only reduce what is working. Never
        #       unfrozen in-process: like `SafetyRails`, a dispute is resolved by
        #       a person and a restart, not by the next agreeing sample.
        if self.frozen is None:
            self.frozen = reason
            if self.recorder is not None:
                self.recorder.record("owner_frozen", reason=reason)

    def stale_deployments(self) -> List[str]:
        # Tech: deployments that have not written an intent within the timeout.
        # Why:  a deployment that died holding a position is not one whose intents
        #       should be waited for, and the owner is the only thing that can
        #       notice. Nothing is closed on this signal — deciding what to do
        #       about an absent deployment holding a lot is a risk call (§7.4),
        #       not a mechanical one, so this reports and stops.
        stale = []
        for slug in self.slugs:
            age = self._in[slug].age_seconds()
            if age is not None and age > self.deployment_timeout:
                stale.append(slug)
        return stale

    def _emit(self, slug: str, kind: str, **fields: Any) -> None:
        # Tech: write one event to that deployment's stream, under the shared
        #       write lock.
        # Why:  fills are written from the venue's callback thread and positions
        #       from the owner's loop; two unsynchronised writers could interleave
        #       a line, which the reader would then correctly refuse to parse and
        #       halt a healthy deployment.
        with self._out_lock:
            self._out[slug].append(kind, **fields)

    def _anomaly(self, message: str) -> None:
        # Tech: record and notify.
        # Why:  same contract as `ShioajiBroker._anomaly` and `OrderBook._anomaly`
        #       — every branch reaching here is a message we could not account
        #       for, and losing one silently means losing track of a position. The
        #       owner has no rails of its own; the process that runs it decides
        #       what a halt means for three deployments at once.
        self.anomalies.append(message)
        if self.recorder is not None:
            self.recorder.record("owner_process_anomaly", message=message)
        if self.on_anomaly is not None:
            self.on_anomaly(message)

    # ---- pricing -----------------------------------------------------------

    def through_price(self, side: str, limit_price: float,
                      ticks: float) -> float:
        # Tech: move the limit `ticks` through the touch — up for a BUY, down for
        #       a SELL — snapped to the tick grid.
        # Why:  identical rule to `ShioajiBroker.through_price`, and identical
        #       reason: the direction is what makes it aggressive, since a BUY
        #       closing a short must be willing to pay *more*. Duplicated rather
        #       than imported because importing would drag the single-order broker
        #       into the owner's import graph for four lines of arithmetic.
        offset = ticks * self.tick_size
        raw = limit_price + offset if side == "BUY" else limit_price - offset
        return round(raw / self.tick_size) * self.tick_size

    def urgent_price(self, side: str, limit_price: float) -> float:
        # Tech: `flatten_aggression_ticks` through the touch.
        # Why:  a FLATTEN halt exists to get risk off, so the order must actually
        #       fill — but it stays a bounded limit, so the worst fill is knowable
        #       before it is sent.
        return self.through_price(side, limit_price,
                                  self.flatten_aggression_ticks)
