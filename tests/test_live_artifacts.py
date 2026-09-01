"""A live run leaves the same evidence a backtest does.

Until 2026-09-02 a live run left `session.jsonl` and `ticks.csv` while a backtest
left five tabular artifacts plus `params.json` and a rendered report, so the two
halves of the project could not be compared without writing a projection — and
`reports.generate_report`, which only ever wanted three CSVs in a directory,
could not be pointed at a soak at all.

The fix is deliberately *not* a projection. `LiveLoop.result()` already returns a
`BacktestResult`, so `live/artifacts.py` hands that object to the backtest's own
`write_all`. These cases pin the two claims that follow from it — the schemas are
identical because one writer produces both, and the report generator accepts a
live directory unchanged — plus the three gates that decide *when* a soak writes,
which is the only part that is genuinely new.
"""
import types

import pandas as pd
import pytest

from forecast_eval.environment import Environment
from forecast_eval.events import FillEvent
from forecast_eval.execution import Execution
from forecast_eval.live.artifacts import ArtifactWriter, live_params
from forecast_eval.live.broker.sim import SimBroker
from forecast_eval.live.config import PINNED
from forecast_eval.live.loop import LiveLoop
from forecast_eval.logging_io import write_all
from forecast_eval.portfolio import Portfolio
from forecast_eval.strategy.threshold import ThresholdEmitter
from forecast_eval.trader import Trader

from forecast_eval.tests.test_live_parity import (
    FEE_RATE, HISTORY_WINDOW, MULTIPLIER, _make_forecaster, _run_backtest,
    _ticks,
)

ARTIFACTS = ("fills", "orders", "forecasts", "signals", "trades")


def _emitter():
    return ThresholdEmitter(buy_threshold=0.0005, sell_threshold=-0.0005)


def _live_loop(ticks):
    """Drive a real LiveLoop and hand back the loop itself, not its result.

    `test_live_parity._run_live` returns `loop.result()`; the writer needs the
    loop, because its change gate reads the accumulating lists.
    """
    execution = Execution(fee_rate=FEE_RATE)
    broker = SimBroker(execution)
    loop = LiveLoop(
        forecaster=_make_forecaster(),
        emitter=_emitter(),
        broker=broker,
        trader=Trader(execution=broker, tick_size=1.0),
        portfolio=Portfolio(contract_multiplier=MULTIPLIER),
        exit_rule=None,
        forced_close_on_session_end=False,
        history_window=HISTORY_WINDOW,
    )
    for market in Environment(ticks).stream():
        loop.on_bar(market)
    return loop


def _runtime(loop, roll_fills=None):
    """The duck type the writer consumes: `.loop`, and optionally `.roll_fills`."""
    return types.SimpleNamespace(loop=loop, roll_fills=roll_fills or set())


@pytest.fixture(scope="module")
def live_dir(tmp_path_factory):
    d = tmp_path_factory.mktemp("live")
    loop = _live_loop(_ticks(n=12_000))
    writer = ArtifactWriter(d)
    writer.write_params(live_params(PINNED, out_dir=d, broker="dryrun",
                                    warmup_days=30, soak_id="soak_test"))
    assert writer.write(_runtime(loop), force=True) is not None
    return d, loop


@pytest.fixture(scope="module")
def backtest_dir(tmp_path_factory):
    d = tmp_path_factory.mktemp("backtest")
    result = _run_backtest(_ticks(n=12_000), emitter=_emitter(),
                           exit_rule=None, forced_close=False)
    write_all(result, d, auto_report=False)
    return d


def test_a_live_run_writes_every_artifact_a_backtest_writes(live_dir, backtest_dir):
    """The headline. A soak's directory is readable by everything that reads a
    backtest's, because both are written by `write_all` over a `BacktestResult`."""
    live, _ = live_dir
    live_names = {p.stem for p in live.iterdir() if p.is_file()}
    bt_names = {p.stem for p in backtest_dir.iterdir() if p.is_file()}
    assert set(ARTIFACTS) <= live_names, f"live is missing {set(ARTIFACTS) - live_names}"
    assert set(ARTIFACTS) <= bt_names
    assert (live / "params.json").exists()


@pytest.mark.parametrize("name", ARTIFACTS)
def test_the_columns_are_identical_to_the_backtests(name, live_dir, backtest_dir):
    """A regression guard against the projection this deliberately is not: if
    someone later writes these files from `session.jsonl` by hand, the schemas
    drift and every downstream reader has to learn two shapes."""
    live, _ = live_dir
    lp = live / f"{name}.csv"
    bp = next(backtest_dir.glob(f"{name}.*"))
    read = pd.read_parquet if bp.suffix == ".parquet" else pd.read_csv
    assert lp.exists(), f"live artifacts must be CSV like real_data.py's: {lp}"
    assert list(pd.read_csv(lp).columns) == list(read(bp).columns)


