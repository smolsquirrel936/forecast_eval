"""Every Shioaji session this repo opens must be a simulation session.

The mode cannot be checked at runtime: `api.simulation` is not exposed by the
SDK (absent before login, `None` after — measured, §7.2), and nothing else on the
object names it. The constructor argument is the *only* guarantee there is, which
makes it exactly the kind of thing that must be asserted in source rather than
trusted to review.

So this walks the AST of every module that constructs a `Shioaji` and requires a
literal `simulation=True`. A variable, a flag, an env lookup or a missing keyword
all fail — not because any of them is wrong in principle, but because each would
move the decision somewhere this test can no longer see it.
"""
import ast
from pathlib import Path

import pytest

LIVE = Path(__file__).resolve().parent.parent / "live"

# Tech: the single module allowed to open a production session.
# Why:  it exists precisely because a simulation session cannot answer whether a
#       "simulated" order reached the real account. The exception is named here
#       rather than left implicit, so adding a second one is a visible edit to a
#       test rather than a line nobody reviews — and the checks below make this
#       one prove it is read-only.
PRODUCTION_AUDIT = "prod_audit.py"

# Tech: order APIs that must never appear in the audit module.
# Why:  the audit holds a live production session. The only thing standing
#       between "read-only by intent" and "read-only by construction" is that
#       none of these names is reachable in it.
ORDER_APIS = ("place_order", "place_comboorder", "update_order", "cancel_order",
              "cancel_comboorder", "FuturesOrder", "Order(")


def _shioaji_calls(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (func.attr if isinstance(func, ast.Attribute)
                else getattr(func, "id", None))
        if name == "Shioaji":
            yield node


def _modules_constructing_shioaji():
    return [p for p in sorted(LIVE.rglob("*.py")) if any(_shioaji_calls(p))]


def test_at_least_one_module_constructs_a_session():
    """Guards the guard: if the call moved or was renamed, the checks below would
    pass vacuously by finding nothing to check."""
    found = _modules_constructing_shioaji()
    assert found, "no Shioaji construction found — has the call site moved?"
    assert {p.name for p in found} >= {"cli.py"}


@pytest.mark.parametrize("path", _modules_constructing_shioaji(),
                         ids=lambda p: p.name)
def test_every_session_is_constructed_with_an_explicit_literal_mode(path):
    """Trading modules must say `simulation=True`; the audit says `False`. Either
    way it has to be a literal, because the mode is unverifiable at runtime and
    this file is the only place it can be read."""
    expected = path.name != PRODUCTION_AUDIT
    for call in _shioaji_calls(path):
        kwargs = {k.arg: k.value for k in call.keywords}
        assert "simulation" in kwargs, (
            f"{path.name}:{call.lineno} constructs Shioaji without "
            f"simulation= — the default is production")
        value = kwargs["simulation"]
        assert isinstance(value, ast.Constant) and value.value is expected, (
            f"{path.name}:{call.lineno} must pass literal "
            f"simulation={expected}")


def test_only_the_audit_module_may_touch_production():
    offenders = [
        p.name for p in _modules_constructing_shioaji()
        if p.name != PRODUCTION_AUDIT
        and any(k.arg == "simulation" and isinstance(k.value, ast.Constant)
                and k.value.value is False
                for call in _shioaji_calls(p) for k in call.keywords)
    ]
    assert not offenders, f"production session outside the audit: {offenders}"


def test_the_audit_module_cannot_place_an_order():
    """It holds a live production session, so 'read-only by intent' is not
    enough — none of the order APIs may be reachable in it at all."""
    source = (LIVE / PRODUCTION_AUDIT).read_text(encoding="utf-8")
    body = "\n".join(line for line in source.splitlines()
                     if not line.lstrip().startswith("#"))
    _, _, after_docstring = body.partition('"""')
    _, _, code = after_docstring.partition('"""')
    found = [name for name in ORDER_APIS if name in code]
    assert not found, f"{PRODUCTION_AUDIT} references order APIs: {found}"


@pytest.mark.parametrize("path", _modules_constructing_shioaji(),
                         ids=lambda p: p.name)
def test_no_positional_argument_could_shadow_the_keyword(path):
    """`Shioaji(...)` is a compiled type whose signature is `(*args, **kwargs)`,
    so a positional argument would not be caught by the keyword check above."""
    for call in _shioaji_calls(path):
        assert not call.args, (
            f"{path.name}:{call.lineno} passes positional arguments to Shioaji; "
            f"keep the call keyword-only so the mode stays legible")
