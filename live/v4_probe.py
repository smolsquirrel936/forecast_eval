"""V4 probe — drive one order through the real stack, on demand.

The strategy signals about **once a night** (28 stride boundaries × 3.74 % non-HOLD),
so waiting for it to exercise the order path costs an evening per attempt. Every
V4 component below `LiveLoop` — `ShioajiBroker.submit`, the order and deal
callbacks, fill dedup, the urgent flatten, and the whole `ShadowBroker`
comparison — has therefore never run against the venue. This places the order
deliberately instead.

**Not `v72_probe`.** That one calls `api.place_order` directly and answers "does
the venue accept a futures order in simulation?". This one goes through
`ShadowBroker → ShioajiBroker`, so what it tests is *our* code: the adapter, the
callbacks, and the measurement.

### Two modes, and only one of them can hold a position

**Default — passive.** A BUY priced far below the touch. It cannot fill, so it
exercises submit → order callback → cancel → the "neither side filled" path of
the gap report. Zero exposure by construction.

**`--allow-fill` — a round trip.** Buys marketably, waits for the fill, then
closes through the **urgent** path. This is the only way to reach the parts most
likely to be wrong — the deal callback, dedup, `FillEvent` construction, and the
shadow's slippage comparison — because none of them runs without a real fill. It
holds one lot for seconds, verifies flat at the end, and reports loudly and with
a non-zero exit if it fails to close.

Safety is structural, matching `v72_probe`: quantity is fixed at 1, the passive
limit floors (so the tick snap can only move it further away), the close runs in
a `finally` with a second attempt, and the closing position is printed.

    uv run --no-sync python -m forecast_eval.live.v4_probe                # passive
    uv run --no-sync python -m forecast_eval.live.v4_probe --allow-fill   # round trip
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from forecast_eval.events import MarketEvent, OrderEvent        # noqa: E402
from forecast_eval.live.broker.shadow import ShadowBroker       # noqa: E402
from forecast_eval.live.broker.shioaji import ShioajiBroker     # noqa: E402
from forecast_eval.live.config import PINNED                    # noqa: E402
from forecast_eval.live.contracts import resolve_txf            # noqa: E402
from forecast_eval.live.recorder import Recorder                # noqa: E402

PASSIVE_OFFSET_POINTS = 500.0


def passive_limit(reference: float, offset: float = PASSIVE_OFFSET_POINTS,
                  tick_size: float = 1.0) -> float:
    # Tech: place the BUY limit `offset` below the reference, floored to the grid.
    # Why:  the no-fill guarantee rests entirely on this being strictly below the
    #       market. Flooring rather than rounding means the snap can only move the
    #       price further away, never back towards a fill. Same argument, and the
    #       same 500-point default, as `v72_probe` — far enough not to fill, near
    #       enough to stay inside TAIFEX's ±10 % daily band, outside which the
    #       order is rejected and the rejection looks like "unsupported".
    return float(int((reference - offset) / tick_size) * tick_size)


def marketable_limit(reference: float, offset: float = 20.0,
                     tick_size: float = 1.0) -> float:
    # Tech: a BUY limit `offset` ABOVE the reference, rounded to the grid.
    # Why:  `--allow-fill` needs the order to actually fill, and a limit at the
    #       touch may simply join the queue (that is §2.1's whole point). Above
    #       the touch it crosses. Still a bounded limit rather than MKT, so the
    #       worst price is knowable before it is sent.
    return float(round((reference + offset) / tick_size) * tick_size)


def live_api_of(broker: Any) -> Any:
    """The Shioaji api behind a ShadowBroker, for a fresh snapshot."""
    return getattr(getattr(broker, "live", None), "api", None)


def _reference_price(api: Any, override: Optional[float]) -> Optional[float]:
    # Tech: the snapshot close, or the override when one is supplied.
    # Why:  one place to ask "what is the market now", so the open and the close
    #       cannot end up pricing off different vintages of the same answer —
    #       which is exactly the bug that left a position open.
    if override is not None:
        return override
    if api is None:
        return None
    try:
        snaps = api.snapshots([_CONTRACT_HOLDER.get("contract")])
        return float(getattr(snaps[0], "close", 0)) or None
    except Exception as exc:
        print(f"  ! snapshots failed: {exc!r}")
        return None


# Tech: the resolved contract, stashed so `_reference_price` can re-snapshot.
# Why:  the alternative is threading the contract through every helper for the
#       sake of one call; a probe is allowed the simpler shape.
_CONTRACT_HOLDER: dict = {}


def _market(price: float) -> MarketEvent:
    # Tech: a MarketEvent standing in for the bar the loop would have supplied.
    # Why:  `poll_fills` takes one because the offline model resolves *from* the
    #       print. The live broker ignores it; the shadow needs it to decide
    #       whether its own limit would have filled.
    return MarketEvent(datetime.now(), float(price), 1, "NIGHT")


def _drain(broker: ShadowBroker, price: float, *, seconds: float,
           label: str, api: Any = None, limit: Optional[float] = None) -> list:
    # Tech: poll for `seconds`, collecting fills and sampling the market as we
    #       go; report the range the market covered and whether it ever reached
    #       the order's limit.
    # Why:  fills arrive on Shioaji's callback thread, so they are not available
    #       the instant `place_order` returns — the docs warn a deal event can
    #       even precede its order event.
    #
    #       The market sampling is the part that was missing, and its absence
    #       cost a wrong conclusion: when a close failed to fill, there was no
    #       record of where the market had gone, so "IOC does not work" got
    #       inferred from what was actually a stale price. A non-fill is only
    #       interesting once we can see whether the market was ever reachable.
    # Tech: drain fills every 0.25 s, but sample the market at most once a
    #       second.
    # Why:  §2.8b — market-data queries are capped at **50 per 10 s** across
    #       `snapshots`/`kbars`/`ticks`, and repeated breaches are answered with
    #       an IP and ID suspension that further attempts extend. Sampling on
    #       every drain iteration would be 4/s, i.e. 40 per 10 s from this loop
    #       alone — under the cap but with no headroom for anything else running.
    #       Fills need the fast loop; the market path does not.
    fills, seen = [], []
    deadline = time.time() + seconds
    next_sample = 0.0
    while time.time() < deadline:
        got = broker.poll_fills(_market(price))
        if got:
            fills.extend(got)
            print(f"  [{label}] fill: " + ", ".join(
                f"{f.side} {f.quantity} @ {f.fill_price}" for f in got))
        now = time.time()
        if api is not None and now >= next_sample:
            next_sample = now + 1.0
            tick = _reference_price(api, None)
            if tick:
                seen.append(tick)
        time.sleep(0.25)

    if seen:
        low, high = min(seen), max(seen)
        note = f"  [{label}] market during window: {low} .. {high} (last {seen[-1]})"
        if limit is not None and not fills:
            reachable = low <= limit if label != "close" else high >= limit
            note += ("  -> the limit WAS reachable; the venue did not fill it"
                     if reachable else
                     "  -> the market never came to the limit")
        print(note)
    return fills


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--contract", default="TXFR1")
    ap.add_argument("--allow-fill", action="store_true",
                    help="Place a MARKETABLE order and close it again. Holds one "
                         "lot for seconds; the only way to exercise the deal "
                         "callback, dedup and the slippage comparison.")
    ap.add_argument("--price", type=float, default=None,
                    help="Reference price override when snapshots are empty")
    ap.add_argument("--wait-seconds", type=float, default=20.0,
                    help="How long to poll for fills after each submit")
    ap.add_argument("--out", type=Path, default=None,
                    help="Artifact dir; default outputs/v4probe_<timestamp>")
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    cfg = PINNED

    api_key = os.environ.get("SHIOAJI_API_KEY")
    secret_key = os.environ.get("SHIOAJI_SECRET_KEY")
    if not api_key or not secret_key:
        print("ERROR: set SHIOAJI_API_KEY and SHIOAJI_SECRET_KEY.",
              file=sys.stderr)
        return 2

    out = args.out or Path("outputs") / f"v4probe_{datetime.now():%Y%m%d_%H%M%S}"
    out.mkdir(parents=True, exist_ok=True)
    recorder = Recorder(out / "session.jsonl", simulation=True,
                        meta={"probe": "v4", "allow_fill": args.allow_fill})

    import shioaji as sj
    api = sj.Shioaji(simulation=True)
    exit_code = 0
    broker: Optional[ShadowBroker] = None
    try:
        api.login(api_key=api_key, secret_key=secret_key)
        contract, note = resolve_txf(api, args.contract)
        print(f"contract {note}")
        _CONTRACT_HOLDER["contract"] = contract

        reference = _reference_price(api, args.price)
        if not reference:
            print("ERROR: no reference price (market closed?). Run during a "
                  "session, or pass --price.", file=sys.stderr)
            return 2
        print(f"reference: {reference}")

        live = ShioajiBroker(api, contract, sj=sj, recorder=recorder,
                            fee_rate=cfg.fee_rate, tick_size=cfg.tick_size)
        broker = ShadowBroker(live, fee_rate=cfg.fee_rate, recorder=recorder)

        held = live.broker_position()
        print(f"position before: {held!r}")
        if held:
            print(f"REFUSING: the account already holds {held} lot(s). Flatten "
                  f"it first — this probe cannot tell its own order from yours.",
                  file=sys.stderr)
            return 5

        if args.allow_fill:
            exit_code = _round_trip(broker, live, reference, cfg, args)
        else:
            exit_code = _passive(broker, reference, cfg, args)

    except Exception as exc:
        print(f"ERROR: {exc!r}", file=sys.stderr)
        exit_code = 1
    finally:
        if broker is not None:
            broker.close()
            print(broker.format_summary())
            # Tech: record under a *probe-specific* event name, never
            #       `fill_gap_summary`, and say so on the console.
            # Why:  these numbers are artifacts, not data. The shadow is fed a
            #       synthetic `MarketEvent` built from one fixed price four times
            #       a second, so it fills trivially and its 100 % rate means
            #       nothing; and the live side is a deliberately marketable order,
            #       where the strategy's sit *at* the touch. Aggregating V4's
            #       ≥5 sessions by scanning for `fill_gap_summary` must not sweep
            #       these up — the phase's headline number would be wrong and
            #       nothing would flag it.
            print("\nNOTE: the figures above are a plumbing check, not V4 data —"
                  "\n      the shadow is fed synthetic prints and the live order"
                  "\n      is deliberately marketable. Only live sessions count.")
            recorder.record("probe_fill_gap_summary", **broker.gap_summary())
            if getattr(broker.live, "anomalies", None):
                print("anomalies reported by the adapter:")
                for a in broker.live.anomalies:
                    print(f"  - {a}")
        recorder.close()
        try:
            api.logout()
        except Exception:
            pass
        print(f"\nartifacts: {out}")
    return exit_code


def _passive(broker: ShadowBroker, reference: float, cfg: Any, args: Any) -> int:
    # Tech: submit a limit far below the market, poll, then cancel.
    # Why:  proves the adapter can place and withdraw an order and that the gap
    #       report handles the "neither side filled" episode — without ever
    #       putting a position at risk.
    limit = passive_limit(reference, tick_size=cfg.tick_size)
    print(f"\n[passive] BUY 1 @ {limit} ({reference - limit:.0f} below market)")
    if limit >= reference:
        print("ABORT: the limit is not below the market.", file=sys.stderr)
        return 1

    order = OrderEvent(datetime.now(), "BUY", limit, "OPEN", quantity=1)
    try:
        broker.submit(order, current_price=reference)
        print(f"  submitted; has_pending={broker.has_pending()}")
        fills = _drain(broker, reference, seconds=args.wait_seconds,
                       label="passive", api=live_api_of(broker), limit=limit)
        if fills:
            print("UNEXPECTED: a passive order filled. Investigate before "
                  "trusting the no-fill argument.", file=sys.stderr)
            return 1
    finally:
        broker.cancel()
        print("  cancel requested")
        _drain(broker, reference, seconds=2.0, label="post-cancel",
               api=live_api_of(broker))

    print(f"  position after: {broker.broker_position()!r}")
    return 0


def _round_trip(broker: ShadowBroker, live: Any, reference: float,
                cfg: Any, args: Any) -> int:
    # Tech: buy marketably, wait for the fill, then close through the urgent
    #       path; verify flat.
    # Why:  the deal callback, dedup, `FillEvent` construction and the shadow's
    #       slippage comparison only run when something actually fills. The close
    #       goes through `urgent()` because that is the one place a fill is
    #       *wanted*, which makes it the natural test of the flatten order type.
    limit = marketable_limit(reference, tick_size=cfg.tick_size)
    print(f"\n[round trip] BUY 1 @ {limit} (marketable, {limit - reference:.0f} "
          f"above market)")
    opened = []
    try:
        broker.submit(OrderEvent(datetime.now(), "BUY", limit, "OPEN",
                                 quantity=1), current_price=reference)
        opened = _drain(broker, reference, seconds=args.wait_seconds,
                        label="open", api=live_api_of(broker), limit=limit)
        if not opened:
            print("  no fill within the window; cancelling and stopping here.")
            broker.cancel()
            _drain(broker, reference, seconds=2.0, label="post-cancel",
               api=live_api_of(broker))
            return 0
    except Exception:
        broker.cancel()
        raise

    # Tech: re-read the market immediately before pricing the close, falling back
    #       to the opening reference only if the snapshot fails.
    # Why:  MEASURED 2026-08-12 — the first round trip priced its close off the
    #       snapshot taken ~6 s earlier, before the open had even filled. The
    #       market moved more than the 5-tick aggression in that gap, so the
    #       "aggressive" SELL arrived *passive*, and being IOC it expired
    #       unfilled and left a lot held. Aggression is measured from the price
    #       now, not from the price when the probe started.
    latest = _reference_price(live_api_of(broker), None) or reference
    if latest != reference:
        print(f"\n  market moved {reference} -> {latest} since the open")
    print("\n[round trip] closing through the urgent (FLATTEN) path")
    try:
        with broker.urgent():
            broker.submit(OrderEvent(datetime.now(), "SELL", latest, "CLOSE",
                                     quantity=1), current_price=latest)
        _drain(broker, latest, seconds=args.wait_seconds, label="close",
               api=live_api_of(broker), limit=broker.live.urgent_price("SELL", latest))
        # Tech: report whether the adapter still believes an order is working.
        # Why:  an IOC that expires unfilled must leave `has_pending()` False, or
        #       the loop's flatten retry — which is gated on exactly that — never
        #       fires again and the position is never closed. Whether the venue
        #       sends a callback for an expiry is unverified, so the probe prints
        #       it rather than anyone assuming.
        print(f"  has_pending after close window: {broker.has_pending()}")
    finally:
        broker.close()

    position = live.broker_position()
    print(f"\nposition after: {position!r}")
    if position:
        # Tech: withdraw the close if it is still working, before reporting.
        # Why:  ROD *rests* where IOC expired (§2.19). That is the point — but it
        #       means an unfilled close outlives the probe, and the next tool to
        #       run would price a second close on top of a live one and could
        #       sell two lots against a position of one. Leaving the account
        #       flat-or-clean is part of the probe's contract, not an extra.
        if broker.has_pending():
            print("  a close is still resting — cancelling it")
            broker.cancel()
            _drain(broker, latest, seconds=3.0, label="post-cancel",
                   api=live_api_of(broker))
            position = live.broker_position()
            print(f"  position after cancel: {position!r}")
        if position:
            print(f"*** NOT FLAT — {position} lot(s) still held. Run "
                  f"`python -m forecast_eval.live.flatten` before anything else; "
                  f"the CLI will refuse to start otherwise. ***",
                  file=sys.stderr)
            return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
