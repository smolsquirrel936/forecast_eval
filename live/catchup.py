"""Catch-up — put a deployment where its strategy would be after downtime.

When a simtrade was not running, its strategy kept "trading" on paper: the
signals still happened, nobody acted on them. This tool replays the deployment's
own strategy package over the missed bars, compares the position it would hold
now with what the deployment's ledger (= the venue) holds, and — only with
`--apply` — trades the venue to that position and records the REAL fills.

The rule it serves: **the ledger only ever holds venue fills.** A catch-up moves
the venue, never the ledger alone, so the owner's start-up reconcile (ledgers vs
venue) never has a reason to refuse. What the strategy would have done during
the gap is kept beside it, in `catchup_<slug>.csv`, which nothing live reads.

Run it while the owner and every deployment are STOPPED, during a session (an
order needs a price), then start the deployment with `--resume`.

    uv run --no-sync python -m forecast_eval.live.catchup --config day --warmup-bars 6000           # plan only
    uv run --no-sync python -m forecast_eval.live.catchup --config day --warmup-bars 6000 --apply   # PLACES ORDERS
"""
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from forecast_eval.environment import Environment, classify_session  # noqa: E402
from forecast_eval.events import FillEvent, MarketEvent, OrderEvent  # noqa: E402
from forecast_eval.exits.base import PositionState                   # noqa: E402
from forecast_eval.live.broker.base import HeldPosition              # noqa: E402
from forecast_eval.live.ledger import (FillLedger, open_position,    # noqa: E402
                                       per_deployment_nets)

# Tech: the RPT tape each strategy package was evaluated on.
# Why:  the replay must run on the same series the package's backtests used —
#       fullday and hybrid both trade DAY+NIGHT and read the fullday tape.
RPT_DIR = Path(r"\\DESKTOP-SQUIRRE\dataset\RPT")
RPT_TAPES = {
    "day": "TXF_RPT_minute_day.csv",
    "night": "TXF_RPT_minute_night.csv",
    "fullday": "TXF_RPT_minute_fullday.csv",
    "hybrid": "TXF_RPT_minute_fullday.csv",
}

CATCHUP_COLUMNS = ["timestamp", "side", "fill_price", "quantity", "fee",
                   "catchup_run"]

# Tech: how far through the touch each leg is priced, in points.
# Why:  the same reasoning and default as `flatten.CLOSE_AGGRESSION_POINTS` —
#       this runs attended, and filling now matters more than the last tick.
CATCHUP_AGGRESSION_POINTS = 30.0


class CatchupRefused(Exception):
    """The venue, the ledgers or the market are not in a state to act on."""


# ---- the tape ---------------------------------------------------------------

def extend_tape(rpt: pd.DataFrame, bars: pd.DataFrame) -> Tuple[pd.DataFrame, int]:
    """`rpt` with every bar after its last minute appended in RPT schema.

    `bars` is a live bar frame (`timestamp, price, volume[, contract]`); the
    result keeps `rpt`'s own columns and returns how many rows were added.
    """
    # Tech: take the bars strictly after the tape's last minute and write each
    #       as an RPT row whose open/high/low equal its close.
    # Why:  `real_data.load_minute_bars` reads only `close` and `volume` (plus
    #       `contract` for the per-minute collapse), so the flat OHLC changes
    #       nothing the replay sees. Appending only past the end keeps every
    #       RPT minute as the backtests read it.
    stamp = "datetime" if "datetime" in rpt.columns else "timestamp"
    last = pd.to_datetime(rpt[stamp]).max()
    new = bars[pd.to_datetime(bars["timestamp"]) > last]
    if "contract" in new.columns:
        contract = new["contract"].astype(str)
    else:
        contract = pd.Series("", index=new.index)
    add = pd.DataFrame({
        stamp: pd.to_datetime(new["timestamp"]).dt.strftime("%Y-%m-%d %H:%M:%S"),
        "contract": contract,
        "open": new["price"].astype(float),
        "high": new["price"].astype(float),
        "low": new["price"].astype(float),
        "close": new["price"].astype(float),
        "volume": new["volume"].astype(float),
    })
    return pd.concat([rpt, add[list(rpt.columns)]], ignore_index=True), len(add)


