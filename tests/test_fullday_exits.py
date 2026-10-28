"""fullday's protection exits (reversal loop decisions 9/10, phase G).

G1 ``BreakEvenLock``: once the best price since entry is ``lock_after_pct`` in
the position's favour, exit on a return to the entry. G2 ``GivebackTrail``:
arms once the best price is 2% in favour (``GIVEBACK_ARM_PCT``), then exits
when the open gain falls to ``(1-giveback_frac)`` of its maximum. Both are
stateful (``adoptable=False``) and reset on a new entry identity; both compose
with ``stop_pct`` through ``AnyOf``.
"""
import pandas as pd
import pytest

from forecast_eval.events import MarketEvent
from forecast_eval.exits.pct_stop import EntryPercentStop
from forecast_eval.strategy.fullday.exits import (
    GIVEBACK_ARM_PCT,
    AnyOf,
    BreakEvenLock,
    ChannelExit,
    GivebackTrail,
)
from forecast_eval.strategy.fullday.wiring import make_strategy


def _mkt(price, minutes=0):
    return MarketEvent(
        timestamp=pd.Timestamp("2024-01-02 09:00:00") + pd.Timedelta(minutes=minutes),
        price=price,
        volume=1,
        session="DAY",
    )


def _pos(side="LONG", entry=100.0, bar=10):
    from forecast_eval.exits.base import PositionState

    return PositionState(
        side=side,
        size=1,
        entry_price=entry,
        entry_bar_idx=bar,
        entry_timestamp=pd.Timestamp("2024-01-02 09:00:00"),
    )


# -------- G1: BreakEvenLock -----------------------------------------------------

def test_lock_not_armed_never_fires_on_return_to_entry():
    lock = BreakEvenLock(lock_after_pct=0.02)
    # Best 101.5 < 102 (entry*1.02): never armed, so a return to 100 holds.
    for px in (100.5, 101.5, 101.0, 100.0):
        assert lock.should_exit(_pos(), _mkt(px), bar_idx=10) is False


def test_lock_armed_fires_at_return_to_entry_long():
    lock = BreakEvenLock(lock_after_pct=0.02)
    for px in (100.5, 102.0, 103.0):           # arms at 102
        assert lock.should_exit(_pos(), _mkt(px), bar_idx=10) is False
    # Price back to the entry -> exit (inclusive boundary).
    assert lock.should_exit(_pos(), _mkt(100.0), bar_idx=10) is True
    # And below it.
    assert lock.should_exit(_pos(), _mkt(99.5), bar_idx=10) is True


def test_lock_armed_fires_at_return_to_entry_short():
    lock = BreakEvenLock(lock_after_pct=0.02)
    for px in (99.5, 98.0, 97.0):              # arms at 98
        assert lock.should_exit(_pos("SHORT"), _mkt(px), bar_idx=10) is False
    assert lock.should_exit(_pos("SHORT"), _mkt(100.0), bar_idx=10) is True


def test_lock_holds_while_still_in_profit():
    lock = BreakEvenLock(lock_after_pct=0.02)
    for px in (100.5, 102.5, 102.0):
        assert lock.should_exit(_pos(), _mkt(px), bar_idx=10) is False
    # Armed, but price has not returned to the entry.
    assert lock.should_exit(_pos(), _mkt(101.0), bar_idx=10) is False


def test_lock_state_resets_on_a_new_entry():
    lock = BreakEvenLock(lock_after_pct=0.02)
    for px in (100.5, 102.0):                  # trade 1 arms
        lock.should_exit(_pos(bar=10), _mkt(px), bar_idx=10)
    # A new trade (different entry bar) starts unarmed even at the same prices.
    assert lock.should_exit(_pos(bar=42), _mkt(100.5), bar_idx=42) is False
    assert lock.should_exit(_pos(bar=42), _mkt(101.5), bar_idx=42) is False


def test_lock_rejects_bad_widths():
    with pytest.raises(ValueError, match="lock_after_pct"):
        BreakEvenLock(lock_after_pct=0.0)
    with pytest.raises(ValueError, match="lock_after_pct"):
        BreakEvenLock(lock_after_pct=1.0)


# -------- G2: GivebackTrail ----------------------------------------------------

def test_trail_never_fires_unarmed():
    # The arm gate: a trade that never earned 2% cannot fire, however deep
    # the retrace (the day_trail V-bottom lesson).
    trail = GivebackTrail(giveback_frac=0.5)
    for px in (100.5, 101.5, 101.0, 99.0, 95.0):
        assert trail.should_exit(_pos(), _mkt(px), bar_idx=10) is False


