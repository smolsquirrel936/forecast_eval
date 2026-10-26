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
  3. the packages' classes (both of hybrid's halves counted separately) are
     genuinely distinct objects, so a change to
     one cannot reach another — checked behaviourally, not just by identity.
"""
import ast
import hashlib
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SESSIONS = ("day", "night", "fullday", "hybrid")

# Where each package keeps its emitter and Trader classes, as attribute names on
# its `wiring` module. `hybrid` has two halves, so two of each.
CLASS_NAMES = {
    "hybrid": (("DayThresholdEmitter", "NightThresholdEmitter"),
               ("DayTrader", "NightTrader")),
}

# Tech: sha256 of every file the three packages start from and must not edit,
#       pinned at the split (2026-09-02). Each digest is taken over the file's
#       bytes with CRLF folded to LF (see `_content_sha256`), so these are the
#       LF-normalised hashes, not the raw working-tree ones.
# Why:  §3.1. These are the shared originals. An agent that edits one instead of
#       copying it into its own package moves all three sessions at once, which
#       is precisely the interference the split exists to prevent — and it would
#       otherwise be invisible until a parity run disagreed weeks later.
SHARED_FILES = {
    "strategy/threshold.py":
        "2b91f9839a573c52329640f17a61655ccb4e3c1721d7ffb15d29cdc10899123c",
    "strategy/dummy.py":
        "0fd21255d8bb42123e414db8e7445c4708b3200d24b1bff2818eee83a4553532",
    "strategy/volmultitf.py":
        "9fcac9650ab983131ff64aec9ec1061e0b5fff4001beedbfec05f1b367f37df5",
    "strategy/base.py":
        "8e1869b7b38ba7b12bda3efe38685c71c8d6184d5afd09fd3a5927177e411580",
    "trader.py":
        "fcb89621716734c4bfb9f9d9274c1886cf894138dd9d9f56955cf94c19bdd2e5",
    "forecaster/toto2.py":
        "1ed756c5527189d19069b1a7bb0b1c999415c65668d1959824ff6f423057173f",
    "forecaster/base.py":
        "c670e2f2b7ef64f4ff151a2149b60b05ae0ea9a1242288138a1463e41ad42393",
}


def _content_sha256(path):
    # Tech: digest the file's bytes with every CRLF folded to a bare LF.
    # Why:  the pin asserts that CONTENT has not changed, and a line ending is
    #       not content. This repo runs `core.autocrlf=true` with no
    #       .gitattributes, so these files check out CRLF on Windows and LF
    #       elsewhere — a raw-bytes digest therefore pins the checkout platform
    #       as tightly as it pins the code, and the same unedited file hashes
    #       two different ways on two machines. Normalising first is what makes
    #       the guard portable; it costs nothing, because an edit that changes
    #       only line endings is not an edit the split cares about.
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


@pytest.mark.parametrize("rel,expected", sorted(SHARED_FILES.items()))
def test_shared_files_are_unmodified(rel, expected):
    path = ROOT / rel
    actual = _content_sha256(path)
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
    # rglob: `hybrid` keeps its halves in sub-folders, and an import hidden one
    # level down re-couples packages just the same.
    for path in sorted(pkg.rglob("*.py")):
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

    emitters, traders = [], []
    for session in SESSIONS:
        mod = __import__(
            f"forecast_eval.strategy.{session}.wiring", fromlist=["wiring"]
        )
        em_names, tr_names = CLASS_NAMES.get(session, (("ThresholdEmitter",), ("Trader",)))
        emitters += [getattr(mod, n) for n in em_names]
        traders += [getattr(mod, n) for n in tr_names]

    distinct = set(map(id, emitters)) | {id(shared_emitter.ThresholdEmitter)}
    assert len(distinct) == len(emitters) + 1, "emitter classes are shared"
    distinct = set(map(id, traders)) | {id(shared_trader.Trader)}
    assert len(distinct) == len(traders) + 1, "Trader classes are shared"


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
