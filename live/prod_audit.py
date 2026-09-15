"""Production audit — did anything we did in "simulation" reach the real account?

**This is the only module in the repo that logs in with `simulation=False`, and
it exists for one reason: everything else cannot answer this question.** The SDK
exposes no way to confirm the mode (§7.2 — `api.simulation` is absent before
login and `None` after), so `[SIMULATION]` in every log line records our
*intent*. The only independent evidence is the production account itself.

Read-only, structurally. It queries positions, trades and settled P&L and
prints them. It imports no order type, constructs no order, and calls no
order API — `tests/test_live_simulation_guard.py` asserts that mechanically, and
that this is the *only* module permitted `simulation=False`.

### Two possible outcomes, and the first one is better

**1. The login is refused for lack of production permission.** MEASURED
2026-08-11: `Token doesn't have production permission.` This is the *strongest*
result available — the credential is scoped to simulation by the venue, so no
order placed with it can reach a real account regardless of what the client
passes. It is enforced server-side, holds for every session rather than the one
tested, and makes the before/after comparison unnecessary. The tool reports it as
a pass and stops.

**2. The login succeeds.** Then the key *can* reach production, the client-side
`simulation=` argument is the only thing keeping orders out of it, and the
before/after comparison below is the way to check that it did.

### Use it as a before/after pair (outcome 2 only)

A production position on its own proves nothing — the account may hold positions
for reasons that have nothing to do with us. What is diagnostic is a **change
across a simulated session**:

    # before the simulated run
    uv run --no-sync python -m forecast_eval.live.prod_audit --i-am-querying-production --baseline

    # ... run the session with --broker shioaji ...

    # after
    uv run --no-sync python -m forecast_eval.live.prod_audit --i-am-querying-production

Positions alone are not enough either: an order that opened *and closed* leaves a
net position of zero. Trades and settled P&L are therefore compared too, since
those record the event rather than its residue.

A clean result means: simulated orders left no trace in the real account, for the
one session tested. That is the strongest statement available from this side, and
it is worth having before the ≥5 sessions V4 needs rather than after.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

DEFAULT_STATE = Path("outputs") / "prod_audit_baseline.json"


def _rows(value: Any) -> List[dict]:
    # Tech: coerce whatever the SDK returns into a list of plain dicts.
    # Why:  the concrete row types are unverified and differ per call; the audit
    #       only needs them to be comparable and printable, and `dict()`/`__dict__`
    #       covers both the pydantic-ish and the plain-object shapes without this
    #       module having to know which it got.
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        value = [value]
    out = []
    for row in value:
        if isinstance(row, dict):
            out.append(dict(row))
            continue
        for accessor in ("dict", "_asdict"):
            fn = getattr(row, accessor, None)
            if callable(fn):
                try:
                    out.append(dict(fn()))
                    break
                except Exception:
                    pass
        else:
            out.append({k: str(v) for k, v in vars(row).items()}
                       if hasattr(row, "__dict__") else {"repr": repr(row)})
    return out


def snapshot(api: Any, account: Any, *, days: int = 7) -> Dict[str, Any]:
    """Everything read-only that would show a real order having happened."""
    # Tech: query positions, trades and settled P&L, tolerating failures per
    #       call and recording the error instead of raising.
    # Why:  a permission gap on one query must not hide the other two. An audit
    #       that dies on `list_profit_loss` would report nothing about positions,
    #       which is the answer we came for.
    end = date.today()
    start = end - timedelta(days=days)
    out: Dict[str, Any] = {"taken_at": datetime.now().isoformat()}
    queries = {
        "positions": lambda: api.list_positions(account),
        "trades": lambda: api.list_trades(),
        "profit_loss": lambda: api.list_profit_loss(
            account, start.isoformat(), end.isoformat()),
    }
    for name, call in queries.items():
        try:
            out[name] = _rows(call())
        except Exception as exc:                      # noqa: BLE001 — see Why
            out[name] = {"error": f"{type(exc).__name__}: {exc}"}
    return out


def compare(before: Dict[str, Any], after: Dict[str, Any]) -> List[str]:
    """Differences that would indicate a 'simulated' order reached production."""
    # Tech: compare counts per section; a section that errored on either side is
    #       reported as unknown rather than as equal.
    # Why:  counting is enough to answer "did anything appear?", and it avoids
    #       inventing an identity for row shapes this module deliberately does
    #       not model. An errored query must never read as "no change" — that is
    #       the one wrong answer, since it would clear the account on no evidence.
    findings = []
    for key in ("positions", "trades", "profit_loss"):
        b, a = before.get(key), after.get(key)
        if not isinstance(b, list) or not isinstance(a, list):
            findings.append(f"{key}: UNKNOWN — a query failed, cannot compare")
            continue
        if len(a) != len(b):
            findings.append(f"{key}: {len(b)} -> {len(a)}  ** CHANGED **")
    return findings


# Tech: the substring identifying a token that is not permitted to reach
#       production at all.
# Why:  MEASURED 2026-08-11 — `simulation=False` returned
#       `BadRequestError: StatusCode: 400, Detail: Token doesn't have production
#       permission.` Matched on the message rather than the exception class so
#       this module needs no `shioaji` import at module scope, consistent with
#       every other duck-typed SDK boundary here.
NO_PRODUCTION_PERMISSION = "production permission"


def is_simulation_only_token(error: BaseException) -> bool:
    """Was this login refused *because the credential cannot reach production*?"""
    # Tech: substring match on the error text.
    # Why:  this is the best possible outcome of the audit and must not be
    #       mistaken for a failure. A token the server refuses to admit to
    #       production cannot place a production order — that is scope enforced
    #       at the venue, which is strictly stronger than any client-side flag
    #       we could pass or check.
    return NO_PRODUCTION_PERMISSION in str(error).lower()


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--i-am-querying-production", action="store_true",
                    help="Required. This logs in with simulation=False. It only "
                         "reads, but it is the real account.")
    ap.add_argument("--baseline", action="store_true",
                    help="Record the current state as the 'before' snapshot")
    ap.add_argument("--state", type=Path, default=DEFAULT_STATE,
                    help=f"Baseline file (default {DEFAULT_STATE})")
    ap.add_argument("--days", type=int, default=7,
                    help="How far back to request settled P&L")
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if not args.i_am_querying_production:
        print("Refusing: this queries the PRODUCTION account. Re-run with "
              "--i-am-querying-production.", file=sys.stderr)
        return 2

    api_key = os.environ.get("SHIOAJI_API_KEY")
    secret_key = os.environ.get("SHIOAJI_SECRET_KEY")
    if not api_key or not secret_key:
        print("ERROR: set SHIOAJI_API_KEY and SHIOAJI_SECRET_KEY.",
              file=sys.stderr)
        return 2

    import shioaji as sj
    # Tech: the one production session in the repo.
    # Why:  the whole point — a simulation session cannot see the real account,
    #       so it cannot answer whether a "simulated" order reached it. Isolated
    #       in this module so `tests/test_live_simulation_guard.py` can keep every
    #       *other* construction pinned to simulation=True.
    print("*** PRODUCTION SESSION (read-only) ***")
    api = sj.Shioaji(simulation=False)
    try:
        try:
            api.login(api_key=api_key, secret_key=secret_key)
        except Exception as exc:                      # noqa: BLE001 — see Why
            # Tech: a login refused for lack of production permission is the
            #       best outcome this tool can report, not an error.
            # Why:  it means the credential is *scoped* to simulation by the
            #       venue. A token that cannot be admitted to production cannot
            #       place a production order, whatever the client passes or
            #       mis-passes — which is a stronger guarantee than the
            #       before/after comparison below was ever going to provide, and
            #       one that holds for every session rather than the one tested.
            if not is_simulation_only_token(exc):
                raise
            print(f"\nlogin refused: {exc}")
            print("\nVERDICT: THE CREDENTIAL CANNOT REACH PRODUCTION.")
            print("  The venue refuses this token a production session at all,")
            print("  so no order placed with it can reach a real account —")
            print("  enforced server-side, not by our simulation= argument.")
            print("  This supersedes the before/after comparison: there is no")
            print("  production account for this key to leave a trace in.")
            print("\n  Re-run this check if the API key is ever reissued or")
            print("  its permissions changed — the guarantee lives in the")
            print("  credential now, not in the code.")
            return 0
        account = getattr(api, "futopt_account", None)
        print(f"futopt_account: {account!r}")
        current = snapshot(api, account, days=args.days)
    finally:
        try:
            api.logout()
        except Exception:
            pass

    for key in ("positions", "trades", "profit_loss"):
        value = current[key]
        if isinstance(value, dict):
            print(f"  {key:12s} QUERY FAILED — {value['error']}")
        else:
            print(f"  {key:12s} {len(value)} row(s)")
            for row in value[:10]:
                print(f"      {row}")

    if args.baseline:
        args.state.parent.mkdir(parents=True, exist_ok=True)
        args.state.write_text(json.dumps(current, indent=2, default=str),
                              encoding="utf-8")
        print(f"\nbaseline written: {args.state}")
        return 0

    try:
        before = json.loads(args.state.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print(f"\nNo baseline at {args.state} — run with --baseline *before* the "
              f"simulated session. Without a before/after pair this is a "
              f"snapshot, not evidence: a position here may predate anything we "
              f"did.", file=sys.stderr)
        return 3

    findings = compare(before, current)
    print(f"\nbaseline taken {before.get('taken_at')}")
    if not findings:
        print("VERDICT: production unchanged — the simulated session left no "
              "trace in the real account.")
        return 0
    print("VERDICT: PRODUCTION CHANGED — investigate before trading again.")
    for line in findings:
        print(f"  {line}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
