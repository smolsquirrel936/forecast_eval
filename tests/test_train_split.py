"""The train/test split — TRAIN_TEST_SPLIT_PLAN §5.

The loop tunes on a window; this is what keeps the window honest. G2 is the
load-bearing one: the whole design rests on a whole-data run's prefix being
exactly what a run that stopped at the split would have produced, so if G2
fails the design is wrong, not the test.
"""
import math

import pandas as pd
import pytest

from forecast_eval.events import FillEvent, Forecast
from forecast_eval.metrics import compute_metrics
from forecast_eval.portfolio import Portfolio
from forecast_eval.run import BacktestResult


def _same(a, b) -> bool:
    """Deep equality that treats NaN as equal to NaN.

    Every trading block carries NaN where a ratio is undefined (no losing
    trade yet, one trade only), and `nan != nan` — so a plain `==` on two
    genuinely identical packs fails. The metric values, not the float
    identity rules, are what these tests are about.
    """
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    if isinstance(a, float) and isinstance(b, float):
        return (a == b) or (math.isnan(a) and math.isnan(b))
    return a == b


def _ts(minutes: int) -> pd.Timestamp:
    return pd.Timestamp("2024-01-02 09:00:00") + pd.Timedelta(minutes=minutes)


def _fill(side, price, ts, fee=0.1) -> FillEvent:
    return FillEvent(timestamp=ts, side=side, fill_price=price,
                     quantity=1, fee=fee)


def _result(fills, signals=(), forecasts=()):
    """A BacktestResult whose portfolio is the fills actually replayed."""
    pf = Portfolio(contract_multiplier=200.0)
    for f in fills:
        pf.apply_fill(f, "DAY")
    return BacktestResult(
        portfolio=pf, n_bars=len(fills), n_signals=len(signals),
        n_orders=len(fills), n_forecasts=len(forecasts),
        fills=list(fills), signals=list(signals),
        forecasts=list(forecasts),
        last_price=fills[-1].fill_price if fills else None,
    )


def _signal(ts, direction, price):
    return {"timestamp": ts, "direction": direction, "price": price,
            "session": "DAY"}


def _ticks(n=80) -> pd.DataFrame:
    return pd.DataFrame({
        "timestamp": [_ts(i) for i in range(n)],
        "price": [100.0 + i for i in range(n)],
        "volume": [1.0] * n,
    })


# Four closed round-trips at minutes 0/10, 20/30, 40/50, 60/70.
FILLS = [
    _fill("BUY", 100, _ts(0)),   _fill("SELL", 108, _ts(10)),
    _fill("BUY", 110, _ts(20)),  _fill("SELL", 104, _ts(30)),
    _fill("BUY", 105, _ts(40)),  _fill("SELL", 115, _ts(50)),
    _fill("BUY", 120, _ts(60)),  _fill("SELL", 112, _ts(70)),
]
SIGNALS = [
    _signal(_ts(0), "BUY", 100), _signal(_ts(10), "SELL", 108),
    _signal(_ts(20), "BUY", 110), _signal(_ts(30), "SELL", 104),
    _signal(_ts(40), "BUY", 105), _signal(_ts(50), "SELL", 115),
    _signal(_ts(60), "BUY", 120), _signal(_ts(70), "SELL", 112),
]
FORECASTS = [Forecast(timestamp=_ts(i), horizon_bars=5, payload={
    "predicted_return": 0.001 if i % 20 == 0 else -0.001})
    for i in range(0, 80, 10)]


# -------- G2: the causality claim --------------------------------------------

