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

ADDED FOR S9b (option (b), the owning process — STRATEGY_SPLIT_PLAN §18):

  X2  How far does `list_positions` trail the deal that moved the position?
      MEASURED indirectly three times (§18.9) and never timed. It matters
      because `broker_position()` polls `list_positions` every bar and
      `SafetyRails.on_reconcile` halts on ANY mismatch with no tolerance
      (`state.py:240`) — so a fill landing just before a bar boundary gives a
      correct mirror, a stale venue read, and a spurious STOP. The rail's
      tolerance (if it gets one) should be chosen from a number, not a guess.

  X1  Must an order that crosses THROUGH flat be split into two?
      Under (b) the owner maps `octype` from the ACCOUNT net, so
      Cover-with-nothing-to-cover cannot be built. What that mapping cannot
      express is one order that both closes and opens: account +1, intents net
      to -1, so the Sell 2 is `Cover` for one lot and `New` for the other. It
      decides whether the owner's place path is one `place_order` or a pair
      (§18.3 item 1), which is why it runs BEFORE the N-order book is written.

MODES:

    --mode accounts   Q0 only. Three logins from `config/secrets.toml`, compares
                      account ids, places nothing. Run this FIRST, and after any
                      change to the credentials.
    --mode session    Q4 + Q5. Needs a live session and places ONE resting order
                      far from the market, so it needs
                      --i-understand-this-places-orders.
    --mode crossing   X1. Refuses unless the account is flat. PHASE A tries to
                      cross +1 to -1 with ONE order; PHASE B does it with the
                      sized pair the owner will issue. Flattens between the
                      phases and at the end. Needs
                      --i-understand-this-places-orders.
    --mode sessions   X3. Opens N simultaneous logins (default 4 — option (b)'s
                      shape), reports `connections` after each, checks the FIRST
                      is still usable, and prints the byte quota. No market, no
                      orders, no acknowledgement needed.
    --mode lag        X2. Times the gap between a deal callback and the moment
                      `list_positions` reflects it. Alternates Buy 1 / Sell 1,
                      so it ends flat. Needs
                      --i-understand-this-places-orders.

SAFETY: every line goes through `ioc_probe.redact`, and account ids are masked,
because Shioaji puts a Taiwanese national ID (`person_id`) in account reprs and a
probe's output is meant to be pasted into a conversation.

    uv run --no-sync python -m forecast_eval.live.multi_deploy_probe --mode accounts
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
from typing import Any, List, Optional, Tuple

from .ioc_probe import redact


# Tech: marker env var that tells a re-executed copy of this module it is the
#       child and must do the real work instead of re-spawning.
# Why:  see `_run_filtered`. A plain flag would work too, but an env var keeps it
#       out of `--help` and out of the argv the child re-parses.
_CHILD_ENV = "FE_PROBE_FILTERED_CHILD"

# Tech: how many times `--mode crossing`'s exit flatten re-reads the position and
#       sends another close before giving up and shouting.
# Why:  MEASURED 2026-09-02 — a single-shot flatten placed a Buy 2 against a -2
#       book, one lot filled, and the probe exited with the account short 1.
#       `flatten.py` already carries the rule ("submitted a close" is not "flat",
#       §2.12); three attempts is the same bounded shape, and bounded rather than
#       unbounded so a venue that will not fill cannot spin here forever.
FLATTEN_ATTEMPTS = 3


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


def _net(api: Any, account: Any, codes: set) -> int:
    # Tech: signed net lots the venue reports for our contract.
    # Why:  every step of the crossing probe is judged on the position AFTER it,
    #       not on what the order object claimed. `flatten.net_position` is the
    #       same reader `flatten.py` trusts, and `contract_codes` is what handles
    #       the alias/resolved split (`contracts.py:71`) — an alias-only filter
    #       reports flat while holding, which would make every step look
    #       rejected.
    from .flatten import net_position
    return net_position(api.list_positions(account) or [], codes)


# Tech: the order states from which no further fill can arrive.
# Why:  MEASURED 2026-09-02, and it invalidated this probe's first run. A fixed
#       `sleep` read STEP 2's position while its Cover-2 was still `Submitted`;
#       the order filled two seconds later, but the probe had already recorded
#       "did not cross", placed STEP 3 on top of a working order, and printed a
#       verdict that the deals in its own output contradict. "Has it finished"
#       is a question about STATUS, and only these three answer it.
_TERMINAL = ("Filled", "Cancelled", "Failed")


def _status_name(trade: Any) -> str:
    # Tech: the bare name of a Trade's current status, or "" when unreadable.
    # Why:  `trade.status.status` is an enum whose `str()` is not its name (the
    #       same trap §13 records for `AccountType.Future`, whose `str()` is
    #       'F'). Comparing `.value`, with `getattr` fallbacks, is what makes
    #       `_TERMINAL` mean what it reads as.
    st = getattr(trade, "status", None)
    inner = getattr(st, "status", None)
    return str(getattr(inner, "value", inner) or "")


