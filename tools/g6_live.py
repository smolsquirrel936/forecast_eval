"""G6 live — two deployments holding at once, against a real session.

    uv run python -m forecast_eval.tools.g6_live --out outputs/g6 \\
        --i-understand-this-places-orders

G6 (STRATEGY_SPLIT_PLAN §8, restated after §14):

> two deployments hold non-flat positions across a full session; neither rail
> halts; the venue's reported position equals the **sum of the deployments' own
> fill ledgers**; and each deployment's PnL is still attributed to it alone.

**Three of those four clauses are already proven offline** by
`tests/test_g6_two_deployments.py`, which drives two real `LiveLoop`s, two real
`ClientBroker`s and a real `OwnerProcess` through a venue that fills. What that
cannot prove is the half that needs a market: **that nothing halts against real
latency, real partial fills, and a real `list_positions`.**

So this does not re-test the arithmetic. It opens two genuinely opposed
positions on the real (simulated) account — the §14 scenario that halted both
deployments when they each placed their own orders — and then simply watches.

**What it is checking, minute by minute:**

  * neither deployment's rails halt;
  * `OwnerProcess.reconcile()` keeps agreeing with the venue;
  * every fill reaches exactly one deployment;
  * the ledgers' sum tracks the venue as positions change.

**It is deliberately small.** Two deployments, one lot each, opposite
directions, for as long as `--minutes` says. It flattens what it opened before
it exits, and shouts if it cannot.

**§15 is suspended for the duration of this run and only by this run.** That is
the point of it: G6 passing is what lifts §15 permanently, and until it does,
one instance at a time remains the rule.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pandas as pd                                                # noqa: E402

from forecast_eval.environment import (DAY_CLOSE, NIGHT_CLOSE,    # noqa: E402
                                       classify_session)
from forecast_eval.events import MarketEvent, OrderEvent          # noqa: E402
from forecast_eval.live.broker.client import ClientBroker         # noqa: E402
from forecast_eval.live.contracts import resolve_txf              # noqa: E402
from forecast_eval.live.ledger import (FillLedger,                # noqa: E402
                                       account_net_position,
                                       per_deployment_nets)
from forecast_eval.live.owner_cli import _futures_account         # noqa: E402
from forecast_eval.live.owner_process import OwnerProcess         # noqa: E402
from forecast_eval.live.state import HaltAction, RailsConfig, SafetyRails  # noqa: E402

SLUGS = ("day", "night")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True,
                    help="A directory for this run's intent/event streams. Use "
                         "a FRESH one — the ledgers here are the run's own.")
    ap.add_argument("--minutes", type=float, default=10.0,
                    help="How long to hold both positions before flattening. "
                         "Ten minutes is enough to cross several reconcile "
                         "cadences; a full session is the eventual goal.")
    ap.add_argument("--credential", default="night")
    ap.add_argument("--contract", default="TXFR1")
    ap.add_argument("--i-understand-this-places-orders", action="store_true",
                    dest="ack")
    ap.add_argument("--offset-points", type=float, default=500.0,
                    help="How far through the touch the opening orders are "
                         "priced, so they actually fill.")
    ap.add_argument("--poll-seconds", type=float, default=2.0)
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if not args.ack:
        print("ERROR: G6 opens two real positions; pass "
              "--i-understand-this-places-orders", file=sys.stderr)
        return 5

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    # Tech: refuse a directory that already holds ledgers.
    # Why:  this run's verdict is "the venue equals the sum of the ledgers", and
    #       a ledger from an earlier run would be counted into that sum while
    #       corresponding to no position the venue still holds. A stale file
    #       would make G6 fail for a reason that has nothing to do with G6.
    existing = sorted(out.glob("fills_*.csv"))
    if existing:
        print(f"ERROR: {out} already holds {[p.name for p in existing]}. "
              f"Use a fresh --out.", file=sys.stderr)
        return 2

    # Tech: classify the wall clock, and check the run fits before the session
    #       closes.
    # Why:  this needs FILLS — it opens two positions and watches them — so
    #       off-hours it measures nothing, exactly as `--mode crossing` and
    #       `--mode lag` do. The second half is specific to this script and
    #       matters more: it FLATTENS at the end, and a run that reaches the
    #       session close still holding cannot close, because a close needs a
    #       market too. That leaves the account holding two opposed lots
    #       overnight, which is the one outcome this must not produce.
    minutes_left = _minutes_until_close(datetime.now())
    session = classify_session(pd.Timestamp.now())
    print(f"local time {datetime.now():%Y-%m-%d %H:%M:%S}  "
          f"session={session or 'CLOSED'}")
    if session is None:
        print("")
        print("  WARNING: no TXF session is open (DAY 08:45-13:45, NIGHT "
              "15:00-05:00, Taipei).")
        print("  Nothing will fill, so nothing will be measured — this exits 4.")
    elif minutes_left is not None and minutes_left < args.minutes + 5:
        print("")
        print(f"  WARNING: about {minutes_left:.0f} min left in this session and "
              f"--minutes is {args.minutes:.0f}.")
        print("  The closing flatten needs a market. Shorten --minutes or wait "
              "for the next session.")

    from forecast_eval.live import credentials
    cred = credentials.load(args.credential)
    if cred is None:
        print(f"ERROR: no credential for {args.credential!r}", file=sys.stderr)
        return 2

    import shioaji as sj
    api = sj.Shioaji(simulation=True)
    owner = None
    try:
        accounts = api.login(api_key=cred.api_key, secret_key=cred.secret_key)
        account = _futures_account(api, accounts)
        contract, note = resolve_txf(api, args.contract)
        print(f"contract {note}")

        # Tech: print every anomaly as the owner records it.
        # Why:  `_submit` returns early and silently on a malformed intent or a
        #       plan with no legs, and `_place` records a raised `place_order`
        #       the same way. Without a callback those messages reach only
        #       `owner.anomalies`, which the INCONCLUSIVE path used to discard —
        #       so a run that placed nothing looked identical to one whose
        #       orders the venue ignored (§20.4 trap 1).
        owner = OwnerProcess(api, contract, directory=out, slugs=SLUGS,
                             account=account, fee_rate=0.0,
                             on_anomaly=lambda m: print(f"  ANOMALY: {m}"))

        # Tech: the account must be flat before two positions are opened on it.
        # Why:  every number this run reports is a delta from a known start. An
        #       inherited lot would be attributed to whichever deployment the
        #       arithmetic happened to reach, which is the exact confusion G6
        #       exists to disprove.
        if owner.reconcile() is False:
            print("\n  REFUSING: the account is not flat (or disagrees with the "
                  "empty ledgers).", file=sys.stderr)
            print(f"      {owner.anomalies[-1]}", file=sys.stderr)
            print("  Flatten it first: python -m forecast_eval.live.flatten",
                  file=sys.stderr)
            return 3
        owner.start()

        rails = {slug: SafetyRails(RailsConfig(), simulation=True)
                 for slug in SLUGS}
        clients = {slug: ClientBroker(out, slug, rails=rails[slug])
                   for slug in SLUGS}
        # Tech: one fill ledger per deployment, written from the fills each
        #       `ClientBroker` drains.
        # Why:  §22.7 — G6's verdict is "the venue equals the SUM of the
        #       deployments' own ledgers", and nothing here was writing one.
        #       `ClientBroker` does not touch the ledger (only `cli.py` builds a
        #       `FillLedger`, for a real deployment), so the run printed
        #       `ledgers on disk {}` and passed anyway. The path is spelled the
        #       way `per_deployment_nets` reads it back.
        ledgers = {slug: FillLedger(out / f"fills_{slug}.csv") for slug in SLUGS}
        booked: Dict[str, List[Any]] = {slug: [] for slug in SLUGS}

        price = _last(api, contract)
        print(f"last {price:.0f}")
        print("\n--- opening two OPPOSED positions (the §14 scenario) ---")
        _open(clients["day"], "BUY", price + args.offset_points)
        _open(clients["night"], "SELL", price - args.offset_points)

        # Tech: wait for BOTH positions to appear before starting the watch,
        #       and give up early if they do not.
        # Why:  the watch measures "two deployments holding at once". Without
        #       both, it would spend the whole --minutes observing nothing and
        #       then report inconclusive — burning the session window on a run
        #       that was already decided in its first minute. §18.10 measured a
        #       fill taking up to 12.8 s, so 90 s is generous rather than tight.
        if not _await_both(owner, clients, ledgers, booked, seconds=90.0):
            print("")
            print(f"  => INCONCLUSIVE: both deployments must hold. "
                  f"day={owner.book.net_for('day'):+d} "
                  f"night={owner.book.net_for('night'):+d}")
            print("     Nothing filled within 90s — is a session open?")
            _diagnose(owner, api, account)
            return 4

        return _watch(owner, clients, rails, args, api, contract,
                      ledgers, booked)
    finally:
        if owner is not None:
            _flatten(owner, api, contract)
        try:
            api.logout()
        except Exception:                               # noqa: BLE001
            pass


def _venue_net(owner: OwnerProcess) -> Optional[int]:
    """The venue's own net for this contract — None when it will not answer."""
    # Tech: ask `list_positions` directly rather than going through
    #       `OwnerProcess.reconcile`, which compares against the book.
    # Why:  §22 — reconcile answers "do the book and the venue agree", and on
    #       the opposed pair G6 opens both are 0 whether both legs filled or
    #       neither did. That makes it blind to exactly the failure this run
    #       hit, so the flatten needs the venue's number on its own.
    from forecast_eval.live.contracts import contract_codes
    from forecast_eval.live.flatten import net_position
    try:
        positions = owner.api.list_positions(owner.account)
    except Exception as exc:                            # noqa: BLE001
        print(f"  list_positions raised: {exc!r}")
        return None
    if positions is None:
        return None
    return net_position(positions, contract_codes(owner.contract))


