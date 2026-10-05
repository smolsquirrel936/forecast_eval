"""Rebuilding a finished run's artifacts from its `session.jsonl`.

`live/artifacts.py` is the answer for a *running* deployment — one writer, no
projection. This is the answer for runs that ended before that existed, where the
loop object is gone and the event stream is all that survives. The cases below
pin the three things that make the reconstruction trustworthy rather than
plausible:

  * the **clock**, because one file carries two timestamp shapes and getting it
    wrong is silent in every other respect;
  * the **guard**, because a reconstruction must never quietly replace artifacts
    a run wrote from its own loop;
  * the **schema**, because the whole point is that a rebuilt directory is read
    by everything that reads a backtest's.
"""
import json

import pandas as pd
import pytest

from forecast_eval.strategy import wiring

from forecast_eval.tools.session_artifacts import (VENUE_UTC_OFFSET, check_alignment,
                                                   main, read_records, rebuild)

# A run shaped like `live_20260824_150112`: ISO local timestamps on bars, orders
# and signals, and a float epoch on fills — the same instant, eight hours apart.
RECORDS = [
    {"event": "meta", "config": "LiveConfig[x]"},
    {"event": "bar", "timestamp": "2026-08-24T20:00:00", "price": 44890.0},
    {"event": "forecast", "timestamp": "2026-08-24T20:01:00", "horizon_bars": 30,
     "payload": {"predicted_return": -0.001, "predicted_price": 44800.0,
                 "last_price": 44890.0}},
    {"event": "signal", "timestamp": "2026-08-24T20:01:00", "direction": "SELL",
     "price": 44890.0, "session": "NIGHT", "strength": 0.7},
    {"event": "order", "timestamp": "2026-08-24T20:01:00", "side": "SELL",
     "limit_price": 44890.0, "intent": "OPEN", "quantity": 1},
    # 1787572931.325908 is 12:02:11 UTC == 20:02:11 in Taipei
    {"event": "fill", "timestamp": 1787572931.325908, "side": "SELL",
     "fill_price": 44893.0, "quantity": 1, "fee": 6.73},
    {"event": "bar", "timestamp": "2026-08-24T23:34:00", "price": 44520.0},
    {"event": "fill", "timestamp": 1787585580.947707, "side": "BUY",
     "fill_price": 44520.0, "quantity": 1, "fee": 6.68},
]


def _session(tmp_path, records=RECORDS):
    d = tmp_path / "live_20260824_150112"
    d.mkdir()
    with (d / "session.jsonl").open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")
    return d


def test_the_two_timestamp_shapes_land_on_one_clock(tmp_path):
    """The load-bearing conversion. A fill's epoch is UTC and everything else is
    naive Taipei local, so an unconverted fill lands eight hours before the order
    that produced it — and nothing else would look wrong."""
    result = rebuild(RECORDS)
    assert str(result.fills[0].timestamp) == "2026-08-24 20:02:11.325907946"
    assert result.orders[0].timestamp == pd.Timestamp("2026-08-24 20:01:00")
    assert result.fills[0].timestamp > result.orders[0].timestamp
    assert VENUE_UTC_OFFSET.total_seconds() == 8 * 3600


def test_a_wrong_clock_is_caught_by_the_bars(tmp_path):
    """`check_alignment` is what makes the offset a checked assumption. A fill
    outside the run's own bar range is the one symptom a bad offset produces."""
    assert check_alignment(rebuild(RECORDS), RECORDS) is None

    off = [dict(r) for r in RECORDS]
    for r in off:
        if r["event"] == "fill":
            r["timestamp"] = float(r["timestamp"]) - 8 * 3600   # "forgot" the offset
    warning = check_alignment(rebuild(off), off)
    assert warning and "outside the run's bars" in warning


def test_the_rebuilt_lists_are_the_real_event_types(tmp_path):
    """Built as `FillEvent` / `OrderEvent` / `Forecast`, not as dict rows, so a
    change to the events breaks this loudly instead of writing the old shape."""
    result = rebuild(RECORDS)
    assert [type(x).__name__ for x in result.fills] == ["FillEvent"] * 2
    assert type(result.orders[0]).__name__ == "OrderEvent"
    assert type(result.forecasts[0]).__name__ == "Forecast"
    assert result.forecasts[0].payload["predicted_return"] == -0.001
    assert result.n_bars == 2


