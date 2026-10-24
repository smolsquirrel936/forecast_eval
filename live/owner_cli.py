"""Run the owning process — the one thing that places orders (§18.2).

    uv run --no-sync python -m forecast_eval.live.owner_cli --out outputs/live_soak \\
        --i-understand-this-places-orders

Three deployments run `live/cli.py --broker client` beside this and submit
intents; this logs in once, places every order, and routes every deal back to the
deployment whose order it was.

**Start this BEFORE the deployments.** A `ClientBroker` treats a stream that has
never spoken as "cannot see the book" and halts — deliberately, so a missing owner
is never mistaken for a flat account (`broker/client.py`).

**It seeds from the ledgers, not from the venue.** On a shared book
`list_positions` gives the account total with no way to split it, so a restart
reads each deployment's own `fills_<slug>.csv` (§16) and reconciles the sum of
those against the venue on the first pass. If they disagree at start-up, that is
a real desync and this refuses rather than adopting a number it cannot attribute.

**This process logs in; so do the deployments.** They still need the market feed
to build bars — only *orders* are delegated. That is four sessions on one
credential, and §18.13 MEASURED that four coexist with the first still usable.

What is **not** measured is what four subscriptions to the same tick stream cost
against the 500 MiB daily allowance. `RestartGuard.check_usage` refuses to START
below 50 MB, because over quota Shioaji returns EMPTY history rather than
erroring — but that is a start-up gate. This process watches the headroom on its
reconcile cadence and warns, because it is the one that outlives all three
deployments.

**It re-logs in before every session open, like the deployments.** A Shioaji
token dies 24 h after login and the SDK cannot refresh one (`relogin.py`, §2.24).
Until 2026-09-29 the owner logged in once and never again, so a run past 24 h
kept its deployments trading while the owner's `list_positions` raised (logged,
not stopped) and its orders failed. It now runs the same `ReloginScheduler`,
three minutes AHEAD of the deployments' windows — see `OWNER_RELOGIN_LEAD`.

**The console prints the account's `person_id`** — a Taiwanese national ID —
from Shioaji's compiled layer, exactly as `live/cli.py` does. Read the output
before pasting it anywhere.
"""
from __future__ import annotations

import argparse
import signal
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from forecast_eval.live import telegram                           # noqa: E402
from forecast_eval.live.alerts import Alerter                     # noqa: E402
from forecast_eval.live.artifacts import git_commit, git_dirty  # noqa: E402
from forecast_eval.live.contracts import resolve_txf            # noqa: E402
from forecast_eval.live.ledger import FillLedger, open_position  # noqa: E402
from forecast_eval.live.owner_process import OwnerProcess       # noqa: E402
from forecast_eval.live.recorder import Recorder                # noqa: E402
from forecast_eval.live.relogin import (Outcome,                 # noqa: E402
                                        ReloginScheduler,
                                        session_open_anchors)
from forecast_eval.live.startup import usage_bytes              # noqa: E402

# Tech: how often the owner drains intents and republishes positions.
# Why:  the deployments decide on one-minute bars, so a second is already far
#       finer than anything downstream can use — but an intent waiting is an
#       order not placed, and the cost of polling a few files is nothing. The
#       reconcile runs on its own, slower cadence because it costs a venue call.
DEFAULT_POLL_SECONDS = 1.0
DEFAULT_RECONCILE_SECONDS = 30.0

# Tech: the owner's re-login and verify leads before each session open —
#       re-login from open-8 min, verify from open-6 min (08:37/08:39,
#       14:52/14:54).
# Why:  the deployments re-login at open-5 min, all three in the same second.
#       A `force_refresh=True` login opens a new connection before the old one
#       drops, ~30 s later (relogin_probe Q9), against a cap of 5 per person_id
#       (§2.8b) with four already open. Going first and three minutes early
#       means the owner's spike has cleared before theirs begin, so the owner
#       adds nothing to that collision — and the process every deployment
#       depends on is refreshed before any of them.
OWNER_RELOGIN_LEAD = timedelta(minutes=8)
OWNER_VERIFY_LEAD = timedelta(minutes=6)


def owner_relogin_anchors():
    """The owner's `(relogin_at, verify_at, open_at)` triples."""
    return session_open_anchors(relogin_lead=OWNER_RELOGIN_LEAD,
                                verify_lead=OWNER_VERIFY_LEAD)


