"""The docs' 1:1 rule, enforced instead of remembered.

`docs/DOCS_PLAN.md` locks a strict rule: one `.md` per non-empty `.py`, and a new
module's page lands *in the same change* as the module. It was written after four
live modules had shipped without pages — and then `prod_audit.py` and
`flatten.py` each shipped without one anyway, both caught by accident.

A rule that depends on someone remembering to run a checker is not a rule. These
tests are the checker.
"""
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
SKIP_DIRS = {"tests", "docs", "__pycache__", ".venv", "outputs", ".pytest_cache"}

# Tech: a package marker carrying only a docstring counts as empty.
# Why:  DOCS_PLAN exempts empty `__init__.py` because there is no public surface
#       to document. `live/__init__.py` is docstring-only orientation prose and
#       is deliberately undocumented; the rule is "no symbols", not "no bytes".
DOCSTRING_ONLY = {"__init__.py"}


def _modules():
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if path.name in DOCSTRING_ONLY:
            continue
        yield rel


@pytest.mark.parametrize("module", list(_modules()), ids=str)
def test_every_module_has_a_page(module):
    page = DOCS / module.with_suffix(".md")
    assert page.exists(), (
        f"{module} has no docs page. DOCS_PLAN's 1:1 rule says it lands in the "
        f"same change as the module — write {page.relative_to(ROOT)}.")


def test_the_sweep_is_not_vacuous():
    """Guards the guard: a broken discovery would make every check above pass by
    finding nothing to check."""
    modules = list(_modules())
    assert len(modules) > 25, f"only found {len(modules)} modules — discovery broke"
    assert pathlib.Path("live/cli.py") in modules


def test_every_page_has_a_module():
    """The other direction: a page for a module that was renamed or deleted is a
    dangling doc, and the hub will link to something that no longer exists."""
    consolidated = {"README.md", "DOCS_PLAN.md", "tests.md", "usage.md",
                    "simtrade_operations.md"}
    orphans = []
    for page in sorted(DOCS.rglob("*.md")):
        if page.name in consolidated:
            continue
        module = ROOT / page.relative_to(DOCS).with_suffix(".py")
        if not module.exists():
            orphans.append(str(page.relative_to(DOCS)))
    assert not orphans, f"pages with no module: {orphans}"


def test_internal_doc_links_resolve():
    """DOCS_PLAN prescribes this check and gives the snippet; running it by hand
    is exactly the habit that let two pages go missing."""
    import re
    known_broken = {"strategy/wiring.md -> ../../autoresearch_trading_plan.md"}
    broken = []
    for page in sorted(DOCS.rglob("*.md")):
        for match in re.finditer(r"\[[^\]]+\]\(([^)]+)\)",
                                 page.read_text(encoding="utf-8")):
            target = match.group(1).split("#")[0]
            if target.startswith("http") or not target:
                continue
            if not (page.parent / target).resolve().exists():
                broken.append(f"{page.relative_to(DOCS)} -> {target}".replace("\\", "/"))
    assert not set(broken) - known_broken, f"broken links: {broken}"
