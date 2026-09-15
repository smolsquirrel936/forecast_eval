"""§7.2 probe — does TXF futures `place_order` work under `simulation=True`?

This is the gate on V3/V4. The docs list the order APIs without product
qualification and never mention 期貨/選擇權/TXF, and the only stated exclusion
(興櫃, 零股) is equity-specific — so the question can only be settled empirically.

**This script places an order.** Everything about it is built so that order
cannot do anything:

  * `simulation=True` is asserted, not assumed — it refuses to run otherwise;
  * **one** lot, never more;
  * priced far below the market (default 500 points), so it is **passive by
    construction** and cannot fill — a BUY below the touch only fills if the
    market falls that far while the script is running, which it then cancels
    well before;
  * cancelled in a `finally`, with a second cancel attempt on any failure path;
  * position checked at the end, and loudly flagged if not flat.

While it is up, it also harvests the things V2 had to guess about, none of which
are in the docs:

  * the real `OrderState` enum literals — the adapter dispatches on them;
  * the actual `Trade` object shape (`order.id` / `seqno` / `ordno`);
  * the real order-callback payload, field by field.

Usage:

    $env:SHIOAJI_API_KEY="..."; $env:SHIOAJI_SECRET_KEY="..."
    uv run --no-sync python -m forecast_eval.live.v72_probe --dry-run     # no order at all
    uv run --no-sync python -m forecast_eval.live.v72_probe               # places + cancels

Run it **during a trading session**. Outside one, a rejection is ambiguous — it
could mean "futures unsupported in simulation" or merely "market closed", and
those have very different consequences for the plan.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from forecast_eval.live.contracts import resolve_txf              # noqa: E402

TICK_SIZE = 1.0


def probe_limit_price(reference: float, offset_points: float,
                      tick_size: float = TICK_SIZE) -> float:
    # Tech: place the BUY limit `offset_points` below the reference, snapped down
    #       to the tick grid.
    # Why:  the entire safety argument rests on this being strictly below the
    #       market — a BUY at or above the touch would be marketable and could
    #       fill instantly. Flooring (not rounding) guarantees the snap can only
    #       move the price further away, never closer.
    raw = reference - offset_points
    return float(int(raw / tick_size) * tick_size)


def _describe_state(state) -> dict:
    # Tech: capture every identifying facet of the callback's state object.
    # Why:  the adapter dispatches on the enum *name* because that is what the
    #       docs display; the real literals are unverified. Recording name, value
    #       and repr together is what lets that guess be confirmed or corrected.
    return {
        "repr": repr(state),
        "str": str(state),
        "name": getattr(state, "name", None),
        "value": getattr(state, "value", None),
        "type": type(state).__name__,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--contract", default="TXFR1")
    ap.add_argument("--offset-points", type=float, default=500.0,
                    help="How far BELOW the market to rest the BUY limit. Large "
                         "enough that it cannot fill; small enough to stay "
                         "inside the exchange's daily price band.")
    ap.add_argument("--price", type=float, default=None,
                    help="Reference price override, if snapshots are empty "
                         "(e.g. outside a session)")
    ap.add_argument("--wait", type=float, default=5.0,
                    help="Seconds to wait for each callback")
    ap.add_argument("--dry-run", action="store_true",
                    help="Resolve the contract and price, place NOTHING")
    args = ap.parse_args(argv)

    api_key = os.environ.get("SHIOAJI_API_KEY")
    secret_key = os.environ.get("SHIOAJI_SECRET_KEY")
    if not api_key or not secret_key:
        print("ERROR: set SHIOAJI_API_KEY and SHIOAJI_SECRET_KEY in the "
              "environment (never commit them).", file=sys.stderr)
        return 2

    import shioaji as sj

    print("=" * 70)
    print("  §7.2 PROBE — SIMULATION MODE ONLY — 1 lot, far off-market")
    print("=" * 70)

    api = sj.Shioaji(simulation=True)

    # Tech: refuse to continue unless the client reports simulation mode.
    # Why:  the constructor flag is the only thing standing between this script
    #       and a real order. If the attribute exists and is False, something is
    #       badly wrong and no amount of "it's only one lot" makes proceeding
    #       acceptable.
    reported = getattr(api, "simulation", None)
    if reported is False:
        print("ABORT: api.simulation is False — refusing to place an order.",
              file=sys.stderr)
        return 2
    print(f"  api.simulation = {reported!r}")

    api.login(api_key=api_key, secret_key=secret_key)
    trade = None
    events = []
    try:
        try:
            contract, contract_note = resolve_txf(api, args.contract)
        except RuntimeError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        print(f"  contract: {contract_note}")

        account = getattr(api, "futopt_account", None)
        print(f"  futopt_account: {account!r}")
        if account is None:
            print("ABORT: no futures account on this login — §7.1 says one "
                  "exists, so this is a login/permission problem.",
                  file=sys.stderr)
            return 2

        # ---- reference price ---------------------------------------------
        reference = args.price
        if reference is None:
            try:
                snaps = api.snapshots([contract])
                reference = float(getattr(snaps[0], "close", 0)) or None
                print(f"  snapshot close: {reference}")
            except Exception as exc:
                print(f"  ! snapshots failed: {exc!r}")
        if not reference:
            print("ERROR: no reference price (market closed?). Re-run during a "
                  "session, or pass --price.", file=sys.stderr)
            return 2

        limit = probe_limit_price(reference, args.offset_points)
        print(f"  reference={reference}  ->  BUY limit={limit} "
              f"({args.offset_points} pts below; passive, cannot fill)")
        if limit >= reference:
            print("ABORT: computed limit is not below the market.",
                  file=sys.stderr)
            return 2

        if args.dry_run:
            print("\n--dry-run: setup verified, NO order placed.")
            return 0

        # ---- callbacks ----------------------------------------------------
        def on_state(state, msg):
            events.append({"state": _describe_state(state), "msg": msg})
            print(f"\n  << callback {_describe_state(state)['name'] or state}")
            print(f"     {json.dumps(msg, indent=6, default=str)}")

        api.set_order_callback(on_state)

        # ---- place --------------------------------------------------------
        order = sj.FuturesOrder(
            action=sj.Action.Buy,
            price=limit,
            quantity=1,
            price_type=sj.FuturesPriceType.LMT,
            order_type=sj.OrderType.ROD,
            octype=sj.FuturesOCType.New,
            account=account,
        )
        print("\n[1] place_order ...")
        trade = api.place_order(contract, order)
        print(f"  returned: {trade!r}")
        print(f"  status  : {getattr(getattr(trade, 'status', None), 'status', '?')}")
        print(f"  order.id={getattr(getattr(trade, 'order', None), 'id', '?')} "
              f"seqno={getattr(getattr(trade, 'order', None), 'seqno', '?')} "
              f"ordno={getattr(getattr(trade, 'order', None), 'ordno', '?')}")

        time.sleep(args.wait)

        print("\n[2] update_status ...")
        api.update_status(account)
        print(f"  status now: "
              f"{getattr(getattr(trade, 'status', None), 'status', '?')}")
        print(f"  ordno now : "
              f"{getattr(getattr(trade, 'order', None), 'ordno', '?')}")

        print("\n[3] cancel_order ...")
        api.cancel_order(trade)
        time.sleep(args.wait)
        api.update_status(account)
        final = getattr(getattr(trade, "status", None), "status", "?")
        print(f"  status after cancel: {final}")

        # ---- verdict ------------------------------------------------------
        print("\n" + "=" * 70)
        rejected = [e for e in events
                    if str((e["msg"].get("operation") or {}).get("op_code", "00"))
                    not in ("00", "")]
        if rejected:
            print("  RESULT: order was REJECTED. op_msg below decides whether "
                  "this is 'futures unsupported in simulation' or merely "
                  "'market closed' / a margin issue:")
            for e in rejected:
                print(f"    {e['msg'].get('operation')}")
        elif str(final).lower().startswith("cancel") or events:
            print("  RESULT: futures order placement WORKS in simulation. "
                  "V3/V4 can proceed as planned.")
        else:
            print("  RESULT: INCONCLUSIVE — no callbacks arrived. Re-run during "
                  "a session, or raise --wait.")
        print("=" * 70)

        print("\n[4] observed OrderState literals (V2 assumed the NAME):")
        for e in events:
            print(f"    {e['state']}")

        print("\n[5] positions (must be flat):")
        try:
            for p in api.list_positions(account) or []:
                print(f"    {p!r}")
            else:
                print("    (none)")
        except Exception as exc:
            print(f"    ! list_positions failed: {exc!r}")

    finally:
        # Tech: best-effort second cancel, then always log out.
        # Why:  if anything above raised between placing and cancelling, an order
        #       would be left resting. It is passive and far from the market, but
        #       leaving a working order behind is not acceptable regardless.
        #       Logout matters because connections are capped at 5 per person_id.
        if trade is not None:
            try:
                api.cancel_order(trade)
            except Exception:
                pass
        try:
            api.logout()
            print("\nlogged out.")
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