def _diagnose(owner: OwnerProcess, api: Any, account: Any) -> None:
    """Print why nothing filled, from the three places that know."""
    # Tech: dump the owner's anomalies, the legs it believes are working, and
    #       the venue's own view of every order placed this session.
    # Why:  an INCONCLUSIVE run costs a session window, and the first one
    #       (2026-09-07 night) spent that window without recording which of
    #       three silent paths it took: the intent never became an order, the
    #       order was rejected, or the order rests unfilled and the fill simply
    #       never arrived. Those need different fixes and the run distinguished
    #       none of them. §20.4 trap 1 says measure the quantity in dispute
    #       rather than find a story that fits — this is what makes it
    #       measurable next time.
    print("\n--- why nothing filled ---")
    print(f"  owner anomalies            {owner.anomalies or 'none'}")
    # Tech: how many callbacks the owner has received, by kind.
    # Why:  §23.6 — this is the number that separates "the deal never arrived"
    #       from "it arrived and was not booked". Two FDEAL after a run that
    #       filled four orders means the venue stopped talking to us; four means
    #       the loss is on our side of the callback.
    print(f"  callbacks received         {owner.events_seen or 'none'}")
    working = owner.book.all_working()
    print(f"  legs the owner thinks work {len(working)}")
    for leg in working:
        print(f"     {leg.slug:5s} {leg.order.side:4s} {leg.octype:5s} "
              f"qty={leg.quantity} filled={leg.filled} "
              f"order_id={leg.order_id!r} ordno={leg.ordno!r}")

    # Tech: refresh order state from the venue and print each trade's status.
    # Why:  the owner learns fills ONLY from `set_order_callback`. Every probe
    #       that has actually observed a fill (§§18.9, 18.10) also called
    #       `update_status`, so whether the push alone is enough has never been
    #       tested — and `list_trades` answers it directly: an order the venue
    #       reports Filled while the owner shows filled=0 means the callback is
    #       the gap, not the order.
    try:
        api.update_status(account)
        trades = api.list_trades() or []
    except Exception as exc:                            # noqa: BLE001
        print(f"  venue view unavailable: {exc!r}")
        return
    print(f"  venue trades this session  {len(trades)}")
    for trade in trades:
        order = getattr(trade, "order", None)
        status = getattr(trade, "status", None)
        print(f"     ordno={getattr(order, 'ordno', None)!r} "
              f"action={getattr(order, 'action', None)!r} "
              f"price={getattr(order, 'price', None)!r} "
              f"qty={getattr(order, 'quantity', None)!r} "
              f"status={getattr(status, 'status', None)!r} "
              f"deals={getattr(status, 'deal_quantity', None)!r} "
              f"msg={getattr(status, 'msg', None)!r}")


