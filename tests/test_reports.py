"""Report rendering: value formatting, dual currency axes, HTML sections.

Pins the invariants that are otherwise only visible by opening report.html —
above all that the right-hand currency axis stays an exact ``point_value``
rescale of the left-hand points axis on *both* panels. Nothing links those
ranges automatically, so an edit could silently unlink them.
"""
import json
import math

import matplotlib
matplotlib.use("Agg")  # must precede the reports import (it pulls in pyplot)

import pandas as pd
import pytest

from forecast_eval.reports import (
    _HAS_PLOTLY,
    _buy_and_hold_curve,
    _currency_axis_range,
    _equity_series,
    _summary_sections_html,
    fmt_value,
    generate_report,
    plot_equity_drawdown_mpl,
    plot_equity_drawdown_plotly,
)

PV = 200.0


def _trades(nets, *, start="2024-01-02 09:00:00", step_min=30,
            hold_min=0) -> pd.DataFrame:
    """A trades frame with the columns the equity curve needs.

    `entry_ts` defaults to the exit, so the zero origin lands on the first exit
    and the buy-and-hold window is unchanged; pass `hold_min` for a real holding
    period.
    """
    ts = pd.date_range(start, periods=len(nets), freq=f"{step_min}min")
    return pd.DataFrame({"entry_ts": ts - pd.to_timedelta(hold_min, unit="m"),
                         "exit_ts": ts, "net_pnl_points": nets})


def _ticks(prices, *, start="2024-01-02 09:00:00") -> pd.DataFrame:
    ts = pd.date_range(start, periods=len(prices), freq="1min")
    return pd.DataFrame({"timestamp": ts, "price": [float(p) for p in prices]})


# -------- fmt_value: percent semantics ---------------------------------------

def test_fmt_value_percent_already_scaled_gets_suffix_only():
    """percent_profitable is computed as *100 already — must not double-scale."""
    assert fmt_value(48.571428, "percent_profitable") == "+48.57%"


@pytest.mark.parametrize("key,val,want", [
    ("hit_rate", 0.4857142, "+48.57%"),
    ("direction_hit_rate", 0.495484, "+49.55%"),
    ("annual_rate_of_return", -0.034656, "-3.47%"),
])
def test_fmt_value_fraction_fields_scale_by_100(key, val, want):
    assert fmt_value(val, key) == want


def test_percent_profitable_and_hit_rate_agree():
    """The two spellings of the same quantity must render identically."""
    assert fmt_value(0.4857142, "hit_rate") == fmt_value(48.57142, "percent_profitable")


@pytest.mark.parametrize("key,want", [
    ("ratio_avg_win_avg_loss", "+65.87%"),   # avg win as a share of avg loss
    ("fee_drag", "+65.87%"),                 # share of gross PnL lost to fees
])
def test_fmt_value_proportion_ratios_render_as_percent(key, want):
    assert fmt_value(0.658680, key) == want


@pytest.mark.parametrize("key", [
    "sharpe_per_trade",          # mean/std — not a share of anything
    "sortino_per_trade",
    "information_coefficient",   # rank correlation
])
def test_fmt_value_leaves_non_proportion_ratios_alone(key):
    assert fmt_value(0.658680, key) == "+0.658680"


@pytest.mark.parametrize("key", ["fee_rate", "lookback_frac"])
def test_fmt_value_leaves_settings_decimal(key):
    """Settings are typed on the CLI as decimals; the report must match the flag."""
    assert fmt_value(0.00015, key) == "+0.000150"


def test_fmt_value_non_finite_does_not_get_percent_suffix():
    assert fmt_value(float("nan"), "hit_rate") == "+nan"
    assert fmt_value(float("inf"), "percent_profitable") == "+inf"


def test_fmt_value_without_key_and_non_floats():
    assert fmt_value(0.5) == "+0.500000"          # no key -> plain float form
    assert fmt_value(0.5, "unknown_field") == "+0.500000"
    assert fmt_value(7, "hit_rate") == "7"        # ints bypass percent handling
    assert fmt_value(None, "hit_rate") == "None"


# -------- _currency_axis_range ----------------------------------------------