def test_trail_fires_on_giveback_once_armed_long():
    trail = GivebackTrail(giveback_frac=0.5)
    # entry 100, best 103 -> armed (>= 102), max_gain 3; fire at gain <= 1.5.
    for px in (100.5, 102.0, 103.0):
        assert trail.should_exit(_pos(), _mkt(px), bar_idx=10) is False
    assert trail.should_exit(_pos(), _mkt(101.6), bar_idx=10) is False
    assert trail.should_exit(_pos(), _mkt(101.5), bar_idx=10) is True
    assert trail.should_exit(_pos(), _mkt(101.0), bar_idx=10) is True


def test_trail_fires_on_giveback_once_armed_short():
    trail = GivebackTrail(giveback_frac=0.5)
    # entry 100 short, best 96 -> armed (<= 98), max_gain 4; fire at gain <= 2,
    # i.e. once price retraces to 98 or above.
    for px in (99.5, 98.0, 96.0):
        assert trail.should_exit(_pos("SHORT"), _mkt(px), bar_idx=10) is False
    assert trail.should_exit(_pos("SHORT"), _mkt(97.5), bar_idx=10) is False
    assert trail.should_exit(_pos("SHORT"), _mkt(98.0), bar_idx=10) is True


def test_trail_does_not_fire_on_the_arming_tick():
    trail = GivebackTrail(giveback_frac=0.5)
    for px in (100.5, 101.0):
        assert trail.should_exit(_pos(), _mkt(px), bar_idx=10) is False
    # The print that arms (102.0) has gain == max_gain: no fire.
    assert trail.should_exit(_pos(), _mkt(102.0), bar_idx=10) is False


def test_trail_width_grows_with_the_gain():
    trail = GivebackTrail(giveback_frac=0.5)
    for px in (102.0, 110.0):                  # max_gain 10, fire at gain <= 5
        assert trail.should_exit(_pos(), _mkt(px), bar_idx=10) is False
    assert trail.should_exit(_pos(), _mkt(105.1), bar_idx=10) is False
    assert trail.should_exit(_pos(), _mkt(105.0), bar_idx=10) is True


def test_trail_state_resets_on_a_new_entry():
    trail = GivebackTrail(giveback_frac=0.5)
    for px in (102.0, 110.0):                  # trade 1 arms and banks gain
        trail.should_exit(_pos(bar=10), _mkt(px), bar_idx=10)
    # A new trade starts unarmed at the same price levels.
    assert trail.should_exit(_pos(bar=42), _mkt(101.5), bar_idx=42) is False


def test_trail_rejects_bad_widths():
    with pytest.raises(ValueError, match="giveback_frac"):
        GivebackTrail(giveback_frac=0.0)
    with pytest.raises(ValueError, match="giveback_frac"):
        GivebackTrail(giveback_frac=1.0)


def test_giveback_arm_constant_is_two_percent():
    assert GIVEBACK_ARM_PCT == 0.02


# -------- AnyOf composition ----------------------------------------------------

def test_anyof_fires_when_any_rule_fires():
    stop = EntryPercentStop(stop_pct=0.05)
    lock = BreakEvenLock(lock_after_pct=0.02)
    combo = AnyOf([stop, lock])
    # Alone: the stop fires at 95, the lock does not (never armed).
    assert stop.should_exit(_pos(), _mkt(95.0), bar_idx=10) is True
    assert lock.should_exit(_pos(), _mkt(95.0), bar_idx=10) is False
    assert combo.should_exit(_pos(), _mkt(95.0), bar_idx=10) is True
    # Alone: the lock fires (armed + back to entry), the stop does not.
    for px in (100.5, 102.5):
        combo.should_exit(_pos(), _mkt(px), bar_idx=10)
    assert combo.should_exit(_pos(), _mkt(100.0), bar_idx=10) is True


def test_anyof_holds_when_no_rule_fires():
    combo = AnyOf([EntryPercentStop(stop_pct=0.05), BreakEvenLock(lock_after_pct=0.02)])
    assert combo.should_exit(_pos(), _mkt(99.0), bar_idx=10) is False


def test_anyof_adoptable_is_the_and_of_its_members():
    assert AnyOf([EntryPercentStop(stop_pct=0.05)]).adoptable is True
    assert AnyOf(
        [EntryPercentStop(stop_pct=0.05), BreakEvenLock(lock_after_pct=0.02)]
    ).adoptable is False


# -------- wiring: the control and the composition --------------------------------

def test_control_returns_the_bare_percent_stop():
    strat = make_strategy(
        warmup_bars=100,
        lock_after_pct=None, giveback_frac=None, channel_exit_bars=None,
    )
    assert type(strat["exit_rule"]) is EntryPercentStop
    assert strat["exit_rule"].stop_pct == 0.05


