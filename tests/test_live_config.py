"""The pinned live config must match the research row it claims to come from.

This is a drift guard, not a unit test. The tuned configuration previously
existed only as a command string in `results.tsv`, while three separate defaults
(`Toto2Forecaster`'s 2.5B, `real_data.py`'s 313m, `strategy/wiring.py`'s ±0.0005)
silently disagreed with it. These tests make any future divergence fail loudly
instead of quietly trading an untuned strategy.
"""
import csv
import re
from pathlib import Path

import pytest

from forecast_eval.live.config import PINNED, LiveConfig
from forecast_eval.live.sessions import SessionPolicy
from forecast_eval.strategy.threshold import ThresholdEmitter

RESULTS = Path(__file__).resolve().parent.parent / "results.tsv"


def _rows():
    with RESULTS.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


def _pinned_row():
    for r in _rows():
        if r["commit"] == PINNED.source_commit:
            return r
    pytest.fail(f"no results.tsv row for pinned commit {PINNED.source_commit}")


def _flag(text, name):
    """Pull `--name <value>` out of a recorded command string."""
    m = re.search(rf"{name}\s+(-?[\d.]+)", text)
    return float(m.group(1)) if m else None


# ---- provenance -------------------------------------------------------------

@pytest.mark.skipif(not RESULTS.exists(), reason="results.tsv not present")
def test_pinned_commit_exists_and_calmar_matches():
    row = _pinned_row()
    assert float(row["calmar"]) == pytest.approx(PINNED.source_calmar), (
        "the pinned Calmar disagrees with results.tsv — provenance was edited "
        "without the values, or vice versa"
    )


@pytest.mark.skipif(not RESULTS.exists(), reason="results.tsv not present")
def test_pinned_row_is_still_the_best_known_config():
    """Fails when research finds something better. That is deliberate: promoting
    a new config to live should be an explicit act, not a silent inheritance.
    To resolve, re-pin `PINNED` to the new row on purpose."""
    rows = _rows()
    best = max(rows, key=lambda r: float(r["calmar"]))
    assert best["commit"] == PINNED.source_commit, (
        f"results.tsv now has a better row: {best['commit']} "
        f"calmar={best['calmar']} vs pinned {PINNED.source_commit} "
        f"calmar={PINNED.source_calmar}. Re-pin live/config.py deliberately."
    )


# ---- the values themselves --------------------------------------------------

@pytest.mark.skipif(not RESULTS.exists(), reason="results.tsv not present")
def test_thresholds_and_min_prob_match_the_recorded_command():
    row = _pinned_row()
    cmd = row["description"]
    assert _flag(cmd, "--buy-threshold") == pytest.approx(PINNED.buy_threshold)
    assert _flag(cmd, "--sell-threshold") == pytest.approx(PINNED.sell_threshold)
    assert _flag(cmd, "--min-prob") == pytest.approx(PINNED.min_prob)


def test_checkpoint_is_the_tuned_model_not_a_default():
    """Every tuning run used the 1B checkpoint. `Toto2Forecaster` defaults to
    2.5B and `real_data.py` to 313m — either would be a *different model*, not a
    different setting, and neither would raise."""
    assert PINNED.checkpoint == "Datadog/Toto-2.0-1B"


def test_session_is_night_matching_the_tuned_dataset():
    assert PINNED.policy == SessionPolicy.night()
    assert "night" in PINNED.source_dataset.lower()


def test_risk_shape_matches_the_backtest():
    """These four are what the drawdown figure was measured under."""
    assert PINNED.aggression_ticks == 0
    assert PINNED.max_position == 1
    assert PINNED.max_aggression_ticks is None
    assert PINNED.forced_close_on_session_end is False


def test_cadence_and_context_match_the_tuned_run():
    assert PINNED.context_length == 3008
    assert PINNED.forecast_stride_bars == 30
    assert PINNED.forecast_horizon_bars == 30
    assert PINNED.bar_freq is None, "tuning fed raw 1-min bars, no re-aggregation"


# ---- the factories can't disagree with the declaration ---------------------

def test_build_emitter_reproduces_every_declared_field():
    em = PINNED.build_emitter()
    assert isinstance(em, ThresholdEmitter)
    assert em.buy_threshold == PINNED.buy_threshold
    assert em.sell_threshold == PINNED.sell_threshold
    assert em.min_prob == PINNED.min_prob
    assert em.min_trend_ticks == PINNED.min_trend_ticks
    assert em.path_momentum == PINNED.path_momentum
    assert em.trend_filter == PINNED.trend_filter
    assert em.short_window == PINNED.short_window


def test_build_emitter_returns_a_fresh_stateful_instance_each_call():
    """ThresholdEmitter carries rolling deques; sharing one across runs silently
    produces wrong numbers."""
    a, b = PINNED.build_emitter(), PINNED.build_emitter()
    assert a is not b
    assert a._short_prices is not b._short_prices


def test_build_forecaster_passes_the_pinned_values_through():
    """Constructing a Toto2Forecaster is cheap and needs no torch — the model
    loads lazily on the first forecast."""
    pytest.importorskip("forecast_eval.forecaster.toto2")
    fc = PINNED.build_forecaster(warmup_bars=3008)
    assert fc.checkpoint == PINNED.checkpoint
    assert fc.context_length == PINNED.context_length
    assert fc.forecast_stride_bars == PINNED.forecast_stride_bars
    assert fc.forecast_horizon_bars == PINNED.forecast_horizon_bars
    assert fc.bar_freq == PINNED.bar_freq
    assert fc.signal_step == PINNED.signal_step


def test_warmup_is_not_pinned_because_it_is_not_a_strategy_property():
    """In the backtest `warmup_bars` is the train/eval split — an experiment
    property. Live it comes from the backfill, so the entry point decides."""
    assert not hasattr(PINNED, "warmup_bars")
    a = PINNED.build_forecaster(warmup_bars=100)
    b = PINNED.build_forecaster(warmup_bars=3008)
    assert a.warmup_bars == 100 and b.warmup_bars == 3008


# ---- misc -------------------------------------------------------------------

def test_config_is_frozen():
    with pytest.raises(Exception):
        PINNED.buy_threshold = 0.1


def test_describe_carries_provenance_for_the_log_banner():
    text = PINNED.describe()
    assert PINNED.source_commit in text
    assert "13.6742" in text
    assert "Toto-2.0-1B" in text
    assert "NIGHT" in text