def test_currency_range_is_exact_rescale_of_points_range():
    lo_hi, c_lo_hi = _currency_axis_range([pd.Series([-10.0, 5.0])], PV)
    for p, c in zip(lo_hi, c_lo_hi):
        assert c == pytest.approx(p * PV)


def test_currency_range_spans_every_series():
    """Both the strategy and buy-and-hold curves must fit inside the range."""
    (lo, hi), _ = _currency_axis_range(
        [pd.Series([-10.0, 5.0]), pd.Series([-40.0, 2.0])], PV,
    )
    assert lo <= -40.0 and hi >= 5.0


def test_currency_range_include_zero_pulls_in_the_baseline():
    """Drawdown fills to zero, so its span must contain zero even when all-negative."""
    (lo, hi), _ = _currency_axis_range([pd.Series([-30.0, -5.0])], PV,
                                       include_zero=True)
    assert lo <= 0.0 <= hi
    # Without the flag the span stays around the data.
    (lo2, hi2), _ = _currency_axis_range([pd.Series([-30.0, -5.0])], PV)
    assert hi2 < 0.0


def test_currency_range_none_when_nothing_to_plot():
    assert _currency_axis_range([], PV) is None
    assert _currency_axis_range([None, pd.Series([], dtype=float)], PV) is None


def test_currency_range_flat_series_still_has_width():
    (lo, hi), (clo, chi) = _currency_axis_range([pd.Series([3.0, 3.0])], PV)
    assert hi > lo
    assert chi == pytest.approx(hi * PV) and clo == pytest.approx(lo * PV)


# -------- equity / buy-and-hold series ---------------------------------------

def test_equity_series_is_cumulative_net_and_drawdown_vs_peak():
    eq = _equity_series(_trades([10.0, -5.0, 3.0]))
    assert list(eq["equity"]) == pytest.approx([10.0, 5.0, 8.0])
    assert list(eq["drawdown"]) == pytest.approx([0.0, -5.0, -2.0])


def test_equity_series_opens_at_zero_on_the_first_entry():
    """A curve that opens at its first exit's value hides the step it exists to
    show — and with a single trade it is one point, which `mode="lines"` draws
    as nothing at all. MEASURED on a live run that had made +359.59 and rendered
    an empty Cumulative PnL panel."""
    eq = _equity_series(_trades([359.59], hold_min=90))
    assert len(eq) == 2
    assert list(eq["equity"]) == pytest.approx([0.0, 359.59])
    assert list(eq["drawdown"]) == pytest.approx([0.0, 0.0])
    assert eq["timestamp"].iloc[0] == pd.Timestamp("2024-01-02 07:30:00")


def test_equity_series_needs_no_origin_when_entry_is_unknown():
    """Older bundles have no `entry_ts`; the origin is skipped, not invented."""
    t = _trades([10.0, -5.0]).drop(columns=["entry_ts"])
    assert list(_equity_series(t)["equity"]) == pytest.approx([10.0, 5.0])


def test_equity_series_sorts_by_exit_time():
    t = _trades([10.0, -5.0])
    t = t.iloc[::-1].reset_index(drop=True)          # feed it out of order
    eq = _equity_series(t)
    assert list(eq["equity"]) == pytest.approx([10.0, 5.0])


def test_equity_series_empty_in_empty_out():
    assert _equity_series(pd.DataFrame(columns=["exit_ts", "net_pnl_points"])).empty


def test_buy_and_hold_curve_charges_the_fee():
    eq = _equity_series(_trades([1.0, 1.0], step_min=1))
    bh = _buy_and_hold_curve(eq, _ticks([100.0, 110.0, 120.0]), 0.001)
    # First point: entry == price, so only the round-trip fee remains.
    assert bh["cum_bh_pnl_points"].iloc[0] == pytest.approx(-(100.0 + 100.0) * 0.001)
    # A fee-free hold would be +10 at the second print; the fee makes it strictly less.
    assert bh["cum_bh_pnl_points"].iloc[1] < 10.0


def test_buy_and_hold_curve_degrades_to_empty_without_prices():
    eq = _equity_series(_trades([1.0]))
    assert _buy_and_hold_curve(eq, None, 0.001).empty
    assert _buy_and_hold_curve(pd.DataFrame(columns=["timestamp"]),
                               _ticks([100.0]), 0.001).empty


