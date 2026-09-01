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

from forecast_eval.live.config import (CONFIGS, PINNED, PINNED_DAY, LiveConfig,
                                        config_named)
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


def _session_of(config):
    """The single session a config's policy trades."""
    # Tech: a config pins one deployment, so its policy names exactly one
    #       session; anything else has no single row to be "best" within.
    # Why:  `results.tsv` is now session-tagged, and every comparison below is
    #       scoped by this. A `both()` policy would make "the best row" undefined
    #       rather than merely wrong, so it fails here instead of silently
    #       picking one.
    assert len(config.policy.sessions) == 1, (
        f"a pinned config must trade exactly one session, got "
        f"{sorted(config.policy.sessions)}"
    )
    return next(iter(config.policy.sessions))


def _rows_for(session):
    return [r for r in _rows() if r["session"] == session]


def _best_row(rows, session, dataset=None):
    """The highest-Calmar `keep` row *within one session and one dataset
    vintage*, or None if there are none."""
    # Tech: when `dataset` is given, rows measured on any other tape vintage are
    #       excluded before the max.
    # Why:  TAPE_REBUILD_PLAN T6. This is C1's argument again, one axis over: a
    #       Calmar measured on a *different tape* is not comparable to one
    #       measured on this tape, so an unscoped max lets a retired vintage
    #       re-pin the live deployment. Measured — the rebuild moved the pinned
    #       configuration from 16.3122 to 3.6006 with **no parameter changed**,
    #       purely because the tape it is scored on was corrected. Without this
    #       scope the ledger would insist the deployment stay pinned to a number
    #       that can no longer be reproduced from any file on disk, which is the
    #       opposite of what a provenance guard is for. D1 is explicit that the
    #       old row survives as history: "one vintage is the forward policy, not
    #       a rewrite of the ledger".
    #
    #       `status` must still be "keep"; rows recorded as `reject` are excluded.
    # Why:  DAY_SESSION_PLAN W5b, MEASURED — the ledger holds rejected cells on
    #       purpose, as the evidence for why they were rejected. The day search's
    #       band=0.004 row scores Calmar 14.7697 on the full window and is the
    #       highest DAY number in this file, but it is 27 trades with 10 of them
    #       out-of-sample and a first-half Calmar of 2.28. Without this filter it
    #       would be "the best known DAY config" and would be what a future DAY
    #       pin is asserted against — the ledger's own counter-example promoted
    #       to its recommendation.
    scoped = [r for r in rows
              if r["session"] == session and r.get("status", "keep") == "keep"
              and (dataset is None or r["dataset"] == dataset)]
    return max(scoped, key=lambda r: float(r["calmar"])) if scoped else None


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
    # Tech: compare only within the pinned config's own session.
    # Why:  DAY_SESSION_PLAN C1. On Calmar a DAY row and a NIGHT row are not
    #       comparable, so an unscoped `max` would let a day-session result
    #       re-pin the *night* deployment — the failure would read as "research
    #       found something better" while actually meaning "a different
    #       deployment was tuned". Scoping is what makes a second pin possible
    #       at all.
    session = _session_of(PINNED)
    assert _rows_for(session), f"results.tsv has no {session} rows to compare against"
    vintage = _pinned_row()["dataset"]
    best = _best_row(_rows(), session, vintage)
    assert best["commit"] == PINNED.source_commit, (
        f"results.tsv now has a better {session} row on {vintage}: "
        f"{best['commit']} calmar={best['calmar']} vs pinned "
        f"{PINNED.source_commit} calmar={PINNED.source_calmar}. "
        f"Re-pin live/config.py deliberately."
    )


