"""Static name checks over every module: undefined globals, and use-before-assign.

This exists because of a failure on 2026-09-09 that the whole suite missed.
`--param` was wired into `live/cli.py:main`, and the edit that was supposed to add
`from forecast_eval.param_override import parse_overrides` silently did not match
(the file uses absolute imports; the patch anchored on a relative one). `main`
then referenced two names that did not exist, and **1342 tests stayed green**,
because entry points are the one place a unit suite does not execute: `main` logs
in, makes directories and talks to a venue, so nothing calls it far enough to
reach line one of the new code. The first thing to run it was a live invocation.

A NameError in an entry point is the cheapest possible bug and the most expensive
possible place to find it. This catches the whole class statically.
"""
import ast
import builtins
import importlib
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SKIP_DIRS = {"tests", "docs", "__pycache__", ".venv", "outputs", ".pytest_cache"}

# Tech: modules whose global references are checked, as import paths.
# Why:  every entry point plus the modules they call into at import time. Kept as
#       a discovered list rather than a hand-written one so a new module is
#       covered without anyone remembering to add it.
def _modules():
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if path.name == "__init__.py":
            continue
        yield "forecast_eval." + ".".join(rel.with_suffix("").parts)


def _bound_names(node):
    """Every name a function binds locally: assignments, imports, args, walrus,
    comprehension targets, `except ... as`, and nested defs."""
    bound = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            bound.add(n.id)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for alias in n.names:
                bound.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(n, ast.ExceptHandler) and n.name:
            bound.add(n.name)
        elif isinstance(n, ast.arg):
            bound.add(n.arg)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(n.name)
    return bound


@pytest.mark.parametrize("dotted", list(_modules()), ids=lambda d: d.split(".")[-1])
def test_functions_reference_no_undefined_globals(dotted):
    try:
        module = importlib.import_module(dotted)
    except ImportError as exc:                       # optional heavy deps
        pytest.skip(f"{dotted} not importable here: {exc}")

    source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    module_names = set(vars(module)) | set(dir(builtins))

    missing = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        bound = _bound_names(node)
        used = {n.id for n in ast.walk(node)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        unresolved = sorted(used - bound - module_names)
        if unresolved:
            missing[node.name] = unresolved

    assert not missing, (
        f"{dotted} references global name(s) that do not exist: {missing}. "
        f"A missing import in an entry point is invisible to every test that "
        f"does not call it — see this module's docstring.")


# ---- use before assignment (the second `main()` bug, 2026-09-09) -------------
#
# `--param`'s resolution was placed AFTER the `Recorder` that stamps it into the
# session `meta` header, so `main` loaded `strategy_params` before binding it.
# The same edit also wrote `artifacts.strategy_sha(...)`, where `artifacts` is a
# module name that `main` rebinds to an `ArtifactWriter` further down — which
# makes it a local for the whole function, so the module was unreachable through
# it. Both are UnboundLocalError at runtime, both invisible to a suite that never
# calls `main()`.


def _own_nodes(scope):
    """Walk a function body WITHOUT descending into nested scopes.

    Nested defs are the false-positive source: a closure defined early and called
    late legitimately loads names its enclosing function binds further down.
    """
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef, ast.Lambda)):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def _exempt(scope):
    """Names that are bound before the body runs, or bound elsewhere."""
    names = {a.arg for a in ast.walk(scope) if isinstance(a, ast.arg)}
    for node in ast.walk(scope):
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            names.update(node.names)
        elif isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp,
                               ast.GeneratorExp)):
            for gen in node.generators:
                names.update(n.id for n in ast.walk(gen.target)
                             if isinstance(n, ast.Name))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update((a.asname or a.name).split(".")[0] for a in node.names)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                               ast.ClassDef)):
            names.add(node.name)
    return names


def use_before_assign(source: str) -> list:
    """Names loaded, in a function's own scope, before that scope binds them."""
    offenders = []
    for scope in ast.walk(ast.parse(source)):
        if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        exempt = _exempt(scope)
        stores, loads = {}, {}
        for node in _own_nodes(scope):
            if isinstance(node, ast.Name):
                table = stores if isinstance(node.ctx, ast.Store) else loads
                table[node.id] = min(table.get(node.id, node.lineno), node.lineno)
        for name, bound_at in stores.items():
            if name in exempt:
                continue
            used_at = loads.get(name)
            if used_at is not None and used_at < bound_at:
                offenders.append(
                    f"{scope.name}: {name!r} loaded at line {used_at}, "
                    f"first bound at line {bound_at}")
    return offenders


def test_the_checker_catches_the_bug_it_was_written_for():
    """A guard nobody has seen fail is not evidence. This is the 2026-09-09
    `main()` bug reduced to three lines."""
    offenders = use_before_assign(
        "def main():\n"
        "    recorder = Recorder(meta=strategy_params)\n"
        "    strategy_params = {}\n")
    assert len(offenders) == 1 and "strategy_params" in offenders[0]


def test_the_checker_does_not_flag_a_closure_called_later():
    """The false positive that made the first draft unusable, and the reason
    `_own_nodes` stops at nested scopes: a closure defined early legitimately
    reads a name its parent binds further down. Found in `live/cli.py:main`
    (`lapsed_reported`), which is correct code."""
    assert use_before_assign(
        "def main():\n"
        "    def on_event():\n"
        "        seen.add(1)\n"
        "    seen = set()\n"
        "    return on_event\n") == []


@pytest.mark.parametrize("dotted", list(_modules()), ids=lambda d: d.split(".")[-1])
def test_no_local_is_loaded_before_it_is_bound(dotted):
    path = ROOT / pathlib.Path(*dotted.split(".")[1:]).with_suffix(".py")
    offenders = use_before_assign(path.read_text(encoding="utf-8"))
    assert not offenders, f"{dotted} — use before assignment: {offenders}"