def _settle(api: Any, account: Any, trade: Any, label: str,
            timeout_s: float) -> str:
    """Poll until the order can produce no further fill; cancel it if it will not.

    Tech: refresh with `update_status` and read the status name until it is
          terminal or `timeout_s` elapses; on timeout cancel and keep polling
          through a short grace.
    Why:  the probe's whole measurement is "where did the position end up", and
          that question is only answerable once the order is finished. Cancelling
          on timeout rather than abandoning it is what stops a still-working
          order from filling underneath the NEXT step and making both
          unattributable — which is exactly what the first run did.
    """
    deadline = time.monotonic() + timeout_s
    cancelled = False
    while True:
        try:
            api.update_status(account)
        except Exception as exc:                        # noqa: BLE001
            _say("  update_status failed", redact(repr(exc))[:120])
        name = _status_name(trade)
        if name in _TERMINAL:
            _say(f"  {label} settled", name)
            return name
        if time.monotonic() >= deadline:
            if cancelled:
                _say(f"  {label} STILL WORKING after cancel", name or "?")
                return name
            _say(f"  {label} not terminal, cancelling", name or "?")
            try:
                api.cancel_order(trade)
            except Exception as exc:                    # noqa: BLE001
                _say("  cancel failed", redact(repr(exc))[:120])
            cancelled = True
            deadline = time.monotonic() + max(5.0, timeout_s / 2)
        time.sleep(0.5)


def _stable_net(api: Any, account: Any, codes: set, label: str,
                reads: int = 3, gap: float = 3.0) -> Tuple[int, bool]:
    """Read the venue position several times; report the last value and whether it moved."""
    # Tech: sample `_net` `reads` times `gap` seconds apart, print every sample,
    #       and return (last, stable).
    # Why:  MEASURED 2026-09-02 (§18.8). After a `Cover` Sell 2 filled against a
    #       1-lot long — a deal reporting quantity 2 — the position read back as
    #       0, not -1. Two explanations fit: the venue CLAMPED the excess lot at
    #       flat, or `list_positions` had not caught up with its own deal. A
    #       single read cannot tell them apart and a verdict that does not know
    #       which is a verdict about nothing. A sequence separates them: a lag
    #       settles, a clamp does not.
    seen: List[int] = []
    for i in range(reads):
        if i:
            time.sleep(gap)
        seen.append(_net(api, account, codes))
    stable = len(set(seen)) == 1
    _say(f"position {label}",
         f"{seen[-1]:+d}   (reads over {gap * (reads - 1):.0f}s: "
         f"{', '.join(f'{v:+d}' for v in seen)}"
         f"{'' if stable else '  <-- STILL MOVING'})")
    return seen[-1], stable


