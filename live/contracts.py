"""Contract resolution — the one place that knows how to reach a TXF contract.

This module exists because of a live failure (2026-08-11, §2.18). `api.Contracts`
is deprecated in favour of `api.contracts`, so the three entry points were
switched — after "verifying" that `sj.Contracts` exposes `Futures`. It does. But
**`api.contracts` does not return a `sj.Contracts`**; it returns a
`ContractsApi`, a different type whose surface is
`futures / futures_by_underlying / futures_roots / get / list / options / …` and
which has no `Futures` at all. The run died at start-up:

    ERROR: AttributeError("'builtins.ContractsApi' object has no attribute 'Futures'")

Two lessons are encoded here rather than remembered:

  * **Introspect the object you will actually call, not one that shares its
    name.** Checking a class in the module namespace proved nothing about what an
    attribute on a live session returns.
  * **A start-up path must not die on an SDK-shape guess.** These runs are bound
    to market hours; a wrong attribute costs a session that cannot be re-run.
    Resolution therefore tries several routes and only fails if *all* of them do.

Route order is deliberate and inverts the usual "prefer the new API" instinct:
the **deprecated** path goes first, because it is the only one with evidence
behind it — four live runs resolved `TXFR1` through it. A deprecation warning is
noise; a failed start-up is a lost session. The `api.contracts` routes follow as
fallbacks, so the day `Contracts` is removed the code still finds a way, and the
route actually used is reported so a live run tells us which of them work.
"""
from __future__ import annotations

import re
from typing import Any, Callable, List, Tuple

# Tech: the shape of a continuous-alias code — `TXFR1`, `TXFR2`.
# Why:  an alias carries a `delivery_month` copied from the dated contract it
#       points at, so it is indistinguishable from a real month by that field
#       alone. Matching the code is the only way to tell a pointer from a thing.
_ALIAS = re.compile(r"^[A-Z]{3}R\d+$")

# Tech: candidate ways to reach the TXF futures root, most-trusted first.
# Why:  the first entry is PROVEN (four live runs). The rest are unverified
#       shapes read off `ContractsApi`'s attribute list and are here only so a
#       future SDK that drops `Contracts` degrades instead of dying. Each is a
#       lambda so nothing is evaluated until it is tried.
ROUTES: Tuple[Tuple[str, Callable[[Any], Any]], ...] = (
    ("api.Contracts.Futures.TXF", lambda api: api.Contracts.Futures.TXF),
    ("api.contracts.futures.TXF", lambda api: api.contracts.futures.TXF),
    ("api.contracts.futures['TXF']", lambda api: api.contracts.futures["TXF"]),
)


def resolve_in(container: Any, name: str) -> Any:
    """Find `name` in a contracts container by attribute, then by key."""
    # Tech: try attribute access then item access, swallowing KeyError and
    #       TypeError as well as AttributeError; return None if neither works.
    # Why:  MEASURED — `getattr(container, name, default)` does NOT protect: the
    #       container's `__getattr__` is a dict lookup that raises **KeyError**,
    #       which getattr's three-argument default does not suppress. Both lookup
    #       styles are attempted because an alias (`TXFR1`) and an explicit month
    #       (`TXF202608`) are not necessarily reachable the same way.
    for access in (lambda: getattr(container, name), lambda: container[name]):
        try:
            found = access()
        except (AttributeError, KeyError, TypeError):
            continue
        if found is not None:
            return found
    return None


def contract_codes(contract: Any) -> set:
    """Every code that legitimately identifies this contract object."""
    # Tech: the object's own `code` plus its `target_code`, when present.
    # Why:  MEASURED (§7.2) — an alias reports `code='TXFR1'` while every tick,
    #       fill and position the venue sends back is stamped with the *resolved*
    #       month, `TXFH6`. Comparing on `code` alone therefore reports a
    #       mismatch between a contract and *itself*. `ShioajiBroker` learned this
    #       the expensive way (it would have halted the loop on the first real
    #       trade) and seeds its accepted-code set from here.
    return {str(c) for c in (getattr(contract, "code", None),
                             getattr(contract, "target_code", None)) if c}


def month_code(contract: Any) -> str:
    """The dated code of the month this contract trades, e.g. `'TXFK6'`."""
    # Tech: `target_code` when the object is an alias, else its own `code`.
    # Why:  the owner places each deployment's order on the month that
    #       deployment holds (2026-10-20, simtrade_operations §11), so the two
    #       processes need one name for a month that survives the wire. The alias
    #       cannot be it — `TXFR1` names October on one side of a roll and
    #       November on the other — and the dated code is what `resolve_txf`
    #       accepts (`TXF202611` does not resolve; `TXFK6` does).
    return str(getattr(contract, "target_code", None)
               or getattr(contract, "code", None) or "")


# Tech: a dated futures code — root, month letter A (January) to L (December),
#       last digit of the year: `TXFK6` is November 2026.
# Why:  the month has to be read off the code alone when the contract object is
#       gone: an expired month drops out of the contract table, and a ledger row
#       or an intent carries only the code.
_DATED = re.compile(r"^[A-Z]{3}([A-L])(\d)$")