def _minutes_until_close(now: datetime) -> Optional[float]:
    """Minutes until the current session's close, or None when none is open."""
    # Tech: measure to 13:45 inside the day window and to 05:00 inside the
    #       night one, wrapping past midnight.
    # Why:  the night session straddles midnight, so "close minus now" is not
    #       one subtraction — at 23:00 the close is 6 h away, not -18 h. The
    #       edges come from `environment.py` rather than being repeated here, so
    #       there is one definition of when TXF trades.
    session = classify_session(pd.Timestamp(now))
    if session is None:
        return None
    minutes_now = now.hour * 60 + now.minute + now.second / 60.0
    if session == "DAY":
        close = DAY_CLOSE.hour * 60 + DAY_CLOSE.minute
        return close - minutes_now
    close = NIGHT_CLOSE.hour * 60 + NIGHT_CLOSE.minute
    return (close - minutes_now) % (24 * 60)


def _last(api: Any, contract: Any) -> float:
    return float(getattr(api.snapshots([contract])[0], "close", 0.0))


def _open(client: ClientBroker, side: str, limit: float) -> None:
    # Tech: submit one marketable OPEN intent through the real client broker.
    # Why:  going through `ClientBroker.submit` rather than the owner directly is
    #       the whole point — the wire is what G6 is testing, and a shortcut past
    #       it would test the half that already has 130 offline tests.
    client.submit(OrderEvent(timestamp=datetime.now(), side=side,
                             limit_price=limit, intent="OPEN", quantity=1),
                  current_price=limit)
    print(f"  {side} 1 @ {limit:.0f} submitted as an intent")