# -------- matplotlib renderer ------------------------------------------------

def test_mpl_writes_a_png_and_pins_both_axes(tmp_path):
    out = tmp_path / "equity.png"
    plot_equity_drawdown_mpl(_trades([10.0, -5.0, 3.0]), out,
                             ticks=_ticks(range(100, 200)), point_value=PV)
    assert out.exists() and out.stat().st_size > 0


def test_mpl_handles_a_run_with_no_trades(tmp_path):
    """A run that never traded must still produce a well-formed chart."""
    out = tmp_path / "empty.png"
    plot_equity_drawdown_mpl(pd.DataFrame(columns=["exit_ts", "net_pnl_points"]),
                             out, ticks=_ticks([100.0, 101.0]), point_value=PV)
    assert out.exists() and out.stat().st_size > 0


# -------- plotly renderer ----------------------------------------------------

pytestmark_plotly = pytest.mark.skipif(not _HAS_PLOTLY, reason="plotly not installed")


@pytestmark_plotly
@pytest.mark.parametrize("primary,secondary,label", [
    ("yaxis", "yaxis2", "equity"),
    ("yaxis3", "yaxis4", "drawdown"),
])
def test_plotly_currency_axis_mirrors_points_axis(primary, secondary, label):
    """The regression this file exists for: the two scales must stay linked."""
    fig = plot_equity_drawdown_plotly(
        _trades([10.0, -5.0, 3.0]), ticks=_ticks(range(100, 200)), point_value=PV,
    )
    prim = fig.layout[primary].range
    sec = fig.layout[secondary].range
    assert prim is not None, f"{label}: points axis not pinned"
    assert sec is not None, f"{label}: currency axis missing"
    for p, c in zip(prim, sec):
        assert c == pytest.approx(p * PV), f"{label}: axes unlinked"


@pytestmark_plotly
@pytest.mark.parametrize("axis,label", [("y2", "equity"), ("y4", "drawdown")])
def test_plotly_secondary_axis_has_a_trace_bound_to_it(axis, label):
    """Plotly only *draws* an axis some trace references.

    Pinning a range in the layout is not enough — an axis with no trace stays
    invisible, which is how the currency axes shipped unrendered once already.
    """
    fig = plot_equity_drawdown_plotly(
        _trades([10.0, -5.0, 3.0]), ticks=_ticks(range(100, 200)), point_value=PV,
    )
    bound = [t for t in fig.data if t.yaxis == axis]
    assert bound, f"{label}: no trace bound to {axis}, axis will not render"


@pytestmark_plotly
def test_plotly_axis_carrier_is_invisible_and_scaled():
    """The carrier must be transparent, legend-less, hover-less — and in currency."""
    trades = _trades([10.0, -5.0, 3.0])
    fig = plot_equity_drawdown_plotly(trades, ticks=_ticks(range(100, 200)),
                                      point_value=PV)
    carrier = next(t for t in fig.data if t.yaxis == "y2")
    assert carrier.showlegend is False
    assert carrier.hoverinfo == "skip"
    assert carrier.line.color == "rgba(0,0,0,0)"
    eq = _equity_series(trades)
    assert list(carrier.y) == pytest.approx(list(eq["equity"] * PV))


@pytestmark_plotly
def test_plotly_toggle_masks_cover_every_trace():
    """Stale masks would blank a carrier (killing an axis) or the drawdown."""
    fig = plot_equity_drawdown_plotly(
        _trades([10.0, -5.0, 3.0]), ticks=_ticks(range(100, 200)), point_value=PV,
    )
    buttons = fig.layout.updatemenus[0].buttons
    bh_idx = next(i for i, t in enumerate(fig.data) if t.name == "buy & hold")
    for b in buttons:
        mask = b.args[0]["visible"]
        assert len(mask) == len(fig.data), "mask does not cover every trace"
    assert all(buttons[0].args[0]["visible"]), "'both' view must show everything"
    off = [i for i, v in enumerate(buttons[1].args[0]["visible"]) if not v]
    assert off == [bh_idx], "'Strategy only' must hide buy & hold and nothing else"


