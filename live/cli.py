"""The live entry point — one policy, one config.

    uv run python -m forecast_eval.live.cli --duration-minutes 60
    uv run python -m forecast_eval.live.cli --broker shioaji \
        --i-understand-this-places-orders

**`--broker shioaji` sends real (simulation-account) orders**, and is gated
behind an explicit acknowledgement flag rather than by not existing. It was
deliberately absent until a session had actually been observed — V2 passed 24
tests against a scripted fake and still carried the alias contract-code bug that
would have halted the loop on the first real trade (§7.2). The flag replaces
that absence: the default remains a *dry run* — real login, real quotes, real
decisions, nothing sent — so reaching the venue takes an act, not a typo at
3 a.m.

Everything decidable lives in `LiveRuntime`, which is fully testable offline;
this module is the thin shell that logs in, subscribes, and turns callbacks into
a single-threaded event stream.

**Threading.** Shioaji delivers quotes on its own thread. That thread only ever
`put()`s onto a queue — one worker thread drains it and is the *sole* caller into
the runtime, so `LiveLoop`'s single-threaded requirement holds without a lock. A
forecast is ~90 ms of GPU work; running it on the callback thread would stall the
quote handler.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import sys
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from forecast_eval.execution import Execution                     # noqa: E402
from forecast_eval.live.broker.dryrun import DryRunBroker         # noqa: E402
from forecast_eval.live.broker.shadow import ShadowBroker         # noqa: E402
from forecast_eval.live.broker.shioaji import ShioajiBroker       # noqa: E402
from forecast_eval.live.broker.sim import SimBroker               # noqa: E402
from forecast_eval.live.capture import TickCapture                # noqa: E402
from forecast_eval.live.config import PINNED                      # noqa: E402
from forecast_eval.live.contracts import resolve_txf              # noqa: E402
from forecast_eval.live.alerts import Alerter                     # noqa: E402
from forecast_eval.live.feedwatch import FeedWatch, Verdict       # noqa: E402
from forecast_eval.live.recorder import Recorder                  # noqa: E402
from forecast_eval.live.relogin import (                          # noqa: E402
    Outcome as ReloginOutcome, ReloginScheduler)
from forecast_eval.live.runtime import LiveRuntime                # noqa: E402
from forecast_eval.live.startup import (                          # noqa: E402
    RestartBlocked, RestartGuard, RestartPolicy,
)
from forecast_eval.live.state import RailsConfig, SafetyRails     # noqa: E402

BROKERS = ("dryrun", "sim", "shioaji")
MIN_FREE_VRAM_GIB = 5.0


def check_vram(min_free_gib: float = MIN_FREE_VRAM_GIB) -> float:
    # Tech: return free VRAM in GiB, raising when below the floor.
    # Why:  MEASURED — the model plus context needs ~4.4 GiB (§2.7). The hazard is
    #       directional: a research run with `--precompute --batch-size auto`
    #       sizes its batch to fill *free* VRAM, so a research job started first
    #       can leave the live process unable to load at all. Failing here is a
    #       clear message; failing inside the first forecast is a stack trace at
    #       the open.
    import torch
    if not torch.cuda.is_available():
        raise RuntimeError("no CUDA device — the pinned config needs the GPU")
    free, total = torch.cuda.mem_get_info()
    free_gib = free / 2 ** 30
    if free_gib < min_free_gib:
        raise RuntimeError(
            f"only {free_gib:.1f} GiB VRAM free of {total/2**30:.1f}; need "
            f"{min_free_gib} GiB. Another process (a research backtest with "
            f"--batch-size auto?) is holding it."
        )
    return free_gib


def _mask(value: Any) -> str:
    # Tech: keep the first and last two characters, star the middle.
    # Why:  enough to recognise your own account at a glance, not enough to
    #       reproduce it from a log that may be pasted into a chat or a ticket.
    #       Short values are starred entirely rather than half-revealed.
    text = "" if value is None else str(value)
    if len(text) <= 4:
        return "*" * len(text)
    return f"{text[:2]}{'*' * (len(text) - 4)}{text[-2:]}"


def _digest(value: Any) -> str:
    # Tech: first 12 hex characters of the SHA-256 of the raw value.
    # Why:  the fingerprint only has to answer "same as last time?", and a digest
    #       answers it exactly while being useless to anyone who obtains the log.
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]


def account_fingerprint(account: Any) -> dict:
    """A stable, non-identifying fingerprint of the account behind a run."""
    # Tech: mask + digest the identifying fields, copy the harmless ones.
    # Why:  simulation mode cannot be verified at runtime — the SDK does not
    #       expose it (§7.2) — so the strongest available check is *change
    #       detection*: if the account behind a run is the same one as the run we
    #       inspected, nothing about the setup silently moved.
    #
    #       `person_id` is a Taiwanese national ID. It is digested and **never
    #       masked**, because even a partial national ID is more than an audit
    #       trail needs to carry.
    return {
        "account_id": _mask(getattr(account, "account_id", None)),
        "account_id_digest": _digest(getattr(account, "account_id", None)),
        "person_id_digest": _digest(getattr(account, "person_id", None)),
        "broker_id": str(getattr(account, "broker_id", "?")),
        "account_type": str(getattr(account, "account_type", "?")),
        "signed": bool(getattr(account, "signed", False)),
    }


def check_account_fingerprint(fingerprint: dict, path: Path) -> Optional[str]:
    """Compare against the last run's fingerprint. `None` == unchanged/first."""
    # Tech: read the stored fingerprint, compare the digests, then write the
    #       current one. Returns a description of the change, or None.
    # Why:  a fingerprint nobody compares is half a feature — it would need two
    #       JSONL files diffed by hand to mean anything. Comparing the *digests*
    #       and not the masks means a change in the underlying value is caught
    #       even though the log never held the value.
    #
    #       This warns rather than refuses. An account change can be legitimate
    #       (a new sub-account), and this cannot tell simulation from production
    #       anyway — so the honest action is to make the change impossible to
    #       miss, not to block on it.
    keys = ("account_id_digest", "person_id_digest", "broker_id", "account_type")
    previous = None
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = None

    changed = []
    if previous:
        for key in keys:
            if previous.get(key) != fingerprint.get(key):
                changed.append(
                    f"{key}: {previous.get(key)!r} -> {fingerprint.get(key)!r}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(fingerprint, indent=2), encoding="utf-8")
    except OSError:
        pass
    if not changed:
        return None
    return ("the account behind this run differs from the previous one: "
            + "; ".join(changed))


def _repair_budget(staleness_seconds: float, interval_seconds: float) -> int:
    """How many feed repairs fit before the staleness backstop halts the run."""
    # Tech: `staleness / interval`, floored, never below 1.
    # Why:  the budget is derived rather than picked — it is exactly "keep trying
    #       right up to the moment we give up", and it moves on its own if either
    #       constant changes. The previous fixed 3 covered only the first 3 of 15
    #       minutes at the shipped defaults, leaving twelve minutes in which the
    #       feed was known broken and nothing was attempted.
    #
    #       The floor of 1 matters because a repair budget of 0 would disarm
    #       FeedWatch's healing entirely while leaving it reporting MISSED — the
    #       silent-disarm shape §2.22's `SANE_AFTER` exists to prevent.
    if interval_seconds <= 0:
        return 1
    return max(1, int(staleness_seconds / interval_seconds))


def _preflight_orders(args: Any) -> Optional[str]:
    """Checks that must pass before an order-placing run starts. `None` == go."""
    # Tech: refuse unless the acknowledgement flag is present. Returns the reason,
    #       or None.
    # Why:  the acknowledgement replaces a safety property that was previously
    #       structural: `shioaji` was *absent* from the choices precisely so it
    #       "cannot be reached by a mistake at 3 a.m." Adding it back as one word
    #       among three would spend that protection for nothing, so it now takes
    #       a second, deliberate flag.
    #
    #       A pre-existing position used to be refused here as well, because the
    #       mirror started at zero and the reconciliation rail (§2.9) would halt
    #       on the first bar. `LiveRuntime.adopt_position` seeds that mirror from
    #       the venue instead (§2.20), so the state is now resumable rather than
    #       fatal — a V5 soak restarts mid-trade far more often than it starts
    #       flat. What remains fatal (a size above `max_position`) is refused
    #       there, where the size is known.
    if not args.i_understand_this_places_orders:
        return ("--broker shioaji places simulated orders. Re-run with "
                "--i-understand-this-places-orders to confirm.")
    return None


def _rails_config(args: Any, cfg: Any) -> RailsConfig:
    # Tech: assemble the rails' configuration from the parsed arguments and the
    #       pinned config. One expression, extracted from `main`.
    # Why:  `main` needs credentials and a login, so anything built inline there
    #       is untestable — and a flag that is parsed but never reaches the rails
    #       looks identical to one that works. That failure has already happened
    #       once in this project (the V4 recorder logged an order type the venue
    #       never received, §2.19), so the wiring gets a seam and a test rather
    #       than a careful reading.
    return RailsConfig(
        max_position=cfg.max_position,
        max_daily_loss_points=args.max_daily_loss_points,
        kill_file=args.kill_file,
        quote_staleness_seconds=args.staleness_seconds,
    )


def _broker_banner(args: Any, contract_note: str) -> str:
    # Tech: one line stating whether this run can send orders, and how.
    # Why:  §5 requires the mode to be unmistakable in the log and on the
    #       console. "dryrun" and "shioaji" differ by one word on the command
    #       line but by everything in consequence, so the banner spells out the
    #       consequence rather than echoing the flag.
    if args.broker == "shioaji":
        return (f"[ORDERS: SIMULATED-LIVE] placing via Shioaji on {contract_note}"
                f" | flatten={args.flatten_aggression_ticks} ticks through, ROD")
    return f"[ORDERS: NONE] broker={args.broker} — nothing is sent to the venue"


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--broker", choices=BROKERS, default="dryrun",
                    help="dryrun: simulated fills, orders recorded not sent. "
                         "sim: same fill model, no order log. "
                         "shioaji: PLACES SIMULATED ORDERS (V4) — also needs "
                         "--i-understand-this-places-orders")
    ap.add_argument("--i-understand-this-places-orders", action="store_true",
                    help="Required with --broker shioaji. Orders are simulated "
                         "(the CLI cannot reach a production account), but they "
                         "are real requests to the venue.")
    ap.add_argument("--flatten-aggression-ticks", type=int, default=5,
                    help="How far through the touch a FLATTEN close is priced "
                         "(--broker shioaji only)")
    ap.add_argument("--contract", default="TXFR1")
    ap.add_argument("--duration-minutes", type=float, default=None,
                    help="Stop after this long. Default: run until the session "
                         "ends or Ctrl-C.")
    ap.add_argument("--warmup-days", type=int, default=30,
                    help="Calendar days of history to request for the context "
                         "and emitter priming (needs ~21 night sessions)")
    ap.add_argument("--out", type=Path, default=None,
                    help="Output dir; default outputs/live_<timestamp>")
    ap.add_argument("--state-file", type=Path, default=None,
                    help="RestartGuard state; default <out>/../live_restart.json")
    ap.add_argument("--no-capture", action="store_true",
                    help="Skip raw tick capture (V1's live half needs it)")
    ap.add_argument("--max-daily-loss-points", type=float, default=None)
    ap.add_argument("--kill-file", type=Path, default=None)
    # Tech: expose the staleness window, defaulting to the rails' own 120 s.
    # Why:  §2.21 — 120 s sits *inside* normal night-session behaviour. A
    #       measured 151 s gap between prints at 02:35 halted a healthy 23-hour
    #       soak 31 s before the market spoke again. Until the rail can ask the
    #       venue whether anything traded, the window is the only knob, and it
    #       has to be reachable without editing code mid-soak. Raising it trades
    #       slower detection of a genuinely dead feed for not stopping on a quiet
    #       one; in the thin hours that is the right trade, because a limit
    #       priced off the last print of a market that has not traded in minutes
    #       is priced off *the* market.
    # Tech: the rails' staleness window, now a backstop rather than the detector,
    #       so its default moves from 120 s to 15 minutes.
    # Why:  §2.21/§2.22 — 120 s sits *inside* normal night-session behaviour (a
    #       measured 151 s gap between prints ended a healthy 23-hour soak). With
    #       `FeedWatch` asking the venue every minute, this timer no longer has to
    #       distinguish a quiet market from a dead feed; it only has to catch the
    #       case where the repair did not work. A halt is a human-attention event,
    #       so it should fire on minutes of unhealed silence, not on seconds of a
    #       thin tape.
    ap.add_argument("--staleness-seconds", type=float, default=900.0,
                    help="Backstop: halt (STOP) after this long with no genuine "
                         "print. FeedWatch does the real detection.")
    ap.add_argument("--feed-check-seconds", type=float, default=60.0,
                    help="Silence before asking the venue whether it traded "
                         "without us. A polling interval, not a threshold — a "
                         "wrong guess costs one ~40 ms query.")
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    cfg = PINNED

    out = args.out or Path("outputs") / f"live_{datetime.now():%Y%m%d_%H%M%S}"
    out.mkdir(parents=True, exist_ok=True)
    state_file = args.state_file or out.parent / "live_restart.json"

    # Tech: the alerter is built first, before anything that can fail, with the
    #       fatal journal beside the restart state and no recorder yet.
    # Why:  the earliest failures (missing credentials, a refused restart) are
    #       exactly the ones a soak needs to hear about, and they happen before
    #       `session.jsonl` exists. The journal lives beside `live_restart.json`
    #       rather than inside the run directory so it accumulates across runs —
    #       "what has ever gone badly wrong" should be one file, not a search.
    #       `recorder` is attached below once there is one.
    alerter = Alerter(fatal_journal=state_file.parent / "fatal.jsonl")

    api_key = os.environ.get("SHIOAJI_API_KEY")
    secret_key = os.environ.get("SHIOAJI_SECRET_KEY")
    if not api_key or not secret_key:
        alerter.fatal("no_credentials",
                      "SHIOAJI_API_KEY / SHIOAJI_SECRET_KEY are not set")
        return 2

    # Tech: the restart gate runs before login, before any query.
    # Why:  §2.8b — every start performs a backfill, so an unsupervised crash loop
    #       becomes a login+query loop, and Shioaji answers that with an IP/ID
    #       suspension that repeated logins then extend. Gate first, connect after.
    guard = RestartGuard(RestartPolicy(state_file=state_file))
    try:
        guard.begin(datetime.now())
    except RestartBlocked as exc:
        alerter.fatal("startup_refused", str(exc), gate="RestartGuard")
        return 3

    recorder = Recorder(out / "session.jsonl", simulation=True,
                        meta={"config": cfg.describe(), "broker": args.broker,
                              "contract": args.contract})
    alerter.recorder = recorder
    capture = None if args.no_capture else TickCapture(out / "ticks.csv")

    execution = Execution(fee_rate=cfg.fee_rate)
    rails_cfg = _rails_config(args, cfg)
    # Tech: build the rails here rather than letting `LiveRuntime` make them, so
    #       the broker and the runtime share one instance.
    # Why:  `ShioajiBroker` reports rejections straight to the rails, and a
    #       rejection the runtime's rails never saw would not halt anything. One
    #       object, two holders.
    rails = SafetyRails(rails_cfg, simulation=True, policy=cfg.policy,
                        recorder=recorder, alerter=alerter)

    import shioaji as sj
    # Tech: simulation is hardcoded, not a flag.
    # Why:  §7.2 measured that `api.simulation` reads back as **None** — the SDK
    #       does not expose the mode, so it cannot be verified after the fact and
    #       the constructor argument is the only guarantee there is. Leaving it
    #       unparameterised means this entry point has no reachable path to a
    #       production account, whatever else is passed.
    api = sj.Shioaji(simulation=True)
    rt = None
    # Tech: bound before the try so the finally block can always test it.
    # Why:  the broker is built after login, and a login failure still runs the
    #       teardown — an unbound name there would replace the real error with a
    #       NameError.
    broker = None
    # Tech: bound before the try, like `broker`, so the finally block can test it.
    # Why:  it is built after login and subscription, and a failure before that
    #       still runs the teardown — an unbound name there would replace the real
    #       error with a NameError.
    feed = None
    # Tech: bound before the try like `broker`, and referenced by name inside the
    #       callback re-assertion closure.
    # Why:  only the shioaji path has an order callback to re-register, and
    #       `_register_callbacks` also runs on the dryrun/sim paths where no
    #       venue broker was ever built.
    live_broker = None
    relogin = None
    login_at = None
    exit_code = 0
    try:
        free = check_vram()
        print(f"VRAM free: {free:.1f} GiB")

        api.login(api_key=api_key, secret_key=secret_key)
        # Tech: the moment the token was issued.
        # Why:  §2.24 — it expires 24 h from here, and `ReloginScheduler` needs
        #       the origin to measure age against.
        login_at = datetime.now()
        guard.check_usage(_usage(api))

        # Tech: fingerprint the futures account, compare against the last run,
        #       print and record it.
        # Why:  simulation mode is unverifiable at runtime — the SDK exposes no
        #       flag (§7.2), so `[SIMULATION]` states our intent rather than
        #       proving anything. Change detection is the strongest check
        #       available: this cannot tell simulation from production, but it
        #       makes "the account behind this run moved" impossible to miss.
        fingerprint = account_fingerprint(getattr(api, "futopt_account", None))
        print(f"account: {fingerprint['account_id']} "
              f"broker={fingerprint['broker_id']} "
              f"type={fingerprint['account_type']} "
              f"signed={fingerprint['signed']} "
              f"digest={fingerprint['account_id_digest']}")
        recorder.record("account", **fingerprint)
        drift = check_account_fingerprint(
            fingerprint, state_file.parent / "live_account.json")
        if drift:
            # Tech: a warning, not a fatal — it does not stop the run.
            # Why:  an account change can be legitimate (a new sub-account), and
            #       this cannot tell simulation from production anyway. The honest
            #       action is to make it impossible to miss, not to block.
            alerter.warn("account_changed", drift)
            recorder.record("account_changed", detail=drift)

        contract, contract_note = resolve_txf(api, args.contract)
        print(f"contract {contract_note}")

        # Tech: the order-placing broker is built here, after the contract is
        #       known; the offline ones need neither and could have been built
        #       earlier, but one construction site keeps the choice in one place.
        # Why:  `--broker shioaji` is the only mode that sends anything, so the
        #       guards that gate it (§5) live together with its construction
        #       rather than being scattered up the file.
        if args.broker == "shioaji":
            live_broker = ShioajiBroker(
                api, contract, sj=sj, rails=rails, recorder=recorder,
                fee_rate=cfg.fee_rate, tick_size=cfg.tick_size,
                flatten_aggression_ticks=args.flatten_aggression_ticks,
            )
            refusal = _preflight_orders(args)
            if refusal:
                alerter.fatal("startup_refused", refusal, gate="preflight")
                return 5
            # Tech: wrap the venue broker so an offline `Execution` sees the same
            #       orders and the two can be diffed.
            # Why:  V4's deliverable. `results.tsv` rests on a fill model that
            #       assumes every order fills at the next print (§2.1); this is
            #       the only way to find out what that assumption costs. The
            #       wrapper returns *only* venue fills, so measuring it cannot
            #       change it.
            broker = ShadowBroker(live_broker, fee_rate=cfg.fee_rate,
                                  recorder=recorder)
        elif args.broker == "dryrun":
            broker = DryRunBroker(execution, recorder=recorder)
        else:
            broker = SimBroker(execution)

        rt = LiveRuntime(cfg, broker=broker, recorder=recorder, rails=rails,
                         simulation=True, tick_capture=capture, alerter=alerter)
        print(rt.rails.banner())
        print(_broker_banner(args, contract_note))
        print(cfg.describe())

        # Tech: adopt whatever the account is already holding, before any bar
        #       reaches the loop; an unadoptable position stops the run here.
        # Why:  §2.20. This is the restart path: the strategy holds for days, so a
        #       soak that is killed and restarted almost always comes back to an
        #       open position. Adopting before warm-up (rather than after) means
        #       the refusal case costs seconds instead of a full backfill and
        #       model prime.
        try:
            adopted = rt.adopt_position(timestamp=datetime.now())
        except ValueError as exc:
            alerter.fatal("startup_refused", str(exc), gate="adopt_position")
            return 5
        if adopted:
            alerter.info("resumed",
                         f"adopted {adopted['position']:+d} lot(s) @ "
                         f"{adopted['entry_price']:.2f} from the venue")
        elif args.broker == "shioaji":
            print("account is flat — starting from no position")

        # --- warm-up: fetch, prime, pre-warm, seed -------------------------
        end = datetime.now().date()
        start = end - timedelta(days=args.warmup_days)
        guard.record_backfill(datetime.now())
        t0 = time.time()
        bars = rt.fetch_warmup_bars(api, contract, start.isoformat(),
                                    end.isoformat())
        print(f"backfill: {len(bars)} bars in {time.time()-t0:.1f}s "
              f"[{bars['timestamp'].iloc[0]} .. {bars['timestamp'].iloc[-1]}]"
              if len(bars) else "backfill: EMPTY")
        t0 = time.time()
        rt.warm_up(bars)
        print(f"warm: context={rt.loop.history_len()} "
              f"emitter_samples={rt._emitter_samples()} in {time.time()-t0:.1f}s")

        # --- the event stream ----------------------------------------------
        q: "queue.Queue" = queue.Queue()

        # Tech: a raw count of every print handed to the tick callback, simtrade
        #       included, plus the value it held at the last re-login.
        # Why:  the verify step (§2.24) asserts a tick has arrived *since* the
        #       re-login. It counts trial prints deliberately: this measures the
        #       **transport**, not the market, and the anchors sit inside the
        #       measured simtrade windows (08:30-08:45, 14:50-15:00) precisely so
        #       there is something to confirm against before real trading starts.
        ticks = {"n": 0, "at_relogin": -1}

        def _on_tick(tick):
            # Tech: enqueue only. No parsing beyond field access, no work.
            # Why:  this runs on Shioaji's quote thread; a ~90 ms forecast here
            #       would stall every other quote behind it.
            #
            #       One argument, not two: the SDK warns that the
            #       `(exchange, data)` form is deprecated in favour of `(data)`.
            #       Verified against a live subscription 2026-08-11.
            ticks["n"] += 1
            q.put((tick.datetime, float(tick.close), int(tick.volume),
                   bool(getattr(tick, "simtrade", False)),
                   str(getattr(tick, "code", ""))))

        # Tech: session events go straight to the trail, unfiltered.
        # Why:  §2.22 — a disconnect is an *event* the SDK reports, not something
        #       to infer from silence. Recording is deliberately all this does:
        #       writing recovery logic against a callback whose shape and timing
        #       are unobserved is how the last four SDK assumptions went wrong.
        #       FeedWatch repairs on *evidence* instead.
        def _on_session_down():
            alerter.warn("session_down", "the quote session dropped")
            recorder.record("session_down", wall=datetime.now())

        def _on_session_event(resp_code, event_code, info, event):
            recorder.record("session_event", resp_code=resp_code,
                            event_code=event_code, info=str(info)[:200],
                            event=str(event))

        def _register_callbacks():
            # Tech: clear then re-register every callback this process owns.
            # Why:  MEASURED 2026-08-17 (§2.24) — callbacks are bound to the
            #       **connection object**, not to the `api` handle. A control run
            #       settled it: a plain `login()` (+0 connections) left ticks and
            #       callbacks untouched, while `force_refresh=True` (+1) killed
            #       both — ticks 172 -> 0, order callbacks 2 -> 0. So anything
            #       that replaces the connection silently deafens this process,
            #       and registration has to be re-callable rather than a one-shot
            #       at start-up.
            #
            #       Clear-before-register for the same reason the subscription is
            #       dropped before being re-taken: if registration appends rather
            #       than replaces, a repeated repair would deliver every print
            #       twice, inflating bar volume and breaking the equality V1
            #       proved. Each clear is best-effort — losing a callback
            #       entirely is far worse than the duplicate it guards against.
            for clear in ("clear_on_tick_fop_v1_callback",
                          "clear_session_down_callback", "clear_event_callback"):
                try:
                    getattr(api, clear)()
                except Exception:                         # noqa: BLE001 — see Why
                    pass
            api.on_tick_fop_v1()(_on_tick)
            api.on_session_down(_on_session_down)
            api.on_event(_on_session_event)
            if live_broker is not None:
                live_broker.attach()

        def _subscribe(drop_first: bool):
            # Tech: take the tick subscription, optionally dropping it first.
            # Why:  `drop_first` is the §2.22 rule — subscribing on top of a live
            #       subscription risks a *doubled* tick stream. After a re-login
            #       there is nothing to drop (Q2 measured the subscription gone),
            #       so the unsubscribe is skipped there rather than fired at a
            #       session that no longer exists.
            if drop_first:
                try:
                    api.unsubscribe(contract, quote_type=sj.QuoteType.Tick)
                except Exception as exc:                  # noqa: BLE001
                    recorder.record("unsubscribe_failed", error=repr(exc))
            api.subscribe(contract, quote_type=sj.QuoteType.Tick)

        _register_callbacks()
        # Tech: streaming subscription, not polling.
        # Why:  the venue *pushes* every print to the callback above. Polling
        #       `snapshots`/`ticks` instead would burn the daily traffic quota
        #       (§2.8b) — the docs are explicit that subscription pushes do not
        #       count against it — and would sample the tape rather than see it,
        #       which cannot reconstruct minute bars faithfully.
        #       `api.subscribe`, not `api.quote.subscribe`: the latter resolves
        #       through a proxy that the SDK marks deprecated.
        _subscribe(drop_first=False)
        print("subscribed (streaming push). Ctrl-C to stop.")

        def _feed_repair():
            # Tech: FeedWatch's repair — re-register the callbacks, then re-take
            #       the subscription.
            # Why:  the old repair was subscription-only, which cannot fix the
            #       other cause of silence. If the SDK reconnects after a dropped
            #       socket it builds a **new connection**, and §2.24 measured that
            #       callbacks die with the connection — leaving this process
            #       alive, subscribed and deaf, with a subscription-only repair
            #       retrying the wrong fix until the staleness backstop halts the
            #       run. Both causes now heal through one path.
            _register_callbacks()
            _subscribe(drop_first=True)

        def _relogin():
            # Tech: the §2.24 re-login — a *refreshing* login, then re-assert
            #       everything it tore down, then note the tick count.
            # Why:  `force_refresh=True` is not optional. A plain `login()` is a
            #       measured NO-OP (+0 connections, nothing disturbed): it would
            #       have left the scheduler firing on schedule, writing `relogin`
            #       records and printing healthy counters while the token expired
            #       anyway — inert while looking like it worked.
            api.login(api_key=api_key, secret_key=secret_key, force_refresh=True)
            _register_callbacks()
            _subscribe(drop_first=False)
            ticks["at_relogin"] = ticks["n"]

        def _verify():
            # Tech: prove the session is usable *and* that prints are still
            #       arriving; raise on either failure.
            # Why:  `usage()` proves the token — it raises `AuthError` on a dead
            #       one (measured) — but says nothing about the feed. A silently
            #       failed re-subscribe would pass a token-only check and open the
            #       session deaf, with FeedWatch unable to see it until the first
            #       in-session clock tick. The tick half closes that: it runs one
            #       minute before the open, inside the trial-print window, with a
            #       whole minute left to repair.
            #
            #       Called directly, never through `_usage` below — that helper
            #       swallows exceptions and returns None, which would make this
            #       check pass forever.
            api.usage()
            # Tech: the tick half only applies on a day the market opens.
            # Why:  §2.25 — the anchors are wall-clock and fire on Saturdays and
            #       Sundays too, when there is no session and therefore no
            #       pre-open trial print to see. Without this the check would
            #       fail every non-trading day, spend a redundant recovery
            #       re-login, and write a `verify_failed` record each time —
            #       training a reader to ignore the one record that matters.
            if not cfg.policy.is_trading_day(datetime.now()):
                return
            if ticks["n"] == ticks["at_relogin"]:
                raise RuntimeError(
                    f"no tick since the re-login (count still {ticks['n']}) — "
                    f"the feed did not come back")

        feed = FeedWatch(
            # Tech: 5 s, not the SDK's 30 s default.
            # Why:  this runs on the consumer thread, so the default would stall
            #       tick processing for half a minute exactly when the feed is
            #       already suspect. Measured latency is 32-82 ms (§2.22), so 5 s
            #       is ~60x headroom and still an order of magnitude below a stall
            #       anyone would notice.
            snapshot=lambda: api.snapshots([contract], timeout=5000)[0],
            resubscribe=_feed_repair,
            interval=args.feed_check_seconds,
            # Tech: the repair budget, derived rather than picked.
            # Why:  `staleness / interval` is exactly the number of attempts that
            #       fit before the backstop halts the run — "keep trying right up
            #       to the moment we give up". The previous 3 covered only the
            #       first 3 of 15 minutes, leaving twelve with no repair attempt
            #       at all. It moves automatically if either constant does.
            max_resubscribes=_repair_budget(args.staleness_seconds,
                                            args.feed_check_seconds),
            recorder=recorder,
        )

        # Tech: the token scheduler, seeded with the start-up login's timestamp.
        # Why:  §2.24 — the session token expires 24 h after login and the SDK has
        #       no refresh. Without `note_login` the first anchor would compare
        #       against nothing.
        relogin = ReloginScheduler(relogin=_relogin, verify=_verify,
                                   recorder=recorder)
        relogin.note_login(login_at)
        print(f"relogin anchors: "
              + ", ".join(f"{a:%H:%M}/{v:%H:%M}" for a, v, _ in
                          [(datetime.combine(date.today(), t[0]),
                            datetime.combine(date.today(), t[1]), t[2])
                           for t in relogin.anchors]))

        deadline = (time.time() + args.duration_minutes * 60
                    if args.duration_minutes else None)
        last_clock = 0.0
        last_verdict = None
        # Tech: one consumer — drain with a timeout, then run clock work.
        # Why:  a single thread touching the runtime means `LiveLoop`'s
        #       single-threaded requirement holds with no locking anywhere. The
        #       timeout is what lets clock work (bar flush, staleness, heartbeat)
        #       happen even when the feed is silent — which is exactly when it
        #       matters most.
        while deadline is None or time.time() < deadline:
            try:
                ts, px, vol, simtrade, code = q.get(timeout=0.5)
                rt.on_tick(ts, px, vol, simtrade=simtrade, code=code)
                # Tech: only genuine prints refresh the feed watch, matching the
                #       staleness rail's rule.
                # Why:  a simtrade print is not evidence the market traded (§2.5),
                #       so counting it would let trial-matching keep a dead feed
                #       looking alive — the same reason the watchdog ignores them.
                if not simtrade:
                    feed.note_tick(ts, datetime.now())
            except queue.Empty:
                pass
            now = time.time()
            if now - last_clock >= 1.0:
                last_clock = now
                rt.on_clock(datetime.now())
                guard.heartbeat(datetime.now())
                # Tech: run the liveness check only inside a session we trade.
                # Why:  silence between 05:00 and 15:00 is expected, and probing
                #       the venue every minute through a ten-hour gap would spend
                #       ~600 queries a day to confirm the market is closed. The
                #       rails make exactly the same session test for exactly the
                #       same reason (§2.12).
                stamp = datetime.now()
                # Tech: the token scheduler, checked every clock tick and
                #       deliberately *not* gated on the session policy.
                # Why:  §2.24 — its anchors sit in the pre-open quiet, which is by
                #       definition outside every session. Gating it the way the
                #       feed check is gated would mean it never fired at all.
                outcome = relogin.check(stamp)
                if outcome is ReloginOutcome.RELOGGED:
                    alerter.info("relogin",
                                 "token refreshed and callbacks re-asserted")
                elif outcome is ReloginOutcome.FAILED:
                    alerter.warn("relogin_failed",
                                 "a re-login attempt failed — retrying inside "
                                 "the window")
                elif outcome is ReloginOutcome.ABANDONED:
                    alerter.warn("relogin_abandoned",
                                 "the re-login window closed with no fresh "
                                 "token")
                elif outcome is ReloginOutcome.VERIFY_FAILED:
                    # Tech: the loudest of the four, and the only one that means
                    #       a session is about to open in a known-bad state.
                    # Why:  the check runs one minute before the open and its
                    #       recovery re-login has already failed. Everything the
                    #       verify window exists to catch has happened.
                    alerter.fatal("session_unverified",
                                  "the session failed its pre-open check and "
                                  "could not be recovered — token or feed is "
                                  "dead as the session opens")
                if cfg.policy.includes(stamp):
                    # Tech: alert only when the verdict *changes*, not on every
                    #       check.
                    # Why:  a sustained outage produces the same verdict every
                    #       60 s, and a message a minute all night is how a
                    #       channel gets muted. Transitions are the information:
                    #       it broke, and later, it came back.
                    verdict = feed.check(stamp)
                    if verdict is not None and verdict is not last_verdict:
                        if verdict is Verdict.QUIET and last_verdict is not None:
                            alerter.info("feed_recovered",
                                         "the feed is delivering again")
                        elif verdict is Verdict.MISSED:
                            alerter.warn("feed_missed",
                                         "the venue traded without us — "
                                         "resubscribing")
                        elif verdict is Verdict.UNREACHABLE:
                            alerter.warn("feed_unreachable",
                                         "cannot reach the venue to check the "
                                         "feed")
                        last_verdict = verdict
                if rt.rails.halted:
                    print(f"HALTED: {rt.rails.halt_reason}")
                    break
    except KeyboardInterrupt:
        print("\ninterrupted.")
    except Exception as exc:
        # Tech: an unhandled exception is fatal by definition — the loop is gone.
        # Why:  this is the case a soak most needs to hear about, because there is
        #       no halt record, no clean shutdown, and nothing else that would
        #       ever mention it. The type name goes in `detail` so a transport can
        #       group crashes without parsing the message.
        alerter.fatal("crashed", repr(exc), error_type=type(exc).__name__)
        exit_code = 1
    finally:
        try:
            api.unsubscribe(contract, quote_type=sj.QuoteType.Tick)
        except Exception:
            pass
        status = rt.shutdown(datetime.now()) if rt else {}
        if rt:
            for k, v in rt.summary().items():
                print(f"  {k:22s} {v}")
        # Tech: the feed watch's own counters, printed and recorded.
        # Why:  a run that healed a broken stream and one that never had to look
        #       identical from the trading numbers alone. `feed_quiet` is also the
        #       direct answer to "was the old 120 s halt a false positive" —
        #       every quiet verdict is a halt that would have happened (§2.21).
        if feed is not None:
            for k, v in feed.summary().items():
                print(f"  {k:22s} {v}")
            recorder.record("feed_summary", **feed.summary())
        # Tech: the token scheduler's counters, printed and recorded.
        # Why:  `relogins` is the direct evidence the §2.24 fix ran at all. A soak
        #       that crosses 24 h with zero of them has disproved the token
        #       lifetime rather than proved the fix; one that halts on a 401
        #       *despite* them says `force_refresh` does not reset the clock.
        #       Either way the number is what distinguishes the two.
        if relogin is not None:
            for k, v in relogin.summary().items():
                print(f"  {k:22s} {v}")
            recorder.record("relogin_summary", **relogin.summary())
            age = relogin.token_age(datetime.now())
            if age is not None:
                print(f"  {'token_age_at_close':22s} {age}")
        # Tech: the alert counters, including how often a transport itself failed.
        # Why:  a Telegram sink that silently stopped working is indistinguishable
        #       from a quiet night — unless the failures are counted. This line is
        #       the only thing that tells them apart.
        for k, v in alerter.summary().items():
            print(f"  {k:22s} {v}")
        # Tech: a position left open after a FLATTEN halt is its own exit code.
        # Why:  see `LiveRuntime.shutdown` — "submitted a close" is not "flat",
        #       and that failure must never be reported as a clean run.
        if status.get("flatten_incomplete"):
            # Tech: the most serious state this program can end in.
            # Why:  a FLATTEN halt fired, the close did not fill, and the process
            #       is about to exit holding a position with nothing watching it
            #       (§2.12). If exactly one alert ever reaches a phone, this is
            #       the one — it is the only ending that needs a human *now*.
            alerter.fatal("flatten_incomplete",
                          "a position is still open after a FLATTEN halt — "
                          "close it in the broker's front end",
                          position=status.get("position_at_close"),
                          halt_reason=status.get("halt_reason"))
            exit_code = 4
        # Tech: score the last order episode, print the gap, and put the summary
        #       in the trail as its own record.
        # Why:  V4's pass criterion is "a written fill-model gap number" over ≥5
        #       sessions, so each session has to leave that number behind in a
        #       form that can be aggregated later — the console alone would not
        #       survive the run. Duck-typed because only the shioaji path has a
        #       shadow to compare against.
        if broker is not None and hasattr(broker, "gap_summary"):
            broker.close()
            print(broker.format_summary())
            recorder.record("fill_gap_summary", **broker.gap_summary())
        if capture:
            capture.close()
        recorder.close()
        try:
            api.logout()
        except Exception:
            pass
        guard.end(datetime.now())
        print(f"\nartifacts: {out}")
    return exit_code


def _usage(api):
    try:
        return api.usage()
    except Exception:
        return None


if __name__ == "__main__":
    sys.exit(main())