def overlap_mismatches(rpt: pd.DataFrame, bars: pd.DataFrame) -> Tuple[int, int]:
    """`(minutes compared, closes that differ)` where `bars` overlaps `rpt`."""
    # Tech: collapse the tape to its highest-volume row per minute, as the
    #       loader does, and compare closes on the minutes both carry.
    # Why:  the fetched bars only extend the tape if they ARE the tape's series.
    #       The kbars-vs-RPT agreement was measured once (2026-11-12, day,
    #       300/300); this re-measures it on every catch-up instead of assuming.
    stamp = "datetime" if "datetime" in rpt.columns else "timestamp"
    df = rpt.assign(_ts=pd.to_datetime(rpt[stamp]))
    if "contract" in df.columns:
        df = df.loc[df.groupby("_ts")["volume"].idxmax()]
    merged = df.merge(bars.assign(_ts=pd.to_datetime(bars["timestamp"])),
                      on="_ts")
    return len(merged), int((merged["close"] != merged["price"]).sum())


# ---- the plan ---------------------------------------------------------------

def fills_from_frame(df: pd.DataFrame) -> List[FillEvent]:
    """A `fills.csv`-shaped frame back into `FillEvent`s, in time order."""
    # Tech: sort stably by timestamp and rebuild one FillEvent per row.
    # Why:  real_data's `fills.csv` has the ledger's first five columns but no
    #       `soak_id`, so `FillLedger` refuses it; `open_position` wants events.
    df = df.sort_values("timestamp", kind="stable")
    return [FillEvent(timestamp=pd.Timestamp(r.timestamp), side=str(r.side),
                      fill_price=float(r.fill_price), quantity=int(r.quantity),
                      fee=float(r.fee))
            for r in df.itertuples()]


def lot_opened_at(fills: Sequence[FillEvent]) -> Optional[pd.Timestamp]:
    """When the lot `fills` leaves open was opened; None when they close flat."""
    # Tech: walk the signed net and remember the last fill that took it from
    #       flat, or through flat, to a held position.
    # Why:  the exit rule's state (a best price since entry) is measured from
    #       the entry, and the ledger records fills, not entries.
    net, opened = 0, None
    for f in fills:
        after = net + (1 if f.side == "BUY" else -1) * int(f.quantity)
        if after and (net == 0 or (net > 0) != (after > 0)):
            opened = pd.Timestamp(f.timestamp)
        net = after
    return opened if net else None


def gap_since(ledger_fills: Sequence[FillEvent],
              last_heartbeat: Optional[str]) -> Optional[pd.Timestamp]:
    """Where the downtime began: the later of the last fill and the last heartbeat."""
    # Tech: the maximum of the ledger's last fill and `live_restart_<slug>.json`'s
    #       `last_heartbeat`, either of which may be missing.
    # Why:  the replay's trades before this point happened while the deployment
    #       was running on its own forecast schedule; they are not missed trades
    #       and must not land in `catchup_<slug>.csv`.
    stamps = [pd.Timestamp(f.timestamp) for f in ledger_fills]
    if last_heartbeat:
        stamps.append(pd.Timestamp(last_heartbeat))
    return max(stamps) if stamps else None


def order_for(held: int, target: int) -> Optional[Tuple[str, int]]:
    """`(side, quantity)` that takes `held` lots to `target`; None when equal."""
    # Tech: one order for the whole difference, through flat if need be.
    # Why:  the venue does not care which deployment intent a lot carries; the
    #       Cover/New split is made later on the account's net (`plan_legs`).
    delta = int(target) - int(held)
    if delta == 0:
        return None
    return ("BUY" if delta > 0 else "SELL"), abs(delta)


def exit_state_after(rule: Any, held: HeldPosition, opened_at: Any,
                     events: Sequence[MarketEvent],
                     ) -> Tuple[Optional[dict], Optional[pd.Timestamp]]:
    """The rule's state after `events` on the held lot, and when it first fired."""
    # Tech: feed every in-session bar from the entry minute on to the rule, as
    #       the live loop would have, and export what it holds afterwards.
    # Why:  the rule's own `should_exit` is the only definition of its state
    #       (ProfitTrail starts its best price at the first print and ratchets
    #       it), so simulating it beats re-deriving "max close since entry" per
    #       rule. A firing is reported, not acted on: the live loop evaluates
    #       the rule again on its first bar and decides on the price it sees.
    side = "LONG" if held.signed_qty > 0 else "SHORT"
    start = pd.Timestamp(opened_at).floor("min")
    position = PositionState(side=side, size=abs(int(held.signed_qty)),
                             entry_price=float(held.avg_price), entry_bar_idx=0,
                             entry_timestamp=pd.Timestamp(opened_at))
    fired = None
    for i, m in enumerate(e for e in events if pd.Timestamp(e.timestamp) >= start):
        if rule.should_exit(position, m, bar_idx=i) and fired is None:
            fired = pd.Timestamp(m.timestamp)
    return rule.export_state(), fired