def make_relogin(api: Any, cred: Any, account: Any, owner: Any):
    """The owner's re-login action: refresh the token, re-take what it drops."""
    def _relogin() -> None:
        # Tech: log out, log in plainly, then subscribe the NEW session's
        #       account to the deal stream and re-attach the order callback;
        #       raise if the subscription is refused.
        # Why:  MEASURED 2026-10-08 (relogin_probe --mode reassert). After a
        #       `force_refresh=True` login no re-assertion brings the order
        #       callback back — not `set_order_callback`, not `subscribe_trade`
        #       with the old account or the new one: 0 callbacks on each. The
        #       2026-10-07 owner re-logged in that way at 14:52 and never heard
        #       the 22:16 night fill. Logout then a plain login opens a genuinely
        #       new session ("Session up") and was heard, 2 of 2, not doubled.
        #       A plain `login()` WITHOUT the logout is still the measured no-op
        #       (Q1/Q8). A refused `subscribe_trade` raises so the scheduler
        #       counts a failed attempt and retries inside the window, instead of
        #       settling on a session that cannot hear fills (§18.14).
        #
        #       The logout is best-effort: one that raises (a token already dead)
        #       must not stop the login that replaces it. The subscription takes
        #       `api.futopt_account` read after the login, the form the probe
        #       measured; `account` stays the owner's for placing and
        #       reconciling, as it did across 2026-10-07's re-login.
        try:
            api.logout()
        except Exception:                               # noqa: BLE001 — see Why
            pass
        api.login(api_key=cred.api_key, secret_key=cred.secret_key)
        fresh = getattr(api, "futopt_account", None) or account
        if fresh is not None and api.subscribe_trade(fresh) is False:
            raise RuntimeError("subscribe_trade returned False after re-login")
        owner.attach()
    return _relogin


def _relogin_tick(relogin: Any, now: datetime, alerter: Any = None) -> None:
    # Tech: let the scheduler act if a window is live, and print any outcome.
    # Why:  the scheduler owns retries, backoff and the verify; the loop only
    #       drives it once a pass and says what happened, so an operator
    #       watching the owner's console sees a failed refresh before the open.
    outcome = relogin.check(now)
    if outcome is not None:
        print(f"relogin: {outcome.value}  (token age "
              f"{relogin.token_age(now)})")
    # Tech: alert on the outcome with the kinds and levels `live/cli.py` uses.
    # Why:  an owner whose token died cannot place or hear an order for any
    #       deployment, so a failed verify is as fatal here as there. `VERIFIED`
    #       is the routine case and stays on the console. A re-login after a
    #       failed or abandoned one is their recovery, so it goes out as WARN.
    if alerter is None or outcome is None:
        return
    if outcome is Outcome.RELOGGED:
        alerter.recovery("relogin", "owner token refreshed and the deal stream "
                                    "re-subscribed",
                         resolves=("relogin_failed", "relogin_abandoned"))
    elif outcome is Outcome.FAILED:
        alerter.warn("relogin_failed", "an owner re-login attempt failed — "
                                       "retrying inside the window")
    elif outcome is Outcome.ABANDONED:
        alerter.warn("relogin_abandoned", "the owner's re-login window closed "
                                          "with no fresh token")
    elif outcome is Outcome.VERIFY_FAILED:
        alerter.fatal("session_unverified",
                      "the owner failed its pre-open check and could not be "
                      "recovered — no deployment can place an order")


# Tech: unanswered reconciles in a row before the owner suspects its session,
#       and the re-logins it then spends on one outage before it gives up.
# Why:  MEASURED in `owner_session.jsonl`. On 2026-10-15 the owner's own
#       session died at 09:54:01 with the deployments' and stayed dead until its
#       14:52 anchor: 597 unanswered reconciles. On 2026-10-14 05:01:55 another
#       loss ran 11 until the owner was stopped. The 30 s reconcile already makes
#       the authenticated call that fails, so 3 unanswered is the evidence — 31 s
#       after the first on 10-15, because a call that blocks on its timeout fires
#       the next at once; never more than ~90 s. Ahead of the deployments' first
#       lost-session re-login (~3 min), so the owner goes first here as at the
#       anchors. The only other runs in the log (10-12 to 10-17) are 30 s
#       `ShioajiTimeoutError`s around the 08:00 roll, at most 2 in a row. The owner has no staleness
#       backstop to bound attempts, so the budget does: four, then a FATAL,
#       because a dead owner session means no deployment can place or hear an
#       order and a person has to look.
OWNER_LOST_AFTER = 3
OWNER_LOST_MAX_RELOGINS = 4


