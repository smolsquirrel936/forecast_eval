"""SafetyRails — every rail must stop the loop, and stop it the right way.

The STOP/FLATTEN distinction is the substance here: flattening is only safe when
the position is the problem *and* prices are still trustworthy. A frozen feed or
a disputed position mirror must send no orders at all.

Second half drives the rails through LiveLoop, so the halt actually changes what
the loop does rather than only setting a flag.
"""
from datetime import datetime, timedelta

import pandas as pd
import pytest

from forecast_eval.events import Forecast, MarketEvent
from forecast_eval.execution import Execution
from forecast_eval.forecaster.base import Forecaster
from forecast_eval.live.broker.sim import SimBroker
from forecast_eval.live.loop import LiveLoop
from forecast_eval.live.recorder import Recorder
from forecast_eval.live.sessions import SessionPolicy
from forecast_eval.live.state import HaltAction, RailsConfig, SafetyRails
from forecast_eval.portfolio import Portfolio
from forecast_eval.strategy.base import SignalEmitter
from forecast_eval.trader import Trader

T0 = datetime(2024, 3, 5, 9, 0, 0)


def _bar(m=0, price=100.0, session="DAY"):
    return MarketEvent(T0 + timedelta(minutes=m), price, 1, session)


def _rails(**kw):
    return SafetyRails(RailsConfig(**kw), simulation=True)


# ---- startup ----------------------------------------------------------------

def test_startup_rejects_max_position_above_the_limit():
    """Sizes above 1 need average-entry accounting that does not exist yet."""
    with pytest.raises(ValueError, match="average-entry"):
        _rails().check_startup(max_position=2)


def test_startup_accepts_the_supported_size():
    _rails().check_startup(max_position=1)


def test_banner_distinguishes_production_from_simulation():
    assert "SIMULATION" in SafetyRails(RailsConfig(), simulation=True).banner()
    assert "PRODUCTION" in SafetyRails(RailsConfig(), simulation=False).banner()


# ---- kill file --------------------------------------------------------------

def test_kill_file_halts_and_flattens(tmp_path):
    kf = tmp_path / "STOP"
    r = _rails(kill_file=kf)
    r.on_bar(_bar(), net_pnl=0.0)
    assert not r.halted
    kf.write_text("")
    r.on_bar(_bar(1), net_pnl=0.0)
    assert r.halted and r.halt_action is HaltAction.FLATTEN
    assert r.flatten_required


# ---- daily loss -------------------------------------------------------------

def test_daily_loss_limit_halts_and_flattens():
    r = _rails(max_daily_loss_points=50.0)
    r.on_bar(_bar(0), net_pnl=0.0)
    r.on_bar(_bar(1), net_pnl=-49.0)
    assert not r.halted
    r.on_bar(_bar(2), net_pnl=-50.0)      # >= limit
    assert r.halted and r.halt_action is HaltAction.FLATTEN


def test_daily_loss_is_measured_from_the_day_anchor_not_from_zero():
    """A trading day that opens already in profit gets its full loss budget."""
    r = _rails(max_daily_loss_points=50.0)
    r.on_bar(_bar(0), net_pnl=0.0)
    # Next trading day, +100 banked — the anchor re-bases here.
    nxt = MarketEvent(datetime(2024, 3, 6, 9, 0), 100.0, 1, "DAY")
    r.on_bar(nxt, net_pnl=100.0)
    r.on_bar(MarketEvent(datetime(2024, 3, 6, 9, 1), 100.0, 1, "DAY"),
             net_pnl=60.0)                 # 40 down from the anchor
    assert not r.halted
    r.on_bar(MarketEvent(datetime(2024, 3, 6, 9, 2), 100.0, 1, "DAY"),
             net_pnl=49.0)                 # 51 down from the anchor
    assert r.halted


