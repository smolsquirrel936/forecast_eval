"""`real_data.py --strategy` resolution (STRATEGY_SPLIT_PLAN S3).

The selector's whole job is that a knob ends up with the right value and the run
can say where it came from. Two ways that goes wrong silently, both covered here:

  * a bare run stops reproducing the commands recorded in `results.tsv`, because
    the twelve flags moved to `argparse.SUPPRESS` and a default was lost in the
    move;
  * an explicitly typed flag is overwritten by the strategy package, so
    `--strategy night --buy-threshold 0.0005` quietly runs 0.00045.

The second is the reason the flags use SUPPRESS at all: with a literal default,
a flag passed *at* its default value is indistinguishable from one never passed.
"""
import argparse

import pytest

from forecast_eval import real_data
from forecast_eval.real_data import CLI_STRATEGY_DEFAULTS, _resolve_strategy_knobs
from forecast_eval.strategy import wiring


def resolve(strategy=None, param=None, **explicit):
    """Parse-and-resolve, standing in for argparse's SUPPRESS behaviour."""
    args = argparse.Namespace(strategy=strategy, param=param or [], **explicit)
    origins = _resolve_strategy_knobs(args)
    return args, origins


def test_bare_run_reproduces_the_old_cli_defaults():
    args, origins = resolve()
    for knob, expected in CLI_STRATEGY_DEFAULTS.items():
        assert getattr(args, knob) == expected, knob
        assert origins[knob] == "cli-default"


@pytest.mark.parametrize("session", wiring.SESSIONS)
def test_a_named_strategy_supplies_every_knob(session):
    args, origins = resolve(strategy=session)
    declared = wiring.params(session)
    # A package must cover every knob this CLI resolves, and may declare more
    # (LIVE_STRATEGY_ROUTING_PLAN D6). The two guard directions are pinned
    # separately below; this one only needs the cover.
    assert set(CLI_STRATEGY_DEFAULTS) <= set(declared)
    for knob, expected in declared.items():
        assert getattr(args, knob) == expected, knob
        assert origins[knob] == session


def test_a_package_may_declare_a_knob_the_cli_has_no_flag_for(monkeypatch):
    """D6. A search that invents a parameter must be able to put it in `PARAMS`.

    Before this, `strategy/fullday/combine.py` had to hide RET_K/PROB_K as module
    constants -- so the parameter behind iter03 and iter05 never reached the
    banner, `params.json` or `results.tsv`. The extra knob resolves from the
    package, is attributed to it, and lands on `args` like any other.
    """
    extended = dict(CLI_STRATEGY_DEFAULTS)
    extended["ret_k"] = 3
    monkeypatch.setattr(real_data.strategy_wiring, "params", lambda s: dict(extended))

    args, origins = resolve(strategy="night")
    assert args.ret_k == 3
    assert origins["ret_k"] == "night"
    # ...and the twelve are untouched by the extra one.
    for knob in CLI_STRATEGY_DEFAULTS:
        assert origins[knob] == "night"


def test_a_typed_param_wins_over_the_package_and_says_so(monkeypatch):
    """D7. `--param` is the only route to a knob with no flag, so it must beat
    the package default -- and the banner has to show that it did, because a run
    carrying a typed knob is not comparable to any `results.tsv` row."""
    extended = dict(CLI_STRATEGY_DEFAULTS)
    extended["ret_k"] = 3
    monkeypatch.setattr(real_data.strategy_wiring, "params", lambda s: dict(extended))

    args, origins = resolve(strategy="night", param=["ret_k=4", "min_prob=0.7"])
    assert args.ret_k == 4 and origins["ret_k"] == "param"
    assert args.min_prob == 0.7 and origins["min_prob"] == "param"
    # An untouched knob still reports the package, not "param".
    assert origins["buy_threshold"] == "night"


def test_param_without_strategy_is_refused():
    """The bare path exists to reproduce recorded `results.tsv` commands, and it
    has no package whose PARAMS could type the value."""
    with pytest.raises(ValueError, match="needs --strategy"):
        resolve(param=["ret_k=4"])


def test_a_package_that_drops_a_cli_knob_still_fails(monkeypatch):
    """The half of the old equality guard that carried the safety.

    A missing knob would silently fall back to a stale literal, so the run and
    the recorded row would disagree -- which is the whole failure `live/config.py`
    exists to prevent. Relaxing to a subset must not relax this direction.
    """
    short = dict(CLI_STRATEGY_DEFAULTS)
    del short["min_prob"]
    monkeypatch.setattr(real_data.strategy_wiring, "params", lambda s: dict(short))

    with pytest.raises(ValueError, match="missing.*min_prob"):
        resolve(strategy="night")


