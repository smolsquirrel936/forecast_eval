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

from forecast_eval.real_data import CLI_STRATEGY_DEFAULTS, _resolve_strategy_knobs
from forecast_eval.strategy import wiring


def resolve(strategy=None, **explicit):
    """Parse-and-resolve, standing in for argparse's SUPPRESS behaviour."""
    args = argparse.Namespace(strategy=strategy, **explicit)
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
    assert set(declared) == set(CLI_STRATEGY_DEFAULTS)
    for knob, expected in declared.items():
        assert getattr(args, knob) == expected, knob
        assert origins[knob] == session


def test_the_pinned_bands_are_what_the_packages_declare():
    # Tech: the two tuned deployments' bands and probability gates, asserted
    #       against `live/config.py`'s pins rather than against literals here.
    # Why:  gate G1 is a full-tape GPU run and cannot live in the unit suite, but
    #       *this* is the part of it that can: if a package's band drifts from the
    #       pin it claims provenance from, the parity run is already lost and this
    #       fails in a second instead of an hour.
    from forecast_eval.live.config import PINNED, PINNED_DAY

    night, day = wiring.params("night"), wiring.params("day")
    for params, cfg in ((night, PINNED), (day, PINNED_DAY)):
        assert params["buy_threshold"] == cfg.buy_threshold
        assert params["sell_threshold"] == cfg.sell_threshold
        assert params["min_prob"] == cfg.min_prob
        assert params["full_confidence_prob"] == cfg.full_confidence_prob
        assert params["checkpoint"] == cfg.checkpoint
        assert params["context_length"] == cfg.context_length
        assert params["stride_bars"] == cfg.forecast_stride_bars
        assert params["horizon_bars"] == cfg.forecast_horizon_bars
        assert params["aggression_ticks"] == cfg.aggression_ticks
        assert params["max_position"] == cfg.max_position
        assert params["max_aggression_ticks"] == cfg.max_aggression_ticks
        assert params["forced_close"] == cfg.forced_close_on_session_end


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


def test_fullday_band_is_seeded_from_the_sweep_and_says_so():
    # Tech: full-day trades the Step-0 band-sweep winner (band +/-0.00075,
    #       min_prob 0.5), not DAY's inherited values, and its docstring names
    #       the sweep as provenance.
    # Why:  autoresearch_trading_plan_fullday.md Step 0 — the sweep is the thing
    #       that replaces "day's values, copied" (iter00), and the docstring is
    #       where the provenance lives. If someone moves the band and forgets
    #       the docstring, this fails and asks for it.
    p = wiring.params("fullday")
    assert p["buy_threshold"] == 0.00075
    assert p["sell_threshold"] == -0.00075
    assert p["min_prob"] == 0.5
    doc = __import__(
        "forecast_eval.strategy.fullday.wiring", fromlist=["wiring"]
    ).__doc__
    assert "Step-0 sweep" in doc


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