def test_daily_loss_anchor_rolls_on_a_night_only_feed():
    """DEFECT FIXED (§2.12). Anchoring used to key on *entry into the DAY
    session*, which never occurs on a night-only feed — so the anchor was set
    once and the 'daily' limit silently became a since-inception limit that
    tightened every day."""
    r = SafetyRails(RailsConfig(max_daily_loss_points=50.0),
                    simulation=True, policy=SessionPolicy.night())

    # Night one: down 40, within budget.
    r.on_bar(MarketEvent(datetime(2024, 3, 5, 16, 0), 100.0, 1, "NIGHT"),
             net_pnl=0.0)
    r.on_bar(MarketEvent(datetime(2024, 3, 6, 2, 0), 100.0, 1, "NIGHT"),
             net_pnl=-40.0)
    assert not r.halted, "same trading day: 15:00->05:00 must not re-anchor"

    # Night two: re-anchored at -40, so -80 is only 40 down from it.
    r.on_bar(MarketEvent(datetime(2024, 3, 6, 16, 0), 100.0, 1, "NIGHT"),
             net_pnl=-40.0)
    r.on_bar(MarketEvent(datetime(2024, 3, 6, 20, 0), 100.0, 1, "NIGHT"),
             net_pnl=-80.0)
    assert not r.halted, "a new trading day must get a fresh loss budget"

    r.on_bar(MarketEvent(datetime(2024, 3, 6, 21, 0), 100.0, 1, "NIGHT"),
             net_pnl=-91.0)                # 51 down from this night's anchor
    assert r.halted


def test_night_session_spanning_midnight_stays_one_trading_day():
    """The anchor rolls on DAY entry, not on the calendar date — a night session
    crosses midnight and must not be cut in half."""
    r = _rails(max_daily_loss_points=50.0)
    r.on_bar(MarketEvent(datetime(2024, 3, 5, 23, 59), 100.0, 1, "NIGHT"),
             net_pnl=0.0)
    r.on_bar(MarketEvent(datetime(2024, 3, 6, 0, 1), 100.0, 1, "NIGHT"),
             net_pnl=-30.0)
    assert not r.halted                    # anchor did NOT reset at midnight
    r.on_bar(MarketEvent(datetime(2024, 3, 6, 0, 2), 100.0, 1, "NIGHT"),
             net_pnl=-51.0)
    assert r.halted


def test_daily_loss_disabled_when_unset():
    r = _rails(max_daily_loss_points=None)
    r.on_bar(_bar(0), net_pnl=0.0)
    r.on_bar(_bar(1), net_pnl=-1e9)
    assert not r.halted


# ---- order rate -------------------------------------------------------------

def test_order_rate_breach_stops_without_flattening():
    """A submit storm means the order path is misbehaving — do not push one more
    order down it."""
    r = _rails(max_orders_per_minute=3)
    for i in range(4):
        r.on_order_submitted(T0 + timedelta(seconds=i))
    assert r.halted and r.halt_action is HaltAction.STOP
    assert not r.flatten_required


def test_order_rate_window_expires():
    r = _rails(max_orders_per_minute=3)
    for i in range(3):
        r.on_order_submitted(T0 + timedelta(seconds=i))
    # Well past the 60s window — the earlier three no longer count.
    for i in range(3):
        r.on_order_submitted(T0 + timedelta(seconds=120 + i))
    assert not r.halted


# ---- staleness --------------------------------------------------------------

def test_stale_feed_stops_and_does_not_flatten():
    """A limit priced off a frozen print is a blind order — including a close."""
    r = _rails(quote_staleness_seconds=60.0)
    r.note_data(T0)
    r.check_staleness(T0 + timedelta(seconds=59))
    assert not r.halted
    r.check_staleness(T0 + timedelta(seconds=61))
    assert r.halted and r.halt_action is HaltAction.STOP
    assert not r.flatten_required


def test_fresh_data_resets_the_staleness_clock():
    r = _rails(quote_staleness_seconds=60.0)
    r.note_data(T0)
    r.note_data(T0 + timedelta(seconds=50))
    r.check_staleness(T0 + timedelta(seconds=100))
    assert not r.halted


def test_staleness_inert_before_any_data_arrives():
    r = _rails(quote_staleness_seconds=60.0)
    r.check_staleness(T0 + timedelta(hours=5))
    assert not r.halted