def test_a_deployment_reads_its_band_from_its_own_package():
    """What the old drift guard was for, restated for a world with one source.

    This used to assert `params[knob] == PINNED.<knob>` -- a package/pin drift
    check. `LiveConfig` holds no band any more (LIVE_STRATEGY_ROUTING_PLAN D2),
    so that drift is not expressible and the old assertion would be comparing a
    value to itself. What is still worth pinning is that the two deployments
    resolve to *different* bands, which is what makes them two deployments.
    """
    from forecast_eval.live.config import PINNED, PINNED_DAY

    night = wiring.params(PINNED.strategy_package)
    day = wiring.params(PINNED_DAY.strategy_package)
    assert night["buy_threshold"] != day["buy_threshold"]
    assert night["min_prob"] != day["min_prob"]
    # ...and the model itself was never re-tuned, so these must still agree.
    # `stride_bars` is not among them: it is a cadence, and night_iter30 tuned
    # night's to 15 (day stays 30).
    assert night["checkpoint"] == day["checkpoint"]
    assert night["context_length"] == day["context_length"]
    assert night["horizon_bars"] == day["horizon_bars"]


def test_an_explicit_flag_beats_the_strategy_at_its_own_default_value():
    # Tech: pass --buy-threshold at the *old CLI default*, which is not NIGHT's
    #       value, and assert the typed value survives and is tagged "flag".
    # Why:  this is the case a literal argparse default cannot express, and the
    #       whole reason for SUPPRESS. Getting it wrong runs a different strategy
    #       than the command line describes, and nothing raises.
    args, origins = resolve(strategy="night", buy_threshold=0.0005)
    assert args.buy_threshold == 0.0005
    assert origins["buy_threshold"] == "flag"
    # ...and the knobs that were not typed still come from the package.
    assert args.sell_threshold == wiring.params("night")["sell_threshold"]
    assert origins["sell_threshold"] == "night"


def test_fullday_band_is_its_own_values_and_names_its_split_source():
    # Tech: full-day's band and probability gate are pinned to their own literal
    #       values (fullday_split_iter07), and its docstring names that source.
    # Why:  §5/D5 — the docstring is what stops these values being read as
    #       whole-data provenance. If someone retunes full-day and forgets the
    #       docstring, this fails and asks for it (SIMTRADE_0929).
    #       Pinned to literals, NOT to params("day"): comparing against DAY made
    #       every day-loop band edit fail a fullday test, coupling two sessions
    #       the split keeps independent (DAYNIGHT_TOTO2_RESET_PLAN §8).
    p = wiring.params("fullday")
    assert p["buy_threshold"] == 0.001
    assert p["sell_threshold"] == -0.001
    assert p["min_prob"] == 0.50
    assert p["stride_bars"] == 15
    doc = __import__(
        "forecast_eval.strategy.fullday.wiring", fromlist=["wiring"]
    ).__doc__
    assert "fullday_split_iter07" in doc


def test_an_unknown_strategy_raises():
    with pytest.raises(ValueError, match="unknown strategy"):
        resolve(strategy="weekend")


# ---- forecaster_sha: the bookkeeping D3 owes (STRATEGY_SPLIT_PLAN §2.4) ------

def test_forecaster_sha_is_per_package_and_tracks_the_file():
    # Tech: the three packages start from identical `forecaster.py` bodies apart
    #       from their docstrings, so their shas differ; and the sha follows the
    #       file's bytes, not the imported module.
    # Why:  the column exists to say "these two rows were scored on the same
    #       forecast stream". A sha that did not change when the file changed
    #       would make the column worse than absent — it would assert
    #       comparability that is not there.
    import pathlib

    shas = {s: wiring.forecaster_sha(s) for s in wiring.SESSIONS}
    assert len(set(shas.values())) == len(wiring.SESSIONS), shas

    path = (pathlib.Path(wiring.__file__).parent / "night" / "forecaster.py")
    original = path.read_bytes()
    try:
        path.write_bytes(original + b"\n# touched\n")
        assert wiring.forecaster_sha("night") != shas["night"]
    finally:
        path.write_bytes(original)
    assert wiring.forecaster_sha("night") == shas["night"]


def test_results_tsv_carries_the_forecaster_sha_column():
    # Tech: the research ledger has the column, and every pre-split row is
    #       marked as such rather than left blank.
    # Why:  §2.4. A blank would read as "not recorded"; "pre-split" says the run
    #       predates a mutable forecaster, which is the actual fact and is what
    #       makes those 20 rows comparable with each other and not with what
    #       comes next.
    import csv
    import pathlib

    path = pathlib.Path(__file__).resolve().parent.parent / "results.tsv"
    with path.open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh, delimiter="\t"))
    assert rows, "results.tsv is empty"
    assert "forecaster_sha" in rows[0]
    assert all(r["forecaster_sha"] for r in rows), "a row has a blank sha"


def test_an_unknown_session_has_no_forecaster_sha():
    with pytest.raises(ValueError, match="no forecaster"):
        wiring.forecaster_sha("weekend")