def session_events(tape: pd.DataFrame, policy: Any,
                   start: Any = None) -> List[MarketEvent]:
    """The tape's bars from `start` as the deployment's loop sees them."""
    # Tech: stream the tape through `Environment` (session labels, off-session
    #       rows dropped) and keep the sessions this deployment trades.
    # Why:  the exit rule must see the bars the live loop would have fed it —
    #       a DAY deployment never sees a NIGHT print. `start` trims the tape
    #       first, because the fullday tape is ~2.4M rows and only the held
    #       lot's bars matter.
    stamp = "datetime" if "datetime" in tape.columns else "timestamp"
    ts = pd.to_datetime(tape[stamp])
    keep = ts >= pd.Timestamp(start).floor("min") if start is not None else ts == ts
    ticks = pd.DataFrame({"timestamp": ts[keep],
                          "price": tape.loc[keep, "close"].astype(float),
                          "volume": tape.loc[keep, "volume"].astype(float)})
    return [m for m in Environment(ticks).stream()
            if m.session in policy.sessions]


def record_gap(path: Path, gap: Sequence[FillEvent], run: str) -> int:
    """Append the gap's would-be fills to `catchup_<slug>.csv`; return how many were new."""
    # Tech: skip any row already recorded (same timestamp, side and price),
    #       then append the rest; CRLF rows, like the ledgers.
    # Why:  two catch-ups over overlapping gaps replay the same trades, and the
    #       record is a log of what the strategy did, not of how often it was
    #       asked.
    seen = set()
    if path.exists():
        prev = pd.read_csv(path)
        seen = {(str(pd.Timestamp(t)), s, float(p))
                for t, s, p in zip(prev["timestamp"], prev["side"],
                                   prev["fill_price"])}
    new = [f for f in gap
           if (str(pd.Timestamp(f.timestamp)), f.side, float(f.fill_price))
           not in seen]
    if not new:
        return 0
    exists = path.exists() and path.stat().st_size > 0
    with path.open("a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if not exists:
            w.writerow(CATCHUP_COLUMNS)
        for f in new:
            w.writerow([pd.Timestamp(f.timestamp), f.side, f.fill_price,
                        f.quantity, f.fee, run])
    return len(new)


# ---- the venue --------------------------------------------------------------

def check_venue(venue: Optional[int], ledger_nets: dict) -> None:
    """Refuse unless the venue holds exactly what the ledgers add up to."""
    # Tech: compare the account's net with the sum of every deployment's ledger.
    # Why:  the same invariant the owner refuses to start without
    #       (`owner_cli`'s start-up reconcile). The legs below are sized on the
    #       account's net (`owner.plan_legs`), so trading on a book that is
    #       already off would size a Cover against lots that are not there —
    #       §18.12 measured that as unreliable (clamped, or crossed to a short).
    if venue is None:
        raise CatchupRefused("the venue did not answer the position query")
    total = sum(ledger_nets.values())
    if venue != total:
        detail = ", ".join(f"{s} {n:+d}" for s, n in sorted(ledger_nets.items()))
        raise CatchupRefused(
            f"the venue holds {venue:+d} lot(s) but the ledgers add up to "
            f"{total:+d} ({detail}). Reconcile them first (simtrade_operations "
            f"§6.9); a catch-up only ever moves the venue and the ledger together.")


def sync_owner_positions(out_parent: Path, slug: str, code: str) -> Optional[str]:
    """Rewrite `slug`'s lots in `owner_positions.json` from its ledger, if the file exists."""
    # Tech: parse the file, replace this slug's lots and entry with what its
    #       ledger now holds (`owner_cli.seed_positions`, the owner's own
    #       reducer), and write it back atomically with the total re-summed.
    # Why:  the owner seeds from this file at start-up and refuses when it
    #       differs from the ledgers (`owner_cli.choose_seed`). A catch-up that
    #       added fills to the ledger alone would make the next start refuse.
    #       Using the owner's reducer means the two agree by construction.
    #       Rows with no month go to `code`, as the owner's seed does.
    from forecast_eval.live import owner_positions
    from forecast_eval.live.owner_cli import seed_positions
    path = owner_positions.positions_path(out_parent)
    text = owner_positions.read_text(path)
    if text is None:
        return None
    doc = json.loads(text)
    slugs = list(doc.get("deployments", {}))
    if slug not in slugs:
        slugs.append(slug)
    holdings, entries = owner_positions.parse(doc, slugs)
    holdings = {k: n for k, n in holdings.items() if k[0] != slug}
    entries.pop(slug, None)
    for month, held in seed_positions(out_parent, [slug])[slug].items():
        holdings[(slug, month or code)] = held.signed_qty
        entries[slug] = held.avg_price
    owner_positions.write(path, owner_positions.render(holdings, entries, slugs))
    return owner_positions.describe(holdings, entries)


def trade_to_target(broker: Any, side: str, quantity: int, account_net: int,
                    reference: Any, *, aggression: float, attempts: int,
                    wait_seconds: float, clock: Any = time) -> List[FillEvent]:
    """Place `side quantity` as owner-style legs; return every fill received."""
    # Tech: split the order with `owner.plan_legs` on the ACCOUNT's net, then
    #       work each leg — priced `aggression` through a fresh reference, ROD —
    #       until its quantity is filled or `attempts` run out, cancelling and
    #       re-pricing in between.
    # Why:  Cover/New is a property of the account, not of this deployment
    #       (§18.9): a BUY that opens day's long is a Cover while a peer is
    #       short. A leg is only finished when its own quantity is met, because
    #       partial fills are real (§18.9). Every fill is returned, even from an
    #       incomplete run, because each one is a real lot the ledger must show.
    from forecast_eval.live.flatten import close_price
    from forecast_eval.live.owner import COVER, plan_legs
    fills: List[FillEvent] = []
    for leg in plan_legs(side, quantity, account_net):
        remaining = leg.quantity
        for _attempt in range(attempts):
            ref = reference()
            if ref is None:
                raise CatchupRefused("no reference price (market closed?); "
                                     "run during a session")
            limit = close_price(ref, side, aggression)
            print(f"  {leg.octype} {side} {remaining} @ {limit} (reference {ref})")
            broker.submit(OrderEvent(datetime.now(), side, limit,
                                     "CLOSE" if leg.octype == COVER else "OPEN",
                                     quantity=remaining), current_price=ref)
            remaining -= _drain(broker, ref, remaining, wait_seconds, fills, clock)
            if remaining == 0:
                break
            broker.cancel()
            remaining -= _drain(broker, ref, remaining, 2.0, fills, clock)
            if remaining == 0:
                break
        if remaining:
            break
    return fills


def venue_time(ts: Any) -> pd.Timestamp:
    """A deal's timestamp as naive Taipei time, the ledger's clock."""
    # Tech: an epoch number becomes UTC seconds + VENUE_UTC_OFFSET, a missing
    #       one becomes now, anything else is parsed as it is.
    # Why:  ShioajiBroker hands the deal callback's raw `ts` (UTC epoch seconds)
    #       through unconverted; the owner converts it the same way
    #       (`owner_process`'s deal handler). Unconverted, the 2026-11-12 night
    #       catch-up wrote `1794444464.509279` into fills_night.csv and the
    #       ledger replay after it failed.
    from forecast_eval.live.owner_process import VENUE_UTC_OFFSET
    if ts is None or ts == "":
        return pd.Timestamp.now()
    if isinstance(ts, (int, float)):
        return pd.Timestamp(ts, unit="s") + VENUE_UTC_OFFSET
    return pd.Timestamp(ts)


def _drain(broker: Any, price: float, want: int, seconds: float,
           into: List[FillEvent], clock: Any) -> int:
    # Tech: poll the broker for fills until `want` lots arrive or `seconds` pass,
    #       stamping each in Taipei time (`venue_time`).
    # Why:  fills arrive on Shioaji's callback thread, never synchronously with
    #       `place_order` (v4_probe's `_drain`, same reason).
    got = 0
    deadline = clock.time() + seconds
    while got < want and clock.time() < deadline:
        for f in broker.poll_fills(MarketEvent(datetime.now(), float(price), 1,
                                               classify_session(pd.Timestamp.now())
                                               or "DAY")):
            f = replace(f, timestamp=venue_time(f.timestamp))
            into.append(f)
            got += int(f.quantity)
            print(f"    fill: {f.side} {f.quantity} @ {f.fill_price}")
        clock.sleep(0.25)
    return got


# ---- the command ------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    # Tech: the CLI; `--config` takes the same deployment names as `live.cli`.
    # Why:  one name per deployment everywhere, so the slug, package, policy and
    #       credential all derive from the config rather than being re-typed.
    from forecast_eval.live.config import CONFIGS
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", required=True, choices=sorted(CONFIGS),
                    help="The deployment, as `live.cli --config` names it.")
    ap.add_argument("--warmup-bars", type=int, default=None,
                    help="real_data --warmup-bars for the replay. Sets the "
                         "forecast schedule, so use the package's adopted run "
                         "(day: 6000). Required unless --replay-dir is given.")
    ap.add_argument("--out-parent", default="outputs",
                    help="Directory holding the live state files (default: outputs).")
    ap.add_argument("--rpt", default=None,
                    help="RPT tape to extend (default: the package's tape in "
                         f"{RPT_DIR}).")
    ap.add_argument("--tape", default=None,
                    help="An already extended tape; skips the kbars fetch.")
    ap.add_argument("--replay-dir", default=None,
                    help="An existing real_data run on the extended tape; skips "
                         "the replay. Needs --tape.")
    ap.add_argument("--since", default=None,
                    help="Start of the downtime (default: the later of the "
                         "ledger's last fill and the last live heartbeat).")
    ap.add_argument("--credential", default=None,
                    help="config/secrets.toml section to log in with "
                         "(default: the --config name).")
    ap.add_argument("--contract", default="TXFR1")
    ap.add_argument("--apply", action="store_true",
                    help="PLACE ORDERS to reach the target, append the real fills "
                         "to the ledger, write the exit state and the gap record.")
    ap.add_argument("--attempts", type=int, default=3)
    ap.add_argument("--wait-seconds", type=float, default=8.0)
    ap.add_argument("--aggression-points", type=float,
                    default=CATCHUP_AGGRESSION_POINTS)
    return ap


