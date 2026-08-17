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

from typing import Any, Callable, List, Tuple

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