class OwnerSessionWatch:
    """Re-login the owner when its reconcile keeps going unanswered."""

    def __init__(self, relogin: Any, probe: Any, scheduler: Any = None,
                 after: int = OWNER_LOST_AFTER,
                 max_relogins: int = OWNER_LOST_MAX_RELOGINS,
                 recorder: Any = None, alerter: Any = None):
        self.relogin, self.probe, self.scheduler = relogin, probe, scheduler
        self.after, self.max_relogins = after, max_relogins
        self.recorder, self.alerter = recorder, alerter
        self.unanswered = 0
        self.relogins_this_outage = 0
        self._gave_up = False
        self.n_relogins = 0
        self.n_relogin_failures = 0

    def note(self, agreed: Optional[bool], now: datetime) -> None:
        # Tech: count unanswered reconciles (None); after `after` in a row, probe
        #       the session and re-login only if the probe raises too.
        # Why:  `reconcile` returns None for any venue failure, and the cure for
        #       a dead session — logout, login, re-subscribe — briefly cuts the
        #       deal stream. 2026-10-06 lost a fill to exactly that cut. So a
        #       second authenticated call, `usage()` (raises on a dead token,
        #       relogin_probe Q7), must fail as well: a session that answers it
        #       is alive, and a `list_positions` hiccup is not worth a fill.
        if agreed is not None:
            if self.unanswered or self.relogins_this_outage:
                self._record("owner_session_answered",
                             relogins=self.relogins_this_outage)
            self.unanswered = 0
            self.relogins_this_outage = 0
            self._gave_up = False
            return
        self.unanswered += 1
        if self.unanswered < self.after:
            return
        # Tech: stand aside inside the scheduler's own windows.
        # Why:  it is already re-logging in and verifying there; a second login
        #       on top would stack connections and race its re-subscribe.
        if self.scheduler is not None and self.scheduler.in_window(now):
            return
        if self.relogins_this_outage >= self.max_relogins:
            if not self._gave_up:
                self._gave_up = True
                self._record("owner_session_lost", relogins=self.max_relogins)
                if self.alerter is not None:
                    self.alerter.fatal(
                        "owner_session_lost",
                        f"the owner's session stayed dead through "
                        f"{self.max_relogins} re-logins — no deployment can "
                        f"place or hear an order; restart the owner")
            return
        unanswered, self.unanswered = self.unanswered, 0
        try:
            self.probe()
        except Exception as exc:                        # noqa: BLE001 — see Why
            self._record("owner_session_probe_failed", unanswered=unanswered,
                         error=repr(exc))
        else:
            self._record("owner_session_alive", unanswered=unanswered)
            return
        self.relogins_this_outage += 1
        try:
            self.relogin()
        except Exception as exc:                        # noqa: BLE001
            # Tech: recorded and alerted, never raised.
            # Why:  this runs in the serve loop; an escape would end the owner,
            #       which is a worse outage than the one being repaired.
            self.n_relogin_failures += 1
            self._record("owner_session_relogin_failed", error=repr(exc),
                         attempt=self.relogins_this_outage)
            if self.alerter is not None:
                self.alerter.warn("session_relogin_failed",
                                  f"owner re-login after a lost session "
                                  f"failed: {exc!r}")
            return
        self.n_relogins += 1
        if self.scheduler is not None:
            self.scheduler.note_login(now)
        self._record("owner_session_relogin", attempt=self.relogins_this_outage)
        if self.alerter is not None:
            self.alerter.recovery("session_relogin",
                                  "the owner's venue calls kept failing; "
                                  "re-logged in and re-subscribed the deal "
                                  "stream",
                                  resolves=("session_relogin_failed",))

    def _record(self, event: str, **fields: Any) -> None:
        if self.recorder is not None:
            self.recorder.record(event, **fields)


DEFAULT_SLUGS = ("day", "night", "day_night")

# Tech: the owner's own audit trail, appended beside the ledgers in `--out`.
# Why:  until 2026-10-07 the owner had none — its anomalies went to a console
#       and nowhere else, so the 2026-10-06 stop had to be reconstructed from
#       gaps in `events_*.jsonl` and token releases in `shioaji.log`. One file
#       for every run, appended (`Recorder` writes a `meta` line per start),
#       because the owner has no per-run folder to put it in.
OWNER_LOG_NAME = "owner_session.jsonl"

