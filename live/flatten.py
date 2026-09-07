"""Flatten — close whatever the account is holding, and confirm it.

Every other tool here refuses to act when a position already exists, because
none of them can tell its own order from one that was already there. That is the
right default and it leaves a gap: something has to be able to close a position
left behind by a killed process, a failed `FLATTEN` halt, or a probe whose close
did not fill. This is that something.

**LMT + ROD, priced through the touch.** MEASURED 2026-08-12: an `IOC` close was
accepted by the simulation venue (`order_id` returned, no rejection) and simply
never filled, leaving a lot held. Aggression is what makes a close fill; the
time-in-force only decides what happens when it *cannot*. ROD rests and can still
fill a moment later, where IOC is gone — so ROD is the safer of the two for the
one order that must not silently disappear.

Read the position, close it, read it back. Retries a bounded number of times and
**exits non-zero if the account is not flat at the end**, because "submitted a
close" is not "flat" (§2.12).

    uv run python -m forecast_eval.live.flatten --dry-run   # show, do nothing
    uv run python -m forecast_eval.live.flatten
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from forecast_eval.live.contracts import contract_codes, resolve_txf  # noqa: E402

CLOSE_AGGRESSION_POINTS = 30.0


def _warn_other_deployments(out_parent, held: int) -> None:
    """Name the deployments whose ledgers say they are holding (§18.3 item 8)."""
    # Tech: read every `fills_*.csv` beside the run directory and report each
    #       deployment's own net; stay silent when there is nothing to read.
    # Why:  the venue can only say what the ACCOUNT holds. The per-deployment
    #       ledgers are the only record of who holds what (§16), so they are what
    #       turns "closing 2 lots" into "closing day's 1 and night's 1". Silence
    #       when no ledger is found is deliberate: the single-deployment case is
    #       still the normal one, and a warning with nothing behind it trains an
    #       operator to ignore warnings.
    if out_parent is None:
        return
    try:
        from .ledger import per_deployment_nets
        holders = [(slug, net)
                   for slug, net in sorted(per_deployment_nets(out_parent).items())
                   if net]
    except Exception as exc:                            # noqa: BLE001
        print(f"  (could not read fill ledgers in {out_parent}: {exc!r})")
        return
    if len(holders) < 2:
        return
    print("")
    print("  *** THIS CLOSES THE WHOLE ACCOUNT, AND MORE THAN ONE DEPLOYMENT "
          "IS HOLDING ***")
    for slug, net in holders:
        print(f"      {slug:<10} {net:+d} lot(s)")
    print(f"      venue      {held:+d} lot(s)  <- what will be closed")
    print("  Their position mirrors will NOT know. After this, restart each of")
    print("  them with --fresh-start, or they halt on the first bar.")
    print("")


def net_position(positions: Any, codes: set) -> int:
    """Signed net lots across the rows belonging to our contract."""
    # Tech: sum `quantity`, signing it by `direction`/`action` when the field is
    #       unsigned, over rows whose code is one of ours.
    # Why:  the alias/resolved split again (§7.2) — positions come back stamped
    #       with the resolved month, so filtering on the alias alone finds
    #       nothing and would report flat while holding. Signing defensively
    #       because whether `quantity` is already signed is unverified.
    total = 0
    for row in (positions or []):
        code = str(getattr(row, "code", "") or "")
        if codes and code not in codes:
            continue
        quantity = int(getattr(row, "quantity", 0) or 0)
        direction = str(getattr(row, "direction", "")
                        or getattr(row, "action", "")).lower()
        if quantity > 0 and ("sell" in direction or "short" in direction):
            quantity = -quantity
        total += quantity
    return total


WORKING_STATUSES = ("pendingsubmit", "presubmitted", "submitted", "partfilled")


def is_working(trade: Any) -> bool:
    """Is this trade still live at the venue?"""
    # Tech: read `status.status`, lower-cased, against the working set.
    # Why:  matched on the lower-cased *name* rather than the enum object because
    #       the concrete literals have been wrong before (§7.2 found `FORDER` /
    #       `FDEAL` where the docs showed `FuturesOrder` / `FuturesDeal`). An
    #       unrecognised status counts as **not** working, so the worst case is a
    #       cancel we skip — never a position we mistake for flat.
    status = getattr(getattr(trade, "status", None), "status", None)
    return str(status).split(".")[-1].lower() in WORKING_STATUSES


def cancel_working(api: Any, account: Any, codes: set) -> int:
    """Cancel every still-working order on our contract; return how many."""
    # Tech: refresh statuses, then cancel each working trade whose contract code
    #       is one of ours, tolerating per-order failures.
    # Why:  `update_status` is what populates `ordno`, which `cancel_order`
    #       needs. Failures are counted and reported rather than raised: one
    #       stubborn order must not stop the others being withdrawn, and the
    #       position read afterwards is the real check.
    try:
        api.update_status(account)
        trades = api.list_trades() or []
    except Exception as exc:
        print(f"  ! could not list working orders: {exc!r}")
        return 0
    n = 0
    for trade in trades:
        code = str(getattr(getattr(trade, "contract", None), "code", "") or "")
        if codes and code and code not in codes:
            continue
        if not is_working(trade):
            continue
        try:
            api.cancel_order(trade)
            n += 1
        except Exception as exc:
            print(f"  ! cancel failed for {code}: {exc!r}")
    return n


def report_orders(api: Any, account: Any, codes: set) -> None:
    """Print every order on our contract this session, with what became of it."""
    # Tech: the same `update_status` + `list_trades` pair `cancel_working` runs,
    #       printed instead of filtered to the working ones.
    # Why:  STRATEGY_SPLIT_PLAN §23.2 — after G6 reported STILL HOLDING, the
    #       operator's question was "did my closes fill?", and this tool already
    #       had the answer in hand and discarded it. On a netted account two
    #       opposed lots read as `+0`, so "already flat" is true and still does
    #       not say whether four orders filled or none did. `deal_quantity` is
    #       what separates them, and nothing else here reports it.
    try:
        api.update_status(account)
        trades = api.list_trades() or []
    except Exception as exc:                            # noqa: BLE001
        print(f"  ! could not list orders: {exc!r}")
        return
    ours = [t for t in trades
            if not codes
            or str(getattr(getattr(t, "contract", None), "code", "") or "") in codes]
    print(f"\norders on this contract this session: {len(ours)}")
    for trade in ours:
        order = getattr(trade, "order", None)
        status = getattr(trade, "status", None)
        print(f"  ordno={getattr(order, 'ordno', None)!r} "
              f"{str(getattr(order, 'action', None)).split('.')[-1]:4s} "
              f"qty={getattr(order, 'quantity', None)} "
              f"@{getattr(order, 'price', None)} "
              f"status={str(getattr(status, 'status', None)).split('.')[-1]} "
              f"dealt={getattr(status, 'deal_quantity', None)}")


def close_price(reference: float, side: str,
                aggression: float = CLOSE_AGGRESSION_POINTS,
                tick_size: float = 1.0) -> float:
    # Tech: price the close `aggression` points through the touch, snapped to the
    #       grid — up for a BUY, down for a SELL.
    # Why:  the direction is the whole point: a BUY closing a short must be
    #       willing to pay *more*. 30 points is far more aggressive than the
    #       5-tick live flatten because this runs attended, after something has
    #       already gone wrong, and filling now matters more than the last tick.
    #       Still a bounded limit — the worst price is knowable before sending.
    raw = reference + aggression if side == "BUY" else reference - aggression
    return float(round(raw / tick_size) * tick_size)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--contract", default="TXFR1")
    ap.add_argument("--credential", default="night",
                    help="Which section of config/secrets.toml to log in with "
                         "(default: night). All three name the same futures "
                         "account (§13), so this only chooses the login.")
    ap.add_argument("--show-orders", action="store_true",
                    help="List every order on this contract this session with "
                         "its status and dealt quantity, before doing anything. "
                         "Read-only. On a netted account two opposed lots read "
                         "as +0, so a flat position does not say whether orders "
                         "filled (STRATEGY_SPLIT_PLAN 23.2).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Report the position and the order that would be sent")
    ap.add_argument("--out-parent", default=None,
                    help="Directory holding the deployments' fills_<slug>.csv. "
                         "When given, a flatten that would close more than one "
                         "deployment's lots says whose they are (§18.3 item 8).")
    ap.add_argument("--attempts", type=int, default=3)
    ap.add_argument("--wait-seconds", type=float, default=8.0)
    ap.add_argument("--aggression-points", type=float,
                    default=CLOSE_AGGRESSION_POINTS)
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    # Tech: resolve the credential through `credentials.load`, which reads
    #       `config/secrets.toml` first and falls back to the legacy
    #       SHIOAJI_API_KEY/SHIOAJI_SECRET_KEY pair.
    # Why:  this was the last live entry point still reading the environment
    #       directly, so the emergency tool was the ONE that could not log in on
    #       a machine configured the way every other tool here expects — found
    #       2026-09-07 when the flatten in §22.5 could not run. The fallback
    #       keeps existing env-only setups working unchanged.
    from forecast_eval.live import credentials
    cred = credentials.load(args.credential)
    if cred is None:
        print(f"ERROR: no credential for {args.credential!r} — add a "
              f"[{args.credential}] section to config/secrets.toml, or set "
              f"SHIOAJI_API_KEY and SHIOAJI_SECRET_KEY.", file=sys.stderr)
        return 2
    print(f"credential {args.credential} (from {cred.source})")

    import shioaji as sj
    api = sj.Shioaji(simulation=True)
    try:
        api.login(api_key=cred.api_key, secret_key=cred.secret_key)
        contract, note = resolve_txf(api, args.contract)
        codes = contract_codes(contract)
        account = getattr(api, "futopt_account", None)
        print(f"contract {note}")

        # Tech: withdraw any order still working on this contract before reading
        #       the position and sending a close.
        # Why:  a resting close from a killed process or an aborted probe is
        #       invisible in `list_positions`, and placing a second Cover on top
        #       of it can fill twice against a single lot — turning a long into a
        #       short, which is a worse state than the one being repaired. ROD
        #       makes this reachable in a way IOC did not (§2.19), so the check
        #       belongs here rather than being assumed away.
        if args.show_orders:
            report_orders(api, account, codes)

        cancelled = cancel_working(api, account, codes)
        if cancelled:
            print(f"cancelled {cancelled} working order(s) first")
            time.sleep(1.0)

        held = net_position(api.list_positions(account), codes)
        print(f"position: {held:+d} lot(s)")
        if held == 0:
            print("already flat — nothing to do.")
            return 0

        # Tech: name every deployment whose fill ledger says it is holding, and
        #       say plainly that this close is account-wide.
        # Why:  STRATEGY_SPLIT_PLAN §18.3 item 8. This tool reads
        #       `list_positions`, which on a shared book is the SUM of what the
        #       deployments hold (§14) — so closing it closes other people's
        #       positions too, and their mirrors will not know. That is still the
        #       right behaviour for the emergency tool: it exists precisely for
        #       when nothing else can be trusted to close a position, and a
        #       flatten that could only close a third of the account would be
        #       useless in the one situation it is for. What it must not do is
        #       stay silent about it — so the operator is told whose lots are
        #       about to be closed, and told to restart those deployments with
        #       --fresh-start afterwards, because a ledger that still claims a
        #       lot the venue no longer holds halts on the first bar.
        _warn_other_deployments(Path(args.out_parent) if args.out_parent else None,
                                held)

        side = "SELL" if held > 0 else "BUY"
        quantity = abs(held)

        for attempt in range(1, args.attempts + 1):
            reference = _reference(api, contract)
            if reference is None:
                print("ERROR: no reference price (market closed?). A close "
                      "cannot be priced; retry during a session.",
                      file=sys.stderr)
                return 3
            limit = close_price(reference, side, args.aggression_points)
            print(f"\n[{attempt}/{args.attempts}] {side} {quantity} @ {limit} "
                  f"(reference {reference}, {args.aggression_points:.0f} through)")
            if args.dry_run:
                print("  --dry-run: nothing sent.")
                return 0

            order = sj.FuturesOrder(
                action=(sj.Action.Sell if side == "SELL" else sj.Action.Buy),
                price=limit, quantity=quantity,
                price_type=sj.FuturesPriceType.LMT,
                order_type=sj.OrderType.ROD,
                octype=sj.FuturesOCType.Cover,
                account=account,
            )
            api.place_order(contract, order)
            time.sleep(args.wait_seconds)

            held = net_position(api.list_positions(account), codes)
            print(f"  position now: {held:+d}")
            if held == 0:
                print("\nFLAT.")
                return 0
            quantity = abs(held)
            side = "SELL" if held > 0 else "BUY"

        print(f"\n*** STILL HOLDING {held:+d} lot(s) after {args.attempts} "
              f"attempts. Close it in the broker's own front end. ***",
              file=sys.stderr)
        return 4
    finally:
        try:
            api.logout()
        except Exception:
            pass


def _reference(api: Any, contract: Any) -> Optional[float]:
    try:
        snaps = api.snapshots([contract])
        return float(getattr(snaps[0], "close", 0)) or None
    except Exception as exc:
        print(f"  ! snapshots failed: {exc!r}")
        return None


if __name__ == "__main__":
    sys.exit(main())
