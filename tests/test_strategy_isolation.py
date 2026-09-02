"""The three-way split's isolation rule, enforced instead of remembered.

STRATEGY_SPLIT_PLAN §3.1 says each session package owns its Forecaster,
SignalEmitter and Trader outright, and that changing a *shared* implementation
means copying it into your package first — never editing it in place, which
would move all three sessions at once and silently invalidate the other two
searches.

That rule needs a mechanism, not a sentence. `test_docs_coverage.py` exists in
this repo for exactly that reason ("a rule that depends on someone remembering
to run a checker is not a rule"); these tests are the checker for this one.

Three things are asserted here:

  1. the shared files still hash to what they hashed at the split (§3.1);
  2. no session package imports from a sibling package;
  3. the three packages' classes are genuinely distinct objects, so a change to
     one cannot reach another — checked behaviourally, not just by identity.
"""
import ast
import hashlib
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SESSIONS = ("day", "night", "fullday")

# Tech: sha256 of every file the three packages start from and must not edit,
#       pinned at the split (2026-09-02).
# Why:  §3.1. These are the shared originals. An agent that edits one instead of
#       copying it into its own package moves all three sessions at once, which
#       is precisely the interference the split exists to prevent — and it would
#       otherwise be invisible until a parity run disagreed weeks later.
SHARED_FILES = {
    "strategy/threshold.py":
        "b7721ff7ea99a02139689229b047b7c3cca21ecf8fc01c469a192770db6b3f7b",
    "strategy/dummy.py":
        "0fd21255d8bb42123e414db8e7445c4708b3200d24b1bff2818eee83a4553532",
    "strategy/volmultitf.py":
        "9fcac9650ab983131ff64aec9ec1061e0b5fff4001beedbfec05f1b367f37df5",
    "strategy/base.py":
        "f72360e494f101a0bc7a558f305607345b104c240fb8dc027f37b55b1663cb23",
    "trader.py":
        "3bea278e07c9d1dd41e0b8cc23f6e02b1864657b7148bf5b60a5bff710e02099",
    "forecaster/toto2.py":
        "b3bdce453c431defc3b1ff3185441db872f1af4793db1505293cf74e4744ae1e",
    "forecaster/base.py":
        "db7cfb2fdef9492b1b3216658b76596868bca6fb4263d62ebedf88e1be655b5a",
}


@pytest.mark.parametrize("rel,expected", sorted(SHARED_FILES.items()))
def test_shared_files_are_unmodified(rel, expected):
    path = ROOT / rel
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    assert actual == expected, (
        f"{rel} has changed, and it is SHARED by all three session packages.\n"
        f"  If you are tuning one session: copy this file into "
        f"strategy/<session>/ and edit the copy — that is what the split is "
        f"for, and the other two sessions must not move with you.\n"
        f"  If this is a deliberate harness-wide change: update the pin in "
        f"tests/test_strategy_isolation.py to {actual} in the SAME commit, and "
        f"re-run the G1 parity gate, because every recorded result was measured "
        f"on the old bytes."
    )


@pytest.mark.parametrize("session", SESSIONS)
def test_no_cross_package_imports(session):
    # Tech: parse every module in this package and reject any import that names
    #       a *sibling* session package, whether absolute or relative.
    # Why:  a copy that quietly imports from `strategy.night` is shared code
    #       wearing a package's name — it re-couples the two searches while
    #       looking isolated, and the hash guard above would never see it.
    siblings = [s for s in SESSIONS if s != session]
    pkg = ROOT / "strategy" / session
    for path in sorted(pkg.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                # A relative `from ..night.emitter import X` has level>0 and
                # module "night.emitter"; an absolute one carries the full path.
                names = [node.module or ""]
            else:
                continue
            for name in names:
                parts = name.split(".")
                for other in siblings:
                    assert other not in parts, (
                        f"{path.relative_to(ROOT)} imports from the {other!r} "
                        f"package. Session packages must not depend on each "
                        f"other — copy what you need into {session}/ instead."
                    )


def test_each_package_has_its_own_classes():
    # Tech: import all three packages and assert the emitter and Trader classes
    #       are distinct objects, both from each other and from the shared ones.
    # Why:  the cheap structural half of "they cannot interfere". If two packages
    #       resolved to the same class object, every guarantee below it is void.
    from forecast_eval.strategy import threshold as shared_emitter
    from forecast_eval import trader as shared_trader

    emitters, traders = {}, {}
    for session in SESSIONS:
        mod = __import__(
            f"forecast_eval.strategy.{session}.wiring", fromlist=["wiring"]
        )
        emitters[session] = mod.ThresholdEmitter
        traders[session] = mod.Trader

    distinct = set(map(id, emitters.values())) | {id(shared_emitter.ThresholdEmitter)}
    assert len(distinct) == len(SESSIONS) + 1, "emitter classes are shared"
    distinct = set(map(id, traders.values())) | {id(shared_trader.Trader)}
    assert len(distinct) == len(SESSIONS) + 1, "Trader classes are shared"


def test_editing_one_package_does_not_reach_another(monkeypatch):
    # Tech: break DAY's emitter as destructively as an edit could — force every
    #       decision to BUY — and assert NIGHT's emitter is unmoved on a payload
    #       that must resolve to HOLD.
    # Why:  gate G3. Distinct class objects (above) is the mechanism; this is the
    #       observable consequence, and it is the property the whole split is
    #       selling. Checked behaviourally so it would still fail if the packages
    #       were ever re-pointed at one implementation through some other route.
    from forecast_eval.events import Forecast
    from forecast_eval.strategy.day import wiring as day
    from forecast_eval.strategy.night import wiring as night

    flat = Forecast(
        timestamp=None,
        horizon_bars=30,
        payload={"predicted_return": 0.0, "prob_up": 0.5, "last_price": 100.0},
    )
    night_emitter = night.ThresholdEmitter(**{
        k: night.PARAMS[k] for k in
        ("buy_threshold", "sell_threshold", "min_prob", "full_confidence_prob")
    })
    assert night_emitter.emit(flat) == "HOLD"

    monkeypatch.setattr(
        day.ThresholdEmitter, "_decide", lambda self, fc: ("BUY", 1.0)
    )
    day_emitter = day.ThresholdEmitter()
    assert day_emitter.emit(flat) == "BUY", "the sabotage did not take effect"
    assert night_emitter.emit(flat) == "HOLD", (
        "editing DAY's emitter changed NIGHT's behaviour — the packages are "
        "not isolated"
    )
