"""The §7.2 probe's one piece of real logic: the safety of its limit price.

The probe places an order. Its entire safety argument is that the order is
passive by construction — a BUY strictly below the market cannot fill on
submission. That property is worth testing even though the rest of the script
needs a live session.
"""
import pytest

from forecast_eval.live.v72_probe import probe_limit_price


def test_limit_is_strictly_below_the_reference():
    assert probe_limit_price(24000.0, 500.0) < 24000.0


def test_offset_is_applied_in_points():
    assert probe_limit_price(24000.0, 500.0) == 23500.0


def test_snapping_moves_away_from_the_market_never_toward_it():
    """Flooring, not rounding: the tick snap must only ever increase the
    distance from the touch."""
    for ref in (24000.7, 24000.5, 24000.2, 23999.9):
        got = probe_limit_price(ref, 500.0)
        assert got <= ref - 500.0 + 1e-9
        assert got < ref


def test_result_sits_on_the_tick_grid():
    for ref in (24000.7, 17501.3, 23456.9):
        assert probe_limit_price(ref, 500.0) % 1.0 == pytest.approx(0.0)


def test_a_larger_offset_is_strictly_safer():
    assert probe_limit_price(24000.0, 1000.0) < probe_limit_price(24000.0, 500.0)


def test_default_offset_stays_inside_a_ten_percent_daily_band():
    """TAIFEX caps daily moves at ±10%; an order priced outside the band is
    rejected, which would masquerade as 'futures unsupported in simulation'."""
    for ref in (17000.0, 24000.0, 30000.0):
        limit = probe_limit_price(ref, 500.0)
        assert limit > ref * 0.90, f"500pts breaches the band at ref={ref}"