@pytest.mark.skipif(not RESULTS.exists(), reason="results.tsv not present")
def test_the_pin_names_the_tape_vintage_it_was_measured_on():
    """A Calmar without a tape vintage is not a fact about anything.

    TAPE_REBUILD_PLAN T6 — the same parameters score 16.3122 on the pre-rebuild
    night tape and 3.6006 on the rebuilt one. `results.tsv` has carried a
    `dataset` column since W0 for this reason; the guard above scopes by it, so
    a pinned row that does not fill it in would silently widen that scope back
    to every vintage ever measured."""
    row = _pinned_row()
    assert "@" in row["dataset"], (
        f"pinned row {row['commit']} records dataset {row['dataset']!r} with no "
        f"@vintage — the scope in _best_row cannot mean anything without it")
    assert row["dataset"].split("@")[0] in PINNED.source_dataset, (
        "the pinned config's source_dataset names a different file from its "
        "results.tsv row")


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


def test_warmup_days_default_is_policy_derived_not_a_flat_constant():
    """DAY_SESSION_PLAN C6 — the old flat 30-day default was night-sized and
    silent about it: at DAY's 300 bars/session it reaches only 109 boundaries,
    expected ~4 non-HOLD samples against the 3-sample floor `_check_primed`
    refuses below. Reproduces the pinned run's own numbers exactly."""
    assert PINNED.warmup_days_default() == 30   # unchanged from the old default
    day_cfg = LiveConfig(
        source_commit=PINNED.source_commit, source_calmar=PINNED.source_calmar,
        source_dataset=PINNED.source_dataset, source_note=PINNED.source_note,
        policy=SessionPolicy.day(), checkpoint=PINNED.checkpoint,
        context_length=PINNED.context_length,
        forecast_horizon_bars=PINNED.forecast_horizon_bars,
        forecast_stride_bars=PINNED.forecast_stride_bars,
    )
    assert day_cfg.warmup_days_default() == 84   # DAY_SESSION_PLAN quoted ~85


# ---- misc -------------------------------------------------------------------

def test_config_is_frozen():
    with pytest.raises(Exception):
        PINNED.buy_threshold = 0.1


def test_describe_carries_provenance_for_the_log_banner():
    # Tech: the Calmar is asserted from `PINNED`, not as a literal.
    # Why:  it was `"13.6742"` until TAPE_REBUILD_PLAN T6 re-pinned the night
    #       deployment, and a hardcoded copy of a provenance value is a second
    #       place to update that nothing points at — exactly the drift this file
    #       exists to catch, reproduced inside the guard itself. What matters is
    #       that the banner carries whatever the pin says, so that is the test.
    text = PINNED.describe()
    assert PINNED.source_commit in text
    assert str(PINNED.source_calmar) in text
    assert "Toto-2.0-1B" in text
    assert "NIGHT" in text


# ---- results.tsv schema (DAY_SESSION_PLAN W0) --------------------------------
#
# A row is: commit, session, dataset, warmup_bars, calmar, net_pnl,
# max_drawdown, status, description.
#
# `session`     NIGHT or DAY — which deployment the row belongs to. Calmar is
#               not comparable across the two, so every comparison is scoped by
#               it and a second pin is possible without disturbing the first.
# `dataset`     "<file>@<coverage-end>" — the tape vintage. The RPT files are
#               regenerated in place, so a filename alone does not identify the
#               data a number came from (DAY_SESSION_PLAN §2.2).
# `warmup_bars` the warmup length in BARS, not as a fraction. `--lookback-frac`
#               is not reproducible across tapes of different length: on a
#               longer file the same fraction moves the forecast grid and the
#               run is no longer comparable to itself (§2.2's alignment trap).

VALID_SESSIONS = {"NIGHT", "DAY"}


@pytest.mark.skipif(not RESULTS.exists(), reason="results.tsv not present")
def test_every_row_carries_session_dataset_and_warmup():
    for r in _rows():
        assert r["session"] in VALID_SESSIONS, (
            f"row {r['commit']} has session={r['session']!r}; "
            f"expected one of {sorted(VALID_SESSIONS)}"
        )
        assert "@" in r["dataset"], (
            f"row {r['commit']} dataset={r['dataset']!r} names no vintage; "
            f"expected '<file>@<coverage-end>'"
        )
        assert r["warmup_bars"].isdigit() and int(r["warmup_bars"]) > 0, (
            f"row {r['commit']} warmup_bars={r['warmup_bars']!r} is not a bar count"
        )


