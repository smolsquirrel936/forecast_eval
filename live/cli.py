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
from functools import partial
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from forecast_eval.events import FillEvent                        # noqa: E402
from forecast_eval.execution import Execution                     # noqa: E402
from forecast_eval.live.broker.dryrun import DryRunBroker         # noqa: E402
from forecast_eval.live.broker.shadow import ShadowBroker         # noqa: E402
from forecast_eval.live.broker.shioaji import ShioajiBroker       # noqa: E402
from forecast_eval.live.broker.sim import SimBroker               # noqa: E402
from forecast_eval.live.artifacts import (ArtifactWriter,        # noqa: E402
                                          live_params)
from forecast_eval.live.capture import TickCapture                # noqa: E402
from forecast_eval.live.config import CONFIGS, PINNED, config_named  # noqa: E402
from forecast_eval.live.ledger import (FillLedger, ledger_path,     # noqa: E402
                                       net_position, open_position)
from forecast_eval.live.contracts import (                        # noqa: E402
    contract_month, dated_months, resolve_txf, txf_root)
from forecast_eval.live.alerts import Alerter                     # noqa: E402
from forecast_eval.live.feedwatch import FeedWatch, Verdict       # noqa: E402
from forecast_eval.live.recorder import Recorder                  # noqa: E402
from forecast_eval.live.relogin import (                          # noqa: E402
    Outcome as ReloginOutcome, ReloginScheduler)
from forecast_eval.live.roll import (                             # noqa: E402
    Roll, RollSchedule, RollWatch, front_month_at, roll_at)
from forecast_eval.live.runtime import LiveRuntime                # noqa: E402
from forecast_eval.live import soak_epoch                         # noqa: E402
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
    # Tech: exactly one of --fresh-start / --resume, always.
    # Why:  SHIOAJI_PLAN.md's "Open items" §3 — retention across a restart was
    #       neither always-fresh nor always-recover, it was just whatever
    #       `adopt_position` happened to do, with no record of which the
    #       operator meant. Explicit and required on every order-placing
    #       launch, matching how `--i-understand-this-places-orders` itself
    #       replaced an absence rather than a default.
    if args.fresh_start and args.resume:
        return "--fresh-start and --resume are mutually exclusive — pick one."
    if not args.fresh_start and not args.resume:
        return ("--broker shioaji needs --fresh-start or --resume: state "
                "retention across a restart must be a deliberate choice.")
    return None


def _resolves_soak_epoch(args: Any) -> bool:
    # Tech: whether this launch resolves — and so may *write* — a soak epoch.
    #       Exactly the launches `_preflight_orders` accepts, and no others.
    # Why:  `soak_epoch.resolve` writes on both branches, and it runs ~100 lines
    #       before `_preflight_orders` gets to refuse anything. Inlined in
    #       `main`, the two conditions were free to disagree, and they did: the
    #       inline guard caught the both-flags case and missed the neither-flag
    #       one, so a launch that was about to be refused still minted a phantom
    #       `soak_epoch.json`. A seam here is the same remedy `_rails_config`
    #       exists for — a condition that is read once in `main` cannot be
    #       tested, and a wrong one looks exactly like a right one.
    return args.broker == "shioaji" and (args.fresh_start != args.resume)


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
    ap.add_argument("--fresh-start", action="store_true",
                    help="Start a new soak epoch. --broker shioaji requires "
                         "exactly one of --fresh-start / --resume; the two are "
                         "mutually exclusive. Refuses at start-up unless the "
                         "account is already flat — flatten with "
                         "`python -m forecast_eval.live.flatten` first; this "
                         "flag never places an order itself.")
    ap.add_argument("--resume", action="store_true",
                    help="Continue the current soak epoch across a restart. "
                         "--broker shioaji requires exactly one of "
                         "--fresh-start / --resume; the two are mutually "
                         "exclusive. Carries the soak id forward so "
                         "session.jsonl files across restarts can be read as "
                         "one soak; bootstraps the first epoch if none exists "
                         "yet.")
    # Tech: --config names the whole deployment; the policy travels inside the
    #       value rather than beside it.
    # Why:  DAY_SESSION_PLAN C2. This used to be `cfg = PINNED` with no way to
    #       say DAY at all. The obvious repair -- a `--session day` flag -- is
    #       worse than the gap: it moves the policy while leaving NIGHT's tuned
    #       band and probability gate in place, which is a night strategy on a
    #       day tape and raises nothing. W5a measured that mistake at Calmar
    #       16.3122 -> 0.2671. Selecting the config makes the pairing unbreakable.
    ap.add_argument("--config", choices=sorted(CONFIGS), default="night",
                    help="Which pinned deployment to run. Selects the WHOLE "
                         "config -- policy, band, probability gate and "
                         "provenance together. Default night (the deployment "
                         "that has soaked); day is DAY_SESSION_PLAN's port and "
                         "has never traded.")
    ap.add_argument("--contract", default="TXFR1")
    ap.add_argument("--duration-minutes", type=float, default=None,
                    help="Stop after this long. Default: run until the session "
                         "ends or Ctrl-C.")
    # Tech: default None here and resolved from the selected config after
    #       parsing, NOT baked from PINNED at parser-build time.
    # Why:  DAY_SESSION_PLAN C6, and this is where that fix would have been
    #       undone. Argparse evaluates defaults when the parser is built, before
    #       --config has been read, so a baked default is always NIGHT's 30. DAY
    #       needs 84: at 300 bars/session, 30 days reaches ~109 emitter
    #       boundaries and about 4 non-HOLD samples against the floor of 3 that
    #       `_check_primed` refuses below -- "one quiet fortnight from refusing
    #       to start". A DAY run silently inheriting 30 is exactly the failure W4
    #       removed, re-entering through the entry point W6 added.
    ap.add_argument("--warmup-days", type=int, default=None,
                    help="Calendar days of history to request for the context "
                         "and emitter priming. Default is derived from the "
                         "SELECTED config's policy bars-per-session "
                         "(DAY_SESSION_PLAN C6), not a flat constant — "
                         f"{PINNED.warmup_days_default()} for night, "
                         f"{config_named('day').warmup_days_default()} for day")
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


