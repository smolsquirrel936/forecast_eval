"""§2.24 Step 1 probe — what does a second `login()` actually do?

`ReloginScheduler` decides *when* to re-login. This decides *what* the injected
callables must do, and it is the only instrument left: `Shioaji` comes from a
compiled `_core.pyd`, and `login` / `logout` / `subscribe` carry **no source and
no docstrings** (introspected 2026-08-15). Reading is exhausted; this measures.

Seven questions, in the order they must be asked:

  Q1  does `login()` on a live session succeed — plain, `force_refresh=True`,
      and `logout()` + `login()`?
  Q2  does the **tick subscription** survive a re-login?
  Q3  does the **order callback** survive, or duplicate? (the dangerous one — a
      duplicate double-counts fills, a drop loses them in silence)
  Q4  does `list_positions()` work on the new token, same answer as before?
  Q5  does the account fingerprint change across a re-login?
  Q6  does Shioaji grant a session on a **non-trading day**? (§2.25 — the
      anchors are wall-clock and will fire on Saturday and Sunday)
  Q7  does **`api.usage()` raise on a dead token**? The verify window calls it
      to prove the session is usable; a `usage()` that returns quietly instead
      of raising would leave that window inert — passing forever while the token
      is already gone.

**Two modes, because five of the seven need a live tape and two do not.**

    --mode offhours    Q6 + Q7 only. Bare login, no subscription, no orders.
                       ~10 seconds, runnable any time, including a weekend.
    --mode session     all seven. Needs live ticks, so run it inside a session —
                       the DAY session (08:45-13:45) serves as well as the night
                       one, since this tests SDK mechanics, not strategy.

**Q7 is destructive and therefore last.** It logs out to invalidate the token,
which is the whole point: it produces a dead token in seconds instead of waiting
out a 24-hour expiry. Nothing can run after it.

**This script places one order, in session mode only.** The safety argument is
`v72_probe`'s, unchanged and reusing its helper: `simulation=True` asserted not
assumed, one lot, priced far below the market so it is passive by construction,
cancelled in a `finally`.

Usage:

    $env:SHIOAJI_API_KEY="..."; $env:SHIOAJI_SECRET_KEY="..."
    uv run python -m forecast_eval.live.relogin_probe --mode offhours
    uv run python -m forecast_eval.live.relogin_probe --mode session
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from forecast_eval.live.cli import account_fingerprint              # noqa: E402
from forecast_eval.live.contracts import resolve_txf                # noqa: E402
from forecast_eval.live.v72_probe import probe_limit_price          # noqa: E402


def call(label: str, fn):
    # Tech: run a call, returning `(ok, value_or_exception_repr)` and printing a
    #       one-line verdict; never raises.
    # Why:  every question here is "what happens when we call this", and an
    #       exception *is* an answer rather than a failure of the probe. Letting
    #       one propagate would abandon the remaining questions and waste the
    #       session — which is exactly how §2.18 cost a market window.
    try:
        value = fn()
    except Exception as exc:                          # noqa: BLE001 — see Why
        # Tech: redact the exception text too, not just the success value.
        # Why:  an SDK error can quote the object that caused it, so the failure
        #       path is as capable of leaking an identifier as the happy one.
        detail = _redact(f"{type(exc).__name__}: {exc}")
        print(f"    {label:38s} RAISED  {detail}")
        return False, detail
    print(f"    {label:38s} ok      {_brief(value)}")
    return True, value


# Tech: identifying fields to strip from any SDK repr before it is printed.
# Why:  MEASURED THE HARD WAY 2026-08-16 — the first run printed
#       `person_id='L125551606'`, a Taiwanese national ID, straight to a console
#       whose output is meant to be pasted into a chat. `cli.py` digests this
#       field and never even masks it, on the grounds that a partial national ID
#       is more than an audit trail needs to carry; a probe that prints it whole
#       defeats that everywhere at once. Redaction lives in the *renderer* rather
#       than at each call site so no future question can reintroduce the leak.
_SECRET_FIELDS = ("person_id", "account_id")

# Tech: the shape of a Taiwanese national ID — one letter then nine digits.
# Why:  MEASURED TWICE. Field-name redaction alone was not enough: run 2 still
#       leaked the ID because the SDK embeds it *bare* inside a session-event
#       string (`#P2P/v:.../PYAPI/L1255…/0815/…`), where there is no `person_id=`
#       to match on. Redacting by **shape** catches it wherever it appears.
#       Over-redaction is the safe direction here.
_ID_PATTERN = re.compile(r"\b[A-Z][0-9]{9}\b")


def _redact(text: str) -> str:
    # Tech: blank the value of any identifying field, then any bare national-ID
    #       shaped token anywhere in the string.
    # Why:  two passes because the leak arrived by two different routes — a
    #       structured `repr()` field, and free text inside an SDK event. The
    #       field pass keeps the output readable; the shape pass is the backstop
    #       that does not depend on the SDK's formatting choices.
    for field in _SECRET_FIELDS:
        text = re.sub(rf"{field}=('[^']*'|\"[^\"]*\"|\w+)",
                      f"{field}=<redacted>", text)
    return _ID_PATTERN.sub("<redacted-id>", text)


def _brief(value, limit: int = 60) -> str:
    # Tech: a short, redacted rendering of an arbitrary SDK return value.
    # Why:  these objects print enormously (a Contracts container is thousands of
    #       lines) and the console output is what gets pasted back, so it has to
    #       stay readable — and free of identifiers. Redact *before* truncating,
    #       or a cut mid-value would leak a prefix.
    text = _redact(repr(value))
    return text if len(text) <= limit else text[:limit] + "…"


def summarise(findings: dict) -> str:
    # Tech: render the collected answers as a block meant to be pasted back.
    # Why:  the operator runs this and reports; a probe whose output has to be
    #       interpreted before it can be reported is a probe that produces
    #       arguments instead of measurements.
    lines = ["", "=" * 70, "  PASTE THIS BACK", "=" * 70]
    for key in sorted(findings):
        lines.append(f"  {key:34s} {findings[key]}")
    lines.append("=" * 70)
    return "\n".join(lines)


def _login(api, api_key, secret_key, **kw):
    return lambda: api.login(api_key=api_key, secret_key=secret_key, **kw)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--mode", choices=("offhours", "session"),
                    default="offhours",
                    help="offhours: Q6+Q7 only, no subscription or orders. "
                         "session: all seven, needs live ticks.")
    ap.add_argument("--relogin-form", choices=("force_refresh", "plain"),
                    default="force_refresh",
                    help="session mode: which login form to test around. "
                         "'plain' is the Q10 control — Q8 measured it as a "
                         "no-op, so whether it also disturbs callbacks decides "
                         "whether callbacks die with the connection object.")
    ap.add_argument("--contract", default="TXFR1")
    ap.add_argument("--tick-seconds", type=float, default=60.0,
                    help="How long to count ticks either side of the re-login")
    ap.add_argument("--offset-points", type=float, default=500.0,
                    help="How far BELOW the market to rest the probe BUY limit")
    ap.add_argument("--watch-minutes", type=float, default=0.0,
                    help="Q9: after the login forms, sample the connection "
                         "count for this many minutes to see whether leaked "
                         "connections are reclaimed inside a live process")
    ap.add_argument("--watch-every", type=float, default=30.0,
                    help="Q9 sampling interval, seconds")
    ap.add_argument("--settle-seconds", type=float, default=2.0,
                    help="Pause after each login so session events and the "
                         "connection count can catch up before sampling")
    ap.add_argument("--no-order", action="store_true",
                    help="Session mode without Q3 — skips the order entirely")
    args = ap.parse_args(argv)

    api_key = os.environ.get("SHIOAJI_API_KEY")
    secret_key = os.environ.get("SHIOAJI_SECRET_KEY")
    if not api_key or not secret_key:
        print("ERROR: set SHIOAJI_API_KEY and SHIOAJI_SECRET_KEY in the "
              "environment (never commit them).", file=sys.stderr)
        return 2

    import shioaji as sj

    findings: dict = {}
    now = datetime.now()
    findings["ran_at"] = now.isoformat(timespec="seconds")
    findings["weekday"] = now.strftime("%A")
    findings["mode"] = args.mode
    findings["shioaji_version"] = getattr(sj, "__version__", "?")

    print("=" * 70)
    print(f"  §2.24 RE-LOGIN PROBE — SIMULATION — mode={args.mode}")
    print(f"  {now:%Y-%m-%d %H:%M:%S} ({now:%A})   shioaji "
          f"{findings['shioaji_version']}")
    print("=" * 70)

    api = sj.Shioaji(simulation=True)
    reported = getattr(api, "simulation", None)
    if reported is False:
        print("ABORT: api.simulation is False.", file=sys.stderr)
        return 2

    # Tech: collect the SDK's session events before the first login.
    # Why:  Q8. A "Session up" event is the only externally visible sign that a
    #       login actually *established* something rather than returning a cached
    #       handle. Registered before login #1 so the baseline is captured too.
    events: list = []

    def _on_event(resp_code, event_code, info, event):   # noqa: ANN001
        events.append({"resp": resp_code, "code": event_code,
                       "event": str(event), "info": _redact(str(info))[:120]})

    try:
        api.on_event(_on_event)
    except Exception as exc:                              # noqa: BLE001
        print(f"  ! on_event not registered: {exc!r}")

    # ---- Q6: does a login work at all, right now? -------------------------
    print("\n  Q6  login on a non-trading day")
    ok, value = call("login #1", _login(api, api_key, secret_key))
    findings["Q6_login_now"] = "GRANTED" if ok else f"REFUSED — {value}"
    if not ok:
        # Tech: a refused first login ends the probe, but it is still an answer.
        # Why:  Q6 asks precisely this. If a weekend session is refused, the
        #       scheduler must not burn its window twice a day against a closed
        #       venue — which is a design change, not a failure to measure.
        print(summarise(findings))
        return 0

    try:
        # ---- Q5 baseline --------------------------------------------------
        before_fp = account_fingerprint(getattr(api, "futopt_account", None))
        findings["Q5_fingerprint_before"] = before_fp.get("account_id_digest")

        # ---- Q4 baseline --------------------------------------------------
        ok, value = call("list_positions (before)", api.list_positions)
        findings["Q4_positions_before"] = (
            f"{len(value)} row(s)" if ok and value is not None else str(value))

        ok, value = call("usage (before)", api.usage)
        findings["Q7_usage_live_token"] = "ok" if ok else f"RAISED — {value}"

        if args.mode == "session":
            rc = _session_questions(api, sj, args, findings,
                                    api_key, secret_key)
            if rc:
                return rc
        else:
            _q8_login_forms(api, args, findings, api_key, secret_key, events)
            if args.watch_minutes:
                watch_connections(api, args.watch_minutes,
                                  args.watch_every, findings)

        # ---- Q5 after -----------------------------------------------------
        after_fp = account_fingerprint(getattr(api, "futopt_account", None))
        findings["Q5_fingerprint_after"] = after_fp.get("account_id_digest")
        findings["Q5_changed"] = (
            before_fp.get("account_id_digest") != after_fp.get("account_id_digest"))

        # ---- Q4 after -----------------------------------------------------
        ok, value = call("list_positions (after)", api.list_positions)
        findings["Q4_positions_after"] = (
            f"{len(value)} row(s)" if ok and value is not None else str(value))

        # ---- Q7: the destructive one, deliberately last -------------------
        # Tech: log out to invalidate the token, then call `usage()` and see
        #       whether it raises or answers.
        # Why:  the verify window of §2.24 calls `usage()` to prove the session
        #       is usable. If a dead token produces a quiet return rather than an
        #       exception, that window is inert — it would pass forever while the
        #       token is already gone, which is worse than not having it. Logout
        #       manufactures a dead token in seconds instead of waiting 24 h.
        print("\n  Q7  usage() on a DEAD token (logout first — nothing runs after)")
        call("logout", api.logout)
        ok, value = call("usage (after logout)", api.usage)
        findings["Q7_usage_dead_token"] = (
            f"RETURNED {_brief(value)} — INERT, use list_positions instead"
            if ok else f"raised {value} — GOOD, usage() is a valid check")
        ok2, value2 = call("list_positions (after logout)", api.list_positions)
        findings["Q7_list_positions_dead_token"] = (
            f"RETURNED {_brief(value2)}" if ok2 else f"raised {value2}")
    finally:
        try:
            api.logout()
        except Exception:
            pass

    print(summarise(findings))
    return 0


def _connections(api):
    # Tech: read `connections` off `usage()`, or None if it cannot be had.
    # Why:  Q8's instrument. §2.8b caps connections at **5 per person_id**, so a
    #       re-login that opens a connection without closing the old one is not
    #       merely untidy — two a day for five days would exceed the cap and the
    #       venue would start refusing the very logins that keep the run alive.
    try:
        return getattr(api.usage(), "connections", None)
    except Exception:                                     # noqa: BLE001
        return None


def _sessions_up(events) -> int:
    # Tech: count "Session up" events seen so far.
    # Why:  the only externally visible evidence that a login *established* a
    #       session rather than returning a cached handle. Matching on the event
    #       text is crude, but the SDK is compiled and exposes no session object.
    return sum(1 for e in events if "session up" in e.get("event", "").lower()
               or "session up" in e.get("info", "").lower())


def _q8_login_forms(api, args, findings, api_key, secret_key, events) -> None:
    # Tech: try each re-login form in turn, sampling connection count and session
    #       events either side of it.
    # Why:  **Q1 asked whether a second login succeeds; it does. Q8 asks whether
    #       it does anything.** The parameter is literally named `force_refresh`
    #       and defaults to False, which is exactly the shape of an SDK that
    #       returns a cached session when it can. If a plain re-login is a no-op,
    #       `ReloginScheduler` would call it faithfully twice a day and the run
    #       would still die at 24 h — the fix would be inert and would look like
    #       it was working, which is the worst outcome available.
    print("\n  Q1/Q8  what a second login actually does")
    baseline = _connections(api)
    n_events = _sessions_up(events)
    print(f"    baseline: connections={baseline}  sessions_up={n_events}")
    findings["Q8_connections_after_login1"] = baseline

    def sample(label):
        time.sleep(args.settle_seconds)
        conns, ups = _connections(api), _sessions_up(events)
        print(f"      -> connections={conns}  sessions_up={ups}")
        findings[label] = f"connections={conns} sessions_up={ups}"
        return conns

    deltas = {}
    prev = baseline

    for label, kw, q1_key, q8_key in (
        ("login #2 (plain)", {}, "Q1_plain", "Q8_after_plain"),
        ("login #3 (force_refresh=True)", {"force_refresh": True},
         "Q1_force_refresh", "Q8_after_force_refresh"),
    ):
        ok, value = call(label, _login(api, api_key, secret_key, **kw))
        findings[q1_key] = "ok" if ok else f"RAISED — {value}"
        conns = sample(q8_key)
        deltas[q1_key] = None if conns is None or prev is None else conns - prev
        prev = conns

    # Tech: sample after the logout *before* logging back in. This returns
    #       **None** against the real venue, and that is not a bug.
    # Why:  `_connections` reads `usage()`, and `usage()` needs authentication —
    #       after a logout it raises `AuthError`, so the connection count is
    #       simply not askable in that state (measured 2026-08-16, run 3).
    #       Kept because a `None` here is the honest answer: it says "logout's
    #       effect on connections is not observable by this instrument" instead
    #       of the inference run 2 invited. Q9 answers the question that actually
    #       matters — whether the count comes back — from the authenticated side.
    ok, _ = call("logout", api.logout)
    after_logout = _connections(api)
    print(f"      -> connections={after_logout} (after logout, before re-login)")
    findings["Q8_after_logout_only"] = str(after_logout)

    ok, value = call("login #4 (after logout)",
                     _login(api, api_key, secret_key))
    findings["Q1_logout_then_login"] = "ok" if ok else f"RAISED — {value}"
    conns = sample("Q8_after_logout_login")
    deltas["Q1_logout_then_login"] = (
        None if conns is None or prev is None else conns - prev)

    findings["Q8_deltas"] = ", ".join(
        f"{k.replace('Q1_', '')}={v:+d}" if isinstance(v, int) else f"{k}=?"
        for k, v in deltas.items())
    findings["Q8_verdict"] = _q8_verdict(deltas, baseline, after_logout)
    findings["Q8_session_events"] = _redact(json.dumps(events[-4:]))[:400]


def _q8_verdict(deltas, baseline, after_logout) -> str:
    # Tech: read the per-form connection deltas into advice about which login
    #       form the `relogin` closure should use.
    # Why:  a form that does not change the connection count did not open a
    #       session, and a login that returns a cached handle cannot have reset
    #       a token lifetime. This is the difference between a fix that works
    #       and one that is inert while printing healthy counters.
    parts = []
    plain = deltas.get("Q1_plain")
    forced = deltas.get("Q1_force_refresh")

    if plain == 0:
        parts.append("plain login is a NO-OP (no new connection) — do NOT use it")
    elif plain:
        parts.append(f"plain login opened {plain}")

    if forced:
        parts.append(f"force_refresh=True opened {forced} — USE THIS FORM")
    elif forced == 0:
        parts.append("force_refresh=True changed nothing either — no form works")

    # Tech: did `logout()` give the connection back?
    # Why:  §2.8b caps connections at 5 per person_id. If logout does not
    #       release, every re-login is a permanent -1 against that budget and
    #       twice-daily re-logins exhaust it in days — the fix killing the run it
    #       exists to protect.
    if after_logout is not None and baseline is not None:
        if after_logout >= baseline:
            parts.append(f"logout did NOT release (still {after_logout}) — "
                         f"connections leak; §2.8b caps at 5, so Q9 (do they "
                         f"come back on their own?) now decides the design")
        else:
            parts.append(f"logout released down to {after_logout}")
    return "; ".join(parts) or "inconclusive"


def watch_connections(api, minutes: float, every: float, findings) -> None:
    # Tech: sample `usage().connections` on a timer and print the series.
    # Why:  **Q9.** Run 2 left three connections open inside one process, and the
    #       *next* run started from one again — but that run had exited, and
    #       process death closes sockets. Whether a long-lived process gets them
    #       back is therefore unmeasured, and it decides whether re-login is
    #       survivable at all: at 2/day against a cap of 5, a leak that never
    #       clears kills a soak on day two or three.
    print(f"\n  Q9  do connections come back? sampling {minutes:.0f} min "
          f"every {every:.0f}s")
    series = []
    deadline = time.time() + minutes * 60
    while time.time() < deadline:
        conns = _connections(api)
        stamp = datetime.now().strftime("%H:%M:%S")
        series.append((stamp, conns))
        print(f"    {stamp}  connections={conns}")
        time.sleep(every)
    findings["Q9_series"] = ", ".join(f"{s}:{c}" for s, c in series)
    values = [c for _, c in series if c is not None]
    if len(values) >= 2:
        findings["Q9_verdict"] = (
            f"RECLAIMED {values[0]} -> {values[-1]} — a leak that clears itself"
            if values[-1] < values[0] else
            f"NOT reclaimed (held at {values[-1]}) — re-login spends a "
            f"connection permanently; §2.8b caps at 5")


def _session_questions(api, sj, args, findings, api_key, secret_key) -> int:
    # Tech: the five questions that need a live tape — subscription survival,
    #       order-callback identity, and the re-login forms in their presence.
    # Why:  separated so the off-hours path cannot accidentally subscribe or
    #       order, and so the risky half is one reviewable block.
    contract, note = resolve_txf(api, args.contract)
    print(f"\n  contract: {note}")

    ticks = {"n": 0}

    @api.on_tick_fop_v1()
    def _on_tick(tick):                                  # noqa: ANN001
        ticks["n"] += 1

    order_events = []

    def _on_order(state, msg):                           # noqa: ANN001
        order_events.append((datetime.now(), repr(state)[:40]))

    api.set_order_callback(_on_order)
    api.subscribe(contract, quote_type=sj.QuoteType.Tick)

    # ---- Q2 before ---------------------------------------------------------
    print(f"\n  Q2  counting ticks for {args.tick_seconds:.0f}s BEFORE re-login")
    time.sleep(args.tick_seconds)
    before = ticks["n"]
    print(f"    ticks before = {before}")
    if before == 0:
        print("    ! no ticks — the market looks closed; Q2/Q3 will be "
              "meaningless. Re-run inside a session.")
    findings["Q2_ticks_before"] = before

    # ---- Q3 before ---------------------------------------------------------
    if not args.no_order:
        _probe_order(api, sj, contract, args, order_events, findings, "before")

    # ---- the login under test ---------------------------------------------
    # Tech: log in again in whichever form `--relogin-form` selects, sampling the
    #       connection count either side.
    # Why:  the form is a *parameter* so the two runs are the same experiment
    #       with one variable changed, rather than two code paths that might
    #       differ in some other way.
    #
    #       `force_refresh` is the form the closure will use, and the Δ check
    #       guards against this test silently degrading into a no-op — an earlier
    #       draft used the plain form here and would have reported a confident
    #       "subscription survives" after a call that did nothing.
    #
    #       `plain` is the **control** (Q10). Q8 measured it as +0 connections;
    #       if callbacks survive it, then callbacks die with the *connection*,
    #       which is the mechanism the whole story rests on. If they die anyway,
    #       that story is wrong and a plain `login()` is dangerous too.
    form = args.relogin_form
    kw = {"force_refresh": True} if form == "force_refresh" else {}
    print(f"\n  Q1  re-login (form = {form})")
    conns_before = _connections(api)
    ok, value = call(f"login ({form})", _login(api, api_key, secret_key, **kw))
    findings[f"Q1_{form}"] = "ok" if ok else f"RAISED — {value}"
    time.sleep(args.settle_seconds)
    conns_after = _connections(api)
    print(f"    connections {conns_before} -> {conns_after}")
    findings["relogin_form"] = form
    findings["connections_across_login"] = f"{conns_before} -> {conns_after}"

    grew = (conns_before is not None and conns_after is not None
            and conns_after > conns_before)
    if form == "force_refresh":
        findings["Q2_relogin_was_real"] = (
            "YES" if grew else
            f"UNCONFIRMED ({conns_before} -> {conns_after}) — Q2/Q3 below may "
            f"be measuring a no-op")
    else:
        findings["Q10_was_a_noop"] = (
            "NO — it opened a connection, so this is not the control it was "
            "meant to be" if grew else "YES (+0 connections, as Q8 measured)")

    # ---- Q2 after ----------------------------------------------------------
    print(f"\n  Q2  counting ticks for {args.tick_seconds:.0f}s AFTER re-login "
          "(no resubscribe)")
    ticks["n"] = 0
    time.sleep(args.tick_seconds)
    after = ticks["n"]
    print(f"    ticks after = {after}")
    findings["Q2_ticks_after"] = after
    findings["Q2_subscription_survives"] = (
        "YES" if after > 0 else "NO — the CLI closure must resubscribe")

    # ---- Q3 after ----------------------------------------------------------
    if not args.no_order:
        before_n = len(order_events)
        _probe_order(api, sj, contract, args, order_events, findings, "after")
        added = len(order_events) - before_n
        findings["Q3_callbacks_after_relogin"] = added
        findings["Q3_verdict"] = _q3_verdict(
            findings.get("Q3_callbacks_before"), added)

    if form == "plain":
        findings["Q10_verdict"] = _q10_verdict(
            before, after, findings.get("Q3_callbacks_before"),
            findings.get("Q3_callbacks_after_relogin"))
    return 0


def _q10_verdict(ticks_before, ticks_after, cb_before, cb_after) -> str:
    # Tech: read the control run — did a *no-op* login disturb anything?
    # Why:  this is the fork the mechanism story turns on. If a login that opens
    #       no connection also breaks nothing, callbacks die with the connection
    #       object, and only session-replacing operations are dangerous. If it
    #       breaks them anyway, the connection story is wrong, every `login()` is
    #       destructive, and the re-assertion has to be unconditional.
    survived_ticks = bool(ticks_after)
    survived_cb = cb_after is None or bool(cb_after)
    if survived_ticks and survived_cb:
        return ("plain login disturbs NOTHING (ticks "
                f"{ticks_before}->{ticks_after}, callbacks {cb_before}->{cb_after})"
                " — callbacks die with the CONNECTION, so only session-replacing"
                " logins need re-assertion")
    return ("plain login BROKE something anyway (ticks "
            f"{ticks_before}->{ticks_after}, callbacks {cb_before}->{cb_after})"
            " — the connection-object story is WRONG; treat every login() as"
            " destructive and re-assert unconditionally")


def _q3_verdict(before, after) -> str:
    # Tech: compare callback counts either side of the re-login.
    # Why:  this is the finding the whole probe exists to protect — a duplicated
    #       order callback double-counts every fill, which would corrupt the
    #       position mirror and the PnL at once, and it would look like a venue
    #       bug rather than ours.
    if not before:
        return "inconclusive — no baseline callbacks"
    if after == 0:
        return "DROPPED — the CLI closure must re-register set_order_callback"
    if after > before:
        return f"DUPLICATED ({before} -> {after}) — must clear before re-register"
    return "survives, not duplicated"


def _probe_order(api, sj, contract, args, order_events, findings, phase) -> None:
    # Tech: place one passive BUY far below the market, watch the callback,
    #       cancel it.
    # Why:  the only way to observe the order callback is to give it an order.
    #       The safety argument is `v72_probe`'s, reused rather than restated:
    #       one lot, strictly below the touch so it cannot fill, cancelled in a
    #       `finally`.
    print(f"\n  Q3  order callback — {phase} re-login")
    try:
        snaps = api.snapshots([contract])
        reference = float(getattr(snaps[0], "close", 0)) or None
    except Exception as exc:                              # noqa: BLE001
        print(f"    ! snapshots failed: {exc!r}")
        reference = None
    if not reference:
        print("    ! no reference price — skipping the order.")
        return

    limit = probe_limit_price(reference, args.offset_points)
    if limit >= reference:
        print("    ABORT: computed limit is not below the market.")
        return
    print(f"    reference={reference} -> BUY limit={limit} (passive)")

    n_before = len(order_events)
    trade = None
    try:
        # Tech: the exact order form `broker/shioaji.py` uses — `sj.FuturesOrder`
        #       with the top-level enums, not `api.Order` with `sj.constant.*`.
        # Why:  these literals were confirmed against the venue by the §7.2
        #       probe, and the adapter has since placed real simulation orders
        #       with them. A probe that invents its own form would be measuring
        #       a code path the live system never takes.
        order = sj.FuturesOrder(
            action=sj.Action.Buy,
            price=limit,
            quantity=1,
            price_type=sj.FuturesPriceType.LMT,
            order_type=sj.OrderType.ROD,
            octype=sj.FuturesOCType.New,
            account=api.futopt_account,
        )
        trade = api.place_order(contract, order)
        time.sleep(5.0)
    except Exception as exc:                              # noqa: BLE001
        print(f"    ! place_order raised: {exc!r}")
    finally:
        if trade is not None:
            try:
                api.update_status(api.futopt_account)
                api.cancel_order(trade)
                time.sleep(2.0)
                print("    cancelled.")
            except Exception as exc:                      # noqa: BLE001
                print(f"    !! CANCEL FAILED: {exc!r} — CHECK THE FRONT END")

    n = len(order_events) - n_before
    print(f"    callbacks seen: {n}")
    if phase == "before":
        findings["Q3_callbacks_before"] = n


if __name__ == "__main__":
    sys.exit(main())
