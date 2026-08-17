"""V4 probe — the pricing safety argument, without a login.

The probe's whole no-fill guarantee is one arithmetic function, and the
round-trip mode's bounded-risk argument is another. Both are pinned here; the
network half needs a session and is exercised by running it.
"""
import pytest

from forecast_eval.live.v4_probe import (
    PASSIVE_OFFSET_POINTS, build_parser, marketable_limit, passive_limit,
)

REFERENCE = 44_949.0


# ---- the passive limit cannot fill -----------------------------------------

def test_the_passive_limit_is_strictly_below_the_market():
    assert passive_limit(REFERENCE) < REFERENCE


def test_the_tick_snap_can_only_move_it_further_away():
    """Flooring, not rounding. Rounding could snap the price back *towards* the
    market, which is the one direction the safety argument forbids."""
    for reference in (44_949.0, 44_949.5, 44_950.7, 17_500.3):
        limit = passive_limit(reference, tick_size=1.0)
        assert limit <= reference - PASSIVE_OFFSET_POINTS


def test_the_offset_stays_inside_the_daily_band():
    """TAIFEX rejects orders outside ±10 %, and such a rejection would masquerade
    as 'the venue refused our order' — the exact thing the probe is testing."""
    assert PASSIVE_OFFSET_POINTS / REFERENCE < 0.10


def test_the_offset_is_far_enough_to_be_unreachable_in_seconds():
    """It only has to survive the few seconds before the cancel."""
    assert PASSIVE_OFFSET_POINTS / REFERENCE > 0.005


# ---- the marketable limit must actually cross ------------------------------

def test_the_marketable_limit_is_above_the_market():
    """At the touch an order may simply join the queue — §2.1's whole point — so
    a fill-seeking order has to cross."""
    assert marketable_limit(REFERENCE) > REFERENCE


def test_the_marketable_limit_stays_bounded():
    """Above the touch, but still a limit: the worst price is knowable before it
    is sent, which is what an unbounded MKT gives up."""
    limit = marketable_limit(REFERENCE)
    assert limit - REFERENCE <= 50.0


def test_both_limits_land_on_the_tick_grid():
    for fn in (passive_limit, marketable_limit):
        assert fn(44_949.5, tick_size=1.0) % 1.0 == 0.0


# ---- the mode that can hold a position is opt-in ---------------------------

def test_the_default_mode_cannot_fill():
    assert build_parser().parse_args([]).allow_fill is False


def test_the_filling_mode_is_explicit():
    assert build_parser().parse_args(["--allow-fill"]).allow_fill is True