def _run_dir(cfg: Any, now: Optional[datetime] = None) -> Path:
    # Tech: the default run directory — `outputs/live_<slug>_<timestamp>/`, with
    #       the slug being the deployment's own `policy.cache_slug()`.
    # Why:  the name used to be `live_<timestamp>` alone, which says when a run
    #       happened but not *what ran*. With two deployments that is a real
    #       ambiguity: a directory of day artifacts and one of night artifacts
    #       are indistinguishable without opening `params.json`, and they are the
    #       two things most likely to be compared side by side.
    #
    #       `cache_slug()` rather than a new name of its own, so the run
    #       directory, the bar cache (`bars_cache_night.csv`) and the fill ledger
    #       (`fills_night.csv`) all carry the *same* token and a reader can see at
    #       a glance which files belong to which deployment. That does mean a
    #       `both()` policy reads as `day_night` and not "fullday" — "fullday" is
    #       a tape name (§2.5), and giving a deployment a second identity is how
    #       two names for one thing start.
    return Path("outputs") / (
        f"live_{cfg.policy.cache_slug()}_{(now or datetime.now()):%Y%m%d_%H%M%S}")


def _bars_cache_path(out_dir: Path, policy: Any) -> Path:
    # Tech: the bar cache, keyed by the deployment's session policy.
    # Why:  C5. One `bars_cache.csv` was shared by every deployment, so a DAY run
    #       wrote DAY bars into the file a NIGHT run reads back as model context.
    #       `warm_up` rejects the mixture rather than trading it, so this was
    #       never silent — but it blocks whichever soak starts second, and the
    #       cache is the only source for months the venue has delisted (§2.8
    #       Finding D), so what it costs is context that cannot be refetched.
    #
    #       The pre-C5 `bars_cache.csv` is NOT adopted automatically. It holds
    #       NIGHT bars, and auto-adopting it would hand them to whichever
    #       deployment ran first — a DAY run inheriting a night cache is the
    #       exact corruption this function exists to prevent. That file was
    #       renamed to `bars_cache_night.csv` once, by hand, on 2026-08-24.
    return out_dir / f"bars_cache_{policy.cache_slug()}.csv"