@pytest.mark.parametrize("cut", [10, 30, 35, 50, 70])
def test_g2_windowed_pack_equals_a_run_that_stopped_there(cut):
    """A whole-data run windowed at `cut` == a run that ended at `cut`.

    This is the property that makes one backtest pass enough
    (TRAIN_TEST_SPLIT_PLAN §2.1). Everything else in the feature is
    bookkeeping on top of it.
    """
    split = _ts(cut)
    whole = _result(FILLS, SIGNALS, FORECASTS)
    truncated = _result(
        [f for f in FILLS if f.timestamp <= split],
        [s for s in SIGNALS if s["timestamp"] <= split],
        [f for f in FORECASTS if f.timestamp <= split],
    )
    ticks = _ticks()

    windowed = compute_metrics(whole, ticks, through=split)
    stopped = compute_metrics(truncated, ticks)

    assert _same(windowed["trading"], stopped["trading"])
    assert _same(windowed["attribution"], stopped["attribution"])
    assert _same(windowed["forecast"], stopped["forecast"])
    assert _same(windowed["trades"], stopped["trades"])


def test_g2_holds_under_forced_close_bucketing():
    """The session-bucketed shape is windowed the same way."""
    split = _ts(30)
    whole = _result(FILLS, SIGNALS, FORECASTS)
    truncated = _result(
        [f for f in FILLS if f.timestamp <= split],
        [s for s in SIGNALS if s["timestamp"] <= split],
        [f for f in FORECASTS if f.timestamp <= split],
    )
    ticks = _ticks()

    windowed = compute_metrics(whole, ticks, forced_close=True, through=split)
    stopped = compute_metrics(truncated, ticks, forced_close=True)
    assert _same(windowed["trading_day"], stopped["trading_day"])
    assert _same(windowed["trading_night"], stopped["trading_night"])


# -------- G3: the default changes nothing ------------------------------------

def test_g3_through_none_is_the_whole_pack():
    res, ticks = _result(FILLS, SIGNALS, FORECASTS), _ticks()
    assert _same(compute_metrics(res, ticks),
                 compute_metrics(res, ticks, through=None))


def test_g3_through_at_or_past_the_last_event_is_the_whole_pack():
    """train_frac 1.0 lands the split on the final bar — same numbers."""
    res, ticks = _result(FILLS, SIGNALS, FORECASTS), _ticks()
    whole = compute_metrics(res, ticks)
    assert _same(compute_metrics(res, ticks, through=_ts(70)), whole)
    assert _same(compute_metrics(res, ticks, through=_ts(79)), whole)


# -------- G4: the straddling trade ------------------------------------------

def test_g4_a_trade_open_across_the_split_is_counted_by_neither_side():
    """Entry at 60 is before the split, exit at 70 after it: dropped.

    `build_trades` books a round-trip only on its closing fill, so this is
    also what a run that stopped at the split would have reported.
    """
    split = _ts(65)
    res, ticks = _result(FILLS, SIGNALS, FORECASTS), _ticks()

    train = compute_metrics(res, ticks, through=split)
    assert train["trading"]["n_trades"] == 3
    assert all(pd.Timestamp(t["exit_ts"]) <= split
               for t in train["trades"]["records"])
    # The whole-data pack still sees all four.
    assert compute_metrics(res, ticks)["trading"]["n_trades"] == 4


def test_g4_the_open_legs_fee_is_not_charged_to_the_train_window():
    """Net PnL is realized minus fees paid, both windowed to the split."""
    split = _ts(65)
    res, ticks = _result(FILLS, SIGNALS, FORECASTS), _ticks()
    train = compute_metrics(res, ticks, through=split)

    # Realized: +8, -6, +10 on the three closed round-trips; six legs at 0.1.
    assert train["trading"]["gross_pnl_points"] == pytest.approx(12.0)
    assert train["trading"]["total_fees_points"] == pytest.approx(0.6)
    # Attribution reads the replayed Portfolio, which HAS paid the open leg's
    # entry fee at minute 60 — seven legs, not six. That asymmetry is the same
    # one the whole-data path carries, which is why the Portfolio is replayed
    # rather than re-summed from trade records.
    assert train["attribution"]["realized_net_pnl_points"] == pytest.approx(
        12.0 - 0.7)


# -------- G1 / G5 / G6: the CLI ---------------------------------------------

