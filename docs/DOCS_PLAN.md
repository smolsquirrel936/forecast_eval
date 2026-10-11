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
  live/{cli,runtime,config,sessions,loop,bars,backfill,state,startup,recorder,capture,contracts,prod_audit,v1_check,v1c_check,v4_probe,v72_probe}.md
  live/broker/{base,sim,dryrun,shioaji,shadow}.md
  tests.md           ← consolidated suite overview
  simtrade_operations.md ← task page: operating the owner + deployments (no module)
  notifications.md   ← task page: alerts and Telegram, the usage manual (no module)
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

48 markdown files: the hub, this plan, 44 module pages, `test_toto2.md`, and the
consolidated `tests.md`. All internal links resolve except one pre-existing
breakage in `strategy/wiring.md` (`../../autoresearch_trading_plan.md` — the file
sits at the repo root, so the link needs one more `../`); left as found. Empty
`__init__.py` package markers are intentionally undocumented. (`main.py`, a
91-byte print stub that the reference tree `Z:\test-autoresearch` does not
carry, was deleted; it is no longer a coverage gap.)

The `live/` package (22 pages under `docs/live/`) follows the same strict 1:1
rule, including the five one-shot validation scripts — `v1_check.py`,
`v1c_check.py`, `v4_probe.py`, `v72_probe.py` and `prod_audit.py` each got a page rather than being consolidated
the way `tests.md` was, since each documents a distinct measured result rather
than a set of cases. (`v1_check.py` shipped with its page in the same change,
which is the rule the coverage-debt note below exists to enforce.)

**Coverage debt closed 2026-08-11.** Four live modules had shipped without pages,
which the 1:1 rule makes a gap rather than a judgement call: `runtime.py`,
`cli.py`, `capture.py` and `broker/dryrun.py`. All four now have pages, the hub's
file-map and entry-point tables list them, and `tests.md` gained the matching
rows for `test_live_runtime.py`, `test_live_cli.py` and `test_live_capture.py`.
Worth noting how it happened: the pages were written module-by-module as the live
package grew, and the four that landed last — the entry point and the assembly
around it — were the ones nobody circled back to. **The 1:1 rule only holds if a
new `.py` and its `.md` land in the same change.**

### Known gap: no porting guide yet — unblocked, scheduled after V5

`live/sessions.py` makes the traded session a parameter specifically so this
strategy can be ported to the day session. A **task-oriented porting guide** ("run
this on the day session") was deferred because the task centred on an entry point
that did not exist: nothing forced the tick aggregator and the backfill to receive
the **same** `SessionPolicy`, so a guide written then would have documented an
aspiration and handed the reader a procedure with a hole in it.

**That precondition is now met.** [live/cli.py](live/cli.md) exists and
[live/runtime.py](live/runtime.md) closes the hole structurally — it derives the
one policy from the pinned config and refuses to accept another, so the
aggregator and the backfill cannot disagree. The guide is therefore writable.

**Decision (2026-08-11): write it after V5**, as part of Phase INT. Two reasons
it is better placed there than here:

- V4 and V5 can still change the procedure. V4 decides `aggression_ticks` from a
  measured fill rate, and V5 is where restart, recovery and boundary-crossing
  behaviour is actually exercised. A guide written now would describe steps that
  have never been run end to end.
- After V5 the guide can say "this has been soaked for five days" instead of
  "this should work" — which is the difference between a procedure and a claim.

Distinct from the coverage debt above, because this is a *task* page rather than
a 1:1 module page — it belongs next to [usage.md](../usage.md), not in the mirror
tree, so the strict 1:1 rule does not cover it and nothing will flag its absence.

Until it exists the rationale lives in [live/sessions.md](live/sessions.md) and
[SHIOAJI_PLAN.md §2.12](../SHIOAJI_PLAN.md); recorded debt with an owner and a
date, not an oversight.