def _warmup_contracts(api: Any, front: Any) -> list:
    """The listed months up to and including `front`, oldest first."""
    # Tech: enumerate the dated TXF months, keep those not later than the front
    #       month, and return the last two. Any failure degrades to `[front]`.
    # Why:  §2.6 — two months is what a spliced warm-up needs: the one trading now
    #       and the one behind it, whose bars cover the window before the roll.
    #       Older months are already delisted and would only cost a wasted fetch.
    #       This runs on the start-up path, where an SDK-shape surprise must not
    #       be fatal (§2.18): the single-contract answer is exactly today's
    #       behaviour, so the degraded case is never worse than not trying.
    try:
        root, _route = txf_root(api)
        month = contract_month(front)
        earlier = [c for c in dated_months(root) if contract_month(c) <= month]
        return earlier[-2:] or [front]
    except Exception:                                  # noqa: BLE001 — see Why
        return [front]


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    cfg = config_named(args.config)
    # Tech: resolve the warm-up default now that the config is known.
    # Why:  see --warmup-days above; None means "whatever this deployment needs".
    if args.warmup_days is None:
        args.warmup_days = cfg.warmup_days_default()

    out = args.out or _run_dir(cfg)
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
    # Tech: the pure argument checks run BEFORE the restart guard.
    # Why:  MEASURED 2026-09-02 — a command missing `--resume` was refused by the
    #       *backfill cap* instead, because the guard gated at start-up and this
    #       check sat 150 lines later, next to the broker it protects. The
    #       operator was told "something is restarting this process repeatedly"
    #       when the real answer was one missing flag. A deterministic check on
    #       `args` alone has no side effects and cannot fail for an environmental
    #       reason, so it belongs in front of every gate that touches persisted
    #       state or the venue.
    if args.broker == "shioaji":
        refusal = _preflight_orders(args)
        if refusal:
            alerter.fatal("startup_refused", refusal, gate="preflight")
            return 5

    guard = RestartGuard(RestartPolicy(state_file=state_file))
    try:
        guard.begin(datetime.now())
    except RestartBlocked as exc:
        alerter.fatal("startup_refused", str(exc), gate="RestartGuard")
        return 3

    # Tech: resolve the soak epoch from --fresh-start/--resume, only for the
    #       order-placing broker — dryrun/sim have no venue ledger for a soak
    #       id to ever get reconciled against. Skipped unless *exactly one*
    #       flag is set, which is the only combination `_preflight_orders`
    #       accepts moments later, once the broker exists.
    # Why:  soak_epoch.py / SHIOAJI_PLAN.md "Open items" §3. This has to
    #       happen before the recorder exists so the epoch id can go in its
    #       `meta` record — the one place every session.jsonl is
    #       self-identifying without a second file to cross-reference. But
    #       `resolve` *writes* on both of its branches — `fresh_start=True`
    #       retires whatever epoch is on disk, and `--resume` with nothing on
    #       disk bootstraps one — and neither may fire for a launch that is
    #       about to be refused anyway. An invalid combination would otherwise
    #       destroy a real epoch's continuity, or mint a phantom epoch, for a
    #       run that never traded a single bar.
    #
    #       MEASURED 2026-08-31: guarding only the both-flags case left the
    #       neither-flag case — equally refused, 100 lines later — writing a
    #       first-ever `soak_epoch.json` on its way to that refusal. The next
    #       `--fresh-start` then retired the phantom into `retired[]`, and the
    #       next `--resume` adopted it as the soak's identity. `!=` is the one
    #       condition `_preflight_orders` already enforces, so the two cannot
    #       drift apart again.
    epoch = None
    if _resolves_soak_epoch(args):
        epoch = soak_epoch.resolve(state_file.parent / "soak_epoch.json",
                                   fresh_start=args.fresh_start,
                                   now=datetime.now())

    recorder = Recorder(out / "session.jsonl", simulation=True,
                        meta={"config": cfg.describe(), "broker": args.broker,
                              "contract": args.contract,
                              "soak_id": epoch.soak_id if epoch else None})
    alerter.recorder = recorder
    capture = None if args.no_capture else TickCapture(out / "ticks.csv")

    # Tech: the run directory gets the backtest's own artifact set — fills,
    #       orders, forecasts, signals, trades and params.json — refreshed while
    #       the run is alive, not only at the end.
    # Why:  a live run used to leave `session.jsonl` and `ticks.csv`, so the two
    #       halves of this project produced different evidence and
    #       `reports.generate_report`, which only ever wanted three CSVs in a
    #       directory, could not be pointed at a soak at all. `LiveLoop.result()`
    #       already returns a `BacktestResult`, so nothing is re-derived: the
    #       same `write_all` runs over the same object, and the schemas cannot
    #       drift because there is only one writer. `params.json` goes down now
    #       rather than at shutdown, so a run that dies in its first minute still
    #       says what it was trying to be.
    artifacts = ArtifactWriter(out, policy=cfg.policy,
                               soak_id=epoch.soak_id if epoch else None)
    # Tech: the deployment's fill ledger, keyed by policy and scoped to the soak.
    # Why:  see `live/ledger.py` — a restart adopts the position but not its
    #       history, so `trades.csv` mis-paired every trade on a resumed run.
    #       Keyed like the bar cache (C5) so DAY and NIGHT can never replay each
    #       other's fills; it lives beside the bar cache, in `out.parent`, since
    #       it outlives any single run directory.
    ledger = FillLedger(ledger_path(out.parent, cfg.policy),
                        soak_id=epoch.soak_id if epoch else None)
    artifacts.write_params(live_params(
        cfg, out_dir=out, broker=args.broker, warmup_days=args.warmup_days,
        soak_id=epoch.soak_id if epoch else None))

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
    roll = None
    schedule = None
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

        # Tech: --fresh-start refuses a non-flat account, before adopt_position
        #       ever runs. It only checks — it never places an order.
        # Why:  soak_epoch.py / SHIOAJI_PLAN.md "Open items" §3. Without this,
        #       `--fresh-start` would still silently adopt whatever the venue
        #       holds, since `adopt_position` below runs unconditionally either
        #       way — making the flag mean "start a new soak id" but not
        #       "start clean," which is the one thing an operator reaching for
        #       it actually wants. `flatten.py` stays the only path that closes
        #       a position; this is a check, not a second implementation of it.
        if args.broker == "shioaji" and args.fresh_start:
            held = rt.broker.broker_position_detail()
            if held is not None and held.signed_qty != 0:
                alerter.fatal(
                    "startup_refused",
                    f"--fresh-start refuses a non-flat account: holds "
                    f"{held.signed_qty:+d} lot(s) @ {held.avg_price:.2f}. "
                    f"Flatten with `python -m forecast_eval.live.flatten`, "
                    f"then restart with --fresh-start again.",
                    gate="fresh_start")
                return 5

        # Tech: adopt whatever the account is already holding, before any bar
        #       reaches the loop; an unadoptable position stops the run here.
        # Why:  §2.20. This is the restart path: the strategy holds for days, so a
        #       soak that is killed and restarted almost always comes back to an
        #       open position. Adopting before warm-up (rather than after) means
        #       the refusal case costs seconds instead of a full backfill and
        #       model prime.
        # Tech: read the ledger before adopting, so a broker with no venue can
        #       adopt from it instead.
        # Why:  purely a file read, and it has to happen first to be available as
        #       the fallback below. A `--broker dryrun` run has no account to ask
        #       and its ledger is then the only record of what the deployment
        #       holds; without this the mirrors start flat while the seeded fills
        #       net non-zero, and the next dry-run fill pairs against a lot the
        #       Trader does not know it has.
        inherited = ledger.replay() if args.resume else []
        try:
            adopted = rt.adopt_position(timestamp=datetime.now(),
                                        fallback=open_position(inherited))
        except ValueError as exc:
            alerter.fatal("startup_refused", str(exc), gate="adopt_position")
            return 5
        if adopted:
            whence = ("the venue" if adopted["source"] == "venue"
                      else f"the {cfg.policy.cache_slug()} ledger")
            alerter.info("resumed",
                         f"adopted {adopted['position']:+d} lot(s) @ "
                         f"{adopted['entry_price']:.2f} from {whence}")
        elif args.broker == "shioaji":
            print("account is flat — starting from no position")

        # Tech: replay this deployment's fills for the live soak, and seed the
        #       loop's list with them; fall back to one synthetic opening fill
        #       when the ledger cannot support the position actually held.
        # Why:  a restart adopts the *position* but not its history, and
        #       `build_trades` walks from flat — so the inherited position's
        #       close was booked as an OPEN and every pair after it shifted
        #       (measured: one SHORT +100 reported where the truth was two LONGs
        #       of +145 and +300, with the run's last fill dropped). Nothing in
        #       the loop reads `fills` to decide anything — `_note_roll_fill`
        #       takes `fills[-1]`, which seeding at the front cannot move — so
        #       this is inert for trading and fixes only the record.
        #
        #       The reconciliation is the load-bearing part. If the replay does
        #       not add up to what the venue holds, the history is wrong (a
        #       manual flatten, a crash between a fill and its sync, a different
        #       account), and seeding it would produce a confidently wrong
        #       `trades.csv`. In that case the ledger is refused and the
        #       synthetic fill is used instead: one opening fill at the venue's
        #       own average price, which cannot reconstruct the history but does
        #       guarantee the pairing. `entry_ts` is then the resume moment, not
        #       the original open — the venue reports an average price, not a
        #       time — which is the same approximation `trader.entry_timestamp`
        #       already makes.
        # Tech: reconcile only when a venue actually answered. `rt.venue_position`
        #       is None when this broker has none to ask, and the check is then
        #       skipped rather than run against a zero nobody reported.
        # Why:  the reconciliation is load-bearing against a REAL account — a
        #       replay that does not add up to what the venue holds means the
        #       history is wrong and seeding it would produce a confidently wrong
        #       `trades.csv`. Against a broker with no venue it was comparing the
        #       ledger to a fabricated flat, so it refused every time and the run
        #       wrote empty artifacts (MEASURED on `live_night_20260902_082640`:
        #       "replays to +1 lot(s) but the venue holds +0", broker `dryrun`).
        #       A `shioaji` query that *raised* still lands here with
        #       `venue_position` None — `has_venue` keeps that case out of the
        #       skip, so an unknown answer refuses instead of being assumed.
        held_now = rt.venue_position
        if inherited and held_now is not None and net_position(inherited) != held_now:
            alerter.warn(
                "fills_replay_refused",
                f"the {cfg.policy.cache_slug()} ledger replays to "
                f"{net_position(inherited):+d} lot(s) but the venue holds "
                f"{held_now:+d} — refusing the history and booking the open "
                f"position from the venue instead")
            recorder.record("fills_replay_refused",
                            ledger_net=net_position(inherited),
                            venue_position=held_now,
                            replayed=len(inherited))
            inherited = []
        elif inherited and held_now is None:
            recorder.record("fills_replay_unreconciled",
                            ledger_net=net_position(inherited),
                            replayed=len(inherited),
                            broker=args.broker)
        if not inherited and adopted:
            inherited = [FillEvent(
                timestamp=adopted["adopted_at"],
                side="BUY" if adopted["position"] > 0 else "SELL",
                fill_price=float(adopted["entry_price"]),
                quantity=abs(int(adopted["position"])), fee=0.0)]
            recorder.record("fills_synthesised", **adopted)
        if inherited:
            rt.loop.fills.extend(inherited)
            ledger.mark_written(len(inherited))
            print(f"resume: {len(inherited)} inherited fill(s) seeded, "
                  f"net {net_position(inherited):+d}")
            recorder.record("fills_replayed", count=len(inherited),
                            net=net_position(inherited),
                            soak_id=ledger.soak_id)

        # --- warm-up: fetch, prime, pre-warm, seed -------------------------
        end = datetime.now().date()
        start = end - timedelta(days=args.warmup_days)
        guard.record_backfill(datetime.now())
        t0 = time.time()
        # Tech: back-fill from every listed month up to the front one, merged
        #       with the local cache, rather than from the front month alone.
        # Why:  §2.6. The front month's *own* history before it became front is
        #       thin back-month trading, so 3 008 of those bars span far more
        #       calendar time than the model expects. Collapsing several months on
        #       volume reproduces the offline loader's front-month-continuous
        #       series; the cache carries the month that just expired, which the
        #       venue delists and cannot serve any more (§2.8 Finding D).
        #
        #       Tech: the splice rule is bound to THIS deployment's policy before
        #             it is handed over.
        #       Why:  TAPE_REBUILD_PLAN §5.3, MEASURED. This argument used to be
        #             the bare function, whose splice instant was the module
        #             default — NIGHT's 15:30 — for every deployment. A DAY run
        #             therefore rolled its position at 12:45 (`RollSchedule`,
        #             built with `cfg.policy` below and always correct) while
        #             splicing its *history* at 15:30, so the 60 minutes
        #             12:45-13:44 of every roll day were warmed on the month it
        #             had already stopped trading: 183 of 183 DAY rolls that
        #             traded, `|dclose|` median 27 points and p95 183, about 6 %
        #             of a DAY start-up's 3 008-bar context. Nothing downstream
        #             could raise, because the live *stream* was right and only
        #             the warm-up was wrong. NIGHT does not move by one bar --
        #             `SessionPolicy.night().roll_at_time()` IS 15:30.
        bars = rt.fetch_warmup_bars(api, _warmup_contracts(api, contract),
                                    start.isoformat(), end.isoformat(),
                                    cache_path=_bars_cache_path(out.parent, rt.policy),
                                    front_month_at=partial(front_month_at,
                                                           policy=cfg.policy))
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
            # Tech: the venue's `event` argument is recorded as `venue_event`,
            #       not `event`.
            # Why:  MEASURED 2026-08-31 — `Recorder.record(self, event, **fields)`
            #       takes the record's *name* as its first parameter, so passing
            #       `event=` as well raised `TypeError: got multiple values for
            #       argument 'event'` on every single venue session event. The
            #       Shioaji Rust layer swallows callback exceptions, so nothing
            #       ever halted and nothing was logged on this side: 14 raises in
            #       `shioaji.log` since 2026-08-13 and **zero** `session_event`
            #       records in any `session.jsonl` ever written. A silent hole in
            #       the trail, in the one channel that reports what the venue
            #       itself said about the session.
            recorder.record("session_event", resp_code=resp_code,
                            event_code=event_code, info=str(info)[:200],
                            venue_event=str(event))

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

        def _apply_roll(rolled, stamp):
            # Tech: adopt the new contract everywhere the old one was held — the
            #       name the closures read, the broker's contract and code set,
            #       and the subscription — then settle any open leg.
            # Why:  §2.6, MEASURED. One expired object was held by four different
            #       things at once, and re-subscribing it was inert: fifteen
            #       repairs, zero ticks, then a staleness halt. Every holder has
            #       to be moved together, in one place, or the next roll finds
            #       whichever one was forgotten.
            nonlocal contract
            old = contract
            try:
                api.unsubscribe(old, quote_type=sj.QuoteType.Tick)
            except Exception as exc:                  # noqa: BLE001
                # Tech: a failed unsubscribe on the expired month is expected.
                # Why:  the venue has already retired it; there may be nothing
                #       left to unsubscribe from. Recorded rather than raised,
                #       because the subscription that matters is the next line.
                recorder.record("unsubscribe_failed", error=repr(exc))
            contract = rolled.contract
            if live_broker is not None:
                live_broker.switch_contract(contract)
            _subscribe(drop_first=False)
            # Tech: re-baseline the verify window's tick counter.
            # Why:  it asserts a tick has arrived *since* the last re-login. The
            #       ticks it counted belong to a contract that no longer prints,
            #       so leaving the baseline where it was would let a stale count
            #       satisfy the check on the new month.
            ticks["at_relogin"] = ticks["n"]
            print(f"contract rolled {rolled.from_month} -> {rolled.to_month}: "
                  f"{rolled.note}")
            alerter.warn("contract_rolled",
                         f"front month {rolled.from_month} -> "
                         f"{rolled.to_month}; re-subscribed and re-pointed")
            # Tech: settle the expiring leg in the mirrors, at the last price this
            #       process saw, and arm the same exposure for the new month.
            # Why:  the alias moves only after the old month is dead, so the venue
            #       has already cash-settled anything held — there is no order
            #       that could close it. `LiveRuntime` re-opens on the first bar
            #       of the new contract, which is what "roll the exposure" means
            #       once the old leg is beyond reach.
            if rt.trader.position != 0:
                mark = rt.last_price
                if mark is None:
                    alerter.fatal("roll_unsettled",
                                  "the contract rolled with a position open and "
                                  "no price to settle it at — position mirror is "
                                  "stale")
                else:
                    rt.settle_rolled_leg(price=mark, timestamp=stamp)

        def _next_month_contract():
            # Tech: the listed month directly after the one being held.
            # Why:  the alias cannot express "the next one" — `TXFR1` still names
            #       the expiring month right up to its expiry, which is exactly
            #       why the scheduled roll cannot be driven by it. `TXFR2` is the
            #       documented second-nearest alias and is tried first; the dated
            #       list is the fallback for the day that alias is not there.
            held = contract_month(contract)
            try:
                nxt, note = resolve_txf(api, "TXFR2")
                if contract_month(nxt) > held:
                    return nxt, note
            except Exception as exc:                  # noqa: BLE001
                recorder.record("roll_next_alias_failed", error=repr(exc))
            root, _route = txf_root(api)
            later = [c for c in dated_months(root)
                     if contract_month(c) > held]
            if not later:
                return None, "no later month is listed"
            c = later[0]
            return c, (f"TXF -> code={getattr(c, 'code', '?')} "
                       f"delivery_month={contract_month(c)} (via dated months)")

        def _roll_evidence(nxt, stamp):
            # Tech: snapshot both months, record their volumes, and return whether
            #       the incoming one is actually printing.
            # Why:  the roll date is a naive third Wednesday with no holiday table
            #       (§2.6), so it is checked against the tape rather than trusted.
            #       The gate is "is the month we are about to trade alive", not
            #       "has volume crossed over" — at the roll instant the expiring
            #       month usually still has the larger volume, so a crossover test
            #       would refuse every correct roll. The volumes are recorded
            #       either way, which is how the calendar gets validated from data
            #       instead of from argument.
            try:
                snaps = api.snapshots([contract, nxt], timeout=5000)
            except Exception as exc:                  # noqa: BLE001
                recorder.record("roll_evidence_failed", when=stamp.isoformat(),
                                error=repr(exc))
                return False
            got = {}
            for label, snap in zip(("old", "new"), list(snaps)[:2]):
                got[f"{label}_volume"] = getattr(snap, "total_volume", None)
                got[f"{label}_ts"] = getattr(snap, "ts", None)
                got[f"{label}_close"] = getattr(snap, "close", None)
            recorder.record("roll_evidence", when=stamp.isoformat(),
                            old_month=contract_month(contract),
                            new_month=contract_month(nxt), **got)
            alive = bool(got.get("new_volume")) and got.get("new_close")
            if not alive:
                alerter.warn("roll_deferred",
                             f"the incoming month {contract_month(nxt)} is not "
                             f"printing yet — not rolling on this tick")
            return bool(alive)

        def _check_schedule(stamp):
            # Tech: run the scheduled roll — start it inside the window, and move
            #       the contract once the runtime says the old leg is closed.
            # Why:  §2.6. This is the *plan*; `_check_roll` below is the net. The
            #       plan moves while both months still trade, which is the only
            #       time a position can be rolled rather than settled.
            if rt.roll_state == "switch":
                nxt, note = _next_month_contract()
                if nxt is None:
                    alerter.fatal("roll_no_next_month",
                                  "the old leg is closed and no later month is "
                                  "listed — the run is flat and cannot re-enter")
                    return
                _apply_roll(Roll(contract=nxt, note=note,
                                 from_month=contract_month(contract),
                                 to_month=contract_month(nxt)), stamp)
                roll.month = contract_month(nxt)
                schedule.done(schedule.month, contract_month(nxt), stamp)
                rt.resume_roll(stamp.isoformat())
                return

            if rt.roll_state is not None:
                return                                # a roll is already running
            if schedule.due(stamp):
                nxt, _note = _next_month_contract()
                if nxt is not None and _roll_evidence(nxt, stamp):
                    alerter.info("roll_started",
                                 f"scheduled roll {schedule.month} -> "
                                 f"{contract_month(nxt)} beginning")
                    rt.request_roll(stamp.isoformat())
            elif (schedule.lapsed(stamp)
                  and schedule.month not in lapsed_reported):
                lapsed_reported.add(schedule.month)
                alerter.warn("roll_window_lapsed",
                             f"the roll window for {schedule.month} closed "
                             f"without rolling — falling back to RollWatch, "
                             f"which settles the position instead of trading it")

        def _check_roll(stamp, reason):
            # Tech: ask the watcher, and apply anything it hands back.
            # Why:  one call site for both triggers, so the reactive path and the
            #       scheduled one cannot drift into doing different things.
            rolled = roll.check(stamp, reason=reason)
            if rolled is not None:
                _apply_roll(rolled, stamp)

        # Tech: the roll watcher, seeded with the month resolved at start-up.
        # Why:  §2.6 — `TXFR1` is dereferenced once and the object outlives the
        #       month it named. `fetch_contracts` is passed as the refresh because
        #       whether a re-login re-downloads the contract table is unmeasured;
        #       calling it explicitly means the answer does not depend on that.
        roll = RollWatch(
            resolve=lambda: resolve_txf(api, args.contract),
            month=contract_month(contract),
            refresh=getattr(api, "fetch_contracts", None),
            recorder=recorder,
        )
        # Tech: the calendar half — when we *plan* to move, as opposed to when the
        #       venue forces us to.
        # Why:  §2.6. The alias only flips after the old month is dead, so a plan
        #       driven by it can never roll a position; it can only settle one.
        #       This fires while both months trade.
        schedule = RollSchedule(month=contract_month(contract),
                                recorder=recorder, policy=cfg.policy)
        lapsed_reported: set = set()
        print(f"roll: {schedule.month} until "
              f"{roll_at(schedule.month, cfg.policy):%Y-%m-%d %H:%M}")

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
                # Tech: refresh the artifact set; the writer throttles on its own
                #       interval and skips when no list has grown.
                # Why:  a soak runs for weeks, so "the files are written at the
                #       end" is not trackable — but ten of every twenty-four
                #       hours are outside the session, where nothing arrives and
                #       an unguarded write would rewrite five files a minute,
                #       byte for byte, for the whole dead zone.
                artifacts.write(rt)
                # Tech: append any fill the ledger has not seen yet.
                # Why:  cheap and bounded — it writes the tail past a high-water
                #       mark, so the cost is the number of NEW fills (0.18-0.37 a
                #       night), not the length of the soak. This is what makes
                #       the *next* restart able to replay this one.
                ledger.sync(rt.loop.fills)
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
                    # Tech: check for a roll immediately after a re-login.
                    # Why:  §2.6 — the anchors sit in the pre-open quiet, which is
                    #       the one moment a contract change can be adopted before
                    #       real prints depend on it, and the login has just given
                    #       the SDK its best chance of a fresh contract table.
                    _check_roll(stamp, "after_relogin")
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
                # Tech: the scheduled roll, checked every clock tick, and the
                #       ladder that re-prices an unfilled roll leg.
                # Why:  §2.6 — the window is an hour wide because the roll is two
                #       orders that both have to fill. The ladder must run on the
                #       *clock*: a bar is a minute wide and the whole point is to
                #       retry inside one, escalating 5 ticks each time until the
                #       leg goes through. Submitting off the bar path is safe —
                #       fill *collection* stays the loop's, so the single-consumer
                #       invariant is untouched.
                _check_schedule(stamp)
                rt.advance_roll_ladder(stamp.isoformat())
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
                            # Tech: MISSED is also what a roll looks like, so ask.
                            # Why:  §2.6 — this is the verdict the dead soak
                            #       produced fifteen times while its repair
                            #       re-subscribed an expired contract. The watcher
                            #       throttles itself, so a genuine feed outage
                            #       cannot turn this into a venue call a minute.
                            _check_roll(stamp, "feed_missed")
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
        if rt is not None and getattr(rt, "roll_fills", None):
            recorder.record("roll_fills", timestamps=sorted(
                str(t) for t in rt.roll_fills))
        if roll is not None:
            for k, v in roll.summary().items():
                print(f"  {k:22s} {v}")
            recorder.record("roll_summary", **roll.summary())
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

        # Tech: the final artifact write, forced past both gates, and the only
        #       one that renders the report.
        # Why:  `force` because the last bars must land even if the throttle just
        #       fired; `report=True` here and nowhere else because rendering
        #       imports matplotlib and redraws every chart — acceptable once, on
        #       a process that is already shutting down, and wasteful every
        #       minute of a three-month soak. A failure is swallowed and counted:
        #       these files record a live run, they never feed one.
        if rt is not None:
            ledger.sync(rt.loop.fills)
        for k, v in ledger.summary().items():
            print(f"  {k:22s} {v}")
        recorder.record("ledger_summary", **ledger.summary())
        written = artifacts.write(rt, report=True, force=True) if rt else None
        if written:
            print(f"  artifacts             {len(written)} files -> {out}")
        for k, v in artifacts.summary().items():
            print(f"  {k:22s} {v}")
        recorder.record("artifact_summary", **artifacts.summary())

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