import json

from forecast_eval import real_data


def _tape(path, rows=240):
    """A minute tape inside DAY hours, written where the CLI can load it.

    240 minutes from 09:00 lands at 12:59, so every row is in-session and
    `n_session == rows` — the split is denominated in in-session bars, and a
    tape that spilled past 13:45 would silently make the two differ.
    """
    ts = pd.date_range("2024-01-02 09:00:00", periods=rows, freq="1min")
    pd.DataFrame({
        "datetime": ts,
        "contract": ["TXF202401"] * rows,
        "open": [100.0 + i for i in range(rows)],
        "high": [100.0 + i for i in range(rows)],
        "low": [100.0 + i for i in range(rows)],
        "close": [100.0 + i for i in range(rows)],
        "volume": [10.0] * rows,
    }).to_csv(path, index=False)
    return path


def _run(tmp_path, monkeypatch, *flags, rows=240):
    """Drive real_data.main with the model and the run loop stubbed out.

    The split arithmetic, the banner, the two metric packs and `params.json`
    all live in `main`, above `run_backtest`. Faking the loop is what lets
    them be tested without a GPU or a 3.9 GB checkpoint.
    """
    data = _tape(tmp_path / "tape.csv", rows)
    out = tmp_path / "out"

    monkeypatch.setattr(real_data, "Toto2Forecaster",
                        lambda **kw: object())
    # Four round-trips spread across the eval window (bars 100..239), so a
    # split at 50 % lands cleanly between whole trades.
    fills, signals = [], []
    for i, base in enumerate((110, 140, 180, 210)):
        fills += [_fill("BUY", 100 + base, _ts(base)),
                  _fill("SELL", 108 + base, _ts(base + 10))]
        signals += [_signal(_ts(base), "BUY", 100 + base),
                    _signal(_ts(base + 10), "SELL", 108 + base)]
    forecasts = [Forecast(timestamp=_ts(i), horizon_bars=5,
                          payload={"predicted_return": 0.001})
                 for i in range(100, rows, 20)]
    monkeypatch.setattr(real_data, "run_backtest",
                        lambda *a, **kw: _result(fills, signals, forecasts))

    rc = real_data.main([
        "--data", str(data), "--output-dir", str(out),
        "--warmup-bars", "100", "--context-length", "10", *flags,
    ])
    return rc, out


def test_g1_frac_derives_the_split_and_bars_overrides_it(tmp_path, monkeypatch,
                                                         capsys):
    # 240 in-session bars, warmup 100 -> eval_bars 140; 0.5 -> 70 train bars.
    rc, out = _run(tmp_path, monkeypatch, "--train-frac", "0.5")
    assert rc == 0
    assert json.loads((out / "params.json").read_text())[
        "train_split_bars"] == 70
    assert "├─ train (50.0%) : 70 bars" in capsys.readouterr().out

    # An explicit bar count wins outright, fraction ignored.
    rc, out = _run(tmp_path, monkeypatch,
                   "--train-frac", "0.5", "--train-bars", "111")
    assert rc == 0
    params = json.loads((out / "params.json").read_text())
    assert params["train_split_bars"] == 111
    assert "[--train-bars 111]" in capsys.readouterr().out


@pytest.mark.parametrize("flags", [
    ("--train-frac", "0"),
    ("--train-frac", "1.5"),
    ("--train-frac", "-0.2"),
])
def test_g1_a_train_frac_outside_the_range_is_refused(tmp_path, monkeypatch,
                                                      flags):
    rc, _ = _run(tmp_path, monkeypatch, *flags)
    assert rc == 2


@pytest.mark.parametrize("bars", ["0", "141", "-5"])
def test_g1_train_bars_is_bounded_by_the_eval_window(tmp_path, monkeypatch,
                                                     bars, capsys):
    """Not by the in-session count and not by the raw row count."""
    rc, _ = _run(tmp_path, monkeypatch, "--train-bars", bars)
    assert rc == 2
    assert "must be in (0, 140]" in capsys.readouterr().err