def _drain(clients: Dict[str, ClientBroker], ledgers: Dict[str, Any],
           booked: Dict[str, List[Any]], market: MarketEvent) -> None:
    """Drain each client's fills and append them to that deployment's ledger."""
    # Tech: extend the per-slug fill list with whatever `poll_fills` returned,
    #       then `sync` — which appends only past its own high-water mark.
    # Why:  §22.7. The ledgers are the operand G6's central clause compares
    #       against the venue, and no component in this script wrote one: in a
    #       real deployment `cli.py` owns the `FillLedger` and the loop feeds it,
    #       and G6 runs neither. Draining in one place keeps the wait and the
    #       watch from recording different things.
    for slug, client in clients.items():
        fills = client.poll_fills(market)
        if fills:
            booked[slug].extend(fills)
            ledgers[slug].sync(booked[slug])


def _await_both(owner: OwnerProcess, clients: Dict[str, ClientBroker],
                ledgers: Dict[str, Any], booked: Dict[str, List[Any]],
                seconds: float) -> bool:
    # Tech: poll the owner until every slug reports a non-flat position.
    # Why:  drains the clients too, so their own mirrors are current before the
    #       watch begins comparing them — a watch that starts mid-fill would
    #       report a disagreement that is its own impatience.
    # Tech: time how long the opens take to be BOOKED, and report it.
    # Why:  §23.7 — this is the quantity in dispute. §18.10 measured a fill at
    #       up to 12.8 s and the flatten waits 40 s on that basis, but run 4
    #       booked in 12 s and run 5 in 42 s: the real spread straddles the
    #       close window, which is why the teardown fails intermittently.
    #       Printing it every run turns a guess about latency into a series.
    started = time.monotonic()
    deadline = started + seconds
    market = MarketEvent(datetime.now(), 0.0, 0, "NIGHT")
    while time.monotonic() < deadline:
        owner.poll()
        _drain(clients, ledgers, booked, market)
        if all(owner.book.net_for(slug) for slug in SLUGS):
            held = {s: owner.book.net_for(s) for s in SLUGS}
            print(f"  both holding: {held}  account "
                  f"{owner.book.account_net:+d}  "
                  f"booked in {time.monotonic() - started:.1f}s  "
                  f"callbacks {owner.events_seen}")
            return True
        time.sleep(1.0)
    return False


