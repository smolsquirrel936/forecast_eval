# AUTORESEARCH_FREEZE.md — moved

**Superseded 2026-09-07. The freeze line now lives in the session plans themselves.**

This file existed for one reason: `autoresearch_trading_plan.md` lives at the repo root
(`night/`), outside `forecast_eval/`, and was not editable from here — so rather than leave
its freeze table silently inverted, this file carried the correct line beside it.

That file is now editable and has been rewritten. It is **three** plans, one per session, and
each carries the freeze line in full:

| session | plan |
|---|---|
| night | `../autoresearch_trading_plan_night.md` — the complete template |
| day | `../autoresearch_trading_plan_day.md` |
| fullday | `../autoresearch_trading_plan_fullday.md` — band sweep first; `iter01` earns the baseline |
| index | `../autoresearch_trading_plan.md` |

**The executable version of the freeze line is
[tests/test_strategy_isolation.py](tests/test_strategy_isolation.py)**, and where prose and
test disagree, the test wins. It pins a sha256 of the seven frozen shared/interface files —
`strategy/threshold.py`, `strategy/dummy.py`, `strategy/volmultitf.py`, `strategy/base.py`,
`trader.py`, `forecaster/toto2.py`, `forecaster/base.py` — and asserts that no session package
imports from a sibling.

**The rule in one line:** mutable is `strategy/<session>/` for your session only; the shared
files are frozen *in place* but copyable — to change one, copy it into your package and edit
the copy.

**A new module there also owns its page under `docs/strategy/<session>/` and its tests
under `tests/`.** The docs page is not optional: `tests/test_docs_coverage.py` asserts every
module has one, so adding a file inside the playground forces a write outside it. That is
permission to ADD your own files, never to edit anyone else's — a page or test belonging to
a frozen file is frozen with it, and editing the guard to pass is the worst version of that.

## Related

[STRATEGY_SPLIT_PLAN.md](STRATEGY_SPLIT_PLAN.md) · [docs/strategy/wiring.md](docs/strategy/wiring.md) · [SPEC.md](SPEC.md)