def probe_crossing(api_key: str, secret_key: str, args) -> int:
    """STRATEGY_SPLIT_PLAN §18.1 — how must an order that crosses flat be sent?

    Under option (b) the owning process maps `octype` from the ACCOUNT net, so
    Cover-with-nothing-to-cover cannot be constructed. The one case that mapping
    cannot express is an order crossing THROUGH flat: the account holds +1, the
    deployments' intents net to -1, and the resulting Sell 2 is `Cover` for its
    first lot and `New` for its second. Does the venue take that as one order —
    and under which `octype` — or must the owner split it into a pair?

    Two phases, because knowing the single call fails is only half an answer:

      A. the single call, both spellings — what the owner must NOT do;
      B. the split — Cover sized to the lots that close, then New for the rest —
         which is what the owner's place path will actually issue (§18.3 item 1).

    B is the one that matters. A failing remedy discovered while writing the
    broker is far more expensive than one discovered here.
    """
    import shioaji as sj

    print("\n=== §18.1: how does an order that crosses THROUGH flat behave? ===")
    api = sj.Shioaji(simulation=True)
    accounts = api.login(api_key=api_key, secret_key=secret_key)
    fut = _futopt(accounts)
    if not fut:
        print("  no futopt account; cannot place an order")
        return 2
    account = fut[0]

    from .contracts import contract_codes
    contract = api.Contracts.Futures.TXF[args.contract]
    codes = contract_codes(contract)
    _say("contract", f"{args.contract} -> {sorted(codes)}")

    def _last() -> float:
        return float(getattr(api.snapshots([contract])[0], "close", 0.0))

    # Tech: classify the wall clock against the TXF session windows and say
    #       plainly what an off-hours run would measure.
    # Why:  every order this probe places is MARKETABLE, and the whole
    #       measurement is "what did the venue do with this fill". Outside
    #       08:45-13:45 and 15:00-05:00 there is nothing to fill against: A1
    #       would time out, be cancelled, and the probe would exit 4 having
    #       measured nothing — the same empty result §14 records for a resting
    #       order. A WARNING and not a refusal, because the simulation venue's
    #       off-hours behaviour is itself unmeasured, and refusing would block
    #       the run that would measure it.
    import pandas as pd
    from ..environment import classify_session
    now = pd.Timestamp.now()
    session = classify_session(now)
    _say("local time", f"{now:%Y-%m-%d %H:%M:%S}  session={session or 'CLOSED'}")
    if session is None:
        print("")
        print("  WARNING: no TXF session is open right now (DAY 08:45-13:45, "
              "NIGHT 15:00-05:00,")
        print("  Taipei). Every order here is marketable and this probe measures "
              "FILLS, so an")
        print("  off-hours run most likely measures nothing and exits 4. Run it "
              "inside a session.")

    # Tech: refuse to start unless the account is already flat.
    # Why:  every step below is read as a DELTA from a known position. Starting
    #       on someone else's lot would make step 2's result unattributable, and
    #       the closing flatten would close a position this probe did not open.
    start = _net(api, account, codes)
    _say("position at start", f"{start:+d}")
    if start != 0:
        print("\n  REFUSED: the account is not flat. Flatten it first —")
        print("    uv run --no-sync python -m forecast_eval.live.flatten")
        _logout(api)
        return 3

    # Tech: every order this probe sends is marketable by construction — buys
    #       priced above the market, sells below it — and LMT+ROD, then settled
    #       to a terminal status before any position is read.
    # Why:  a resting order measures nothing (the lesson §14 records: the first
    #       --mode session run compared "0 positions vs 0" and concluded from
    #       it). The question is what the VENUE does with an octype, and only a
    #       completed fill answers it. A bounded limit rather than MKT keeps the
    #       worst price knowable, exactly as `broker/shioaji.py` does.
    def _send(action, quantity: int, octype, label: str):
        last = _last()
        price = (last + args.offset_points if action == sj.Action.Buy
                 else last - args.offset_points)
        order = sj.FuturesOrder(
            price=price, quantity=quantity, action=action,
            price_type=sj.FuturesPriceType.LMT,
            order_type=sj.OrderType.ROD,
            octype=octype, account=account,
        )
        try:
            trade = api.place_order(contract, order)
        except Exception as exc:                        # noqa: BLE001
            _say(f"{label} RAISED", redact(repr(exc))[:200])
            return None, "Raised"
        _say(f"{label} placed",
             f"qty={quantity} @ {price:.0f} octype={octype} "
             f"id={getattr(getattr(trade, 'order', None), 'id', '?')}")
        # Tech: settle before returning, and only then may the caller read the
        #       position.
        # Why:  see `_settle`. Reading a position against a `Submitted` order is
        #       reading it against an order that has not happened yet — the
        #       defect that invalidated this probe's first run (§18.6).
        return trade, _settle(api, account, trade, label, args.fill_timeout)

    # ---------------- PHASE A — the single call ----------------------------
    print("\n--- PHASE A: can ONE order cross from +1 to -1? ---")

    # Tech: open the long the crossing order will have to cross.
    # Why:  +1 is the account state the mapping question is asked from.
    _, st1 = _send(sj.Action.Buy, 1, sj.FuturesOCType.New, "A1 New buy 1")
    held = _net(api, account, codes)
    _say("position after A1", f"{held:+d}")
    if held != 1:
        print(f"\n  INCONCLUSIVE: the opening order ended {st1!r} and left "
              f"{held:+d}, not +1,")
        print("  so there is nothing to cross. Nothing further is placed.")
        _flatten_probe(api, account, contract, codes, args, sj, _last)
        _logout(api)
        return 4

    # Tech: the crossing order as ONE call, spelled `Cover`, then the position
    #       sampled repeatedly rather than once.
    # Why:  `Cover` is what the owner would send if it mapped octype from the lot
    #       that genuinely closes. The repeated sampling is §18.8: the answer
    #       turns on whether an unexpected reading settles or holds.
    _, st2 = _send(sj.Action.Sell, 2, sj.FuturesOCType.Cover, "A2 Cover sell 2")
    held, stable = _stable_net(api, account, codes, "after A2")

    if held == -1:
        verdict = ("ONE CALL, octype=Cover",
                   "The venue accepted a quantity-2 Cover against a 1-lot long "
                   "and left the account short 1. The owner's place path stays "
                   "a single `place_order`, with octype taken from the lot that "
                   "closes.")
    elif held == 0:
        # Tech: the measured case — the Cover filled in full on paper and the
        #       account came to rest at flat instead of short 1.
        # Why:  §18.8. The excess lot closed nothing and opened nothing. That is
        #       the single most important thing this probe found, because the
        #       venue did not REFUSE it: it reported a 2-lot fill and moved the
        #       book by 1. A place path that trusted either number alone would be
        #       wrong about the position from the first crossing order onward.
        verdict = ("COVER CLAMPS AT FLAT",
                   f"The Cover-2 ended {st2!r} — a deal reporting TWO lots — and "
                   f"the account came to rest at 0, not -1"
                   f"{'' if stable else ' (and it was still moving when sampled)'}"
                   f". The excess lot neither closed nor opened anything. `Cover` "
                   f"cannot cross through flat, and it does not refuse: it "
                   f"reports a full fill and clamps. The owner must size the "
                   f"Cover leg to the lots that actually close and issue the "
                   f"remainder as a separate `New` — Phase B measures that.")
    else:
        verdict = ("SINGLE CALL LANDS WRONG",
                   f"The Cover-2 ended {st2!r} and the account came to rest at "
                   f"{held:+d}, neither -1 nor 0"
                   f"{'' if stable else ' (and it was still moving when sampled)'}"
                   f". A single call reaches neither the intended position nor a "
                   f"clean refusal, so the owner must split the crossing intent "
                   f"and size each leg itself.")

    print(f"\n  => PHASE A: {verdict[0]}")
    for line in _wrap(verdict[1]):
        print(f"     {line}")

    # ---------------- PHASE B — the split, which is the remedy -------------
    # Tech: return to flat, re-open +1, then cross it with a sized PAIR — Cover
    #       for exactly the lots held, then New for the rest — and check that the
    #       account lands on -1.
    # Why:  Phase A only establishes what does not work. The owner's place path
    #       is going to issue this pair (§18.3 item 1), and measuring it here
    #       costs four orders in simulation; measuring it by shipping it costs a
    #       live desync. It also answers a question Phase A cannot: whether the
    #       New leg opens correctly immediately after a Cover leg on the same
    #       account, with no settling gap between them.
    print("\n--- PHASE B: does the SPLIT pair land on -1? ---")
    if _flatten_probe(api, account, contract, codes, args, sj, _last) != 0:
        print("  SKIPPED: could not return to flat before Phase B.")
        _logout(api)
        return 6

    _send(sj.Action.Buy, 1, sj.FuturesOCType.New, "B1 New buy 1")
    held = _net(api, account, codes)
    _say("position after B1", f"{held:+d}")
    if held != 1:
        print(f"  SKIPPED: could not re-open +1 (got {held:+d}).")
        _flatten_probe(api, account, contract, codes, args, sj, _last)
        _logout(api)
        return 4

    # Tech: leg 1 — Cover exactly what is held, never more.
    # Why:  Phase A measured that a Cover beyond the held size is absorbed
    #       silently. `min(needed, held)` is the rule that keeps every Cover
    #       inside what exists.
    _, stb2 = _send(sj.Action.Sell, 1, sj.FuturesOCType.Cover, "B2 Cover sell 1")
    mid, _ = _stable_net(api, account, codes, "after B2 (Cover leg)")

    # Tech: leg 2 — New for the lots that open, sent only if the Cover leg
    #       actually reached flat.
    # Why:  if the close leg has not landed, the open leg would be sized against
    #       a position that is not there — the same mistake in a new place.
    if mid != 0:
        print(f"\n  => PHASE B INCONCLUSIVE: the Cover leg left {mid:+d}, not 0, "
              f"so the New leg was not sent.")
        left = _flatten_probe(api, account, contract, codes, args, sj, _last)
        _logout(api)
        return 6 if left else 0

    _, stb3 = _send(sj.Action.Sell, 1, sj.FuturesOCType.New, "B3 New sell 1")
    end, stable_b = _stable_net(api, account, codes, "after B3 (New leg)")

    if end == -1:
        print("\n  => PHASE B: SPLIT PAIR CONFIRMED")
        for line in _wrap(
                f"Cover 1 ({stb2!r}) then New 1 ({stb3!r}) took the account from "
                f"+1 through 0 to -1, with no settling gap needed between the "
                f"legs. This is the path the owner's `submit` must issue for a "
                f"crossing intent: Cover for min(intent, held), then New for the "
                f"remainder, in that order."):
            print(f"     {line}")
    else:
        print("\n  => PHASE B: SPLIT PAIR DID NOT LAND")
        for line in _wrap(
                f"Cover 1 ({stb2!r}) then New 1 ({stb3!r}) left the account at "
                f"{end:+d}, not -1"
                f"{'' if stable_b else ' (and it was still moving when sampled)'}"
                f". The remedy Phase A points at does not work as written, and "
                f"§18.3 item 1 needs rethinking before the place path is built."):
            print(f"     {line}")

    left = _flatten_probe(api, account, contract, codes, args, sj, _last)
    _logout(api)
    return 6 if left else 0