def _login(cfg_name: str, credential: Optional[str]) -> Tuple[Any, Any]:
    # Tech: log in with the deployment's credential from config/secrets.toml.
    # Why:  same lookup as every other live tool (`credentials.load`); the
    #       simulation flag matches the simtrades this tool serves.
    from forecast_eval.live import credentials
    name = credential or cfg_name
    cred = credentials.load(name)
    if cred is None:
        raise CatchupRefused(f"no credential for {name!r} in config/secrets.toml")
    import shioaji as sj
    api = sj.Shioaji(simulation=True)
    api.login(api_key=cred.api_key, secret_key=cred.secret_key)
    return api, sj


def _fetch_bars(api: Any, contract: Any, policy: Any, start: str) -> pd.DataFrame:
    # Tech: the front month plus the month before it, spliced by the roll rule
    #       bound to this deployment's policy — `cli.py`'s warm-up fetch, with
    #       each bar's month kept.
    # Why:  a gap that spans a roll needs both months, and the tape's
    #       `contract` column must say which month each minute came from.
    from functools import partial
    from forecast_eval.live.backfill import collapse_front_month, fetch_bars
    from forecast_eval.live.contracts import contract_month, dated_months, txf_root
    from forecast_eval.live.roll import front_month_at
    try:
        root, _route = txf_root(api)
        month = contract_month(contract)
        contracts = [c for c in dated_months(root)
                     if contract_month(c) <= month][-2:] or [contract]
    except Exception:                                   # noqa: BLE001
        contracts = [contract]
    end = datetime.now().date().isoformat()
    frames = []
    for c in contracts:
        try:
            got = fetch_bars(api, c, start, end, policy=policy)
        except Exception as exc:                        # noqa: BLE001
            print(f"  ! kbars failed for {getattr(c, 'code', c)}: {exc!r}")
            continue
        frames.append(got.assign(contract=str(getattr(c, "delivery_month", ""))))
    return collapse_front_month(frames, front_month_at=partial(front_month_at,
                                                               policy=policy))


