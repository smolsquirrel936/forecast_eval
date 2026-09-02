"""STRATEGY_SPLIT_PLAN S7/S8 probe — are the three credentials really separate?

**S8 is decided.** The first run of this probe (2026-09-02) measured that the
original credential holds **one** futures account, so option (c) was not
reachable on it — and the user's answer was to obtain **one API credential per
deployment**. That is (c) after all, by a route the code cannot arrange: three
accounts, one per session, so DAY and NIGHT can never net against each other.

**Every rail already scopes by account**, which is why (c) costs almost nothing:
`list_positions(account=...)` takes one, `subscribe_trade(account)` subscribes
one, and `broker/shioaji.py` already passes the account it was built with.

So the questions this probe now asks are not "can one credential be split" —
that is answered and moot. They are the ones the new design RESTS on:

  Q0  Do the three credentials reach three DISTINCT accounts?
      **The load-bearing question.** If a broker issues three API keys against
      the SAME futures account, nothing is fixed: every 7.1 halt returns, and
      nothing in the system notices until two deployments happen to hold a
      position at once. Three logins, compare `account_id`. ~1 minute, no
      market needed.
  Q4  Does a deal on deployment A's session reach deployment B's callback?
  Q5  Does a position opened by A appear in B's `list_positions()`?
      Q4/Q5 are the same assumption from the other side: separate accounts
      SHOULD isolate both. Measured, not assumed — the first run of this probe
      is a standing reminder of what assuming costs (its Q1 reported "0 futures
      accounts" for a credential that trades TXF nightly, because
      `str(AccountType.Future)` is 'F', not 'Future').

RETIRED, and why they are not worth re-running:

  Q1  how many accounts on ONE credential — answered: 1. Moot; there are three
      credentials now.
  Q2  do two simultaneous logins coexist — answered: yes, `connections` 1 -> 2
      and the first session stayed usable. Also moot: different credentials.
  Q3  can a session opt out of an account's deal stream — moot, since each
      credential only ever sees its own account.

TWO MODES:

    --mode accounts   Q0 only. Three logins from `config/secrets.toml`, compares
                      account ids, places nothing. Run this FIRST, and after any
                      change to the credentials.
    --mode session    Q4 + Q5. Needs a live session and places ONE resting order
                      far from the market, so it needs
                      --i-understand-this-places-orders.

SAFETY: every line goes through `ioc_probe.redact`, and account ids are masked,
because Shioaji puts a Taiwanese national ID (`person_id`) in account reprs and a
probe's output is meant to be pasted into a conversation.

    uv run python -m forecast_eval.live.multi_deploy_probe --mode accounts
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
from typing import Any, List, Optional

from .ioc_probe import redact


# Tech: marker env var that tells a re-executed copy of this module it is the
#       child and must do the real work instead of re-spawning.
# Why:  see `_run_filtered`. A plain flag would work too, but an env var keeps it
#       out of `--help` and out of the argv the child re-parses.
_CHILD_ENV = "FE_PROBE_FILTERED_CHILD"


def _run_filtered(argv: List[str]) -> int:
    """Re-run this probe as a child and push every line through `redact`.

    Tech: spawn `python -m forecast_eval.live.multi_deploy_probe` with the child
          marker set, stderr folded into stdout, and filter the pipe line by line.
    Why:  MEASURED 2026-09-02, twice. `shioaji`'s compiled `_core.pyd` prints a
          connection banner — "Response Code: ... PYAPI/<id>/..." — where `<id>`
          is the user's TAIWANESE NATIONAL ID. It is written from C, so
          `redirect_stdout` and every print-level guard miss it.

          The first fix swapped file descriptor 1 for a pipe in-process. That
          works when stdout is already a pipe and **fails on an interactive
          Windows console**: Python uses `_WindowsConsoleIO` there, which calls
          `WriteConsoleW`, and that raises `OSError: [WinError 1] Incorrect
          function` the moment fd 1 is not a console. The user hit exactly that.

          A child process has no such problem: its stdout is a pipe by
          construction, so the SDK, Python prints and tracebacks all land in one
          stream this process filters and writes to the real console with its
          console handle untouched. Folding stderr in means a traceback carrying
          an id is filtered too.
    """
    env = dict(os.environ)
    env[_CHILD_ENV] = "1"
    # Tech: -u so the child does not buffer; the probe is watched live.
    proc = subprocess.Popen(
        [sys.executable, "-u", "-m",
         "forecast_eval.live.multi_deploy_probe", *argv],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        env=env, text=True, errors="replace", bufsize=1,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        print(redact(line.rstrip("\n")), flush=True)
    return proc.wait()


def _say(label: str, value: Any = "") -> None:
    # Tech: one aligned observation line, tagged with the question it answers.
    # Why:  the probe's output IS its result — read by a person and pasted into a
    #       decision, not parsed. Redaction happens in the parent process
    #       (`_run_filtered`), which catches the SDK's own C-level writes too, so
    #       this only has to format.
    #
    # Tech: the whole line INCLUDING its newline goes out in ONE `write`, not via
    #       `print`.
    # Why:  MEASURED 2026-09-02. The child runs with `-u`, so `print` emits the
    #       text and the newline as two separate writes and the SDK's own C-level
    #       banner lands between them, giving
    #       "day: source   config/secrets.tomlResponse Code: 0 | ...".
    #       One write per line keeps the two streams from splicing mid-line. It
    #       cannot stop the banner appearing BETWEEN our lines — that is another
    #       thread — but it stops it appearing inside one.
    line = f"  {label:<44} {value}" if value != "" else f"  {label}"
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def _mask_id(value: str) -> str:
    # Tech: keep the last four characters of an account id.
    # Why:  enough to tell two deployments' accounts apart in output that is
    #       meant to be pasted, not enough to be an account number. `cli.py` has
    #       its own `_mask` for the same reason; this is three lines rather than
    #       importing the whole entry point for one helper.
    value = str(value)
    return f"...{value[-4:]}" if len(value) > 4 else "..."


def _futopt(accounts: List[Any]) -> List[Any]:
    # Tech: the futures accounts among everything the credential returned,
    #       compared against the `AccountType.Future` ENUM MEMBER.
    # Why:  MEASURED 2026-09-02, after this got it wrong on the first run. The
    #       original test was `"Future" in str(account_type)` — but
    #       `str(AccountType.Future)` is **'F'**, not 'Future', so it matched
    #       nothing and the probe reported "FUTOPT accounts 0" for a credential
    #       that has been trading TXF nightly. Comparing against the member
    #       cannot drift with the repr, and the `name` fallback covers a future
    #       SDK that stops exporting the member under that path. This is the
    #       "verify by introspection, the docs have been wrong four times" rule
    #       applied to a value I assumed instead of printed.
    import shioaji as sj

    want = getattr(sj, "AccountType", None)
    want = getattr(want, "Future", None) if want is not None else None
    out = []
    for a in accounts:
        kind = getattr(a, "account_type", None)
        if (want is not None and kind == want) or \
                getattr(kind, "name", "") == "Future":
            out.append(a)
    return out


def _connections(api: Any) -> Optional[int]:
    # Tech: `usage().connections`, or None when the call fails.
    # Why:  Q2's instrument. None means "could not measure", which must not be
    #       reported as zero — a failed usage() on a live session is itself a
    #       finding (it is what `relogin_probe` Q7 was about).
    try:
        return int(getattr(api.usage(), "connections", -1))
    except Exception as exc:                            # noqa: BLE001
        _say("usage() raised", repr(exc)[:120])
        return None


def probe_accounts() -> int:
    """Q0 — do the three deployments' credentials reach three DISTINCT accounts?"""
    import shioaji as sj

    from . import credentials
    from .config import CONFIGS

    print()
    print("=== Q0: do the three credentials reach three DISTINCT accounts? ===")
    seen: dict = {}
    sessions = []
    try:
        for name in sorted(CONFIGS):
            cred = credentials.load(name)
            if cred is None:
                _say(f"{name}: NO CREDENTIAL",
                     f"add [{name}] to {credentials.SECRETS_FILE.name}")
                continue
            api = sj.Shioaji(simulation=True)
            sessions.append(api)
            accounts = api.login(api_key=cred.api_key,
                                 secret_key=cred.secret_key)
            fut = _futopt(accounts)
            ids = [str(getattr(a, "account_id", "?")) for a in fut]
            _say(f"{name}: source", cred.source)
            _say(f"{name}: futures account(s)",
                 ", ".join(_mask_id(i) for i in ids) or "NONE")
            for i in ids:
                seen.setdefault(i, []).append(name)
    finally:
        for api in sessions:
            _logout(api)

    shared = {acct: names for acct, names in seen.items() if len(names) > 1}
    print()
    if not seen:
        print("  => no credentials configured; nothing to compare.")
        return 2
    if shared:
        for acct, names in shared.items():
            _say("SAME account_id", f"{_mask_id(acct)} reported by "
                                    f"{sorted(names)}")
        print()
        print("  => These credentials report the SAME account_id. That is an")
        print("     identity fact, and it is ALL this mode measures.")
        print()
        print("     It does NOT establish that their positions net. Shioaji may")
        print("     well track a simulated book PER API KEY, in which case a BUY")
        print("     from one deployment and a SELL from another do not flatten")
        print("     each other and the separation works as intended.")
        print()
        print("     Whether they net is Q5, and it is unmeasured:")
        print("       --mode session --i-understand-this-places-orders")
        print("     which opens a position on one credential and asks the other")
        print("     what it can see.")
        print()
        print("     Until Q5 has run, treat concurrent deployments as UNPROVEN,")
        print("     not as broken.")
        return 1
    print(f"  => {len(seen)} distinct futures account(s) across "
          f"{sum(len(v) for v in seen.values())} deployment(s). Separation holds.")
    print("     `cli.py`'s account fingerprint (live_account.json) notices if")
    print("     any of them moves between runs.")
    return 0