def test_lock_composes_with_the_stop_through_anyof():
    strat = make_strategy(warmup_bars=100, lock_after_pct=0.02)
    rule = strat["exit_rule"]
    assert type(rule) is AnyOf
    assert len(rule.rules) == 2
    assert rule.adoptable is False


def test_giveback_composes_with_the_stop_through_anyof():
    strat = make_strategy(
        warmup_bars=100, lock_after_pct=None, giveback_frac=0.5
    )
    rule = strat["exit_rule"]
    assert type(rule) is AnyOf
    assert len(rule.rules) == 2


def test_all_three_exit_rules_compose():
    strat = make_strategy(warmup_bars=100, lock_after_pct=0.02, giveback_frac=0.5)
    assert len(strat["exit_rule"].rules) == 3


def test_no_exit_knobs_set_means_no_exit_rule():
    strat = make_strategy(
        warmup_bars=100, stop_pct=None,
        lock_after_pct=None, giveback_frac=None, channel_exit_bars=None,
    )
    assert strat["exit_rule"] is None


def test_wiring_rejects_bad_lock_after_pct():
    with pytest.raises(ValueError, match="lock_after_pct"):
        make_strategy(warmup_bars=100, lock_after_pct=0.0)
    with pytest.raises(ValueError, match="lock_after_pct"):
        make_strategy(warmup_bars=100, lock_after_pct=1.5)


def test_wiring_rejects_bad_giveback_frac():
    with pytest.raises(ValueError, match="giveback_frac"):
        make_strategy(warmup_bars=100, giveback_frac=0.0)
    with pytest.raises(ValueError, match="giveback_frac"):
        make_strategy(warmup_bars=100, giveback_frac=1.5)


# -------- H: ChannelExit -------------------------------------------------------

def test_channel_long_exits_below_the_prior_low():
    ch = ChannelExit(channel_exit_bars=3)
    # Four in-position prints (age >= 3): window = [100, 103, 102], and 99
    # prints below the window's low -> exit.
    for px in (100.0, 103.0, 102.0):
        assert ch.should_exit(_pos(), _mkt(px), bar_idx=10) is False
    assert ch.should_exit(_pos(), _mkt(99.0), bar_idx=13) is True


def test_channel_short_exits_above_the_prior_high():
    ch = ChannelExit(channel_exit_bars=3)
    for px in (100.0, 97.0, 98.0):
        assert ch.should_exit(_pos("SHORT"), _mkt(px), bar_idx=10) is False
    assert ch.should_exit(_pos("SHORT"), _mkt(101.0), bar_idx=13) is True


def test_channel_is_inert_before_n_bars():
    ch = ChannelExit(channel_exit_bars=3)
    # Only 3 prints seen -> fewer than 3 PRIOR bars -> never fires, however
    # far price breaks down.
    for px in (100.0, 103.0, 99.0):
        assert ch.should_exit(_pos(), _mkt(px), bar_idx=10) is False


def test_channel_holds_inside_the_window():
    ch = ChannelExit(channel_exit_bars=3)
    for px in (100.0, 103.0, 102.0, 101.0, 102.5):
        assert ch.should_exit(_pos(), _mkt(px), bar_idx=10) is False


def test_channel_does_not_fire_on_its_own_extreme():
    ch = ChannelExit(channel_exit_bars=3)
    for px in (100.0, 103.0, 102.0):
        assert ch.should_exit(_pos(), _mkt(px), bar_idx=10) is False
    # A print exactly at the window's low (100) is not below it.
    assert ch.should_exit(_pos(), _mkt(100.0), bar_idx=13) is False
    assert ch.should_exit(_pos(), _mkt(99.9), bar_idx=13) is True


def test_channel_state_resets_on_a_new_entry():
    ch = ChannelExit(channel_exit_bars=3)
    for px in (100.0, 103.0, 102.0, 99.0):
        ch.should_exit(_pos(bar=10), _mkt(px), bar_idx=10)  # arms + fires
    # A new trade starts an empty channel: inert again.
    assert ch.should_exit(_pos(bar=42), _mkt(99.0), bar_idx=42) is False


def test_channel_rejects_bad_bar_counts():
    with pytest.raises(ValueError, match="channel_exit_bars"):
        ChannelExit(channel_exit_bars=0)
    with pytest.raises(ValueError, match="channel_exit_bars"):
        ChannelExit(channel_exit_bars=-5)
    with pytest.raises(ValueError, match="channel_exit_bars"):
        ChannelExit(channel_exit_bars=1.5)