def test_the_round_trip_pairs_and_prices_correctly(tmp_path):
    """End to end: the SHORT opened at 44893 and closed at 44520 is +373 gross."""
    d = _session(tmp_path)
    assert main([str(d), "--no-report"]) == 0
    trades = pd.read_csv(d / "trades.csv")
    assert len(trades) == 1
    assert trades.iloc[0]["side"] == "SHORT"
    assert trades.iloc[0]["pnl_points"] == pytest.approx(373.0)


@pytest.mark.parametrize("name", ["fills", "orders", "forecasts", "signals", "trades"])
def test_every_artifact_is_written_in_the_backtests_schema(name, tmp_path):
    from forecast_eval.logging_io import _EMPTY_COLUMNS

    d = _session(tmp_path)
    main([str(d), "--no-report"])
    assert list(pd.read_csv(d / f"{name}.csv").columns) == _EMPTY_COLUMNS[name]


def test_it_refuses_to_overwrite_artifacts_a_run_wrote_itself(tmp_path, capsys):
    """A run from `live/artifacts.py` wrote its artifacts from the loop. Those are
    the primary record; replacing them with a reconstruction is a downgrade, and
    doing it silently is worse."""
    d = _session(tmp_path)
    (d / "fills.csv").write_text("timestamp,side,fill_price,quantity,fee\n")

    assert main([str(d), "--no-report"]) == 3
    assert "already has" in capsys.readouterr().err
    assert main([str(d), "--no-report", "--force"]) == 0


def test_a_partial_final_line_is_skipped(tmp_path):
    """The recorder flushes per line so the trail survives a kill -9, which means
    the last line of a killed run can be half-written."""
    d = _session(tmp_path)
    with (d / "session.jsonl").open("a", encoding="utf-8") as fh:
        fh.write('{"event": "fill", "timestamp": 178757')
    assert len(read_records(d / "session.jsonl")) == len(RECORDS)


def test_a_missing_session_file_is_an_error_not_a_crash(tmp_path):
    assert main([str(tmp_path), "--no-report"]) == 2


# ---- seeding the ledger, which is a migration and not the ordinary path ------


def _epoch(run_dir, soak_id="soak_test"):
    import json as _json
    (run_dir.parent / "soak_epoch.json").write_text(
        _json.dumps({"soak_id": soak_id, "retired": []}), encoding="utf-8")


def test_seeding_writes_the_deployments_ledger(tmp_path):
    """The migration this exists for: the position a soak holds can predate the
    ledger, and then --resume has nothing to replay even though the history is
    sitting in a log on disk."""
    from forecast_eval.live.config import PINNED
    from forecast_eval.live.ledger import FillLedger, ledger_path, net_position

    d = _session(tmp_path)
    _epoch(d)
    assert main([str(d), "--no-report", "--seed-ledger", "--config", "night"]) == 0

    led = FillLedger(ledger_path(tmp_path, PINNED.policy), soak_id="soak_test")
    replayed = led.replay()
    assert len(replayed) == 2
    assert net_position(replayed) == 0        # this fixture opens and closes


def test_seeding_twice_is_refused(tmp_path, capsys):
    """Appending again doubles every fill, which breaks the reconciliation in
    cli.py — and that check is the only thing between a wrong ledger and a
    confidently wrong trades.csv."""
    d = _session(tmp_path)
    _epoch(d)
    assert main([str(d), "--no-report", "--seed-ledger"]) == 0
    assert main([str(d), "--no-report", "--force", "--seed-ledger"]) == 4
    assert "already has rows" in capsys.readouterr().err


def test_seeding_needs_a_soak_to_tag_the_rows_with(tmp_path, capsys):
    """Untagged rows would be replayed by every soak, including one that
    --fresh-start deliberately started clean."""
    d = _session(tmp_path)
    assert main([str(d), "--no-report", "--seed-ledger"]) == 4
    assert "--soak-id" in capsys.readouterr().err


def test_day_and_night_ledgers_stay_separate_when_seeding(tmp_path):
    """The C5 rule again: seeding a night run must not put fills where a DAY
    deployment will replay them."""
    from forecast_eval.live.config import PINNED_DAY
    from forecast_eval.live.ledger import FillLedger, ledger_path

    d = _session(tmp_path)
    _epoch(d)
    main([str(d), "--no-report", "--seed-ledger", "--config", "night"])
    day = FillLedger(ledger_path(tmp_path, PINNED_DAY.policy), soak_id="soak_test")
    assert day.replay() == []


# ---- params.json, which is what puts the Settings block on the report --------