# Tech: the traffic headroom below which the owner starts warning, in bytes.
# Why:  STRATEGY_SPLIT_PLAN §18.13. `RestartGuard.check_usage` already refuses to
#       START a deployment below 50 MB, because over quota Shioaji returns EMPTY
#       history rather than erroring — but that is a start-up gate and cannot see
#       a quota consumed DURING a session, which is exactly where four
#       subscriptions to one tick stream would bite. The owner is the process
#       that outlives all three deployments, so it is the one that can watch.
#
#       Warns rather than stops: the owner stopping halts every deployment at
#       once, and running low on data allowance is not a reason to abandon open
#       positions. The refusal stays where it already is, at the next start-up.
DEFAULT_MIN_REMAINING_BYTES = 50 * 1024 * 1024


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True,
                    help="The directory the deployments' fills_<slug>.csv and "
                         "the intent/event streams live in — the same "
                         "`out.parent` `live/cli.py --broker client` uses.")
    ap.add_argument("--slugs", nargs="+", default=list(DEFAULT_SLUGS),
                    help="Deployment slugs to serve (default: day night "
                         "day_night).")
    ap.add_argument("--contract", default="TXFR1",
                    help="The owner's start-up month: an intent naming no month "
                         "is placed on it, and a ledger row carrying no month "
                         "is seeded on it. Rows a deployment has stamped are "
                         "seeded on their own month.")
    ap.add_argument("--credential", default="night",
                    help="Which credential from config/secrets.toml to log in "
                         "with. The owner holds the ONE session that places "
                         "orders, so this is the account every deployment trades.")
    ap.add_argument("--i-understand-this-places-orders", action="store_true",
                    dest="ack",
                    help="Required. This process is the only one that sends "
                         "orders to the venue.")
    ap.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    ap.add_argument("--reconcile-seconds", type=float,
                    default=DEFAULT_RECONCILE_SECONDS)
    ap.add_argument("--fee-rate", type=float, default=0.0002)
    ap.add_argument("--tick-size", type=float, default=1.0)
    ap.add_argument("--flatten-aggression-ticks", type=int, default=5)
    ap.add_argument("--min-remaining-mb", type=float,
                    default=DEFAULT_MIN_REMAINING_BYTES / (1024 * 1024),
                    help="Warn once the account's remaining daily traffic falls "
                         "below this. The default matches the floor "
                         "`RestartGuard` refuses to start below; raise it once a "
                         "real per-session figure is known (§18.13).")
    ap.add_argument("--no-telegram", action="store_true",
                    help="Do not send alerts to Telegram even if [telegram] is "
                         "configured in config/secrets.toml")
    ap.add_argument("--once", action="store_true",
                    help="Run a single poll and reconcile, then exit. For "
                         "checking wiring without holding the session open.")
    return ap


def _futures_account(api: Any, accounts: Any) -> Any:
    """The futures account this owner will trade, or None."""
    # Tech: prefer `api.futopt_account`, falling back to the first account whose
    #       type is the futures one in the list `login()` returned.
    # Why:  two sources because §13 measured what happens with one: the first
    #       run of `multi_deploy_probe` reported "0 futures accounts" for a
    #       credential that trades TXF nightly, because it compared
    #       `str(AccountType.Future)` — which is 'F'. `futopt_account` is the
    #       SDK's own answer and needs no classification; the list is the second
    #       opinion, matched on the enum member rather than its `str()`.
    account = getattr(api, "futopt_account", None)
    if account is not None:
        return account
    # Tech: `sj.AccountType.Future`, not `sj.constant.AccountType` — the latter
    #       does not exist in 1.7.2 and raises on attribute access.
    # Why:  caught by this function's own test. The probe already resolves it the
    #       working way (`multi_deploy_probe._futopt`), which is also why the
    #       `.name == "Future"` fallback is here: the two spellings between them
    #       survive the enum moving again, and §13 is the record of what one
    #       wrong spelling costs — "0 futures accounts" for a credential trading
    #       TXF nightly.
    import shioaji as sj
    wanted = getattr(getattr(sj, "AccountType", None), "Future", None)
    for candidate in (accounts or []):
        kind = getattr(candidate, "account_type", None)
        if ((wanted is not None and kind == wanted)
                or getattr(kind, "name", "") == "Future"):
            return candidate
    return None