def test_generate_report_renders_from_a_live_run_directory(live_dir):
    """The second half of the point. `generate_report` wants fills, trades and
    signals in a directory and nothing else — it never knew what produced them,
    and now a soak can supply them."""
    matplotlib = pytest.importorskip("matplotlib")
    del matplotlib
    from forecast_eval.reports import generate_report

    live, _ = live_dir
    written = generate_report(live)
    assert written, "no report artifacts were produced"
    assert (live / "report.html").exists()


# ---- the gates, which are the only genuinely new behaviour -----------------


def test_nothing_is_written_when_no_list_has_grown(live_dir):
    """A soak spends ten of every twenty-four hours outside its session. Without
    the change gate it would rewrite five files a minute, byte for byte, for the
    whole dead zone."""
    _, loop = live_dir
    writer = ArtifactWriter(_tmp(), min_interval_seconds=0.0)
    rt = _runtime(loop)
    assert writer.write(rt) is not None          # first write always lands
    assert writer.write(rt) is None              # nothing changed since
    assert writer.writes == 1


def test_the_throttle_bounds_how_often_the_files_are_touched(live_dir):
    """The clock tick calls this once a second; the interval is what keeps that
    from becoming the run's dominant I/O."""
    _, loop = live_dir
    writer = ArtifactWriter(_tmp(), min_interval_seconds=3600.0)
    rt = _runtime(loop)
    assert writer.write(rt) is not None
    loop.fills.append(_fill("2024-01-02 09:00:00", "BUY"))   # content DID change
    assert writer.write(rt) is None, "the throttle should still hold it back"
    loop.fills.pop()


def test_force_ignores_both_gates(live_dir):
    """Shutdown must land the last bars even if the throttle just fired and even
    if the counts happen to match."""
    _, loop = live_dir
    writer = ArtifactWriter(_tmp(), min_interval_seconds=3600.0)
    rt = _runtime(loop)
    assert writer.write(rt) is not None
    assert writer.write(rt) is None
    assert writer.write(rt, force=True) is not None
    assert writer.writes == 2


def test_a_write_failure_is_counted_and_never_raised(live_dir, monkeypatch):
    """These files record a live run; they never feed one. A full disk must not
    be able to stop a process that is holding a position."""
    _, loop = live_dir
    writer = ArtifactWriter(_tmp(), min_interval_seconds=0.0)

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr("forecast_eval.live.artifacts.write_all", boom)
    assert writer.write(_runtime(loop), force=True) is None
    assert writer.failures == 1
    assert writer.summary()["artifact_failures"] == 1


def test_a_rolled_position_stays_one_trade(live_dir):
    """`roll_fills` has to reach `build_trades`, or a position carried across a
    contract roll is booked as two round trips live and one in the backtest —
    a disagreement with nothing to do with trading."""
    _, loop = live_dir
    fills = [_fill("2024-02-01 09:00:00", "BUY"), _fill("2024-02-02 09:00:00", "SELL"),
             _fill("2024-02-02 09:00:00", "BUY"), _fill("2024-02-03 09:00:00", "SELL")]
    stub = types.SimpleNamespace(
        loop=types.SimpleNamespace(
            fills=fills, orders=[], forecasts=[], signals=[],
            result=lambda: _result_with(loop, fills)),
        roll_fills={pd.Timestamp("2024-02-02 09:00:00")})

    d = _tmp()
    ArtifactWriter(d).write(stub, force=True)
    n_rolled = len(pd.read_csv(d / "trades.csv"))

    stub.roll_fills = set()
    d2 = _tmp()
    ArtifactWriter(d2).write(stub, force=True)
    n_plain = len(pd.read_csv(d2 / "trades.csv"))

    assert n_rolled == 1 and n_plain == 2, (n_rolled, n_plain)


def test_params_json_carries_the_backtests_strategy_keys():
    """`params.json` is what a report reads for its Settings block, and what
    someone diffs when a live run disagrees with the backtest it came from. The
    shared keys have to be spelled the same on both sides."""
    p = live_params(PINNED, out_dir="outputs/x", broker="shioaji",
                    warmup_days=90, soak_id="soak_1")
    for key in ("checkpoint", "context_length", "horizon_bars", "stride_bars",
                "buy_threshold", "sell_threshold", "aggression_ticks",
                "min_prob", "max_position", "forced_close"):
        assert key in p, key
    assert p["broker"] == "shioaji" and p["soak_id"] == "soak_1"
    assert p["sessions"] == ["NIGHT"]
    assert p["source_commit"] == PINNED.source_commit


# ---- helpers ---------------------------------------------------------------

_counter = iter(range(1_000))


def _tmp():
    import tempfile
    from pathlib import Path
    return Path(tempfile.mkdtemp(prefix=f"art{next(_counter)}_"))


def _fill(ts, side):
    return FillEvent(timestamp=pd.Timestamp(ts), side=side, fill_price=100.0,
                     quantity=1, fee=0.0)


def _result_with(loop, fills):
    r = loop.result()
    r.fills = fills
    r.orders = []
    r.forecasts = []
    r.signals = []
    return r
