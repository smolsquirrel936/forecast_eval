# [param_override.py](../param_override.py)

**Role:** Coerces `--param KEY=VALUE` strings into the types a strategy package's `PARAMS` declares.
**Pipeline:** CLI strings → **param_override** → `make_strategy(**overrides)`.
**Depends on:** nothing (stdlib only).
**Used by:** [real_data.py](real_data.md) *(live/cli.py wires it in Phase 4 of the routing plan; not yet)*

## Responsibility
One function. It exists so a knob typed at the backtest CLI and the same knob
typed at the live CLI are coerced by identical rules, and so a parameter a search
*invents* is reachable from a command line the day it lands in `PARAMS` —
without either parser learning its name.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `parse_overrides(pairs, params)` | function | `["k=v", …]` + a package's `PARAMS` → `{knob: typed value}` |

## The coercion table
`params` supplies the target types only — there are no annotations to read, so a
knob's **declared default is its contract**.

| declared default | accepted | rejected |
|---|---|---|
| `bool` | `true/1/yes`, `false/0/no` (any case) | anything else |
| `int` | an integer literal | `3008.5` — refused, never truncated |
| `float` | any number | a word |
| `str` | anything, verbatim | — |
| `None` | an int, then a float | a word (the type is unknown) |
| anything else | — | rejected: not expressible on a command line |

`none` (any case) yields `None` for **any** knob, not only ones defaulting to
None. `PARAMS` cannot express optionality — `min_prob` defaults to `0.5` in the
night package and to `None` in `real_data.py`'s CLI table — and disabling a
probability gate is a legitimate request. Whether a given knob tolerates `None`
is the strategy's business, and it raises on its own if it does not.

> **Why booleans get a table.** `bool("false")` is `True`; every non-empty string
> is. `forced_close` decides whether positions close at the session boundary, so
> a boolean silently flipping is a strategy change, not a formatting one. This is
> the one branch where the obvious implementation is wrong, and
> [tests/test_param_override.py](tests.md) pins every literal.

## Unknown names are forwarded, not rejected
A key the package does not declare is passed through **uncoerced**. That is
deliberate: [strategy/wiring.py](strategy/wiring.md)'s `make_strategy` already
raises `unknown strategy override(s) [...]; this package declares [...]`, which
names the real knob set. Duplicating the check here would produce a worse message
from a module that knows less.

## Why not generated flags
Generating a real argparse flag per knob would give `--help` listings and
completion, but the parser would have to import the package before parsing — and
the package is chosen *by* a flag. That is two-pass parsing for cosmetics. The
trade is that argparse cannot catch a typo, which is what the forwarding rule
above recovers.

## Related
[LIVE_STRATEGY_ROUTING_PLAN.md](../LIVE_STRATEGY_ROUTING_PLAN.md) D6/D7 ·
[strategy/wiring.md](strategy/wiring.md) · [real_data.md](real_data.md)