def _wrap(text: str, width: int = 68) -> List[str]:
    # Tech: greedy word wrap for the verdict paragraph.
    # Why:  the verdict is the one thing a reader takes away, and an unwrapped
    #       200-character line in a terminal is the one thing they will not read.
    lines, cur = [], ""
    for word in text.split():
        if cur and len(cur) + 1 + len(word) > width:
            lines.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}".strip()
    if cur:
        lines.append(cur)
    return lines


def _flatten_probe(api: Any, account: Any, contract: Any, codes: set,
                   args, sj: Any, last_fn) -> int:
    """Close whatever this probe is holding. Returns the lots still held."""
    # Tech: re-read the position, re-snapshot the market, send a marketable
    #       close, settle it, and repeat up to FLATTEN_ATTEMPTS times.
    # Why:  MEASURED 2026-09-02 — the first run's single-shot flatten placed a
    #       Buy 2 against a -2 book, ONE lot filled, and the probe exited
    #       announcing the account was still holding. `flatten.py` already
    #       learned this ("submitted a close" is not "flat", §2.12) and retries;
    #       so must this, because a probe that leaves a position is worse than
    #       one that measures nothing. Re-snapshotting each attempt matters for
    #       the same reason the retry does: a price from before the previous
    #       attempt may no longer be marketable.
    for attempt in range(1, FLATTEN_ATTEMPTS + 1):
        held = _net(api, account, codes)
        if held == 0:
            _say("account flat", True)
            return 0
        last = last_fn()
        action = sj.Action.Sell if held > 0 else sj.Action.Buy
        price = last - args.offset_points if held > 0 else last + args.offset_points
        _say(f"FLATTEN attempt {attempt}", f"{held:+d} -> qty={abs(held)} @ {price:.0f}")
        try:
            # Tech: `Auto` here deliberately, not the explicit octype the steps
            #       above measure.
            # Why:  the flatten is not part of the measurement and must work from
            #       whatever state the steps left — including one where an
            #       explicit `Cover` was just refused. Letting the venue decide
            #       is the only spelling that is right in every one of them.
            trade = api.place_order(contract, sj.FuturesOrder(
                price=price, quantity=abs(held), action=action,
                price_type=sj.FuturesPriceType.LMT,
                order_type=sj.OrderType.ROD,
                octype=sj.FuturesOCType.Auto, account=account,
            ))
            _settle(api, account, trade, f"FLATTEN {attempt}", args.fill_timeout)
        except Exception as exc:                        # noqa: BLE001
            _say("  flatten order failed", redact(repr(exc))[:160])

    held = _net(api, account, codes)
    if held:
        print(f"\n  *** THE ACCOUNT IS STILL HOLDING {held:+d} after "
              f"{FLATTEN_ATTEMPTS} attempts. Flatten it by hand: "
              f"uv run --no-sync python -m forecast_eval.live.flatten ***")
    else:
        _say("account flat", True)
    return held