# -------- G5: not a strategy knob -------------------------------------------

def test_g5_neither_flag_is_a_strategy_knob():
    assert "train_frac" not in real_data.CLI_STRATEGY_DEFAULTS
    assert "train_bars" not in real_data.CLI_STRATEGY_DEFAULTS


@pytest.mark.parametrize("session", list(real_data.strategy_wiring.SESSIONS))
def test_g5_no_package_declares_the_scoring_window(session):
    """A package must not be able to choose its own measurement."""
    declared = real_data.strategy_wiring.params(session)
    assert "train_frac" not in declared
    assert "train_bars" not in declared


def test_g5_param_cannot_reach_the_scoring_window():
    """`parse_overrides` forwards an unknown name uncoerced by design — the
    refusal lives one layer down, in `make_strategy`, which is where a typo
    is caught too."""
    with pytest.raises(Exception):
        real_data.strategy_wiring.make_strategy(
            "night", warmup_bars=10, tick_size=1.0, train_frac=0.7)


# -------- G6: the report stays whole-data ------------------------------------

def test_g6_the_report_bundle_is_whole_data_while_the_split_is_active(
        tmp_path, monkeypatch, capsys):
    rc, out = _run(tmp_path, monkeypatch, "--train-frac", "0.5")
    assert rc == 0
    params = json.loads((out / "params.json").read_text())

    # All four round-trips reach the artifacts, not the two inside the train
    # window — the bundle the report renders is the whole tape.
    trades = pd.read_csv(out / "trades.csv")
    assert len(trades) == 4

    printed = capsys.readouterr().out
    assert "THE DECISION NUMBERS" in printed
    assert "WHOLE data -- report figure, do NOT record" in printed
    assert params["train_split_bars"] == 70


def test_g6b_params_key_order_is_deliberate(tmp_path, monkeypatch):
    _, out = _run(tmp_path, monkeypatch, "--train-frac", "0.5")
    keys = list(json.loads((out / "params.json").read_text()))
    assert keys[-5:] == ["eval_bars", "train_frac", "train_split_bars",
                         "train_split_ts", "command"]
    assert "train_bars" not in keys


def test_g6c_a_pinned_split_records_none_not_one(tmp_path, monkeypatch):
    """`1.0` would read as "no split" on a run that has one."""
    _, out = _run(tmp_path, monkeypatch, "--train-bars", "70")
    params = json.loads((out / "params.json").read_text())
    assert params["train_frac"] is None
    assert params["train_split_bars"] == 70


def test_g6_default_run_records_the_whole_window(tmp_path, monkeypatch, capsys):
    """No split: train_frac 1.0, the whole eval window, and no extra block."""
    rc, out = _run(tmp_path, monkeypatch)
    assert rc == 0
    params = json.loads((out / "params.json").read_text())
    assert params["train_frac"] == 1.0
    assert params["train_split_bars"] == 140

    printed = capsys.readouterr().out
    assert "=== Trading metrics ===" in printed
    assert "THE DECISION NUMBERS" not in printed
    assert "do NOT record" not in printed
    assert "HELD OUT" not in printed


# -------- G7: the ledgers ----------------------------------------------------

def test_g7_every_ledger_carries_the_split_columns():
    import csv
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    for name in ("results.tsv", "results_day.tsv", "results_night.tsv",
                 "results_fullday.tsv"):
        with (root / name).open(encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh, delimiter="\t"))
        header_seen = (root / name).read_text(encoding="utf-8").splitlines()[0]
        assert "train_frac" in header_seen, name
        assert "train_split_bars" in header_seen, name
        for r in rows:
            if not any(v for v in r.values()):
                continue          # the blank trailing lines in results_fullday
            assert r["train_frac"], f"{name}: blank train_frac"
            assert r["train_split_bars"], f"{name}: blank train_split_bars"