def test_out_of_session_silence_does_not_halt_a_night_deployment():
    """DEFECT FIXED (§2.12). A night-only feed goes quiet from 05:00 to 15:00 —
    ten hours against a 120-second window. Without session awareness this halted
    the loop on its first morning, every morning."""
    r = SafetyRails(RailsConfig(quote_staleness_seconds=120.0),
                    simulation=True, policy=SessionPolicy.night())
    r.note_data(datetime(2024, 3, 6, 4, 59))       # last tick of the night
    for hour in (6, 9, 12, 14):                     # the daytime silence
        r.check_staleness(datetime(2024, 3, 6, hour, 0))
        assert not r.halted, f"halted at {hour}:00 on expected silence"
    # Session reopens; the watchdog must re-arm rather than see an 10h gap.
    r.check_staleness(datetime(2024, 3, 6, 15, 1))
    assert not r.halted


def test_a_genuinely_frozen_feed_still_halts_inside_the_session():
    """The session exemption must not neuter the rail during trading hours."""
    r = SafetyRails(RailsConfig(quote_staleness_seconds=120.0),
                    simulation=True, policy=SessionPolicy.night())
    r.note_data(datetime(2024, 3, 5, 16, 0))
    r.check_staleness(datetime(2024, 3, 5, 16, 1))
    assert not r.halted
    r.check_staleness(datetime(2024, 3, 5, 16, 5))   # 5 min of silence at 16:05
    assert r.halted and r.halt_action is HaltAction.STOP


def test_staleness_spans_midnight_within_one_night_session():
    """23:58 and 00:30 are the same session instance, so a real freeze across
    midnight must still be caught."""
    r = SafetyRails(RailsConfig(quote_staleness_seconds=120.0),
                    simulation=True, policy=SessionPolicy.night())
    r.note_data(datetime(2024, 3, 5, 23, 58))
    r.check_staleness(datetime(2024, 3, 6, 0, 30))
    assert r.halted


def test_two_different_nights_are_not_one_gap():
    """04:59 and the next 15:01 are both labelled NIGHT — comparing labels alone
    would treat the ten hours between them as a frozen feed."""
    r = SafetyRails(RailsConfig(quote_staleness_seconds=120.0),
                    simulation=True, policy=SessionPolicy.night())
    r.note_data(datetime(2024, 3, 6, 4, 59))
    r.check_staleness(datetime(2024, 3, 6, 15, 1))
    assert not r.halted


def test_day_deployment_ignores_night_silence():
    """The mirror image, for the planned day-session port."""
    r = SafetyRails(RailsConfig(quote_staleness_seconds=120.0),
                    simulation=True, policy=SessionPolicy.day())
    r.note_data(datetime(2024, 3, 5, 13, 44))
    r.check_staleness(datetime(2024, 3, 5, 20, 0))
    r.check_staleness(datetime(2024, 3, 6, 3, 0))
    assert not r.halted


# ---- reconciliation ---------------------------------------------------------

def test_mirror_mismatch_stops_and_never_auto_corrects():
    r = _rails()
    r.on_reconcile(mirror=1, venue=0)
    assert r.halted and r.halt_action is HaltAction.STOP
    assert not r.flatten_required, "closing is undefined when the size is disputed"


def test_reconcile_skipped_when_the_venue_cannot_answer():
    """The offline sim has no venue position; that is not a mismatch."""
    r = _rails()
    r.on_reconcile(mirror=1, venue=None)
    assert not r.halted


def test_matching_positions_pass():
    r = _rails()
    r.on_reconcile(mirror=-1, venue=-1)
    assert not r.halted


# ---- rejections -------------------------------------------------------------

def test_rejected_order_stops_the_loop():
    r = _rails()
    r.on_order_report(op_code="03", op_msg="margin insufficient")
    assert r.halted and r.halt_action is HaltAction.STOP
    assert "margin insufficient" in r.halt_reason


def test_successful_order_report_is_not_a_halt():
    r = _rails()
    r.on_order_report(op_code="00")
    assert not r.halted


# ---- latching ---------------------------------------------------------------

