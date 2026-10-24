# [live/contracts.py](../../live/contracts.py)

**Role:** The one place that knows how to reach a TXF contract — and what counts as "the same contract".
**Pipeline:** every entry point → **resolve_txf** → a contract object handed to `subscribe` / `kbars` / `place_order`.
**Depends on:** — (pure; imports no `shioaji`)   **Used by:** [live/cli.py](cli.md), [live/v1_check.py](v1_check.md), [live/v1c_check.py](v1c_check.md), [live/v72_probe.py](v72_probe.md), [live/broker/shioaji.py](broker/shioaji.md)

## Responsibility
Two small jobs, both of which exist because getting them wrong has already cost
something real.

**1. Reaching the contracts container.** `api.Contracts` is deprecated in favour
of `api.contracts` — but the two return *different types*. `api.contracts` gives
a `ContractsApi` (`futures / futures_by_underlying / futures_roots / get / list`)
with **no `Futures`**, and switching to it killed a start-up on a market window
that could not be re-run (§2.18).

**2. Deciding what "our contract" means.** `TXFR1` is an alias: the object reports
`code='TXFR1'` while every tick, fill and position the venue sends back is stamped
with the *resolved* month, `TXFH6`. Comparing on `code` alone reports a contract
as different from itself.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `resolve_txf(api, name="TXFR1")` | function | `(contract, description)`; the description carries the resolved code |
| `txf_root(api)` | function | `(container, route_label)` for the TXF futures root |
| `resolve_in(container, name)` | function | attribute lookup, then key lookup; `None` if neither works |
| `contract_codes(contract)` | function | every code identifying this contract — `code` **and** `target_code` |
| `describe_contract_match(contract, captured)` | function | `(note, is_mismatch)` against codes seen in a capture |
| `contract_month(contract)` | function | `delivery_month` as a plain string; `''` when it has none |
| `month_code(contract)` | function | the dated code of the month traded (`TXFK6`): `target_code` for an alias, else `code`. The month's name on the owner wire. `resolve_txf` accepts it, while `TXF202611` does not resolve |
| `code_month(code, today)` | function | the delivery month a dated code names (`TXFK6` → `202611`): month letter A–L, year digit taken in the decade nearest `today`. `''` for an alias or anything not dated. Works on a code whose contract has left the table |
| `dated_months(root)` | function | every dated month under a root, nearest delivery first — **aliases excluded** |
| `ROUTES` | constant | the ordered candidates below |

## The alias is a pointer, and `dated_months` drops it
`TXFR1` carries a `delivery_month` copied from the month it points at, so it is
indistinguishable from a real contract by that field alone — matching the *code*
(`^[A-Z]{3}R\d+$`) is the only way to tell a pointer from a thing. Counting it
would make the same contract appear twice and could sort a pointer ahead of its
target.

The [roll](roll.md) needs both halves of this: `contract_month` is what the roll
is *detected* by, and `dated_months` is how the month behind the front one is
reached — the alias cannot express "the previous one", and a spliced warm-up
needs exactly that.

## Route order — deliberately "worst" first
| # | Route | Status |
|---|---|---|
| 1 | `api.Contracts.Futures.TXF` | **PROVEN** — five live runs |
| 2 | `api.contracts.futures.TXF` | unverified fallback |
| 3 | `api.contracts.futures['TXF']` | unverified fallback |

This inverts the usual "prefer the new API" instinct on purpose. The deprecated
route is the only one with evidence behind it, and these runs are bound to market
hours: **a deprecation warning is noise, a failed start-up is a lost session.**
The `api.contracts` routes are fallbacks so that the day `Contracts` is removed
the code degrades instead of dying — their inner layout is a guess and is
labelled as one in the source.

## Key invariants / gotchas
- **Resolution never raises a bare `AttributeError` from one guessed attribute.**
  If every route fails, the `RuntimeError` names each route and what it raised —
  enough to fix it without spending another live session finding out.
- **The route used is returned, not hidden**, and entry points print it. That is
  how a live run tells us whether the v2 routes work.
- **The missing deprecation warning proves nothing.** Python shows
  `DeprecationWarning` by default only from `__main__`; the call now lives here,
  so the warning is filtered. We are still on route 1.
- **`getattr(container, name, default)` does not protect.** The container's
  `__getattr__` is a dict lookup raising **KeyError**, which the three-argument
  default does not suppress — so `resolve_in` catches `KeyError` and `TypeError`
  as well. Both lookup styles are tried because an alias (`TXFR1`) and an explicit
  month (`TXF202608`) are not necessarily reachable the same way.
- **`contract_codes` is the single source for the alias rule.**
  [ShioajiBroker](broker/shioaji.md) seeds its accepted-code set from it and
  [v1_check](v1_check.md) compares through it. Two copies drifted once already —
  the broker had the fix and `v1_check` reintroduced the bug thirty lines away.
- **A false mismatch is worse than no check.** It labels a valid result
  meaningless and trains the reader to ignore the warning for the time it is
  real, so `describe_contract_match` only flags when the capture holds codes and
  *none* of them identifies the contract.

## Related
[live/cli.py](cli.md) · [live/broker/shioaji.py](broker/shioaji.md) · [live/v1_check.py](v1_check.md) · [SHIOAJI_PLAN.md §2.18](../../SHIOAJI_PLAN.md)
