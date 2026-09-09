"""`--param KEY=VALUE` parsing for the strategy packages' declared knobs.

One function, shared by `real_data.py` (backtest) and `live/cli.py` (live), so a
knob typed at either CLI is coerced by exactly the same rules. The values land in
`strategy.wiring.make_strategy(**overrides)`, which is already a parameterised
entry point: it merges them over the package's `PARAMS` and rejects any name the
package does not declare.

Deliberately NOT generated argparse flags. Building a flag per knob means
importing the package before parsing, and the package is chosen *by* a flag —
two-pass parsing for cosmetics. The cost of this choice is that argparse cannot
catch a typo, which is why unknown names are forwarded rather than rejected here
(see `parse_overrides`).

LIVE_STRATEGY_ROUTING_PLAN D7.
"""
from typing import Any, Dict, Iterable, Mapping

# Tech: the literals accepted for a knob whose declared default is a bool.
# Why:  `bool("false")` is **True** — every non-empty string is. A knob set to
#       `false` on the command line silently becoming True is the single most
#       likely way this module could quietly corrupt a run, so booleans get an
#       explicit table and everything outside it is an error rather than a guess.
_TRUE = {"true", "1", "yes"}
_FALSE = {"false", "0", "no"}


def _coerce(knob: str, raw: str, default: Any) -> Any:
    # Tech: turn one raw string into the type the package's declared default
    #       implies, raising with the knob's name on anything ambiguous.
    # Why:  `PARAMS` is the only type information available — there are no
    #       annotations to read — so the default's type *is* the contract. Every
    #       branch below either returns a value of that type or raises; nothing
    #       falls through to a best guess, because a silently mistyped knob
    #       produces a plausible-looking run with the wrong strategy in it.
    if raw.lower() == "none":
        # Tech: `none` is accepted for any knob, not only those defaulting to None.
        # Why:  `PARAMS` cannot express optionality — `min_prob` defaults to 0.5
        #       in the night package and to None in the CLI table, and disabling
        #       the probability gate on a package that declares a float is a
        #       legitimate thing to ask for. Which knobs tolerate None is the
        #       strategy's business, and it will raise on its own if one does not.
        return None

    # Tech: bool BEFORE int.
    # Why:  `bool` is a subclass of `int`, so `isinstance(True, int)` is True and
    #       an int-first ordering would send every boolean knob down the int
    #       branch, where `int("true")` raises and `int("1")` returns 1 rather
    #       than True.
    if isinstance(default, bool):
        if raw.lower() in _TRUE:
            return True
        if raw.lower() in _FALSE:
            return False
        raise ValueError(
            f"--param {knob}={raw!r}: expected a boolean "
            f"({'/'.join(sorted(_TRUE | _FALSE))})"
        )

    if default is None:
        # Tech: no declared default means no type to read; try int, then float.
        # Why:  the knobs that default to None here are numeric in practice
        #       (`max_aggression_ticks` is a tick count). Guessing `str` instead
        #       would accept anything and defer the failure to somewhere far from
        #       the typo, so an unparseable value is refused with its reason.
        for cast in (int, float):
            try:
                return cast(raw)
            except ValueError:
                pass
        raise ValueError(
            f"--param {knob}={raw!r}: the package declares no default for "
            f"{knob!r}, so its type is unknown — give an int, a float, or 'none'"
        )

    for kind, name in ((int, "an integer"), (float, "a number")):
        if isinstance(default, kind):
            try:
                return kind(raw)
            except ValueError:
                raise ValueError(f"--param {knob}={raw!r}: expected {name}") from None

    if isinstance(default, str):
        return raw

    raise ValueError(
        f"--param {knob}={raw!r}: {knob!r} defaults to a "
        f"{type(default).__name__}, which cannot be given on a command line"
    )


def parse_overrides(pairs: Iterable[str], params: Mapping[str, Any]) -> Dict[str, Any]:
    """Coerce ``["k=v", ...]`` against a package's ``PARAMS``.

    ``params`` supplies the target types only. A name it does not declare is
    forwarded **uncoerced** rather than rejected, so that `make_strategy` raises
    the one error that already names the package's real knob set.
    """
    # Tech: split each pair on its FIRST `=`, reject a repeat, and coerce against
    #       the declared default when the package declares one.
    # Why:  splitting on the first `=` keeps a value that itself contains one
    #       intact. Rejecting a repeated knob rather than taking the last is the
    #       difference between a typo that stops the run and a typo that produces
    #       a number; a repeatable flag makes that mistake easy to reach.
    out: Dict[str, Any] = {}
    for pair in pairs:
        key, sep, raw = pair.partition("=")
        key, raw = key.strip(), raw.strip()
        if not sep or not key:
            raise ValueError(f"--param expects KEY=VALUE, got {pair!r}")
        if key in out:
            raise ValueError(f"--param {key!r} given more than once")
        out[key] = raw if key not in params else _coerce(key, raw, params[key])
    return out