@pytest.mark.skipif(not RESULTS.exists(), reason="results.tsv not present")
def test_pinned_row_session_matches_the_pinned_policy():
    """The row and the policy must name the same session. This is the pairing
    C2 exists to protect: a policy changed without the strategy is the
    2026-08-11 defect, and it starts with these two disagreeing."""
    assert _pinned_row()["session"] == _session_of(PINNED)


@pytest.mark.skipif(not RESULTS.exists(), reason="results.tsv not present")
def test_pinned_row_dataset_matches_the_pinned_source():
    """`PINNED.source_dataset` is a path; the row names a file plus a vintage.
    The filenames must agree — a pin whose row was measured on a different tape
    has no provenance at all."""
    row_file = _pinned_row()["dataset"].split("@")[0]
    assert row_file.lower() in PINNED.source_dataset.lower().replace("\\", "/")


def test_a_day_row_cannot_repin_the_night_deployment():
    """The regression for the unscoped `max`. A DAY row that beats every NIGHT
    row must leave the NIGHT pin alone; before W0 it would have been picked as
    "the best known config" and failed the drift guard against a result from a
    different deployment."""
    rows = [
        {"commit": "night_a", "session": "NIGHT", "calmar": "13.6742"},
        {"commit": "night_b", "session": "NIGHT", "calmar": "10.5482"},
        {"commit": "day_x", "session": "DAY", "calmar": "99.0000"},
    ]
    assert _best_row(rows, "NIGHT")["commit"] == "night_a"
    assert _best_row(rows, "DAY")["commit"] == "day_x"
    # and the unscoped comparison this replaced would have picked the DAY row
    assert max(rows, key=lambda r: float(r["calmar"]))["commit"] == "day_x"


def test_a_rejected_row_cannot_become_the_best_known_config():
    """W5b's overfit cell is in `results.tsv` as evidence, and evidence must not
    be mistaken for a recommendation. A `reject` row outscoring every `keep` row
    is the normal case for a rejected cell, not an anomaly."""
    rows = [
        {"commit": "day_keep", "session": "DAY", "calmar": "4.6805", "status": "keep"},
        {"commit": "day_overfit", "session": "DAY", "calmar": "14.7697",
         "status": "reject"},
    ]
    assert _best_row(rows, "DAY")["commit"] == "day_keep"


def test_best_row_is_none_when_a_session_has_no_rows():
    """A DAY pin before any DAY tuning has happened must fail loudly, not
    silently compare against NIGHT."""
    rows = [{"commit": "night_a", "session": "NIGHT", "calmar": "13.6742"}]
    assert _best_row(rows, "DAY") is None


# ---- PINNED_DAY (W6 / C1) ---------------------------------------------------
#
# The same drift guards as PINNED, applied to the second deployment. C1 asked
# for "a drift test keyed to a session-tagged row" — these are it. They are
# written against `config_named` rather than the constant so that adding a third
# deployment later cannot leave it unguarded by accident.


def _row_for(config):
    """The row a deployment is pinned to: its session's best `keep` row **on the
    tape vintage that pin was measured against**.

    Not a lookup by commit. W5a and W5b were produced by one code commit
    (`fdd1348`), so three DAY rows share it and the commit alone does not
    identify a row -- unlike the NIGHT chain, where every edit:/keep: step was a
    separate commit. Resolving through `_best_row` is what the pin actually
    means anyway: "the best known configuration for this deployment".

    The vintage comes from the pinned commit's own row (TAPE_REBUILD_PLAN T6).
    Without it "best" spans tapes, and after the rebuild that means a Calmar of
    16.3122 that no file on disk can reproduce outranks the 3.6006 the same
    parameters actually score on the tape now shipped."""
    session = _session_of(config)
    vintage = next((r["dataset"] for r in _rows()
                    if r["commit"] == config.source_commit
                    and r["session"] == session), None)
    row = _best_row(_rows(), session, vintage)
    if row is None:
        pytest.fail(f"results.tsv has no {session} rows to pin against")
    return row