def seed_positions(out_parent: Any, slugs: List[str]) -> Dict[str, Dict[str, Any]]:
    """What each deployment's own ledger says it holds, by month, at what entry.

    `{slug: {code: HeldPosition}}`; a flat slug maps to `{}`, and rows with no
    month group under `''`.
    """
    # Tech: replay every row of each served slug's `fills_<slug>.csv`, across
    #       all soak epochs, month by month, and reduce each month with
    #       `ledger.open_position`. A slug with no ledger, or one that closes
    #       flat, seeds nothing.
    # Why:  a deployment that has never traded has no ledger, and treating that
    #       as "unknown" would block a start-up that is completely normal.
    #       Size AND entry, because the owner publishes both and
    #       `adopt_position` seeds the Trader and the Portfolio from them. Until
    #       2026-10-07 this returned lot counts alone, so the owner published a
    #       held lot at avg_price 0.0: fullday's `--resume` adopted its +1 "@
    #       0.00", and closing it would have booked ~+50 000 points. Same reducer
    #       `--resume` replays the ledger with, so the two cannot disagree.
    #       By month because the owner's `--contract` names one month, and
    #       between two deployments' rolls the account holds two (2026-10-20,
    #       simtrade_operations §11).
    seeds: Dict[str, Dict[str, Any]] = {}
    for slug in slugs:
        by_month = FillLedger(
            Path(out_parent) / f"fills_{slug}.csv").replay_by_month()
        seeds[slug] = {code: held for code, held in
                       ((c, open_position(f)) for c, f in by_month.items())
                       if held is not None}
    return seeds


def _describe_seed(seeds: Dict[str, Dict[str, Any]]) -> Dict[str, str]:
    """`{slug: "+1 @ 49884.00 TXFK6"}` for the console; flat slugs read "0"."""
    return {slug: ", ".join(f"{held.signed_qty:+d} @ {held.avg_price:.2f}"
                            + (f" {code}" if code else "")
                            for code, held in months.items()) or "0"
            for slug, months in seeds.items()}


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if not args.ack:
        print("ERROR: the owner is the process that places orders; pass "
              "--i-understand-this-places-orders", file=sys.stderr)
        return 5

    out_parent = Path(args.out)
    if not out_parent.is_dir():
        print(f"ERROR: {out_parent} is not a directory. It must be the same "
              f"directory the deployments write fills_<slug>.csv into.",
              file=sys.stderr)
        return 2

    # Tech: the owner's alerter, built once `--out` is known, with Telegram on
    #       the shared level file and no fatal journal; the recorder is attached
    #       once it exists.
    # Why:  until 2026-10-12 the owner had no alerter, so a freeze or a refused
    #       start reached only its console, and the phone heard about it ~2 min
    #       later as three `owner silent` halts. No `fatal.jsonl`: the status
    #       page attributes that file's entries to deployment soaks by timing,
    #       and an owner entry would be pinned on whichever soak it fell inside.
    #       The owner's own trail, `owner_session.jsonl`, records every alert.
    #       The ack and `--out` refusals above stay console-only: they are a
    #       typo at a terminal someone is looking at.
    alerter = Alerter()
    notifier = None if args.no_telegram else telegram.attach(
        alerter, deployment="owner",
        level_file=out_parent / "telegram_level.json")
    try:
        return _main(args, out_parent, alerter)
    finally:
        if notifier is not None:
            notifier.close()
            for k, v in notifier.summary().items():
                print(f"  {k:22s} {v}")