def test_channel_composes_through_wiring():
    strat = make_strategy(warmup_bars=100, channel_exit_bars=1140)
    rule = strat["exit_rule"]
    assert type(rule) is AnyOf
    assert len(rule.rules) == 3          # stop + lock + channel (lock is kept)
    assert rule.adoptable is False


def test_wiring_rejects_bad_channel_exit_bars():
    with pytest.raises(ValueError, match="channel_exit_bars"):
        make_strategy(warmup_bars=100, channel_exit_bars=0)


# -------- restoring the state on a live resume (2026-10-29) ---------------------

def test_a_restored_armed_lock_fires_on_the_first_bar():
    """The point of restoring: an armed lock resumes armed. A fresh lock seeded
    at the restart's price would hold at the entry — the protection lost."""
    lock = BreakEvenLock(lock_after_pct=0.02)
    adopted = _pos(bar=-1)
    lock.restore_state(adopted, {"best_price": 103.0})
    assert lock.should_exit(adopted, _mkt(100.0), bar_idx=0) is True
    assert BreakEvenLock(lock_after_pct=0.02).should_exit(
        adopted, _mkt(100.0), bar_idx=0) is False


def test_a_restored_unarmed_lock_still_arms_later():
    lock = BreakEvenLock(lock_after_pct=0.02)
    adopted = _pos(bar=-1)
    lock.restore_state(adopted, {"best_price": 101.5})
    assert lock.should_exit(adopted, _mkt(100.0), bar_idx=0) is False
    assert lock.should_exit(adopted, _mkt(102.0), bar_idx=1) is False
    assert lock.should_exit(adopted, _mkt(100.0), bar_idx=2) is True


def test_the_lock_exports_its_best_price():
    lock = BreakEvenLock(lock_after_pct=0.02)
    assert lock.export_state() is None
    for px in (100.5, 102.5, 101.0):
        lock.should_exit(_pos(), _mkt(px), bar_idx=10)
    assert lock.export_state() == {"best_price": 102.5}


def test_a_restored_trail_keeps_its_max_gain():
    trail = GivebackTrail(giveback_frac=0.5)
    adopted = _pos(bar=-1)
    trail.restore_state(adopted, {"best_price": 110.0})   # max gain 10
    assert trail.should_exit(adopted, _mkt(105.1), bar_idx=0) is False
    assert trail.should_exit(adopted, _mkt(105.0), bar_idx=1) is True


def test_anyof_with_the_lock_is_restorable_by_best_price():
    stop = EntryPercentStop(stop_pct=0.05)
    lock = BreakEvenLock(lock_after_pct=0.02)
    combo = AnyOf([stop, lock])
    assert combo.adoptable is False and combo.restorable is True
    assert combo.state_keys == ("best_price",)
    combo.restore_state(_pos(bar=-1), {"best_price": 103.0})
    assert lock.export_state() == combo.export_state() == {"best_price": 103.0}


def test_anyof_hands_one_best_price_to_the_lock_and_the_trail():
    lock = BreakEvenLock(lock_after_pct=0.02)
    trail = GivebackTrail(giveback_frac=0.5)
    combo = AnyOf([EntryPercentStop(stop_pct=0.05), lock, trail])
    combo.restore_state(_pos(bar=-1), {"best_price": 110.0})
    assert lock.export_state() == trail.export_state() == {"best_price": 110.0}


def test_a_channel_in_the_set_keeps_it_refused():
    """ChannelExit carries N bars of prices, not a best price: not restorable."""
    combo = AnyOf([BreakEvenLock(lock_after_pct=0.02),
                   ChannelExit(channel_exit_bars=3)])
    assert combo.restorable is False


def test_the_live_fullday_rule_is_restorable():
    """The adopted config (iter34: stop 0.05 + lock 0.03) — the rule that refused
    to resume on 2026-10-29."""
    rule = make_strategy(warmup_bars=100)["exit_rule"]
    assert rule.adoptable is False
    assert rule.restorable is True and rule.state_keys == ("best_price",)


@pytest.mark.usefixtures("cheap_strategy")
def test_a_resume_restores_the_live_rule(tmp_path):
    from forecast_eval.live.broker.base import HeldPosition

    from .test_live_resume import NOW, _runtime
    rt = _runtime(HeldPosition(1, 46616.0))
    rule = rt.loop.exit_rule = make_strategy(warmup_bars=100)["exit_rule"]
    adopted = rt.adopt_position(timestamp=NOW,
                                exit_state_path=tmp_path / "absent.json",
                                exit_state_injected={"best_price": 47376.0})
    assert adopted["exit_state_source"] == "injected"
    assert rule.export_state() == {"best_price": 47376.0}
