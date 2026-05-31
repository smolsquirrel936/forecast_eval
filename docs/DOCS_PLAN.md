# Documentation plan — `docs/`

The plan behind this `docs/` folder: what it is, how it's structured, the
conventions every page follows, and how to extend it when code changes. Read this
before adding or editing a page.

## Goal

A **fast-orientation layer**: one page per Python file telling a newcomer *what the
file owns* and *which file to open next*. It is **not** a replacement for:

- [SPEC.md](../SPEC.md) — the design / single source of truth (architecture, behavior).
- [usage.md](../usage.md) — how to run things.
- [CLAUDE.md](../CLAUDE.md) — environment, interpreters, conventions.
- The inline `Tech:` / `Why:` comments — line-level rationale.

**Guiding principle: navigate and orient, don't restate.** Pages link to SPEC
sections and source instead of copying them, so they don't drift when code changes.

## Decisions (locked)

| Decision | Choice | Rationale |
|---|---|---|
| Location | `docs/` **mirror tree** (mirrors the package layout) | keeps source dirs clean; one place to browse all docs |
| Granularity | **strict 1:1** — one `.md` per non-empty `.py` | direct, predictable mapping from code to docs |
| Diagrams | **Mermaid** in the hub | renders as real diagrams in GitHub/VSCode preview |
| Tests | **one consolidated** [tests.md](tests.md) | per-test docstrings already explain each case; 6 thin pages would duplicate them |
| Source links | each page **title links to its `.py`** | jump straight from doc to code |
| Empty `__init__.py` | **no page** (mentioned in the hub) | nothing to document in an empty package marker |

## Structure (hub-and-spoke)

```
docs/
  README.md          ← hub: diagrams, file map, reading order, entry points
  DOCS_PLAN.md       ← this file
  <module>.md        ← one spoke per Python file, mirroring the package tree
  data/loader.md
  forecaster/{base,naive,toto2}.md
  strategy/{base,dummy,threshold}.md
  exits/{base,stop_loss,time_stop}.md
  tests.md           ← consolidated suite overview
```

- **Hub** ([README.md](README.md)) is the page read first: a Mermaid architecture
  diagram, a Mermaid per-tick-loop flowchart, the three core invariants, a
  file-map table ("open this when…"), a recommended reading order, and the
  runnable entry points.
- **Spokes** are short (~30–60 lines), skimmable, and link-heavy.

## Per-file page template

Replace the bracketed parts. Worked example (a top-level page documenting
`execution.py`):

```markdown
# [execution.py](../execution.py)

**Role:** one-line responsibility.
**Pipeline:** where it sits, e.g. Trader -> Execution -> Portfolio · SPEC §x
**Depends on:** ... **Used by:** ...

## Responsibility
2-3 sentences: what it owns, and what it deliberately does NOT do.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| ... | class/method/function | ... |

## Key invariants / gotchas
- the non-obvious things worth knowing before editing.

## Related
[trader.md](trader.md) · [SPEC §x](../SPEC.md)
```

(The title links to the source `.py`; the *Related* line links to sibling pages and
SPEC — adjust the `../` depth per the cheatsheet below.) Tailor the middle sections
to the file: a state machine gets a transition table; a CLI gets a flags list; an
ABC gets a "the contract" section. Keep it short.

## Relative-link cheatsheet (mirror tree)

Because the tree mirrors the package, link depth depends on the page's folder:

| From | To source `.py` | To sibling doc | To nested doc | To `SPEC.md` |
|---|---|---|---|---|
| `docs/execution.md` (top level) | `../execution.py` | `trader.md` | `forecaster/toto2.md` | `../SPEC.md` |
| `docs/forecaster/toto2.md` (nested) | `../../forecaster/toto2.py` | `base.md` | — | `../../SPEC.md` |
| nested → top-level doc | — | — | `../execution.md` | — |

## Maintenance — keep docs in sync with code

When you change code, update its page (and the hub if the change is structural):

- **New module** → add `docs/<path>.md` from the template; add a row to the hub's
  file-map table and, if it's a core concept, the reading order.
- **Renamed/removed symbol** → fix the page's *Public surface* table.
- **Changed invariant / data flow** → update the page's *Key invariants* and, if it
  touches the per-tick loop, the hub's Mermaid flowchart.
- **New entry point** → add a row to the hub's entry-point table.

### Verify links after editing

All internal links must resolve. Quick check (run from the package root):

```bash
python -c "
import re, pathlib
docs = pathlib.Path('docs')
link_re = re.compile(r'\[[^\]]+\]\(([^)]+)\)')
bad = []
for md in docs.rglob('*.md'):
    for m in link_re.finditer(md.read_text(encoding='utf-8')):
        t = m.group(1).split('#')[0]
        if t.startswith('http') or not t:
            continue
        if not (md.parent / t).resolve().exists():
            bad.append(f'{md.relative_to(docs)} -> {t}')
print('BROKEN:', bad if bad else 'none')
"
```

## Current coverage

25 markdown files: the hub, this plan, 22 module pages, `test_toto2.md`, and the
consolidated `tests.md`. Every internal link is verified to resolve. Empty
`__init__.py` package markers are intentionally undocumented.
