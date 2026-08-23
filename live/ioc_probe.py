"""IOC probe — what does the venue report when an IOC expires unfilled?

The scheduled contract roll (§2.6) wants an **escalating-aggression ladder**: an
IOC priced through the touch, re-priced every few seconds until it fills. IOC is
the right time-in-force for that — there is nothing resting to cancel, so the
retry *is* the re-price, and the cancel/replace race of §2.2 never happens.

One measured fact blocks it. From the V4 probe (2026-08-12, §2.19):

    an expired IOC left `has_pending()` **True**

If that is still so, every retry in the ladder is blocked by the pending check
and the whole thing is **inert while logging beautifully** — the same shape as
the plain-`login()` no-op that would have made the §2.24 fix hollow. So before
any of it is built, one question has to be answered by measurement:

  Q1  does an IOC that expires unfilled produce an order callback at all?
  Q2  what does that message carry — `op_type`, `op_code`, `status`? Is there
      anything `ShioajiBroker._handle_order` could key on to retire the order?
  Q3  how long does it take to arrive? (the ladder's retry interval cannot be
      shorter than the answer, or attempt N+1 races attempt N's expiry)
  Q4  is it consistent across attempts, or did 2026-08-12 see a one-off?

**Safety.** Every order this places is unfillable by construction: a BUY priced
`--offset` points *below* the market, one lot, in an account where
`simulation=True` is asserted rather than assumed. An IOC there cannot fill — it
can only expire, which is the thing being measured. The optional ROD control is
cancelled in a `finally`.

**This must run inside a trading session.** Off-hours the venue *rejects* an
order rather than expiring it, and "rejected because the market is closed" is a
different answer to "expired because nothing matched" — one that would look like
a measurement while telling us nothing.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from forecast_eval.live.contracts import resolve_txf              # noqa: E402
from forecast_eval.live.v72_probe import probe_limit_price        # noqa: E402

# Tech: how far below the market the probe's BUY is priced, in index points.
# Why:  the whole safety argument is that this order cannot fill. 200 points on a
#       ~45 000 index is 0.4 % — far enough that no plausible print reaches it
#       inside the probe's ten-second window, and nowhere near the ±10 % daily
#       limit that would get the order rejected instead of accepted.
DEFAULT_OFFSET = 200.0

# Tech: identifying fields to strip from any SDK repr before it is printed, and
#       the shape of a Taiwanese national ID.
# Why:  MEASURED TWICE on the §2.24 probe — field-name redaction alone was not
#       enough, because the SDK embeds the ID *bare* inside event strings where
#       there is no `person_id=` to match on. This console output is meant to be
#       pasted into a chat; both passes are reproduced here rather than imported
#       so a probe can never be run without them.
_SECRET_FIELDS = ("person_id", "account_id")
_ID_PATTERN = re.compile(r"\b[A-Z][0-9]{9}\b")


def redact(text: str) -> str:
    """Blank identifying fields, then any bare national-ID-shaped token."""
    for field in _SECRET_FIELDS:
        text = re.sub(rf"{field}=('[^']*'|\"[^\"]*\"|\w+)",
                      f"{field}=<redacted>", text)
    return _ID_PATTERN.sub("<redacted-id>", text)


def message_facts(msg: Any) -> Dict[str, Any]:
    # Tech: pull the fields `ShioajiBroker._handle_order` dispatches on out of a
    #       callback message, whatever shape it arrives in.
    # Why:  the decision this probe informs is precisely "is there something to
    #       key on". Reporting the same fields the adapter already reads means the
    #       answer can be applied directly instead of re-interpreted, and the raw
    #       repr is kept beside them so a field we did not think to ask for is
    #       still visible.
    if not isinstance(msg, dict):
        return {"raw": redact(repr(msg))[:300]}
    order = msg.get("order") or {}
    status = msg.get("status") or {}
    return {
        "op_type": msg.get("op_type"),
        "op_code": msg.get("op_code"),
        "op_msg": redact(str(msg.get("op_msg", "")))[:80],
        "status": status.get("status") if isinstance(status, dict) else None,
        "order_type": (order.get("order_type")
                       if isinstance(order, dict) else None),
        "price_type": (order.get("price_type")
                       if isinstance(order, dict) else None),
        "quantity": order.get("quantity") if isinstance(order, dict) else None,
        "raw": redact(repr(msg))[:300],
    }


def verdict(events: List[Dict[str, Any]], waited: float) -> Dict[str, Any]:
    # Tech: reduce one attempt's callbacks to the four answers, without judging
    #       what they *should* have been.
    # Why:  a probe that decides the design is a probe that can be wrong twice.
    #       This says what arrived and how fast; whether that is enough to retire
    #       an order is a reading made against the adapter, not here.
    if not events:
        return {"reported": False, "n_events": 0, "first_after": None,
                "op_types": [], "note": f"nothing in {waited:.0f}s"}
    ops = [e["facts"].get("op_type") for e in events]
    return {
        "reported": True,
        "n_events": len(events),
        "first_after": round(events[0]["elapsed"], 3),
        "last_after": round(events[-1]["elapsed"], 3),
        "op_types": ops,
        "op_codes": [e["facts"].get("op_code") for e in events],
        "statuses": [e["facts"].get("status") for e in events],
    }


def summarise(findings: Dict[str, Any]) -> str:
    # Tech: render the answers as a block meant to be pasted back verbatim.
    # Why:  the operator runs this and reports; a probe whose output has to be
    #       interpreted before it can be reported produces arguments rather than
    #       measurements. Already redacted — but say so, because the SDK prints
    #       its own banner lines above this that are not.
    lines = ["", "=" * 72, "  PASTE THIS BACK (already redacted)", "=" * 72]
    for key in sorted(findings):
        lines.append(f"  {key:28s} {findings[key]}")
    lines.append("=" * 72)
    lines.append("  NOTE: lines ABOVE this block come from the SDK and may")
    lines.append("        contain your national ID. Paste only this block.")
    lines.append("=" * 72)
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--contract", default="TXFR1")
    ap.add_argument("--attempts", type=int, default=3,
                    help="how many IOC orders to place (default 3)")
    ap.add_argument("--wait", type=float, default=10.0,
                    help="seconds to listen after each order (default 10)")
    ap.add_argument("--offset", type=float, default=DEFAULT_OFFSET,
                    help="points BELOW the market to price the BUY (default 200)")
    ap.add_argument("--control-rod", action="store_true",
                    help="also place an unfillable ROD and cancel it, to compare "
                         "what a cancellation reports against an expiry")
    ap.add_argument("--i-understand-this-places-orders", action="store_true",
                    dest="ack",
                    help="required: this places real (simulated) orders")
    args = ap.parse_args(argv)

    if not args.ack:
        print("REFUSED: this probe places orders. Re-run with "
              "--i-understand-this-places-orders", file=sys.stderr)
        return 2

    api_key = os.environ.get("SHIOAJI_API_KEY")
    secret_key = os.environ.get("SHIOAJI_SECRET_KEY")
    if not (api_key and secret_key):
        print("REFUSED: SHIOAJI_API_KEY / SHIOAJI_SECRET_KEY are not set",
              file=sys.stderr)
        return 2

    import shioaji as sj

    api = sj.Shioaji(simulation=True)
    # Tech: refuse to continue unless the client itself reports simulation mode.
    # Why:  §7.2's rule, unchanged — the safety argument for placing orders at all
    #       rests on this being a simulation account, and an assumption is not an
    #       assertion. Every probe in this directory checks it the same way.
    if getattr(api, "simulation", None) is not True:
        print("ABORT: api.simulation is not True — refusing to place orders.",
              file=sys.stderr)
        return 3
    api.login(api_key=api_key, secret_key=secret_key)

    contract, note = resolve_txf(api, args.contract)
    print(f"contract {note}")

    findings: Dict[str, Any] = {"contract": note.split(" (via")[0]}
    events: List[Dict[str, Any]] = []
    t0 = [time.time()]

    def on_order(state, msg):
        # Tech: stamp every order callback with its delay since the last submit.
        # Why:  Q3 is a latency question — the ladder's retry interval cannot be
        #       shorter than the time an expiry takes to be reported, or attempt
        #       N+1 races attempt N's expiry and the pending check sees both.
        events.append({"elapsed": time.time() - t0[0],
                       "state": str(state),
                       "facts": message_facts(msg)})

    api.set_order_callback(on_order)

    try:
        snap = api.snapshots([contract], timeout=5000)[0]
        reference = float(getattr(snap, "close", 0) or 0)
        if reference <= 0:
            print("ABORT: snapshot returned no usable close — is the session "
                  "open? This probe must run inside one.", file=sys.stderr)
            return 4
        price = probe_limit_price(reference, args.offset)
        findings["reference_close"] = reference
        findings["probe_price"] = price
        print(f"market {reference} -> unfillable BUY at {price} "
              f"({args.offset:.0f} points below)")

        for attempt in range(1, args.attempts + 1):
            events.clear()
            t0[0] = time.time()
            order = sj.FuturesOrder(
                action=sj.Action.Buy,
                price=price,
                quantity=1,
                price_type=sj.FuturesPriceType.LMT,
                order_type=sj.OrderType.IOC,
                octype=sj.FuturesOCType.New,
                account=api.futopt_account,
            )
            placed = datetime.now()
            trade = api.place_order(contract, order)
            print(f"[{attempt}] IOC submitted at {placed:%H:%M:%S} "
                  f"-> {redact(repr(getattr(trade, 'status', None)))[:70]}")
            time.sleep(args.wait)
            api.update_status(api.futopt_account)
            time.sleep(0.5)
            v = verdict(list(events), args.wait)
            findings[f"attempt_{attempt}"] = v
            for e in events:
                print(f"    +{e['elapsed']:6.3f}s  state={e['state']}  "
                      f"op_type={e['facts'].get('op_type')} "
                      f"op_code={e['facts'].get('op_code')} "
                      f"status={e['facts'].get('status')}")
            if not events:
                print("    (no callback at all)")
            else:
                print(f"    first message raw: {events[0]['facts']['raw']}")

        # Tech: the same order as a ROD, cancelled — the path the adapter already
        #       handles — so an expiry can be compared against a known-good shape.
        # Why:  if an IOC expiry reports the same way a cancellation does, the
        #       adapter may already retire it and the ladder needs no change at
        #       all. That is the cheapest possible outcome and worth one order to
        #       find out. Cancelled in a `finally` so nothing is left resting.
        if args.control_rod:
            events.clear()
            t0[0] = time.time()
            rod = sj.FuturesOrder(
                action=sj.Action.Buy, price=price, quantity=1,
                price_type=sj.FuturesPriceType.LMT,
                order_type=sj.OrderType.ROD,
                octype=sj.FuturesOCType.New, account=api.futopt_account)
            trade = api.place_order(contract, rod)
            print("[control] ROD submitted, cancelling in 3s")
            try:
                time.sleep(3)
                api.cancel_order(trade)
                time.sleep(args.wait)
                api.update_status(api.futopt_account)
            finally:
                findings["control_rod"] = verdict(list(events), args.wait)
                for e in events:
                    print(f"    +{e['elapsed']:6.3f}s  "
                          f"op_type={e['facts'].get('op_type')} "
                          f"op_code={e['facts'].get('op_code')} "
                          f"status={e['facts'].get('status')}")

        print(summarise(findings))
        return 0
    finally:
        try:
            api.logout()
        except Exception:                              # noqa: BLE001
            pass


if __name__ == "__main__":
    raise SystemExit(main())
