"""The strategy seam — dispatch to one of the three per-session packages.

Before STRATEGY_SPLIT_PLAN this module held a single `make_strategy()` that
nothing called: `live/config.py` records that its entrypoint no longer existed,
and the winning research runs were produced through `real_data.py` CLI flags
instead. So the seam disagreed with the tuned configuration on three knobs
(checkpoint, band, `min_prob`) and nothing raised.

It is now a dispatcher over `strategy/day`, `strategy/night` and
`strategy/fullday` (STRATEGY_SPLIT_PLAN §3). Each package owns its Forecaster,
SignalEmitter and Trader outright and is the only place a search for that
session may edit; this file holds no strategy values of its own, so it cannot
drift from them the way the old one did.
"""
import hashlib
import pathlib
from importlib import import_module
from typing import Any, Dict

# Tech: the packages this seam can raise, in the order the CLI lists them.
# Why:  a declared tuple rather than a directory scan — `--strategy` renders its
#       `choices=` from this, and an accidental sibling directory (a stray
#       `__pycache__`, a half-finished experiment) must not become a selectable
#       deployment. Mirrors how `live/config.py:CONFIGS` names its deployments
#       explicitly for the same reason.
SESSIONS = ("day", "night", "fullday")


def _package(session: str) -> Any:
    # Tech: import `strategy.<session>.wiring`, raising on an unknown name.
    # Why:  the caller is a CLI flag with `choices=`, so this should be
    #       unreachable from the command line; it exists so programmatic callers
    #       fail loudly rather than silently falling back to a default session,
    #       which is the `live/config.py:config_named` failure mode arriving by
    #       another door.
    if session not in SESSIONS:
        raise ValueError(
            f"unknown strategy {session!r}; known sessions: {list(SESSIONS)}"
        )
    return import_module(f".{session}.wiring", __package__)


def params(session: str) -> Dict[str, Any]:
    # Tech: this session's declared default knobs, as a fresh copy.
    # Why:  `real_data.py` needs the defaults *before* it builds anything, to
    #       resolve them against explicit CLI flags and print the resulting
    #       banner. A copy because the caller merges overrides into it, and a
    #       mutated `PARAMS` would silently re-tune every later run in-process.
    return dict(_package(session).PARAMS)


def forecaster_sha(session: str) -> str:
    # Tech: the first 12 hex of sha256 over this session's `forecaster.py`.
    # Why:  STRATEGY_SPLIT_PLAN §2.4, and it is bookkeeping the split *owes*.
    #       Making the forecaster per-session mutable (D3) gives up the guarantee
    #       the autoresearch loop rested on -- that forecasts are a fixed stream,
    #       so every iteration is comparable and `--precompute` amortises. Once
    #       the forecaster can move, a Calmar improvement may come from the
    #       forecaster rather than the strategy and the log cannot tell which.
    #       Recording this in the session's own `results_<session>.tsv` restores
    #       the distinction: rows sharing a sha were scored on the same forecast
    #       stream and are comparable; a sha change marks a stream change and has
    #       to be argued for. (The pre-split `results.tsv` has the column as well,
    #       with every row marked `pre-split`.)
    #
    #       Hashed from the file rather than from the imported module so it does
    #       not require torch, and so an edit counts even if the process has the
    #       old bytes already imported.
    path = pathlib.Path(__file__).resolve().parent / session / "forecaster.py"
    if session not in SESSIONS or not path.exists():
        raise ValueError(f"no forecaster for strategy {session!r}")
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def make_strategy(session: str, **kwargs: Any) -> Dict[str, Any]:
    # Tech: build the named session's strategy stack — `forecaster`, `emitter`,
    #       `make_trader`, `exit_rule`, `forced_close`, `params`.
    # Why:  one entry point means the backtest entry point never imports a
    #       session package directly, so adding a fourth session is a tuple entry
    #       and a directory rather than an edit at every call site.
    return _package(session).make_strategy(**kwargs)