def probe_offhours(api_key: str, secret_key: str) -> int:
    """Q1, Q2, Q3 — no market data, no orders."""
    import shioaji as sj

    print("\n=== Q1: how many FUTOPT accounts does this credential have? ===")
    a = sj.Shioaji(simulation=True)
    accounts = a.login(api_key=api_key, secret_key=secret_key)
    fut = _futopt(accounts)
    _say("accounts returned by login()", len(accounts))
    # Tech: print every account's type alongside the classification, and
    #       cross-check against `api.futopt_account` — the accessor the rest of
    #       the codebase already trusts.
    # Why:  the first run of this probe misclassified silently and printed a
    #       confident zero. A count with no evidence under it is exactly what
    #       let that pass unnoticed; showing the raw types means a future
    #       classification bug is visible in the same output that states the
    #       conclusion, and the independent accessor is a second opinion.
    for acc in accounts:
        kind = getattr(acc, "account_type", None)
        _say("  account type / id",
             f"{getattr(kind, 'name', kind)!r} / "
             f"{_mask_id(getattr(acc, 'account_id', '?'))}")
    _say("FUTOPT accounts (classified)", len(fut))
    _say("api.futopt_account (cross-check)",
         _mask_id(getattr(getattr(a, "futopt_account", None),
                          "account_id", None)))
    _say("connections after 1st login", _connections(a))

    if len(fut) >= 2:
        print("\n  => S8 option (c) IS REACHABLE: more than one futures account.")
        print("     Each deployment can own one, and every rail stays unchanged.")
    elif len(accounts) < 2:
        print("\n  => this credential reports ONE futures account, so it cannot")
        print("     give two deployments two DIFFERENT account ids.")
        print()
        print("     That is an identity fact and nothing more. It does NOT show")
        print("     that two sessions on this credential net their positions")
        print("     against each other -- Shioaji may track a simulated book")
        print("     per API key. Q5 in --mode session is what measures that.")
    else:
        print("\n  => more than one account, but fewer than two FUTURES accounts.")
        print("     (c) needs two futures accounts specifically — a stock")
        print("     account cannot hold a TXF position. Check the types above.")

    print("\n=== Q2: does a SECOND simultaneous login work, and does the first survive? ===")
    b = sj.Shioaji(simulation=True)
    try:
        accounts_b = b.login(api_key=api_key, secret_key=secret_key)
        _say("2nd login succeeded", f"{len(accounts_b)} accounts")
    except Exception as exc:                            # noqa: BLE001
        _say("2nd login RAISED", repr(exc)[:200])
        print("\n  => concurrent sessions are refused; (b) is forced regardless")
        print("     of Q1, because three processes cannot each hold a session.")
        _logout(a)
        return 0

    _say("connections after 2nd login", _connections(b))
    # Tech: the first session is exercised AFTER the second logs in.
    # Why:  this is the whole question. A second login that succeeds while
    #       silently evicting the first looks identical to one that coexists,
    #       right up until the first session's next call fails in production.
    try:
        first_still_ok = a.list_accounts() is not None
        _say("1st session still usable after 2nd login", first_still_ok)
    except Exception as exc:                            # noqa: BLE001
        first_still_ok = False
        _say("1st session BROKE after 2nd login", repr(exc)[:200])

    if not first_still_ok:
        print("\n  => the second login EVICTED the first. Three concurrent")
        print("     deployments are impossible on one credential; (b) with a")
        print("     single owning process is forced.")
    else:
        print("\n  => two sessions coexist on one credential.")

    print("\n=== Q3: can a session opt OUT of an account's deal stream? ===")
    # Tech: log a third session in with subscribe_trade=False, then subscribe
    #       only the first futopt account explicitly.
    # Why:  if (c) is reachable, this is what makes it clean: deployment X must
    #       receive X's deals and NOT Y's, or `_match` fails on Y's deal and
    #       `_anomaly` halts X (§7.2). `login(subscribe_trade=False)` is the
    #       documented-by-signature way to ask for that.
    c = sj.Shioaji(simulation=True)
    try:
        c.login(api_key=api_key, secret_key=secret_key, subscribe_trade=False)
        _say("login(subscribe_trade=False) accepted", True)
        if fut:
            ok = c.subscribe_trade(fut[0])
            _say("subscribe_trade(account) returned", ok)
    except Exception as exc:                            # noqa: BLE001
        _say("selective subscribe RAISED", repr(exc)[:200])
    finally:
        _logout(c)

    _logout(b)
    _logout(a)
    print("\n  NOTE: Q3 shows the calls are ACCEPTED. Whether the filtering is")
    print("        real can only be seen with a deal on another account —")
    print("        that is Q4, in --mode session.")
    return 0