# -------- Report rendering of the split -------------------------------------

def test_train_frac_renders_as_an_unsigned_percentage():
    """`0.8` reads as `80%`, not `+0.800000` and not `+80.00%`."""
    from forecast_eval.reports import fmt_value
    assert fmt_value(0.8, "train_frac") == "80%"
    assert fmt_value(1.0, "train_frac") == "100%"
    assert fmt_value(0.755, "train_frac") == "75.5%"
    assert fmt_value(0.3333, "train_frac") == "33.33%"
    # Every other float keeps the signed 6-decimal form it always had.
    assert fmt_value(0.8, "lookback_frac") == "+0.800000"


def test_a_pinned_run_still_shows_a_percentage_in_settings():
    """`--train-bars` stores train_frac=None; the table derives the share."""
    from forecast_eval.reports import _kv_table_html
    html = _kv_table_html({"eval_bars": 40000, "train_frac": None,
                           "train_split_bars": 32000})
    assert ">80%<" in html


def test_settings_falls_back_when_the_share_cannot_be_derived():
    """No eval_bars (an older bundle) -> the stored value, untouched."""
    from forecast_eval.reports import _kv_table_html
    html = _kv_table_html({"train_frac": None, "train_split_bars": 32000})
    assert ">None<" in html


def test_split_ts_coercion_is_safe_on_old_bundles():
    from forecast_eval.reports import _split_ts
    assert _split_ts(None) is None
    assert _split_ts("not a date") is None
    assert _split_ts("2026-08-07 19:49:00") == pd.Timestamp("2026-08-07 19:49:00")


def test_the_boundary_line_is_drawn_on_all_three_figures(tmp_path, monkeypatch):
    """Every figure in the report marks the split, and none does without one."""
    import matplotlib
    matplotlib.use("Agg")
    from forecast_eval import reports

    drawn = []
    monkeypatch.setattr(reports, "_axvline_split",
                        lambda ax, ts, **kw: drawn.append(("mpl", ts)))
    monkeypatch.setattr(reports, "_add_vline_split",
                        lambda fig, ts, **kw: drawn.append(("plotly", ts)))

    _, out = _run(tmp_path, monkeypatch, "--train-frac", "0.5")
    split = pd.Timestamp(json.loads(
        (out / "params.json").read_text())["train_split_ts"])

    # 4 matplotlib axes (equity + drawdown, price, attribution) and, when plotly
    # is installed, 3 interactive figures.
    mpl = [ts for kind, ts in drawn if kind == "mpl"]
    assert len(mpl) == 4, drawn
    assert all(ts == split for ts in mpl)
    if reports._HAS_PLOTLY:
        assert [ts for kind, ts in drawn if kind == "plotly"] == [split] * 3


def test_no_split_means_no_boundary_line(tmp_path, monkeypatch):
    import matplotlib
    matplotlib.use("Agg")
    from forecast_eval import reports

    seen = []
    monkeypatch.setattr(reports, "_axvline_split",
                        lambda ax, ts, **kw: seen.append(ts))
    monkeypatch.setattr(reports, "_add_vline_split",
                        lambda fig, ts, **kw: seen.append(ts))
    _run(tmp_path, monkeypatch)
    assert seen and all(ts is None for ts in seen), seen


def test_a_recorded_boundary_at_the_last_bar_is_not_drawn():
    """A default run records train_split_ts too — it is just the last bar."""
    from forecast_eval.reports import _active_split_ts
    assert _active_split_ts({"eval_bars": 140, "train_split_bars": 140,
                             "train_split_ts": "2024-01-02 12:59:00"}) is None
    assert _active_split_ts({"eval_bars": 140, "train_split_bars": 70,
                             "train_split_ts": "2024-01-02 11:49:00"}) == \
        pd.Timestamp("2024-01-02 11:49:00")
    assert _active_split_ts(None) is None
    assert _active_split_ts({}) is None