def test_first_halt_reason_wins():
    """Later rails firing during shutdown must not overwrite the diagnosis."""
    r = _rails()
    r.halt("first", HaltAction.STOP)
    r.halt("second", HaltAction.FLATTEN)
    assert r.halt_reason == "first"
    assert r.halt_action is HaltAction.STOP


def test_halt_is_recorded(tmp_path):
    rec = Recorder(tmp_path / "r.jsonl", simulation=True)
    r = SafetyRails(RailsConfig(), simulation=True, recorder=rec)
    r.halt("because", HaltAction.FLATTEN)
    rec.close()
    import json
    last = json.loads((tmp_path / "r.jsonl").read_text().splitlines()[-1])
    assert last["event"] == "halt"
    assert last["reason"] == "because" and last["action"] == "flatten"


# ---- integration through LiveLoop ------------------------------------------

class _AlwaysBuy(SignalEmitter):
    def emit(self, forecast):
        return "BUY"


class _Flat(Forecaster):
    warmup_bars = 0
    forecast_stride_bars = 1
    forecast_horizon_bars = 1

    def forecast(self, history_df):
        return Forecast(
            timestamp=history_df["timestamp"].iloc[-1],
            horizon_bars=1,
            payload={"predicted_return": 0.01,
                     "last_price": float(history_df["price"].iloc[-1])},
        )


def _loop(rails, recorder=None):
    execution = Execution(fee_rate=0.0)
    broker = SimBroker(execution)
    trader = Trader(execution=broker, tick_size=1.0, aggression_ticks=0)
    return LiveLoop(
        forecaster=_Flat(), emitter=_AlwaysBuy(), broker=broker, trader=trader,
        portfolio=Portfolio(contract_multiplier=1.0), rails=rails,
        recorder=recorder,
    ), trader


def test_loop_flattens_then_stops_on_a_kill_file(tmp_path):
    kf = tmp_path / "STOP"
    loop, trader = _loop(_rails(kill_file=kf))
    for i in range(4):
        loop.on_bar(_bar(i))
    assert trader.position == 1, "should be long before the halt"

    kf.write_text("")
    orders_before = loop._n_orders
    loop.on_bar(_bar(4))                      # halt latches, close submitted
    assert loop.rails.halted
    assert loop._n_orders == orders_before + 1
    assert loop.orders[-1].intent == "CLOSE"

    loop.on_bar(_bar(5))                      # close fills
    assert trader.position == 0

    n = loop._n_orders
    for i in range(6, 10):
        loop.on_bar(_bar(i))
    assert loop._n_orders == n, "a halted loop must submit nothing once flat"


def test_the_flatten_close_is_submitted_inside_the_urgent_scope(tmp_path):
    """V4/§5 — a FLATTEN close must reach the broker marked urgent, so a live
    venue prices it through the touch and sends it IOC. The submit is issued by
    `Trader.submit_exit`, and `trader.py` is not ours to modify, so the scope is
    the only thing carrying that intent to the broker."""

    class _SpyBroker(SimBroker):
        def __init__(self, execution):
            super().__init__(execution)
            self.urgent_depth = 0
            self.submitted_urgent = []

        def urgent(self):
            outer = super().urgent()

            class _Scope:
                def __enter__(_s):
                    self.urgent_depth += 1
                    outer.__enter__()
                    return None

                def __exit__(_s, *exc):
                    self.urgent_depth -= 1
                    return outer.__exit__(*exc)

            return _Scope()

        def submit(self, order, *, current_price):
            self.submitted_urgent.append((order.intent, self.urgent_depth > 0))
            return super().submit(order, current_price=current_price)

    kf = tmp_path / "STOP"
    execution = Execution(fee_rate=0.0)
    broker = _SpyBroker(execution)
    trader = Trader(execution=broker, tick_size=1.0, aggression_ticks=0)
    loop = LiveLoop(
        forecaster=_Flat(), emitter=_AlwaysBuy(), broker=broker, trader=trader,
        portfolio=Portfolio(contract_multiplier=1.0), rails=_rails(kill_file=kf),
    )
    for i in range(4):
        loop.on_bar(_bar(i))
    assert trader.position == 1
    assert all(not urgent for _, urgent in broker.submitted_urgent), \
        "ordinary strategy orders must never be marked urgent"

    kf.write_text("")
    loop.on_bar(_bar(4))
    assert broker.submitted_urgent[-1] == ("CLOSE", True)
    assert broker.urgent_depth == 0, "the scope must close again"