def _median(values: List[float]) -> float:
    # Tech: middle value of a sorted copy, averaging the two middles on an even
    #       count.
    # Why:  the lag summary needs a centre that one slow outlier cannot drag, and
    #       a probe with four samples should not pull in a dependency for it.
    ordered = sorted(values)
    n = len(ordered)
    if n == 0:
        return float("nan")
    mid = n // 2
    return ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0


def probe_lag(api_key: str, secret_key: str, args) -> int:
    """X2 — how far does `list_positions` trail the deal that moved the position?

    STRATEGY_SPLIT_PLAN §18.9. Three runs of `--mode crossing` disagreed about
    what a Cover-2 did, and the disagreement resolved to this: the venue's
    position read lags its own deal stream. That is not a curiosity —
    `broker_position()` polls `list_positions` live every bar and
    `SafetyRails.on_reconcile` halts on ANY mismatch with no tolerance
    (`state.py:240`), so a fill landing shortly before a bar boundary yields a
    correct mirror, a stale venue read, and a spurious STOP.

    §18.9 recorded the lag as the best explanation of three runs but NOT as a
    measured quantity, and refused to pick a rail tolerance without one. This
    measures it: for each sample, timestamp the deal callback, then poll
    `list_positions` until it reflects the new net, and report the interval.

    Alternates Buy 1 / Sell 1 so the account returns to flat after every pair and
    ends flat.
    """
    import shioaji as sj

    print("\n=== X2: how far does list_positions trail its own deals? ===")
    api = sj.Shioaji(simulation=True)
    accounts = api.login(api_key=api_key, secret_key=secret_key)
    fut = _futopt(accounts)
    if not fut:
        print("  no futopt account; cannot place an order")
        return 2
    account = fut[0]

    from .contracts import contract_codes
    contract = api.Contracts.Futures.TXF[args.contract]
    codes = contract_codes(contract)
    _say("contract", f"{args.contract} -> {sorted(codes)}")

    import pandas as pd
    from ..environment import classify_session
    now = pd.Timestamp.now()
    session = classify_session(now)
    _say("local time", f"{now:%Y-%m-%d %H:%M:%S}  session={session or 'CLOSED'}")
    if session is None:
        print("")
        print("  WARNING: no TXF session is open (DAY 08:45-13:45, NIGHT "
              "15:00-05:00, Taipei).")
        print("  This measures the gap between a FILL and the position that "
              "reflects it, so with")
        print("  nothing filling it measures nothing.")

    start = _net(api, account, codes)
    _say("position at start", f"{start:+d}")
    if start != 0:
        print("\n  REFUSED: the account is not flat. Flatten it first —")
        print("    uv run --no-sync python -m forecast_eval.live.flatten")
        _logout(api)
        return 3

    # Tech: record the arrival time of every FDEAL on the session's own callback.
    # Why:  the deal callback is the EARLIEST moment this process can know a fill
    #       happened, which is exactly the instant the mirror moves in
    #       `ShioajiBroker._match`. Measuring from anywhere later (the order's
    #       status, say) would understate the gap the rail is exposed to.
    deals: List[float] = []
    lock = threading.Lock()

    def on_event(*msg: Any) -> None:
        if msg and "Deal" in str(msg[0]):
            with lock:
                deals.append(time.monotonic())

    api.set_order_callback(on_event)
    _say("deal callback registered", True)

    def _first_deal_after(t0: float, timeout: float) -> Optional[float]:
        # Tech: block until a deal timestamped after `t0` appears, or give up.
        # Why:  one order is in flight at a time, so the first deal after the
        #       placement is that order's. Waiting on the CALLBACK rather than on
        #       a status poll is what makes the measured interval start at the
        #       fill itself.
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with lock:
                for ts in deals:
                    if ts >= t0:
                        return ts
            time.sleep(0.05)
        return None

    rows: List[Tuple[int, str, Optional[float], Optional[float]]] = []
    held = 0
    for i in range(1, args.samples + 1):
        # Tech: alternate Buy / Sell so the account walks 0 -> +1 -> 0 -> +1 ...
        # Why:  every sample is a real position change with a known expected
        #       result, and the account is flat after each pair. Quantity 1
        #       throughout so a sample is one deal, not a partial-fill sequence
        #       whose "the position now reflects it" moment is ambiguous.
        buying = (held == 0)
        action = sj.Action.Buy if buying else sj.Action.Sell
        expect = held + (1 if buying else -1)
        last = _last_price(api, contract)
        price = last + args.offset_points if buying else last - args.offset_points
        order = sj.FuturesOrder(
            price=price, quantity=1, action=action,
            price_type=sj.FuturesPriceType.LMT,
            order_type=sj.OrderType.ROD,
            octype=sj.FuturesOCType.New if buying else sj.FuturesOCType.Cover,
            account=account,
        )
        t_place = time.monotonic()
        try:
            api.place_order(contract, order)
        except Exception as exc:                        # noqa: BLE001
            _say(f"sample {i} RAISED", redact(repr(exc))[:200])
            rows.append((i, "place raised", None, None))
            break

        t_deal = _first_deal_after(t_place, args.fill_timeout)
        if t_deal is None:
            _say(f"sample {i}", f"no deal within {args.fill_timeout:.0f}s — "
                                f"nothing filled, sample dropped")
            rows.append((i, "no fill", None, None))
            break

        # Tech: poll `list_positions` as fast as the accounting limit allows
        #       until it reports `expect`, and record when it first does.
        # Why:  0.25 s is 4 reads/second, under the 25-per-5-s accounting limit
        #       `broker/shioaji.py` documents. The interval this yields is a
        #       CEILING on the lag with a 0.25 s granularity, not an exact value —
        #       the position may have settled at any point since the previous
        #       poll — and that is the right direction to err for a rail
        #       tolerance.
        deadline = time.monotonic() + args.lag_timeout
        t_seen = None
        while time.monotonic() < deadline:
            if _net(api, account, codes) == expect:
                t_seen = time.monotonic()
                break
            time.sleep(0.25)

        if t_seen is None:
            _say(f"sample {i}", f"position never reached {expect:+d} within "
                                f"{args.lag_timeout:.0f}s")
            rows.append((i, f"{'Buy' if buying else 'Sell'} -> {expect:+d}",
                         t_deal - t_place, None))
            held = _net(api, account, codes)
            continue

        fill_s = t_deal - t_place
        lag_s = t_seen - t_deal
        _say(f"sample {i} {'Buy' if buying else 'Sell'} -> {expect:+d}",
             f"place->deal {fill_s:6.2f}s   deal->position {lag_s:6.2f}s")
        rows.append((i, f"{'Buy' if buying else 'Sell'} -> {expect:+d}",
                     fill_s, lag_s))
        held = expect

    lags = [lag for _, _, _, lag in rows if lag is not None]
    print("\n  --- deal -> position lag ---")
    for idx, label, fill_s, lag_s in rows:
        fill_txt = f"{fill_s:6.2f}s" if fill_s is not None else "     --"
        lag_txt = f"{lag_s:6.2f}s" if lag_s is not None else "     --"
        print(f"    {idx}  {label:<16} place->deal {fill_txt}   "
              f"deal->position {lag_txt}")

    if lags:
        print(f"\n  samples {len(lags)}   min {min(lags):.2f}s   "
              f"median {_median(lags):.2f}s   max {max(lags):.2f}s")
        print(f"  (ceiling values: polled every 0.25s)")
        worst = max(lags)
        print("")
        for line in _wrap(
                f"`on_reconcile` halts on any mismatch with no tolerance "
                f"(`state.py:240`) and the loop reconciles once per bar. A fill "
                f"landing within ~{worst:.1f}s of a bar boundary is therefore the "
                f"exposed window: the mirror is right, the venue read is stale, "
                f"and the rail STOPs. Whether that becomes a tolerance, a "
                f"re-read, or nothing is STRATEGY_SPLIT_PLAN's call, not this "
                f"probe's."):
            print(f"  {line}")
    else:
        print("\n  NO SAMPLES. Nothing filled, so nothing was measured — run "
              "this inside a session.")

    left = _flatten_probe(api, account, contract, codes, args, sj,
                          lambda: _last_price(api, contract))
    _logout(api)
    if not lags:
        return 4
    return 6 if left else 0