@pytest.mark.skipif(not RESULTS.exists(), reason="results.tsv not present")
@pytest.mark.parametrize("name", sorted(CONFIGS))
def test_every_deployment_is_pinned_to_the_best_keep_row_for_its_session(name):
    """Fails when research finds something better *for that deployment*, and
    when a pin drifts from the row it claims. The session scoping is what lets
    two pins coexist: a DAY row cannot re-pin NIGHT."""
    cfg = config_named(name)
    session = _session_of(cfg)
    assert _rows_for(session), f"results.tsv has no {session} rows"
    row = _row_for(cfg)
    assert row["status"] == "keep", (
        f"{name} is pinned to a {row['status']!r} row — a rejected result is "
        f"evidence, not a configuration"
    )
    assert row["commit"] == cfg.source_commit, (
        f"results.tsv now has a better {session} row: {row['commit']} "
        f"calmar={row['calmar']} vs pinned {cfg.source_commit} "
        f"calmar={cfg.source_calmar}. Re-pin live/config.py deliberately."
    )
    assert float(row["calmar"]) == pytest.approx(cfg.source_calmar), (
        f"{name}'s pinned Calmar disagrees with results.tsv — provenance was "
        f"edited without the values, or vice versa"
    )


@pytest.mark.skipif(not RESULTS.exists(), reason="results.tsv not present")
@pytest.mark.parametrize("name", sorted(CONFIGS))
def test_every_deployment_row_agrees_with_its_policy_and_dataset(name):
    """The pairing C2 exists to protect. A policy that disagrees with the row it
    was tuned on is the 2026-08-11 defect, and it starts exactly here."""
    cfg = config_named(name)
    row = _row_for(cfg)
    assert row["session"] == _session_of(cfg)
    row_file = row["dataset"].split("@")[0]
    assert row_file.lower() in cfg.source_dataset.lower().replace("\\", "/")
    assert _session_of(cfg).lower() in cfg.source_dataset.lower(), (
        f"{name}'s dataset {cfg.source_dataset!r} does not name its own session"
    )


def test_the_two_deployments_do_not_share_a_tuned_band():
    """W5a, MEASURED. Running NIGHT's band on the day tape is Calmar
    16.3122 -> 0.2671; the two deployments having equal parameters would mean
    one of them is untuned."""
    assert PINNED_DAY.buy_threshold != PINNED.buy_threshold
    assert PINNED_DAY.min_prob != PINNED.min_prob
    # ...but the model itself was never re-tuned, so these must still agree
    assert PINNED_DAY.checkpoint == PINNED.checkpoint
    assert PINNED_DAY.context_length == PINNED.context_length
    assert PINNED_DAY.forecast_stride_bars == PINNED.forecast_stride_bars
    assert PINNED_DAY.forecast_horizon_bars == PINNED.forecast_horizon_bars


def test_day_inherits_the_risk_shape_the_backtest_measured():
    """C8 is open, not silently answered. `forced_close=False` here is the same
    deliberate choice as NIGHT's: median hold is ~4 days on the day tape too, so
    closing at every session end would be a different strategy."""
    assert PINNED_DAY.aggression_ticks == 0
    assert PINNED_DAY.max_position == 1
    assert PINNED_DAY.max_aggression_ticks is None
    assert PINNED_DAY.forced_close_on_session_end is False


def test_day_provenance_records_that_it_has_not_earned_a_deployment():
    """The caveats are part of the value, not a comment above it: the config
    travels into the recorder's meta header, so an artifact carries them even
    when the plan is not to hand.

    The `provisional` marker this used to require was resolved by
    TAPE_REBUILD_PLAN T7 -- the pin now names a measurement on the shipped tape.
    What T7 did not resolve is that the cell still trails its own passive floor
    and has still never traded, so those two are what the note must carry."""
    note = PINNED_DAY.source_note.lower()
    assert "never traded" in note
    assert "buy-and-hold" in note or "buy and hold" in note