@pytestmark_plotly
def test_plotly_curve_fits_inside_the_pinned_range():
    """Pinning the range must not clip the data it was derived from."""
    trades = _trades([10.0, -5.0, 3.0])
    fig = plot_equity_drawdown_plotly(trades, ticks=_ticks(range(100, 200)),
                                      point_value=PV)
    lo, hi = fig.layout.yaxis.range
    eq = _equity_series(trades)
    assert lo <= eq["equity"].min() and eq["equity"].max() <= hi


@pytestmark_plotly
def test_plotly_drawdown_range_includes_zero():
    fig = plot_equity_drawdown_plotly(_trades([-5.0, -3.0]), point_value=PV)
    lo, hi = fig.layout.yaxis3.range
    assert lo <= 0.0 <= hi


@pytestmark_plotly
def test_plotly_point_value_scales_only_the_secondary_axis():
    kw = dict(ticks=_ticks(range(100, 200)))
    a = plot_equity_drawdown_plotly(_trades([10.0, -5.0]), point_value=PV, **kw)
    b = plot_equity_drawdown_plotly(_trades([10.0, -5.0]), point_value=50.0, **kw)
    assert a.layout.yaxis.range == b.layout.yaxis.range           # points unchanged
    for ca, cb in zip(a.layout.yaxis2.range, b.layout.yaxis2.range):
        assert ca == pytest.approx(cb * 4)                        # 200 / 50


@pytestmark_plotly
def test_plotly_no_trades_leaves_ranges_unpinned():
    fig = plot_equity_drawdown_plotly(
        pd.DataFrame(columns=["exit_ts", "net_pnl_points"]), point_value=PV,
    )
    assert fig.layout.yaxis2.range is None
    assert fig.layout.yaxis4.range is None


# -------- HTML summary sections ----------------------------------------------

def test_summary_renders_trade_analysis_under_the_trading_block():
    html = _summary_sections_html(
        None,
        {"trading": {"n_trades": 3}, "trade_analysis": {"percent_profitable": 66.667}},
        None,
    )
    assert "<h3>Trading metrics [trading]</h3>" in html
    assert "<h3>Trade analysis</h3>" in html
    assert html.index("Trading metrics") < html.index("Trade analysis")
    assert "+66.67%" in html          # formatted through fmt_value


def test_summary_renders_annual_returns_rows_as_percentages():
    html = _summary_sections_html(
        None,
        {"trading": {"n_trades": 3},
         "annual_returns": {"rows": [
             {"year": 2022, "rate_of_return": 0.02, "net_pnl_currency": 1234.5678,
              "partial_year": False},
             {"year": 2023, "rate_of_return": -0.0098, "net_pnl_currency": -9.87654,
              "partial_year": True},
         ]}},
        None,
    )
    assert "<h3>Annual returns (by calendar year of exit)</h3>" in html
    assert html.index("Trading metrics") < html.index("Annual returns")
    assert "<th>year</th>" in html
    # 3 decimals, comma-grouped digits — this table's convention (fmt_annual_value)
    assert "+2.000%" in html and "-0.980%" in html
    assert "+1,234.568" in html and "-9.877" in html
    # integer-valued columns stay literal: the year must not be grouped into 2,022
    assert ">2022<" in html


def test_summary_omits_annual_returns_when_empty():
    html = _summary_sections_html(
        None, {"trading": {"n_trades": 0}, "annual_returns": {"rows": []}}, None,
    )
    assert "Annual returns" not in html


def test_summary_omits_missing_sections():
    html = _summary_sections_html(None, {"trading": {"n_trades": 0}}, None)
    assert "<h2>Settings</h2>" not in html
    assert "<h3>Trade analysis</h3>" not in html
    assert "<h3>Forecast quality</h3>" not in html
    assert _summary_sections_html(None, None, None) == ""


def test_summary_skips_the_trades_record_list():
    """`trades` holds records, not scalars — it has no table form."""
    html = _summary_sections_html(
        None, {"trading": {"n_trades": 1}, "trades": {"records": [{"a": 1}]}}, None,
    )
    assert "records" not in html