def _meta(cfg, **kw):
    """A `meta` header shaped the way `live/cli.py` writes one.

    Since LIVE_STRATEGY_ROUTING_PLAN Phase 5 that is the `config` summary *plus*
    the provenance fields the summary line cannot carry — the package, its
    content sha, the repo commit and the per-knob origins. They live beside the
    line rather than inside it so `_DESCRIBE_RE` keeps parsing sessions recorded
    before they existed.
    """
    from forecast_eval.live import artifacts
    from forecast_eval.tools.session_artifacts import params_from_meta
    from pathlib import Path
    pkg = cfg.strategy_package
    rec = {"event": "meta", "config": cfg.describe(), "broker": "shioaji",
           "strategy_package": pkg,
           "strategy_sha": artifacts.strategy_sha(pkg),
           "git_commit": artifacts.git_commit(),
           "git_dirty": artifacts.git_dirty(),
           "strategy_origins": {k: pkg for k in wiring.params(pkg)}}
    rec.update(kw)
    return params_from_meta([rec], Path("outputs/live_x"))


@pytest.mark.parametrize("name", ["night", "day"])
def test_the_rebuilt_params_are_the_live_ones_key_for_key(name):
    """The drift guard. `live_params` and this parser both claim to describe the
    same run, and a rebuilt report is meant to be read beside a live one — so the
    key sets and the order have to match, and every field `describe()` carries
    has to come back as the value it went in as."""
    from forecast_eval.live.artifacts import live_params
    from forecast_eval.live.config import config_named
    from pathlib import Path

    cfg = config_named(name)
    if name == "night" and not cfg.is_tuned:
        # SIMTRADE_0929: night's pin is unprovenanced. `describe()` prints
        # UNTUNED/nan, so a rebuilt report reads the source as "UNTUNED";
        # live's params.json carries None. night_iter30's band is symmetric
        # (+-0.0004), so the band itself now round-trips -- only the source
        # does not (measured 2026-10-05). Not fixed on this branch.
        pytest.skip("night's unprovenanced pin cannot round-trip through the "
                    "one-line describe() summary")
    pkg = cfg.strategy_package
    live = live_params(cfg, strategy_params=wiring.params(pkg),
                       strategy_origins={k: pkg for k in wiring.params(pkg)},
                       out_dir=Path("outputs/live_x"), broker="shioaji",
                       warmup_days=30, soak_id="soak_test")
    got = _meta(cfg, soak_id="soak_test")

    assert list(got) == list(live)
    # The three the one-line summary does not carry; None, not a plausible guess.
    for key in ("source_dataset", "max_aggression_ticks", "warmup_days"):
        assert got[key] is None
    for key in live:
        if key not in ("source_dataset", "max_aggression_ticks", "warmup_days"):
            assert got[key] == live[key], key


def test_an_unparseable_meta_line_leaves_the_report_alone(tmp_path):
    """A run whose header this cannot read must still rebuild — without a
    Settings block, which is where it was before this existed."""
    from forecast_eval.tools.session_artifacts import params_from_meta

    assert params_from_meta([{"event": "meta", "config": "LiveConfig[x]"}],
                            tmp_path) is None
    assert params_from_meta([{"event": "bar"}], tmp_path) is None
    d = _session(tmp_path)                              # RECORDS' meta is "LiveConfig[x]"
    assert main([str(d), "--no-report"]) == 0
    assert not (d / "params.json").exists()


def test_a_runs_own_params_are_never_overwritten(tmp_path, capsys):
    """`--force` is about the artifacts the loop derives. `params.json` came from
    the run's own LiveConfig and is complete; this parser's output is a
    reconstruction from a one-line summary, so it is strictly a downgrade."""
    from forecast_eval.live.config import PINNED

    d = _session(tmp_path, [dict(RECORDS[0], config=PINNED.describe())] + RECORDS[1:])
    (d / "params.json").write_text('{"data": "live", "source_dataset": "kept"}',
                                   encoding="utf-8")
    assert main([str(d), "--no-report", "--force"]) == 0
    assert json.loads((d / "params.json").read_text())["source_dataset"] == "kept"
    assert "left alone" in capsys.readouterr().out


def test_the_settings_block_lands_on_the_rebuilt_report(tmp_path):
    """End to end: the section a rebuilt report was missing next to a live one."""
    pytest.importorskip("plotly")
    from forecast_eval.live.config import PINNED

    d = _session(tmp_path, [dict(RECORDS[0], config=PINNED.describe())] + RECORDS[1:])
    assert main([str(d)]) == 0
    html = (d / "report.html").read_text(encoding="utf-8")
    assert "<h2>Settings</h2>" in html
    # SIMTRADE_0929: an unprovenanced pin renders as UNTUNED, not None.
    assert (PINNED.source_commit or "UNTUNED") in html