def _watch(owner: OwnerProcess, clients: Dict[str, ClientBroker],
           rails: Dict[str, SafetyRails], args: Any, api: Any,
           contract: Any, ledgers: Dict[str, Any],
           booked: Dict[str, List[Any]]) -> int:
    """Poll the owner, drain both clients, and report anything that goes wrong."""
    deadline = time.monotonic() + args.minutes * 60.0
    last_report = 0.0
    failures: List[str] = []

    while time.monotonic() < deadline:
        owner.poll()
        # Tech: drain each client so its rails see the position events.
        # Why:  a `ClientBroker` halts on a silent owner, and that check only
        #       runs when something asks it for a position. In a real deployment
        #       the bar loop asks every minute; here nothing else would.
        market = MarketEvent(datetime.now(), 0.0, 0, "NIGHT")
        _drain(clients, ledgers, booked, market)
        for client in clients.values():
            client.broker_position()

        for slug, rail in rails.items():
            if rail.halted:
                failures.append(f"{slug} HALTED: {rail.halt_reason}")

        if time.monotonic() - last_report >= 30.0:
            last_report = time.monotonic()
            agreed = owner.reconcile()
            nets = {s: owner.book.net_for(s) for s in SLUGS}
            print(f"  [{datetime.now():%H:%M:%S}] per-deployment {nets}  "
                  f"account {owner.book.account_net:+d}  "
                  f"reconcile={agreed}")
            if agreed is False:
                failures.append(f"reconcile FAILED: {owner.anomalies[-1]}")

        if failures:
            break
        time.sleep(args.poll_seconds)

    owner.poll()
    return _verdict(owner, clients, failures, args)


def _verdict(owner: OwnerProcess, clients: Dict[str, ClientBroker],
             failures: List[str], args: Any) -> int:
    print("\n=== G6 ===")
    nets = {s: owner.book.net_for(s) for s in SLUGS}
    print(f"  per-deployment positions   {nets}")
    print(f"  account (owner's book)     {owner.book.account_net:+d}")
    print(f"  ledgers on disk            {per_deployment_nets(Path(args.out))}")
    print(f"  ledger sum                 {account_net_position(Path(args.out))}")
    print(f"  owner anomalies            {len(owner.anomalies)}")

    # Tech: the clause each check corresponds to, named.
    # Why:  a pass that does not say WHAT passed is not evidence anyone can act
    #       on, and G6 is the gate that lifts §15.
    if failures:
        print("\n  => G6 FAILED")
        for line in failures:
            print(f"     {line}")
        return 1
    if not all(nets.get(s) for s in SLUGS):
        print("\n  => INCONCLUSIVE: both deployments must hold a non-flat "
              "position. One or both never filled.")
        return 4

    # Tech: refuse to pass unless the ledger clause was actually CHECKED — the
    #       files must exist, match the owner's book per deployment, and sum to
    #       what the venue reports.
    # Why:  §22.7 — the 2026-09-07 run printed `ledgers on disk {}` and
    #       `ledger sum None` and passed anyway, claiming "every fill reached
    #       exactly one deployment" on evidence it had just shown was absent.
    #       G6 is the gate that lifts §15; a verdict that asserts an unchecked
    #       clause is worse than no verdict, because it retires a safety rule on
    #       a measurement nobody made (§20.4 trap 2).
    on_disk = per_deployment_nets(Path(args.out))
    ledger_sum = account_net_position(Path(args.out))
    if ledger_sum is None or set(on_disk) != set(SLUGS):
        print("\n  => INCONCLUSIVE: no fill ledger was written, so the clause "
              "'venue == sum of the deployments' ledgers' was never checked.")
        return 4
    if on_disk != nets:
        print(f"\n  => G6 FAILED: ledgers {on_disk} disagree with the owner's "
              f"book {nets} — a fill reached the wrong deployment, or none.")
        return 1
    venue = _venue_net(owner)
    if venue is None:
        print("\n  => INCONCLUSIVE: the venue would not report its position, "
              "so the ledger sum could not be compared against it.")
        return 4
    if venue != ledger_sum:
        print(f"\n  => G6 FAILED: venue {venue:+d} != ledger sum "
              f"{ledger_sum:+d}.")
        return 1

    print("\n  => G6 PASSED (live half)")
    print("     Two deployments held opposed positions; neither rail halted;")
    print("     the owner and the venue agreed throughout; every fill reached")
    print("     exactly one deployment. §15 can be lifted for this shape.")
    return 0