def _main(args: Any, out_parent: Path, alerter: Alerter) -> int:
    # Tech: everything after the argument checks: login, seed, reconcile, serve.
    # Why:  split from `main` so Telegram's drain wraps every return and raise
    #       below in one `finally`, without re-indenting the start-up sequence.
    from forecast_eval.live import credentials
    cred = credentials.load(args.credential)
    if cred is None:
        print(f"ERROR: no credential for {args.credential!r} — add a "
              f"[{args.credential}] section to {credentials.SECRETS_FILE}",
              file=sys.stderr)
        alerter.fatal("no_credentials",
                      f"owner: no credential for {args.credential!r}")
        return 2

    import shioaji as sj
    # Tech: simulation is hardcoded, not a flag.
    # Why:  the same reason `live/cli.py` hardcodes it — §7.2 measured that
    #       `api.simulation` reads back as None, so the constructor argument is
    #       the only guarantee there is, and an entry point with no reachable
    #       path to a production account cannot be argued into one.
    api = sj.Shioaji(simulation=True)
    recorder = Recorder(out_parent / OWNER_LOG_NAME, simulation=True,
                        meta={"process": "owner", "slugs": list(args.slugs),
                              "git_commit": git_commit(),
                              "git_dirty": git_dirty()})
    alerter.recorder = recorder
    try:
        accounts = api.login(api_key=cred.api_key, secret_key=cred.secret_key)
        login_at = datetime.now()
        account = _futures_account(api, accounts)

        # Tech: subscribe the futures account's order/deal stream explicitly and
        #       check what it returns.
        # Why:  §18.14. `login()` defaults `subscribe_trade=True`, so the owner
        #       worked by relying on a default — and this is the ONE process that
        #       must hear deals: with (b) it is the only `_match` there is, so a
        #       silent unsubscribe means every deployment's fills stop arriving
        #       while their orders keep filling. §14b Q3 measured the call returns
        #       True, so a False here is a real answer and not an unknown. Called
        #       after login rather than instead of it: re-subscribing an already
        #       subscribed account is harmless, and a default that changes in a
        #       future SDK release would otherwise fail silently.
        if account is not None:
            try:
                ok = api.subscribe_trade(account)
                print(f"subscribe_trade: {ok}")
                if ok is False:
                    print("", file=sys.stderr)
                    print("  *** REFUSING TO START: the account's deal stream "
                          "was not subscribed. ***", file=sys.stderr)
                    print("  The owner is the only `_match` there is — without "
                          "deals, orders would fill", file=sys.stderr)
                    print("  and no deployment would ever hear about it.",
                          file=sys.stderr)
                    alerter.fatal("startup_refused",
                                  "the owner's deal stream was not subscribed "
                                  "— do not start the deployments",
                                  gate="subscribe_trade")
                    return 7
            except Exception as exc:                    # noqa: BLE001
                print(f"subscribe_trade raised: {exc!r} — relying on login()'s "
                      f"default")

        contract, note = resolve_txf(api, args.contract)
        print(f"contract {note}")

        # Tech: `resolve` turns a dated code from a deployment's intent into the
        #       contract the owner places on.
        # Why:  2026-10-20 (simtrade_operations §11) — the owner never rolled;
        #       it now follows each deployment's month instead of holding one.
        #       By code because `TXF202611` does not resolve and `TXFK6` does.
        owner = OwnerProcess(
            api, contract, directory=out_parent, slugs=args.slugs,
            fee_rate=args.fee_rate, tick_size=args.tick_size,
            flatten_aggression_ticks=args.flatten_aggression_ticks,
            recorder=recorder,
            resolve=lambda code: resolve_txf(api, code)[0],
        )
        skipped = {s: n for s, n in owner.skipped_intents.items() if n}
        if skipped:
            print(f"intents already on disk, not replayed: {skipped}")

        # Tech: seed from the ledgers, then check that sum against the venue
        #       BEFORE serving anything.
        # Why:  the owner's whole authority rests on `account_net` being right —
        #       it is what `plan_legs` sizes every Cover against (§18.9). Starting
        #       to place orders on a seed the venue disagrees with would turn one
        #       stale ledger into a stream of wrongly-sized legs. Refusing here
        #       costs a restart; not refusing costs positions.
        seeds = seed_positions(out_parent, args.slugs)
        owner.seed_from(seeds)
        print(f"seeded from ledgers: {_describe_seed(seeds)}  "
              f"account={owner.book.account_net:+d}")
        recorder.record("owner_seeded", seeds=_describe_seed(seeds),
                        account=owner.book.account_net)

        agreed = owner.reconcile()
        if agreed is False:
            print("\n  *** REFUSING TO START: the ledgers and the venue "
                  "disagree. ***", file=sys.stderr)
            for line in owner.anomalies:
                print(f"      {line}", file=sys.stderr)
            print("  Reconcile them by hand — `python -m "
                  "forecast_eval.live.flatten --out-parent <dir>` reports who "
                  "holds what — then restart.", file=sys.stderr)
            alerter.fatal("startup_refused",
                          "the ledgers and the venue disagree: "
                          + "; ".join(owner.anomalies), gate="reconcile")
            return 6
        if agreed is None:
            print("  WARNING: the venue would not answer; starting on the "
                  "ledger seed alone.")
            alerter.warn("venue_unanswered", "the venue would not answer the "
                         "start-up reconcile; the owner started on the ledger "
                         "seed alone")

        used, limit, remaining = usage_bytes(_usage(api))
        if used is not None:
            print(f"traffic: {used:,} bytes used today"
                  + (f" of {limit:,}" if limit else ""))

        owner.start()

        # Tech: the token scheduler, verified by `api.usage()`.
        # Why:  `usage()` raises on a dead token (relogin_probe Q7), and it is an
        #       authenticated REST call — the kind that died at 24 h. The owner
        #       has no tick feed, so unlike `live/cli.py` there is no tick half.
        owner_relogin = make_relogin(api, cred, account, owner)
        relogin = ReloginScheduler(
            relogin=owner_relogin,
            verify=api.usage, anchors=owner_relogin_anchors())
        relogin.note_login(login_at)
        print("relogin anchors: " + ", ".join(
            f"{a:%H:%M}/{v:%H:%M}" for a, v, _ in owner_relogin_anchors()))

        print(f"owner serving {args.slugs} — poll {args.poll_seconds}s, "
              f"reconcile {args.reconcile_seconds}s")
        print("START THE DEPLOYMENTS NOW (live/cli.py --broker client).")

        # Tech: the lost-session repair, on the anchors' own re-login action.
        # Why:  `OwnerSessionWatch` — between the anchors nothing else would
        #       ever log a dead owner back in.
        session_watch = OwnerSessionWatch(
            relogin=owner_relogin, probe=api.usage, scheduler=relogin,
            recorder=recorder, alerter=alerter)
        return _serve(owner, args, api, relogin, alerter, session_watch)
    except Exception as exc:
        # Tech: alert, then re-raise unchanged.
        # Why:  an owner that dies leaves every deployment unable to trade; the
        #       traceback and the exit code stay exactly what they were.
        alerter.fatal("crashed", f"owner: {exc!r}",
                      error_type=type(exc).__name__)
        raise
    finally:
        recorder.record("owner_stop")
        recorder.close()
        try:
            api.logout()
        except Exception:                               # noqa: BLE001
            pass