def _reference(api: Any, contract: Any) -> Optional[float]:
    # Tech: the snapshot close, None when there is none.
    # Why:  each attempt prices off a fresh read, as `flatten` does — a stale
    #       reference turns an aggressive limit into a passive one.
    try:
        snaps = api.snapshots([contract])
        return float(getattr(snaps[0], "close", 0)) or None
    except Exception as exc:                            # noqa: BLE001
        print(f"  ! snapshots failed: {exc!r}")
        return None


def _working_orders(api: Any, account: Any, codes: set) -> int:
    # Tech: count still-working orders on our contract, by `flatten.is_working`.
    # Why:  a resting order can fill on top of ours; unlike `flatten` this tool
    #       refuses rather than cancels, because it has no emergency to excuse
    #       withdrawing an order it did not place.
    from forecast_eval.live.flatten import is_working
    api.update_status(account)
    return sum(1 for t in (api.list_trades() or [])
               if is_working(t)
               and str(getattr(getattr(t, "contract", None), "code", "") or "")
               in codes)


def main(argv=None) -> int:
    # Tech: read the ledger, extend the tape, replay, print the plan; with
    #       --apply, check the venue, trade, record fills, exit state and gap.
    # Why:  the plan alone changes nothing live, so it can be run any time to
    #       see what a catch-up would do; every venue action sits behind --apply.
    args = build_parser().parse_args(argv)
    from forecast_eval.live import exit_state, soak_epoch
    from forecast_eval.live.config import config_named
    from forecast_eval.strategy import wiring as strategy_wiring

    cfg = config_named(args.config)
    policy = cfg.policy
    slug = policy.cache_slug()
    package = cfg.strategy_package
    out_parent = Path(args.out_parent)
    if args.replay_dir and not args.tape:
        print("ERROR: --replay-dir needs --tape (the tape it was run on).",
              file=sys.stderr)
        return 2
    if not args.replay_dir and args.warmup_bars is None:
        print("ERROR: --warmup-bars is required for the replay.", file=sys.stderr)
        return 2

    ledger_file = out_parent / f"fills_{slug}.csv"
    ledger_fills = FillLedger(ledger_file).replay()
    held = open_position(ledger_fills)
    held_net = held.signed_qty if held else 0
    heartbeat = None
    restart = out_parent / f"live_restart_{slug}.json"
    if restart.exists():
        heartbeat = json.loads(restart.read_text(encoding="utf-8")).get("last_heartbeat")
    since = pd.Timestamp(args.since) if args.since else gap_since(ledger_fills, heartbeat)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    work = out_parent / f"catchup_{slug}_{stamp}"
    work.mkdir(parents=True, exist_ok=True)
    print(f"catch-up {slug} (package {package}) -> {work}")
    print(f"ledger: {held_net:+d} lot(s)"
          + (f" @ {held.avg_price:.1f}" if held else "")
          + f"; downtime from {since}")

    # --- tape ----------------------------------------------------------------
    try:
        if args.tape:
            tape_path = Path(args.tape)
            tape = pd.read_csv(tape_path, dtype={"contract": str})
        else:
            rpt_path = Path(args.rpt) if args.rpt else RPT_DIR / RPT_TAPES[package]
            rpt = pd.read_csv(rpt_path, dtype={"contract": str})
            stamp_col = "datetime" if "datetime" in rpt.columns else "timestamp"
            start = pd.to_datetime(rpt[stamp_col]).max().date().isoformat()
            api, _sj = _login(args.config, args.credential)
            try:
                from forecast_eval.live.contracts import resolve_txf
                contract, _note = resolve_txf(api, args.contract)
                bars = _fetch_bars(api, contract, policy, start)
            finally:
                api.logout()
            compared, differ = overlap_mismatches(rpt, bars)
            print(f"kbars vs RPT on the overlap: {compared} minute(s), "
                  f"{differ} close(s) differ")
            if differ:
                print("  WARNING: the fetched bars are not the tape's series on "
                      "the overlap; the replay may not match the backtests.")
            tape, added = extend_tape(rpt, bars)
            tape_path = work / "tape.csv"
            tape.to_csv(tape_path, index=False)
            print(f"tape: +{added} bar(s), ends {tape.iloc[-1, 0]}")
    except CatchupRefused as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    # --- replay --------------------------------------------------------------
    if args.replay_dir:
        replay_dir = Path(args.replay_dir)
    else:
        replay_dir = work / "replay"
        # Tech: run real_data for the package on the extended tape, from the
        #       repo root, with this interpreter.
        # Why:  the replay must be the package's own backtest — same loop, fill
        #       model and exit rule — or "where the strategy would be" means
        #       something else. No forecast cache: where a branch has one
        #       (`forecast_cache.py`), its key hashes the whole tape, so an
        #       extended tape never hits.
        cmd = [sys.executable, "-m", "forecast_eval.real_data",
               "--strategy", package, "--data", str(tape_path.resolve()),
               "--warmup-bars", str(args.warmup_bars), "--precompute",
               "--output-dir", str(replay_dir.resolve())]
        print("replay: " + " ".join(cmd))
        with (work / "replay.log").open("w", encoding="utf-8") as log:
            rc = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT,
                                cwd=str(Path(__file__).resolve().parents[2])
                                ).returncode
        if rc != 0:
            print(f"ERROR: the replay failed (exit {rc}); see {work / 'replay.log'}",
                  file=sys.stderr)
            return 6
    replay_fills = fills_from_frame(pd.read_csv(replay_dir / "fills.csv"))
    target = open_position(replay_fills)
    target_net = target.signed_qty if target else 0
    gap = [f for f in replay_fills
           if since is None or pd.Timestamp(f.timestamp) > since]

    print(f"\nreplay trades during the downtime: {len(gap)}")
    for f in gap:
        print(f"  {pd.Timestamp(f.timestamp)}  {f.side:4s} {f.quantity} @ {f.fill_price}")
    print(f"strategy would hold: {target_net:+d}"
          + (f" (entered @ {target.avg_price:.1f} at "
             f"{lot_opened_at(replay_fills)})" if target else ""))
    order = order_for(held_net, target_net)
    print("order needed: " + (f"{order[0]} {order[1]}" if order else "none"))

    # Tech: a fresh exit rule for every use.
    # Why:  rules are stateful (CLAUDE.md, "Stateful objects in sweeps"); a
    #       rule that already ran over the plan's bars must not be reused.
    def make_rule() -> Any:
        return strategy_wiring.make_strategy(
            package, warmup_bars=args.warmup_bars or 1)["exit_rule"]
    restorable = getattr(make_rule(), "restorable", False)
    keep_state, fired = None, None
    if held and restorable and order is None:
        opened = lot_opened_at(ledger_fills)
        keep_state, fired = exit_state_after(
            make_rule(), held, opened, session_events(tape, policy, opened))
        print(f"exit state for the held lot: {keep_state}")
        if fired is not None:
            print(f"  NOTE: the exit rule would have closed this lot at {fired}; "
                  f"the live loop re-checks it on its first bar.")

    plan = {"slug": slug, "package": package, "since": str(since),
            "ledger_net": held_net, "target_net": target_net,
            "order": order, "gap_trades": len(gap), "tape": str(tape_path),
            "replay_dir": str(replay_dir), "exit_state": keep_state,
            "exit_rule_fired_at": str(fired) if fired is not None else None}
    (work / "plan.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
    if not args.apply:
        print("\nplan only - nothing sent, nothing recorded. Re-run with --apply.")
        return 0

    # --- apply ---------------------------------------------------------------
    from forecast_eval.live.broker.shioaji import ShioajiBroker
    from forecast_eval.live.contracts import contract_codes, month_code, resolve_txf
    epoch = soak_epoch.load(out_parent / f"soak_epoch_{slug}.json")
    new_fills: List[FillEvent] = []
    try:
        api, sj = _login(args.config, args.credential)
    except CatchupRefused as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    try:
        contract, note = resolve_txf(api, args.contract)
        code = month_code(contract)
        print(f"\ncontract {note}")
        account = getattr(api, "futopt_account", None)
        broker = ShioajiBroker(api, contract, sj=sj, fee_rate=cfg.fee_rate,
                               tick_size=cfg.tick_size)
        try:
            # Tech: refuse on a working order, a venue/ledger mismatch, or a
            #       held lot on a month other than the front.
            # Why:  each makes the legs wrong: a resting order can fill on top
            #       of ours (flatten's reason), a mismatched book sizes a Cover
            #       against lots that are not there, and a lot on the old month
            #       is the roll's job — this tool trades the front month only.
            if _working_orders(api, account, contract_codes(contract)):
                raise CatchupRefused("orders are still working on this contract; "
                                     "see `flatten --show-orders`")
            venue = broker.broker_position()
            nets = per_deployment_nets(out_parent)
            check_venue(venue, nets)
            months = {m for m, fs in FillLedger(ledger_file).replay_by_month().items()
                      if open_position(fs) is not None}
            if order and months - {code, ""}:
                raise CatchupRefused(f"{slug} holds a lot on {sorted(months)}, not "
                                     f"the front month {code}; let the roll move it")
        except CatchupRefused as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 5
        print(f"venue {venue:+d} lot(s) = ledgers {nets}")

        if order:
            new_fills = trade_to_target(
                broker, order[0], order[1], venue,
                lambda: _reference(api, contract),
                aggression=args.aggression_points, attempts=args.attempts,
                wait_seconds=args.wait_seconds)
        after = broker.broker_position()
    except CatchupRefused as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        after = None
    finally:
        # Tech: whatever filled is written to the ledger before anything else
        #       can fail.
        # Why:  each fill is a real lot at the venue; a ledger without it is
        #       exactly the mismatch the owner refuses to start on.
        if new_fills:
            FillLedger(ledger_file, soak_id=epoch.soak_id if epoch else None,
                       contract=code).sync(new_fills)
            print(f"ledger: +{len(new_fills)} fill(s) -> {ledger_file}")
            synced = sync_owner_positions(out_parent, slug, code)
            if synced is not None:
                print(f"owner_positions.json: {synced}")
        try:
            api.logout()
        except Exception:                               # noqa: BLE001
            pass

    expected = sum(nets.values()) + (target_net - held_net)
    if after != expected:
        print(f"\n*** the venue holds {after} lot(s), expected {expected:+d}. "
              f"The ledger has every fill that arrived; reconcile before "
              f"starting. ***", file=sys.stderr)
        return 4

    # Tech: write (or clear) the exit-rule state for what the deployment now holds.
    # Why:  a restorable rule refuses `--resume` without it (`exit_state.resolve`).
    #       A lot opened here starts its state at the real fill; a lot kept from
    #       before carries the state simulated over the downtime's bars.
    final_fills = FillLedger(ledger_file).replay()
    final = open_position(final_fills)
    state_path = out_parent / f"exit_state_{slug}.json"
    rule = make_rule()
    writer = exit_state.ExitStateFile(state_path, rule=rule, slug=slug,
                                      contract=code,
                                      soak_id=epoch.soak_id if epoch else None)
    kept_bars = ([] if final is None or new_fills
                 else session_events(tape, policy, lot_opened_at(final_fills)))
    if final is None or not restorable:
        writer.update(None, None)
    elif not new_fills and not kept_bars:
        print(f"  WARNING: the tape has no bars since this lot's entry; "
              f"{state_path.name} left as it is.")
    else:
        last = new_fills[-1] if new_fills else None
        bars_for = ([MarketEvent(pd.Timestamp(last.timestamp),
                                 float(last.fill_price), 1,
                                 classify_session(pd.Timestamp(last.timestamp))
                                 or sorted(policy.sessions)[0])]
                    if new_fills else kept_bars)
        exit_state_after(rule, final, lot_opened_at(final_fills), bars_for)
        side = "LONG" if final.signed_qty > 0 else "SHORT"
        writer.update(PositionState(side=side, size=abs(final.signed_qty),
                                    entry_price=final.avg_price, entry_bar_idx=0,
                                    entry_timestamp=lot_opened_at(final_fills)),
                      bars_for[-1])
        print(f"exit state -> {state_path}: {rule.export_state()}")

    recorded = record_gap(out_parent / f"catchup_{slug}.csv", gap, work.name)
    print(f"gap record: +{recorded} row(s) -> {out_parent / f'catchup_{slug}.csv'}")
    print(f"\nDONE. {slug} holds {(final.signed_qty if final else 0):+d}; "
          f"start it with --resume.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