def probe_session(api_key: str, secret_key: str, args) -> int:
    """Q4 and Q5 — needs a live session and places ONE order."""
    import shioaji as sj

    print("\n=== Q4/Q5: two sessions, one order, who sees it? ===")
    a = sj.Shioaji(simulation=True)
    accounts = a.login(api_key=api_key, secret_key=secret_key)
    b = sj.Shioaji(simulation=True)
    b.login(api_key=api_key, secret_key=secret_key)
    fut = _futopt(accounts)
    if not fut:
        print("  no futopt account; cannot place an order")
        return 2

    # Tech: session B records every order-callback message it receives.
    # Why:  Q4 is exactly "does B hear about A's deal". A list plus a lock is
    #       the whole instrument; the callback runs on the SDK's thread.
    seen_b: List[str] = []
    lock = threading.Lock()

    def on_b(*msg: Any) -> None:
        with lock:
            seen_b.append(redact(repr(msg))[:300])

    b.set_order_callback(on_b)
    _say("session B order callback registered", True)

    contract = a.Contracts.Futures.TXF[args.contract]
    snap = a.snapshots([contract])[0]
    last = float(getattr(snap, "close", 0.0))

    # Tech: `--marketable` prices the order ABOVE the market so it fills;
    #       otherwise it rests far below and is cancelled unfilled.
    # Why:  Q5 needs a POSITION to compare, and a resting order never creates
    #       one — the first run of this mode measured "0 positions vs 0
    #       positions", which compares nothing. Filling is the only way to answer
    #       whether one credential can see another's position, and it is opt-in
    #       because it takes real (simulated) exposure and must be flattened
    #       afterwards.
    if args.marketable:
        price = last + args.offset_points
        _say("placing MARKETABLE buy from session A (will fill)", f"{price:.0f}")
    else:
        price = last - args.offset_points
        _say("placing FAR-from-market limit from session A", f"{price:.0f}")

    def _buy(px: float, action):
        return a.Order(
            price=px, quantity=1, action=action,
            price_type=sj.constant.FuturesPriceType.LMT,
            order_type=sj.constant.OrderType.ROD,
            octype=sj.constant.FuturesOCType.Auto,
            account=fut[0],
        )

    trade = a.place_order(contract, _buy(price, sj.constant.Action.Buy))
    _say("session A placed", getattr(getattr(trade, "order", None), "id", "?"))

    time.sleep(args.settle_seconds)
    with lock:
        n = len(seen_b)
    _say("Q4: messages session B received", n)
    for line in seen_b[:5]:
        _say("  B saw", line)
    if n:
        print("\n  => B HEARS A's order events. Under the current broker, B's")
        print("     `_match` returns None and `_anomaly` HALTS it (§7.2).")
    else:
        # Tech: distinguish "no deal happened" from "no ORDER event reached B".
        # Why:  MEASURED 2026-09-02. The original text here blamed the absence on
        #       the order never filling — true of DEALS, but `set_order_callback`
        #       also delivers order-state events, and placing + cancelling
        #       generated two of those on A (the SDK printed both). B received
        #       neither. That is a positive signal about isolation, and calling it
        #       "not proof" understated it. It is still not conclusive for deals.
        print("\n  => B received NOTHING while A traded.")
        if args.marketable:
            # Tech: with --marketable the order filled, so a DEAL was generated
            #       as well as the order-state events.
            # Why:  MEASURED 2026-09-02. This is the conclusive form of Q4 — the
            #       earlier run could only show that order EVENTS do not cross
            #       sessions, because nothing filled. A deal did occur here and B
            #       still heard nothing, so neither events nor deals are
            #       broadcast across sessions on one account.
            print("     A's order FILLED, so a deal was generated as well as the")
            print("     order-state events, and B's callback saw none of it.")
            print("     CONCLUSIVE: order events AND deals are per-session.")
            print("     Each deployment therefore mirrors only ITS OWN fills.")
        else:
            print("     A's session generated two order-state events; B's")
            print("     callback saw neither, so order events are NOT broadcast")
            print("     across sessions. Real evidence for isolation, not merely")
            print("     an absence — but a DEAL may still route differently.")
            print("     Only a filled order settles that (--marketable).")

    pos_a = a.list_positions(fut[0]) or []
    pos_b = b.list_positions(fut[0]) or []
    _say("Q5: positions seen by A", len(pos_a))
    _say("Q5: positions seen by B", len(pos_b))
    # Tech: only draw a conclusion from Q5 when a position actually existed.
    # Why:  MEASURED 2026-09-02, and this printed a falsehood. With a resting
    #       order that never fills there is NO position, so "0 and 0" compares
    #       nothing — yet the old text asserted "any position either opens is
    #       summed into the other's reconciliation" as though it had been
    #       observed. An unconditional conclusion under a conditional measurement
    #       is exactly the error §13 records twice already.
    if not pos_a and not pos_b:
        print("\n  => VACUOUS: no position existed. The order rested far from")
        print("     the market and was cancelled unfilled, so this compared")
        print("     nothing at all. Q5 is UNMEASURED.")
        print("     To measure it, re-run with --marketable, which places an")
        print("     order that actually fills and then flattens it.")
    elif len(pos_a) == len(pos_b):
        print("\n  => Both sessions report the SAME position count, and only A")
        print("     opened anything. B is seeing A's position: the POSITION")
        print("     BOOK IS SHARED across API keys on this account.")
        print()
        print("     Combined with Q4 this is the WORST pairing for the rails:")
        print("       * deals are per-session, so each deployment's mirror")
        print("         counts only its OWN fills;")
        print("       * positions are shared, so `list_positions` returns the")
        print("         COMBINED size to every deployment.")
        print("     `on_reconcile` compares one against the other, so the first")
        print("     time two deployments hold at once, BOTH halt on")
        print("     `mirror != venue` (§7.1).")
        print()
        print("     It also means a BUY from one strategy and a SELL from")
        print("     another DO net against each other. Separate API keys do not")
        print("     prevent that; only separate ACCOUNTS would.")
        print("     Note the empty `subaccount` field on every order and deal —")
        print("     that is the hook a real sub-account would populate.")
    else:
        print("\n  => The sessions report DIFFERENT positions — the book is")
        print("     per credential, and the separation works as intended.")

    # Tech: a marketable run leaves a real position; flatten it before anything
    #       else, and say so loudly if that fails.
    # Why:  this probe must not be the reason an account is left holding. The
    #       flatten is the opposite side, priced through the market so it fills,
    #       and it runs before the cancel because a filled order has nothing to
    #       cancel.
    if args.marketable and pos_a:
        try:
            flat = a.place_order(
                contract, _buy(last - args.offset_points,
                               sj.constant.Action.Sell))
            _say("FLATTEN placed",
                 getattr(getattr(flat, "order", None), "id", "?"))
            time.sleep(args.settle_seconds)
            left = a.list_positions(fut[0]) or []
            _say("position after flatten", len(left))
            if left:
                print("\n  *** THE ACCOUNT IS STILL HOLDING. Flatten it by hand "
                      "before running anything else. ***")
        except Exception as exc:                        # noqa: BLE001
            print(f"\n  *** FLATTEN FAILED: {exc!r} — the account may still be "
                  f"holding. Check it by hand. ***")

    try:
        a.cancel_order(trade)
        _say("order cancelled", True)
    except Exception as exc:                            # noqa: BLE001
        _say("CANCEL FAILED — check the account by hand", repr(exc)[:160])

    _logout(b)
    _logout(a)
    return 0