def test_loop_does_not_resubmit_while_a_close_is_working(tmp_path):
    """The flatten retry is gated on has_pending(), so it cannot become a storm."""
    kf = tmp_path / "STOP"
    loop, trader = _loop(_rails(kill_file=kf))
    for i in range(4):
        loop.on_bar(_bar(i))
    kf.write_text("")
    loop.on_bar(_bar(4))
    n = loop._n_orders
    # Same price, so a passive close would not fill; the loop must still not
    # stack a second order on top of the working one.
    assert loop.broker.has_pending() or trader.position == 0
    if loop.broker.has_pending():
        loop._on_halted(_bar(5), 5)
        assert loop._n_orders == n


def test_stop_halt_sends_no_orders_at_all():
    """Contrast with FLATTEN: a STOP halt leaves the position alone."""
    rails = _rails()
    loop, trader = _loop(rails)
    for i in range(4):
        loop.on_bar(_bar(i))
    assert trader.position == 1
    rails.halt("frozen feed", HaltAction.STOP)
    n = loop._n_orders
    for i in range(4, 8):
        loop.on_bar(_bar(i))
    assert loop._n_orders == n
    assert trader.position == 1, "STOP must not unwind the position"


def test_halted_loop_still_books_fills():
    """A fill is a fact; halting must not lose it from the accounting."""
    rails = _rails()
    loop, trader = _loop(rails)
    loop.on_bar(_bar(0))                      # signal -> BUY submitted
    rails.halt("stop now", HaltAction.STOP)
    loop.on_bar(_bar(1))                      # that order fills on this bar
    assert trader.position == 1
    assert len(loop.fills) == 1


# ---- the weekend (§2.25) ----------------------------------------------------

def test_a_soak_survives_the_weekend():
    """REGRESSION — measured 2026-08-15: stepping a one-minute clock from
    Friday's 05:00 close through a weekend halted the run at **Saturday
    15:16** with `no market data for 960.0s (limit 900.0s)`, because
    `classify_session` reads time-of-day only and called Saturday 15:00 a NIGHT
    session. Five consecutive unattended days must contain a weekend, so V5 was
    unreachable until this passed.
    """
    from forecast_eval.live.sessions import SessionPolicy

    policy = SessionPolicy.night()
    rails = SafetyRails(RailsConfig(quote_staleness_seconds=900.0),
                        simulation=True, policy=policy)

    last_print = datetime(2026, 8, 15, 5, 0, 0)      # Saturday 05:00 close
    rails.note_data(last_print)

    now = last_print
    end = datetime(2026, 8, 17, 8, 0, 0)             # Monday, pre-open
    polls = 0
    while now < end:
        now += timedelta(minutes=1)
        if policy.includes(now):
            polls += 1                               # FeedWatch's gate
        rails.check_staleness(now)
        assert not rails.halted, f"halted at {now}: {rails.halt_reason}"

    # FeedWatch also stops spending queries on a closed market: this was ~1 680
    # snapshots a weekend, one a minute through two sessions that never open.
    assert polls == 0


def test_the_staleness_rail_still_halts_a_frozen_feed_on_a_trading_day():
    """Guards the guard: the weekend fix must not disarm the rail generally."""
    from forecast_eval.live.sessions import SessionPolicy

    policy = SessionPolicy.night()
    rails = SafetyRails(RailsConfig(quote_staleness_seconds=900.0),
                        simulation=True, policy=policy)

    start = datetime(2026, 8, 17, 20, 0, 0)          # Monday night, mid-session
    rails.note_data(start)
    now = start
    while now < start + timedelta(minutes=30) and not rails.halted:
        now += timedelta(minutes=1)
        rails.check_staleness(now)
    assert rails.halted
    assert "no market data" in rails.halt_reason