def code_month(code: Any, today: Any) -> str:
    """The delivery month a dated code names, e.g. `'TXFK6'` -> `'202611'`.

    `''` for anything that is not a dated code, such as the alias `TXFR1`.
    """
    # Tech: decode the month letter, then take the year ending in the code's
    #       digit that lies within five years of `today`.
    # Why:  the code carries one digit of year. TXF lists months about a year
    #       out, so the nearest decade is the only one a held or listed month
    #       can be in.
    m = _DATED.match(str(code or ""))
    if m is None:
        return ""
    month = ord(m.group(1)) - ord("A") + 1
    digit = int(m.group(2))
    year = next(y for y in range(today.year - 5, today.year + 5)
                if y % 10 == digit)
    return f"{year}{month:02d}"


def describe_contract_match(contract: Any, captured: Any) -> Tuple[str, bool]:
    """Compare a resolved contract against codes seen in a capture."""
    # Tech: return a human note and whether this is a genuine mismatch — genuine
    #       meaning the capture holds codes and *none* of them identifies this
    #       contract.
    # Why:  a false "mismatch" here is worse than no check at all: it labels a
    #       valid diff "meaningless" and invites a good result to be thrown away.
    #       The first version compared `code` only and did exactly that.
    codes = contract_codes(contract)
    seen = sorted({str(c) for c in captured if c})
    note = f"contract codes {sorted(codes)}; capture holds {seen or ['(none)']}"
    if seen and not (codes & set(seen)):
        return note + "  ** MISMATCH — different contract, diff is meaningless **", True
    return note, False


def txf_root(api: Any) -> Tuple[Any, str]:
    """Return `(container, route_label)` for the TXF futures root."""
    # Tech: walk ROUTES in order, returning the first that yields a non-None
    #       container; collect what each failure was for the error message.
    # Why:  §2.18 — the caller must never see a bare AttributeError from one
    #       guessed attribute. If every route fails, the exception names all of
    #       them and what each raised, which is the information needed to fix it
    #       without another live run.
    tried: List[str] = []
    for label, get in ROUTES:
        try:
            root = get(api)
        except Exception as exc:                      # noqa: BLE001 — see Why
            tried.append(f"{label} -> {type(exc).__name__}: {exc}")
            continue
        if root is None:
            tried.append(f"{label} -> None")
            continue
        return root, label
    raise RuntimeError(
        "could not reach the TXF futures contracts. Tried:\n  "
        + "\n  ".join(tried)
    )


def resolve_txf(api: Any, name: str = "TXFR1") -> Tuple[Any, str]:
    """Return `(contract, description)` for a TXF contract such as `TXFR1`."""
    # Tech: find the root, then the named contract within it; report the route
    #       and the resolved code together.
    # Why:  the resolved code is not the name asked for — `TXFR1` is an alias and
    #       fills, positions and callbacks come back stamped with the *resolved*
    #       month (`TXFH6`). Surfacing both at start-up is what made that
    #       mismatch visible the first time (§7.2), so the description carries it.
    root, route = txf_root(api)
    contract = resolve_in(root, name)
    if contract is None:
        raise RuntimeError(f"contract {name!r} not found via {route}")
    code = getattr(contract, "code", "?")
    month = getattr(contract, "delivery_month", "?")
    return contract, f"{name} -> code={code} delivery_month={month} (via {route})"


def contract_month(contract: Any) -> str:
    """The contract's delivery month, e.g. `'202608'`; `''` when it has none."""
    # Tech: read `delivery_month` and stringify it, mapping a missing attribute
    #       and an explicit None onto the same empty string.
    # Why:  the roll is decided by comparing this value across two reads of the
    #       same alias, so it has to be a plain comparable scalar rather than
    #       whatever the SDK happens to box it in. Empty-on-missing keeps a
    #       duck-typed fake or a future shape change from raising inside a
    #       liveness check whose whole job is to keep the run alive.
    month = getattr(contract, "delivery_month", None)
    return "" if month is None else str(month)


def dated_months(root: Any) -> List[Any]:
    """Every dated month contract under a futures root, nearest delivery first."""
    # Tech: enumerate the container (falling back to attribute access when it is
    #       not iterable), drop the `R`-aliases, and sort by delivery month.
    # Why:  the backfill needs the month *behind* the front one to splice a
    #       continuous series across a roll, and the alias cannot express "the
    #       previous one". Aliases are excluded because `TXFR1` duplicates a
    #       dated month it merely points at — counting it would make the same
    #       contract appear twice and could sort a pointer ahead of its target.
    #       Duck-typed enumeration for the reason `v1c_check` uses it: the
    #       concrete container type is unverified and must not be guessed (§2.18).
    try:
        items = list(root)
    except TypeError:
        items = [getattr(root, k) for k in dir(root) if not k.startswith("_")]
    out = []
    for c in items:
        code = getattr(c, "code", None)
        month = contract_month(c)
        if code is None or not month:
            continue
        if _ALIAS.match(str(code)):
            continue
        out.append(c)
    return sorted(out, key=contract_month)