def test_summary_renders_settings_including_command():
    html = _summary_sections_html(
        {"command": "python -m forecast_eval.real_data --n-bars 10",
         "point_value": 200.0}, None, None,
    )
    assert "<h2>Settings</h2>" in html
    assert "python -m forecast_eval.real_data --n-bars 10" in html


# -------- generate_report ----------------------------------------------------

def _write_bundle(d, params=None):
    """Minimal fills/trades/signals bundle that generate_report can consume."""
    d.mkdir(parents=True, exist_ok=True)
    _trades([10.0, -5.0]).to_csv(d / "trades.csv", index=False)
    pd.DataFrame({
        "timestamp": pd.date_range("2024-01-02 09:00:00", periods=2, freq="30min"),
        "side": ["BUY", "SELL"], "fill_price": [100.0, 110.0],
    }).to_csv(d / "fills.csv", index=False)
    pd.DataFrame(columns=["timestamp", "direction", "price"]).to_csv(
        d / "signals.csv", index=False)
    if params is not None:
        (d / "params.json").write_text(json.dumps(params), encoding="utf-8")


def test_generate_report_raises_naming_the_missing_artifacts(tmp_path):
    (tmp_path / "run").mkdir()
    with pytest.raises(FileNotFoundError) as e:
        generate_report(tmp_path / "run")
    assert "fills" in str(e.value) and "trades" in str(e.value)


def test_generate_report_reads_point_value_from_params(tmp_path):
    """A standalone re-render must pick up the run's own point_value."""
    d = tmp_path / "run"
    _write_bundle(d, params={"point_value": 50.0, "fee_rate": 0.0002})
    paths = generate_report(d)
    assert paths["equity_png"].exists()
    if _HAS_PLOTLY:
        html = paths["html"].read_text(encoding="utf-8")
        assert "currency @ 50" in html


def test_generate_report_defaults_point_value_for_older_bundles(tmp_path):
    """Run dirs written before point_value existed fall back to the TXF value."""
    d = tmp_path / "run"
    _write_bundle(d, params={"fee_rate": 0.00015})
    paths = generate_report(d)
    if _HAS_PLOTLY:
        assert "currency @ 200" in paths["html"].read_text(encoding="utf-8")


def test_generate_report_survives_a_malformed_params_file(tmp_path):
    """A bad params.json must not take down a finished backtest's report."""
    d = tmp_path / "run"
    _write_bundle(d)
    (d / "params.json").write_text("{not json", encoding="utf-8")
    assert generate_report(d)["equity_png"].exists()


def test_both_attribution_curves_open_from_flat():
    """The panel IS the gap between these two lines, so they have to start at the
    same height. `signal_attribution_curve` emits one row per close, which opened
    it at the first round trip's PnL while the realized line opened at zero."""
    from forecast_eval.reports import _curves

    signals = pd.DataFrame([
        {"timestamp": pd.Timestamp("2024-01-02 09:00"), "direction": "HOLD",
         "price": 100.0},
        {"timestamp": pd.Timestamp("2024-01-02 09:30"), "direction": "BUY",
         "price": 100.0},
        {"timestamp": pd.Timestamp("2024-01-02 10:30"), "direction": "SELL",
         "price": 112.0},
    ])
    sig, real = _curves(signals, _trades([10.0], start="2024-01-02 10:30",
                                         hold_min=60))
    assert list(sig["cum_signal_pnl_points"]) == pytest.approx([0.0, 12.0])
    assert list(real["cum_realized_pnl_points"]) == pytest.approx([0.0, 10.0])
    # Both open at the moment a side was taken, not at the close.
    assert sig["timestamp"].iloc[0] == pd.Timestamp("2024-01-02 09:30")
    assert real["timestamp"].iloc[0] == pd.Timestamp("2024-01-02 09:30")


def test_a_hold_only_signal_stream_still_has_no_curve():
    """No side was ever taken, so there is no origin to open from either."""
    from forecast_eval.reports import _curves

    signals = pd.DataFrame([{"timestamp": pd.Timestamp("2024-01-02 09:00"),
                             "direction": "HOLD", "price": 100.0}])
    sig, _ = _curves(signals, _trades([]))
    assert sig.empty