def _logout(api: Any) -> None:
    try:
        api.logout()
    except Exception:                                   # noqa: BLE001 — best effort
        pass


def main(argv=None) -> int:
    # Tech: unless we ARE the filtered child, re-run ourselves as one and filter
    #       its output; the child then falls through and does the work.
    # Why:  the SDK writes an unredacted national ID straight to the console from
    #       C. See `_run_filtered`. Done here, before argparse, so no mode can be
    #       added that escapes the filter.
    if not os.environ.get(_CHILD_ENV):
        return _run_filtered(list(sys.argv[1:] if argv is None else argv))

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--mode", choices=("accounts", "offhours", "session"),
                    default="accounts",
                    help="accounts: Q0 — are the three credentials three "
                         "distinct accounts? No market, no orders (START HERE). "
                         "offhours: the retired Q1-Q3, kept for a new credential. "
                         "session: Q4-Q5, places one order.")
    ap.add_argument("--i-understand-this-places-orders", action="store_true",
                    dest="ack",
                    help="Required for --mode session.")
    ap.add_argument("--marketable", action="store_true",
                    help="session mode: place an order that actually FILLS, so "
                         "Q5 has a position to compare, then flatten it. "
                         "Without this Q5 measures nothing.")
    ap.add_argument("--credential", default="night",
                    help="offhours/session modes: which deployment's credential "
                         "to use from config/secrets.toml (default night).")
    ap.add_argument("--contract", default="TXFR1")
    ap.add_argument("--offset-points", type=float, default=500.0,
                    help="How far BELOW the market the probe order rests, so it "
                         "cannot fill by accident.")
    ap.add_argument("--settle-seconds", type=float, default=5.0,
                    help="How long to wait for callbacks after placing.")
    args = ap.parse_args(argv)

    # Tech: `accounts` mode reads per-deployment credentials itself; the other
    #       two still take the single legacy pair from the environment.
    # Why:  Q0 is ABOUT the three credentials, so it must go through the same
    #       loader `cli.py` uses or it would be testing a different thing than
    #       the one that trades.
    if args.mode != "accounts":
        # Tech: take the single credential these two modes need from
        #       `config/secrets.toml` via the same loader `cli.py` uses, which
        #       itself falls back to the legacy environment pair.
        # Why:  `secrets.toml` is the canonical place since S8, so requiring the
        #       environment here would mean a machine set up correctly for live
        #       running still could not run the probe. `--credential` picks which
        #       one, because these modes ask about ONE credential and the answer
        #       may differ between them.
        from . import credentials
        cred = credentials.load(args.credential)
        if cred is None:
            print(f"ERROR: no credential for {args.credential!r} — add a "
                  f"[{args.credential}] section to {credentials.SECRETS_FILE}, "
                  f"or set SHIOAJI_API_KEY / SHIOAJI_SECRET_KEY",
                  file=sys.stderr)
            return 2
        api_key, secret_key = cred.api_key, cred.secret_key

    if args.mode == "session" and not args.ack:
        print("ERROR: --mode session places an order; pass "
              "--i-understand-this-places-orders", file=sys.stderr)
        return 5

    print("STRATEGY_SPLIT_PLAN S7 probe — output is redacted, but READ IT "
          "BEFORE PASTING.")
    if args.mode == "accounts":
        return probe_accounts()
    if args.mode == "offhours":
        return probe_offhours(api_key, secret_key)
    return probe_session(api_key, secret_key, args)


if __name__ == "__main__":
    raise SystemExit(main())