def _last_price(api: Any, contract: Any) -> float:
    # Tech: the contract's most recent close from a snapshot.
    # Why:  every probe order is priced off the live touch and re-snapshotted per
    #       order; a price carried from the start of a run may no longer be
    #       marketable by the last sample.
    return float(getattr(api.snapshots([contract])[0], "close", 0.0))


def _usage(api: Any) -> Any:
    # Tech: `api.usage()`, or None when the call fails.
    # Why:  a failed `usage()` on a live session is itself a result — it is what
    #       `cli.py`'s verify window uses to prove a token is alive (§2.24) — so
    #       the failure must be visible rather than reported as a zero.
    try:
        return api.usage()
    except Exception as exc:                            # noqa: BLE001
        _say("usage() raised", redact(repr(exc))[:120])
        return None


def probe_sessions(args) -> int:
    """X3 — how many simultaneous logins does one credential actually allow?

    STRATEGY_SPLIT_PLAN §18. Option (b) runs the owner PLUS three deployments,
    and each deployment still logs in for the market feed — only orders are
    delegated. That is **four** sessions where §14b Q2 measured **two**.

    Two things are measured, and the second was never looked at before:

      * `connections` after each login, and whether the FIRST session is still
        usable once the last one is open. Q0 already opened three concurrently
        as a side effect (it holds every session until its `finally`), so three
        logins succeeding is known; that the first still WORKS with three open
        is not.
      * `limit_bytes` / `remaining_bytes`. Four sessions subscribe to the same
        TXF tick stream and download it four times against one credential's
        allowance. Nothing in this repo has ever read these fields, and a quota
        exhausted mid-session stops a deployment forecasting while it is still
        holding a position.

    No market and no orders — this only logs in.
    """
    import shioaji as sj

    from . import credentials
    from .config import CONFIGS

    print()
    print(f"=== X3: do {args.sessions} simultaneous logins coexist? ===")

    # Tech: cycle through the configured deployment credentials, reusing them
    #       when more sessions are asked for than there are credentials.
    # Why:  this is the real shape — `owner_cli.py` defaults to `--credential
    #       night` while the three deployments use their own, so the fourth
    #       session genuinely is a second login on one of the keys. Reusing
    #       rather than refusing keeps the probe honest about that.
    names = sorted(CONFIGS)
    creds = [(n, credentials.load(n)) for n in names]
    creds = [(n, c) for n, c in creds if c is not None]
    if not creds:
        print(f"  no credentials configured; add sections to "
              f"{credentials.SECRETS_FILE.name}")
        return 2

    sessions: List[Any] = []
    first: Optional[Any] = None
    try:
        for i in range(args.sessions):
            name, cred = creds[i % len(creds)]
            api = sj.Shioaji(simulation=True)
            try:
                api.login(api_key=cred.api_key, secret_key=cred.secret_key)
            except Exception as exc:                    # noqa: BLE001
                _say(f"session {i + 1} ({name}) LOGIN FAILED",
                     redact(repr(exc))[:200])
                print()
                print(f"  => THE LIMIT IS {i}. Session {i + 1} was refused.")
                print(f"     Option (b) needs the owner plus one session per")
                print(f"     deployment. With {i} the architecture does not fit as")
                print(f"     built — the owner would have to forward prices too,")
                print(f"     which it does not.")
                return 1
            sessions.append(api)
            if first is None:
                first = api
            usage = _usage(api)
            _say(f"session {i + 1} ({name}) up",
                 f"connections={getattr(usage, 'connections', '?')}")

        # Tech: prove the FIRST session still answers with every session open.
        # Why:  Q2's real finding was not that a second login succeeds but that
        #       the first SURVIVES it. A login that quietly evicts an earlier
        #       session would look identical to one that coexists, right up to
        #       the moment the owner stops hearing deals.
        print()
        usage = _usage(first)
        if usage is None:
            print(f"  => THE FIRST SESSION IS DEAD with {len(sessions)} open.")
            print("     Later logins evict earlier ones. Option (b) cannot run")
            print("     as built.")
            return 1
        _say("first session still usable", True)
        _say("connections seen by the first session",
             getattr(usage, "connections", "?"))

        # Tech: report the byte quota from whichever session answered.
        # Why:  the second half of this probe, and the half nothing here has ever
        #       looked at. `usage()` has been called three times in this repo and
        #       only ever as a liveness check.
        limit = getattr(usage, "limit_bytes", None)
        remaining = getattr(usage, "remaining_bytes", None)
        used = getattr(usage, "bytes", None)
        print()
        _say("bytes used today", f"{used:,}" if isinstance(used, int) else used)
        _say("limit_bytes", f"{limit:,}" if isinstance(limit, int) else limit)
        _say("remaining_bytes",
             f"{remaining:,}" if isinstance(remaining, int) else remaining)
        if isinstance(limit, int) and isinstance(used, int) and limit > 0:
            share = used / limit
            print()
            for line in _wrap(
                    f"That is {share:.1%} of the daily allowance, on a run that "
                    f"only logged in. The figure to watch is what ONE deployment "
                    f"consumes over a full session of TXF ticks: option (b) "
                    f"multiplies it by the number of sessions, because each one "
                    f"subscribes to the same stream separately. Compare a "
                    f"single-deployment soak's end-of-session `bytes` against "
                    f"`limit_bytes` before running four."):
                print(f"  {line}")

        print()
        print(f"  => {len(sessions)} simultaneous logins coexist, and the first")
        print(f"     is still usable. Option (b) fits at this session count.")
        return 0
    finally:
        for api in sessions:
            _logout(api)


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
    ap.add_argument("--mode",
                    choices=("accounts", "offhours", "session", "crossing",
                             "lag", "sessions"),
                    default="accounts",
                    help="accounts: Q0 — are the three credentials three "
                         "distinct accounts? No market, no orders (START HERE). "
                         "offhours: the retired Q1-Q3, kept for a new credential. "
                         "session: Q4-Q5, places one order. "
                         "crossing: §18.1 — must an order that crosses THROUGH "
                         "flat be split? Places up to six orders and flattens. "
                         "lag: §18.9 — how far does list_positions trail its own "
                         "deals? Alternates Buy/Sell 1 and times each. "
                         "sessions: §18 X3 — do N simultaneous logins coexist, "
                         "and how much of the byte quota is left? No orders.")
    ap.add_argument("--i-understand-this-places-orders", action="store_true",
                    dest="ack",
                    help="Required for --mode session and --mode crossing.")
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
    ap.add_argument("--sessions", type=int, default=4,
                    help="sessions mode: how many simultaneous logins to open. "
                         "4 is option (b)'s shape — the owner plus one per "
                         "deployment.")
    ap.add_argument("--samples", type=int, default=4,
                    help="lag mode: how many fills to time. Alternates Buy 1 / "
                         "Sell 1, so an even count ends flat.")
    ap.add_argument("--lag-timeout", type=float, default=30.0,
                    help="lag mode: how long to keep polling list_positions for "
                         "a deal to show up in it before giving up on a sample.")
    ap.add_argument("--fill-timeout", type=float, default=30.0,
                    help="crossing mode: how long to wait for an order to reach "
                         "a TERMINAL status before cancelling it. The position "
                         "is never read against an order still working.")
    args = ap.parse_args(argv)

    # Tech: `accounts` mode reads per-deployment credentials itself; the other
    #       two still take the single legacy pair from the environment.
    # Why:  Q0 is ABOUT the three credentials, so it must go through the same
    #       loader `cli.py` uses or it would be testing a different thing than
    #       the one that trades.
    if args.mode not in ("accounts", "sessions"):
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

    # Tech: both order-placing modes are gated on the same acknowledgement.
    # Why:  `crossing` places up to THREE orders and deliberately takes a
    #       position in order to cross it, so it needs at least the gate
    #       `session` needs. Naming the mode in the message keeps the two
    #       distinguishable when one is refused.
    if args.mode in ("session", "crossing", "lag") and not args.ack:
        print(f"ERROR: --mode {args.mode} places orders; pass "
              "--i-understand-this-places-orders", file=sys.stderr)
        return 5

    print("STRATEGY_SPLIT_PLAN S7 probe — output is redacted, but READ IT "
          "BEFORE PASTING.")
    if args.mode == "accounts":
        return probe_accounts()
    if args.mode == "sessions":
        return probe_sessions(args)
    if args.mode == "offhours":
        return probe_offhours(api_key, secret_key)
    if args.mode == "crossing":
        return probe_crossing(api_key, secret_key, args)
    if args.mode == "lag":
        return probe_lag(api_key, secret_key, args)
    if args.mode == "sessions":
        return probe_sessions(args)
    return probe_session(api_key, secret_key, args)


if __name__ == "__main__":
    raise SystemExit(main())
