"""EntryPercentStop — the shared fraction-of-entry stop, plus the wiring
contract in all three session packages.

Mechanics (threshold arithmetic, side flip, exact-at-threshold firing, the
(0,1) construction guard) plus the per-session wiring contract: every
package's ``stop_pct`` defaults OFF so the unedited package builds no exit
rule, an explicit knob builds the shared rule, and an unknown override is
still refused.
"""
import importlib
from datetime import datetime

import pytest

from forecast_eval.events import MarketEvent
from forecast_eval.exits.base import PositionState
from forecast_eval.exits.pct_stop import EntryPercentStop

SESSIONS = ("day", "night", "fullday")


def _wiring(session):
    return importlib.import_module(
        f"forecast_eval.strategy.{session}.wiring"
    )


def _pos(side, entry):
    return PositionState(
        side=side,
        size=1,
        entry_price=entry,
        entry_bar_idx=0,
        entry_timestamp=datetime(2026, 1, 1, 9, 0),
    )


def _mkt(price):
    return MarketEvent(
        timestamp=datetime(2026, 1, 1, 9, 1), price=price, volume=1,
        session="DAY",
    )


class TestConstruction:
    @pytest.mark.parametrize("bad", [0.0, -0.01, 1.0, 2.5])
    def test_rejects_non_fractional_widths(self, bad):
        with pytest.raises(ValueError, match="stop_pct"):
            EntryPercentStop(stop_pct=bad)

    def test_accepts_fractional_width(self):
        assert EntryPercentStop(stop_pct=0.03).stop_pct == 0.03


class TestShouldExit:
    def test_long_fires_exactly_at_threshold(self):
        rule = EntryPercentStop(stop_pct=0.03)
        assert rule.should_exit(_pos("LONG", 20000.0), _mkt(19400.0), bar_idx=5)

    def test_long_fires_past_threshold(self):
        rule = EntryPercentStop(stop_pct=0.03)
        assert rule.should_exit(_pos("LONG", 20000.0), _mkt(19000.0), bar_idx=5)

    def test_long_holds_above_threshold(self):
        rule = EntryPercentStop(stop_pct=0.03)
        assert not rule.should_exit(
            _pos("LONG", 20000.0), _mkt(19400.01), bar_idx=5
        )

    def test_short_fires_exactly_at_threshold(self):
        rule = EntryPercentStop(stop_pct=0.03)
        assert rule.should_exit(_pos("SHORT", 20000.0), _mkt(20600.0), bar_idx=5)

    def test_short_holds_below_threshold(self):
        rule = EntryPercentStop(stop_pct=0.03)
        assert not rule.should_exit(
            _pos("SHORT", 20000.0), _mkt(20599.99), bar_idx=5
        )

    def test_width_scales_with_entry_price(self):
        # Tech: the same 3% width must be a wider point gap at a higher entry.
        # Why: the era-dependence the percent denomination exists to fix — a
        #       fixed point gap is a different fraction of the contract at
        #       2011 vs 2026 price levels.
        rule = EntryPercentStop(stop_pct=0.03)
        low_entry, high_entry = 7000.0, 24000.0
        assert not rule.should_exit(
            _pos("LONG", high_entry), _mkt(high_entry - 0.03 * low_entry),
            bar_idx=5,
        )
        assert rule.should_exit(
            _pos("LONG", low_entry), _mkt(low_entry - 0.03 * low_entry),
            bar_idx=5,
        )

    def test_stateless_across_positions(self):
        # Tech: query a rule against one position, then another with a
        #       different entry; the second answer must depend only on the
        #       second entry.
        # Why:  the rule has no arming/re-key machinery — after a stop fires
        #       and the strategy re-enters, the new position's snapshot must
        #       carry its own threshold.
        rule = EntryPercentStop(stop_pct=0.05)
        assert rule.should_exit(_pos("LONG", 10000.0), _mkt(9000.0), bar_idx=1)
        assert not rule.should_exit(_pos("LONG", 20000.0), _mkt(19050.0), bar_idx=2)
        assert rule.should_exit(_pos("SHORT", 15000.0), _mkt(15800.0), bar_idx=3)


class TestWiring:
    @pytest.mark.parametrize("session", SESSIONS)
    def test_knob_default_builds_what_params_declares(self, session):
        # Tech: whatever the package's PARAMS declares is what the unedited
        #       package builds — None means no exit rule, a value means the
        #       shared rule at exactly that width.
        # Why:  all three packages now carry adopted widths — day 0.10
        #       (day_pctstop_iter05, its one above-incumbent cell), night
        #       0.10 (the catastrophe rail, tightened from the inert 0.20
        #       by user go 2026-10-12, NIGHT_PCTSTOP_POSTMORTEM) and
        #       fullday 0.10 (the catastrophe rail, user decision
        #       2026-10-12, FULLDAY_PCTSTOP_POSTMORTEM). The invariant is
        #       that the package default and the built rule agree — pinning
        #       "None" here would make an adoption itself a test failure
        #       instead of a recorded decision.
        w = _wiring(session)
        strat = w.make_strategy(warmup_bars=10)
        declared = w.PARAMS["stop_pct"]
        if declared is None:
            assert strat["exit_rule"] is None
        else:
            assert isinstance(strat["exit_rule"], EntryPercentStop)
            assert strat["exit_rule"].stop_pct == declared

    @pytest.mark.parametrize("session", SESSIONS)
    def test_knob_builds_the_rule(self, session):
        # Tech: an explicit knob builds the SHARED rule class — one
        #       implementation, three packages.
        w = _wiring(session)
        strat = w.make_strategy(warmup_bars=10, stop_pct=0.03)
        rule = strat["exit_rule"]
        assert isinstance(rule, EntryPercentStop)
        assert rule.stop_pct == 0.03

    @pytest.mark.parametrize("session", SESSIONS)
    def test_unknown_override_still_refused(self, session):
        w = _wiring(session)
        with pytest.raises(ValueError, match="unknown strategy override"):
            w.make_strategy(warmup_bars=10, stop_loss_ticks=900)