def _usage(api: Any) -> Any:
    # Tech: `api.usage()`, or None when it fails.
    # Why:  a traffic reading is a statistic, and a statistic must never take the
    #       owner down — `cli.py` carries the identical helper for the identical
    #       reason. The one place that must NOT swallow the exception is a
    #       liveness check, and this is not one.
    try:
        return api.usage()
    except Exception:                                   # noqa: BLE001
        return None


def _serve(owner: OwnerProcess, args: Any, api: Any = None,
           relogin: Any = None, alerter: Any = None,
           session_watch: Any = None) -> int:
    """The loop: drain intents, publish, reconcile on a slower cadence."""
    # Tech: install a SIGINT/SIGTERM handler that asks the loop to stop, then
    #       poll until it does.
    # Why:  a killed owner leaves three deployments with no one to place their
    #       orders, and they will halt on their own timeout — correct, but it
    #       should be a clean stop rather than a hang. The flag is checked
    #       between passes so a stop never lands mid-placement, which is the one
    #       moment an order exists at the venue but not yet in the book.
    stopping = {"now": False}

    def _stop(signum, frame):                           # noqa: ARG001
        stopping["now"] = True
        print("\nstopping after this pass — the deployments will halt on their "
              "owner timeout.")

    for sig in (signal.SIGINT, getattr(signal, "SIGTERM", signal.SIGINT)):
        try:
            signal.signal(sig, _stop)
        except (ValueError, OSError):                   # pragma: no cover
            pass                                        # not the main thread

    # Tech: None, not 0.0, as the "never reconciled" sentinel.
    # Why:  `time.monotonic()` counts from BOOT and starts near zero on a
    #       freshly booted machine, so a 0.0 sentinel makes the first pass look
    #       like it is still inside the cadence (`now - 0.0 < reconcile_seconds`)
    #       and the START-UP reconcile is skipped — the very pass that refuses
    #       to trade on a desynced ledger (caught 2026-10-14 by the cadence test
    #       on a 6-minute-old boot).
    last_reconcile: Optional[float] = None
    agreed_since_freeze = False
    # Tech: what the two repeating warnings last alerted on.
    # Why:  both reprint every 30 s on the console; the alert fires on a change
    #       only, so a night of thin traffic is one message, not 1 200.
    traffic_alerted = False
    stale_alerted: List[str] = []
    while not stopping["now"]:
        owner.poll()
        if relogin is not None:
            _relogin_tick(relogin, datetime.now(), alerter)

        now = time.monotonic()
        if last_reconcile is None or now - last_reconcile >= args.reconcile_seconds:
            last_reconcile = now
            agreed = owner.reconcile()
            if session_watch is not None:
                session_watch.note(agreed, datetime.now())
            if agreed is False and owner.frozen is None:
                # Tech: a disagreement FREEZES the owner — no new orders — and
                #       the loop keeps serving.
                # Why:  the account net is what every subsequent leg is sized
                #       against, so placing on a number the venue disputes is how
                #       one desync becomes many; same rule as
                #       `SafetyRails.on_reconcile`, which never auto-corrects.
                #       Until 2026-10-07 this RETURNED, and the logout cut the deal
                #       stream while a fill was still on its way (2026-10-06,
                #       `DEFAULT_RECONCILE_GRACE`). Frozen, the owner still books
                #       that fill and tells its deployment.
                owner.freeze(owner.anomalies[-1])
                print("\n  *** RECONCILIATION FAILED — OWNER FROZEN ***",
                      file=sys.stderr)
                print(f"      {owner.anomalies[-1]}", file=sys.stderr)
                print("      No new orders are placed; a deployment that submits "
                      "is rejected and halts.\n      Deals are still booked and "
                      "positions still published. Reconcile by hand, then\n"
                      "      Ctrl-C the deployments and this owner, and restart.",
                      file=sys.stderr)
                if alerter is not None:
                    alerter.fatal("owner_frozen",
                                  f"reconciliation failed: {owner.anomalies[-1]}"
                                  f" — no new orders; reconcile by hand")
            elif agreed is True and owner.frozen is not None:
                if not agreed_since_freeze:
                    agreed_since_freeze = True
                    print("  the venue and the book agree again — still frozen; "
                          "restart the owner to trade.", file=sys.stderr)
                    if alerter is not None:
                        alerter.recovery("owner_agrees_again",
                                         "the venue and the book match again; "
                                         "the owner stays frozen until "
                                         "restarted",
                                         resolves=("owner_frozen",))
            elif agreed is True and owner.reconcile_note:
                print(f"  reconcile pending: {owner.reconcile_note}")
            # Tech: read the traffic meter on the same cadence as the venue
            #       reconcile, and warn below the floor.
            # Why:  §18.13 — `usage()` itself costs bytes, so it rides the
            #       slower of the two cadences rather than the per-second poll.
            #       It reports the headroom in the units the operator sets it in,
            #       because "51,380,224 bytes" is not a number anyone acts on.
            if api is not None:
                _, limit, remaining = usage_bytes(_usage(api))
                floor = args.min_remaining_mb * 1024 * 1024
                if remaining is not None and remaining < floor:
                    print(f"  WARNING: traffic headroom {remaining / 1048576:.0f} "
                          f"MB left of {(limit or 0) / 1048576:.0f} MB, below the "
                          f"{args.min_remaining_mb:.0f} MB floor. Over quota "
                          f"Shioaji returns EMPTY history rather than erroring, "
                          f"so the next start-up will be refused.")
                    if alerter is not None and not traffic_alerted:
                        alerter.warn("traffic_low",
                                     f"{remaining / 1048576:.0f} MB of daily "
                                     f"traffic left — the next start-up is "
                                     f"refused below "
                                     f"{args.min_remaining_mb:.0f} MB")
                    traffic_alerted = True
                elif remaining is not None:
                    traffic_alerted = False

            stale = owner.stale_deployments()
            if stale:
                # Tech: report, do not act.
                # Why:  §7.4 — what to do about an absent deployment holding a
                #       lot is a risk decision, not a mechanical one. Closing its
                #       position would be trading on someone else's behalf; the
                #       owner says so and leaves it.
                print(f"  WARNING: no intents from {stale} for "
                      f"{owner.deployment_timeout:.0f}s. Their positions are "
                      f"still open and still counted.")
            if alerter is not None and stale and list(stale) != stale_alerted:
                alerter.warn("deployment_silent",
                             f"no intents from {list(stale)} for "
                             f"{owner.deployment_timeout:.0f}s; their positions "
                             f"are still open")
            stale_alerted = list(stale)

        if args.once:
            break
        time.sleep(args.poll_seconds)
    # Tech: a frozen owner exits 6 when it is finally stopped.
    # Why:  6 already means "the book and the venue disagreed"; the code is how
    #       a wrapper or a later reader tells a clean stop from a frozen one.
    return 6 if owner.frozen is not None else 0


if __name__ == "__main__":
    raise SystemExit(main())