def _flatten(owner: OwnerProcess, api: Any, contract: Any) -> None:
    # Tech: close whatever this run opened, through the owner, and say loudly if
    #       the account is not flat afterwards.
    # Why:  the same rule every probe here follows — this must never be the
    #       reason an account is left holding. Going through the owner rather
    #       than `flatten.py` keeps each deployment's ledger correct, which
    #       `flatten.py` explicitly cannot do (§18.3 item 8).
    print("\n--- flattening ---")
    try:
        for slug in SLUGS:
            held = owner.book.net_for(slug)
            if not held:
                continue
            side = "SELL" if held > 0 else "BUY"
            price = _last(api, contract)
            limit = price - 500.0 if held > 0 else price + 500.0
            owner._submit(slug, {                        # noqa: SLF001
                "timestamp": datetime.now().isoformat(), "side": side,
                "limit_price": limit, "intent": "CLOSE",
                "quantity": abs(held), "urgent": True,
            })
            print(f"  {slug}: closing {held:+d}")
        # Tech: wait up to 90 s for both closes to be reported, polling once a
        #       second, and report how long it took.
        # Why:  this was 10 s, then 40 s, and MEASURED 2026-09-07 (§23.7) the
        #       opens were booked in 12 s on run 4 and 42 s on run 5 — a spread
        #       that straddles a 40 s window, which is why the teardown failed
        #       intermittently on identical code. 90 s matches what
        #       `_await_both` already allows for the same event in the other
        #       direction; there is no reason the close deserves less than the
        #       open. The elapsed time is printed either way, because a close
        #       that took 70 s is a fact worth having and a silent success hides
        #       it.
        closes_started = time.monotonic()
        for _ in range(90):
            time.sleep(1.0)
            owner.poll()
            if owner.book.account_net == 0 and not any(
                    owner.book.net_for(s) for s in SLUGS):
                # Tech: confirm against `list_positions` before saying flat.
                # Why:  MEASURED 2026-09-07 (§22) — the book read 0 while the
                #       venue had filled BOTH legs, and this printed "flat."
                #       without ever asking. It was true only because the two
                #       legs were opposed and netted to zero; the same code
                #       would have reported flat on a same-direction pair that
                #       left the account holding overnight. The book is the
                #       thing under test here, so it cannot also be the witness.
                venue = _venue_net(owner)
                if venue:
                    print(f"\n  *** BOOK SAYS FLAT BUT VENUE HOLDS {venue:+d}. "
                          f"Flatten by hand: "
                          f"python -m forecast_eval.live.flatten ***")
                    return
                took = time.monotonic() - closes_started
                print(f"  flat. (closes booked in {took:.1f}s, "
                      f"callbacks {owner.events_seen})" if venue == 0 else
                      "  book flat; venue would not answer — verify by hand.")
                return
        # Tech: report the venue's own net and dump the same diagnostic the
        #       INCONCLUSIVE path gets.
        # Why:  §23 — this branch fired twice on 2026-09-07 saying only "STILL
        #       HOLDING", which does not distinguish a close that never filled
        #       from one that filled and was not attributed, and those need
        #       opposite fixes. The success branch was taught to ask the venue
        #       (§22) and this one was not; a failure path less informative than
        #       the success path beside it is backwards, since this is the branch
        #       that fires when something is actually wrong.
        # Tech: say which of the two books is non-flat, and only send the
        #       operator to `flatten.py` when the VENUE is the one holding.
        # Why:  §23.5 — "STILL HOLDING" was read as account risk and it was not.
        #       Two opposed lots net to zero on a netted account, so the venue
        #       was flat both times this fired and `flatten.py` correctly
        #       answered "already flat — nothing to do", twice. What was non-flat
        #       is the owner's per-deployment attribution, which no account-level
        #       tool can repair and which carries no market risk. Sending an
        #       operator to run an emergency close against a flat account teaches
        #       them the message is noise.
        venue = _venue_net(owner)
        nets = {s: owner.book.net_for(s) for s in SLUGS}
        if venue:
            print(f"\n  *** THE VENUE HOLDS {venue:+d}. Flatten by hand: "
                  f"python -m forecast_eval.live.flatten ***")
        elif venue is None:
            print("\n  *** THE VENUE WOULD NOT ANSWER — verify by hand: "
                  "python -m forecast_eval.live.flatten --show-orders ***")
        else:
            print(f"\n  The venue is FLAT (net 0) — no position, nothing for "
                  f"`flatten.py` to close.")
            print(f"  What did not clear is the owner's own attribution: "
                  f"{nets}.")
            print(f"  The closes were placed; whether they FILLED is what the "
                  f"dump below answers.")
        _diagnose(owner, api, owner.account)
    except Exception as exc:                             # noqa: BLE001
        print(f"\n  *** FLATTEN FAILED: {exc!r} — check the account by hand ***")


if __name__ == "__main__":
    raise SystemExit(main())
