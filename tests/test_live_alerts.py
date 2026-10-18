"""Alerts — the one door everything needing a human goes through.

Built so a transport (Telegram) can be attached later without touching a single
call site. The properties that matter are the ones that make it safe to put an
alert inside a halt path:

  * a sink that raises must never reach the caller — the alert path cannot
    become a new way to lose a session;
  * a severity a sink did not subscribe to must never reach it, because that is
    what lets fatals and info go down different channels;
  * a fatal must land in the standalone journal even when everything else fails.
"""
import json
from pathlib import Path

import pytest

from forecast_eval.live.alerts import ALL, Alert, Alerter, Severity
from forecast_eval.live.state import HaltAction, RailsConfig, SafetyRails


def _alerter(tmp_path, **kw):
    kw.setdefault("console", False)
    return Alerter(fatal_journal=tmp_path / "fatal.jsonl", **kw)


# ---- routing by severity is the whole point --------------------------------

def test_a_sink_only_sees_the_levels_it_asked_for(tmp_path):
    """"Different tunnels" is implemented here and nowhere else — a call site
    never names a channel."""
    fatals, everything = [], []
    a = _alerter(tmp_path)
    a.add_sink(fatals.append, levels=(Severity.FATAL,))
    a.add_sink(everything.append, levels=ALL)

    a.fatal("halt", "position mirror disagrees")
    a.warn("feed_missed", "resubscribing")
    a.info("resumed", "adopted +1")

    assert [x.kind for x in fatals] == ["halt"]
    assert [x.kind for x in everything] == ["halt", "feed_missed", "resumed"]


def test_a_new_sink_defaults_to_fatal_only(tmp_path):
    """Over-notifying trains you to ignore the channel, which is worse than not
    having it. A transport must opt *in* to the noisy levels."""
    seen = []
    a = _alerter(tmp_path)
    a.add_sink(seen.append)
    a.fatal("halt", "x")
    a.info("started", "y")
    assert [x.severity for x in seen] == [Severity.FATAL]


def test_trade_is_its_own_level_and_counted(tmp_path):
    seen = []
    a = _alerter(tmp_path)
    a.add_sink(seen.append, levels=(Severity.TRADE,))
    a.trade("position_opened", "LONG 1 @ 46200")
    a.info("relogin", "token refreshed")
    assert [x.kind for x in seen] == ["position_opened"]
    assert a.summary()["alerts_trade"] == 1
    assert a.summary()["alerts_info"] == 1


# ---- a recovery goes out at the level of the problem it ends ----------------

def test_a_recovery_with_nothing_open_is_info(tmp_path):
    """The routine 08:40 re-login: nothing failed, so it stays INFO."""
    a = _alerter(tmp_path)
    r = a.recovery("relogin", "token refreshed", resolves=("relogin_failed",))
    assert r.severity is Severity.INFO
    assert "resolves" not in r.detail


def test_a_recovery_after_its_warning_is_warn(tmp_path):
    """Under WARN + FATAL the phone saw `feed_missed`; it must also see the
    all-clear, or the incident looks open forever."""
    a = _alerter(tmp_path)
    a.warn("feed_missed", "resubscribing")
    r = a.recovery("feed_recovered", "delivering again",
                   resolves=("feed_missed", "feed_unreachable"))
    assert r.severity is Severity.WARN
    assert r.detail["resolves"] == "feed_missed"


def test_a_recovery_closes_what_it_resolved(tmp_path):
    """One warning, one all-clear: the next recovery with nothing new open
    is routine again."""
    a = _alerter(tmp_path)
    a.warn("relogin_failed", "retrying")
    a.recovery("relogin", "ok", resolves=("relogin_failed",))
    assert a.recovery("relogin", "ok",
                      resolves=("relogin_failed",)).severity is Severity.INFO


def test_a_recovery_ignores_warnings_it_does_not_resolve(tmp_path):
    a = _alerter(tmp_path)
    a.warn("traffic_low", "50 MB left")
    r = a.recovery("relogin", "ok", resolves=("relogin_failed",))
    assert r.severity is Severity.INFO
    assert a.recovery("feed_recovered", "x",
                      resolves=("traffic_low",)).severity is Severity.WARN


def test_a_recovery_from_a_fatal_is_capped_at_warn(tmp_path):
    """Good news is never "act now", and must not land in the fatal journal."""
    a = _alerter(tmp_path)
    a.fatal("owner_frozen", "reconcile failed")
    r = a.recovery("owner_agrees_again", "match again",
                   resolves=("owner_frozen",))
    assert r.severity is Severity.WARN
    lines = (tmp_path / "fatal.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1


# ---- the alert path must never take down the run ---------------------------

def test_a_throwing_sink_is_swallowed_and_counted(tmp_path):
    """These calls live inside halt paths and exception handlers. An alert that
    raises there replaces the real diagnosis with a traceback from the notifier."""
    def _boom(alert):
        raise RuntimeError("telegram is down")

    a = _alerter(tmp_path)
    a.add_sink(_boom, levels=ALL)
    a.fatal("halt", "still has to return")          # must not raise
    assert a.sink_failures == 1
    assert a.summary()["alert_sink_failures"] == 1


def test_one_broken_sink_does_not_starve_the_others(tmp_path):
    got = []
    a = _alerter(tmp_path)
    a.add_sink(lambda x: (_ for _ in ()).throw(OSError("nope")), levels=ALL)
    a.add_sink(got.append, levels=ALL)
    a.fatal("halt", "x")
    assert len(got) == 1


def test_a_broken_journal_path_does_not_raise(tmp_path):
    """The journal is a file write during an incident; a full disk or a bad path
    must not convert a halt into a crash."""
    a = Alerter(fatal_journal=tmp_path / "a_file" / "fatal.jsonl", console=False)
    (tmp_path / "a_file").write_text("not a directory", encoding="utf-8")
    a.fatal("halt", "x")
    assert a.sink_failures == 1


# ---- the standalone fatal journal ------------------------------------------

def test_only_fatals_reach_the_journal(tmp_path):
    a = _alerter(tmp_path)
    a.fatal("halt", "daily loss limit")
    a.warn("feed_missed", "resubscribing")
    a.info("started", "ok")

    lines = (tmp_path / "fatal.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["kind"] == "halt" and row["severity"] == "fatal"
    assert "when" in row


def test_the_journal_accumulates_across_runs(tmp_path):
    """It lives beside `live_restart.json`, not inside a run directory, so that
    "what has ever gone badly wrong" is one file rather than a search."""
    for i in range(3):
        _alerter(tmp_path).fatal("crashed", f"run {i}")
    lines = (tmp_path / "fatal.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3


def test_detail_survives_into_the_journal(tmp_path):
    a = _alerter(tmp_path)
    a.fatal("flatten_incomplete", "position still open", position=-1,
            halt_reason="kill file")
    row = json.loads((tmp_path / "fatal.jsonl").read_text(encoding="utf-8"))
    assert row["position"] == -1 and row["halt_reason"] == "kill file"


# ---- the trail --------------------------------------------------------------

def test_every_severity_reaches_the_recorder(tmp_path):
    from forecast_eval.live.recorder import Recorder
    rec = Recorder(tmp_path / "s.jsonl", simulation=True)
    a = _alerter(tmp_path, recorder=rec)
    a.fatal("halt", "x")
    a.info("started", "y")
    rec.close()
    text = (tmp_path / "s.jsonl").read_text(encoding="utf-8")
    assert text.count('"event": "alert"') == 2


# ---- rendering for a phone --------------------------------------------------

def test_format_is_one_line_with_the_detail_appended():
    a = Alert(severity=Severity.FATAL, kind="halt", message="no market data",
              when=__import__("datetime").datetime(2026, 8, 13, 2, 37),
              detail={"action": "stop"})
    assert a.format() == "[FATAL] halt: no market data | action=stop"


# ---- the wiring that matters most ------------------------------------------

def test_every_rails_halt_raises_a_fatal(tmp_path):
    """A halt is *the* definition of "needs a human". Raising the alert inside
    the latch means a rail added later cannot forget to notify."""
    seen = []
    a = _alerter(tmp_path)
    a.add_sink(seen.append, levels=ALL)
    rails = SafetyRails(RailsConfig(), simulation=True, alerter=a)

    rails.halt("position mirror 0 != venue 1", HaltAction.STOP)
    assert len(seen) == 1
    assert seen[0].severity is Severity.FATAL
    assert seen[0].kind == "halt"
    assert seen[0].detail["action"] == "stop"


def test_a_latched_halt_notifies_exactly_once(tmp_path):
    """First reason wins, and the phone should not buzz once per bar for the
    rest of the night."""
    seen = []
    a = _alerter(tmp_path)
    a.add_sink(seen.append, levels=ALL)
    rails = SafetyRails(RailsConfig(), simulation=True, alerter=a)
    rails.halt("first", HaltAction.STOP)
    rails.halt("second", HaltAction.FLATTEN)
    assert len(seen) == 1 and seen[0].message == "first"


def test_rails_without_an_alerter_are_unchanged():
    """Default `None` is what keeps V0 parity and every offline test untouched."""
    rails = SafetyRails(RailsConfig(), simulation=True)
    rails.halt("x", HaltAction.STOP)
    assert rails.halted and rails.alerter is None


# ---- trades: the rarest and most interesting event in a soak ---------------
#
# At 0.37 orders/night a fill is roughly one message every three days, so it
# cannot cause fatigue — and it is the one thing worth reading on a phone.

def _traded(tmp_path, fills):
    """Drive fills through a LiveLoop and return the alerts they produced."""
    from datetime import datetime, timedelta

    from forecast_eval.events import FillEvent, MarketEvent
    from forecast_eval.live.loop import LiveLoop
    from forecast_eval.portfolio import Portfolio
    from forecast_eval.trader import Trader

    class _Broker:
        def __init__(self):
            self.queue = list(fills)
        def submit(self, order, *, current_price): pass
        def cancel(self): pass
        def has_pending(self): return False
        def pending_order(self): return None
        def poll_fills(self, market):
            out, self.queue = self.queue[:1], self.queue[1:]
            return out

    class _Fc:
        forecast_stride_bars, warmup_bars, context_length = 10_000, 10_000, 10
        def forecast(self, history_df): raise AssertionError("not reached")

    seen = []
    a = _alerter(tmp_path)
    a.add_sink(seen.append, levels=ALL)
    broker = _Broker()
    loop = LiveLoop(forecaster=_Fc(), emitter=None, broker=broker,
                    trader=Trader(execution=broker, tick_size=1.0),
                    portfolio=Portfolio(contract_multiplier=200.0), alerter=a)
    t0 = datetime(2026, 8, 13, 20, 0)
    for i in range(len(fills)):
        loop.on_bar(MarketEvent(timestamp=t0 + timedelta(minutes=i),
                                price=46200.0, volume=1, session="NIGHT"))
    return seen


def _fill(side, price, ts_min):
    from datetime import datetime, timedelta

    from forecast_eval.events import FillEvent
    return FillEvent(timestamp=datetime(2026, 8, 13, 20, 0)
                     + timedelta(minutes=ts_min),
                     side=side, fill_price=price, quantity=1, fee=0.0)


def test_an_entry_is_announced_with_its_side_and_price(tmp_path):
    seen = _traded(tmp_path, [_fill("BUY", 46200.0, 0)])
    assert len(seen) == 1
    assert seen[0].kind == "position_opened"
    assert seen[0].severity is Severity.TRADE
    assert "LONG 1 @ 46200" in seen[0].message


def test_a_short_entry_reads_as_short(tmp_path):
    seen = _traded(tmp_path, [_fill("SELL", 46200.0, 0)])
    assert "SHORT 1 @ 46200" in seen[0].message


def test_an_exit_carries_this_trade_s_pnl_not_the_running_total(tmp_path):
    """The Portfolio only exposes a cumulative figure. An alert reading it
    afterwards would report the sum of every trade tonight — so the delta is
    taken across the apply."""
    seen = _traded(tmp_path, [_fill("BUY", 46200.0, 0),
                              _fill("SELL", 46350.0, 1)])
    assert [s.kind for s in seen] == ["position_opened", "position_closed"]
    assert [s.severity for s in seen] == [Severity.TRADE, Severity.TRADE]
    assert seen[1].detail["points"] == pytest.approx(150.0)
    assert "+150 points" in seen[1].message


def test_a_losing_exit_is_signed(tmp_path):
    seen = _traded(tmp_path, [_fill("BUY", 46200.0, 0),
                              _fill("SELL", 46120.0, 1)])
    assert seen[1].detail["points"] == pytest.approx(-80.0)
    assert "-80 points" in seen[1].message


def test_a_second_round_trip_reports_only_its_own_result(tmp_path):
    """The regression the delta exists to prevent: trade two must not inherit
    trade one's +150."""
    seen = _traded(tmp_path, [_fill("BUY", 46200.0, 0),
                              _fill("SELL", 46350.0, 1),
                              _fill("BUY", 46300.0, 2),
                              _fill("SELL", 46280.0, 3)])
    closes = [s for s in seen if s.kind == "position_closed"]
    assert [c.detail["points"] for c in closes] == pytest.approx([150.0, -20.0])


def test_a_loop_without_an_alerter_still_fills(tmp_path):
    """V0 parity: the alerter is a passenger that reads state after a fill and
    never influences one."""
    from datetime import datetime

    from forecast_eval.events import MarketEvent
    from forecast_eval.live.loop import LiveLoop
    from forecast_eval.portfolio import Portfolio
    from forecast_eval.trader import Trader

    class _Broker:
        def __init__(self): self.queue = [_fill("BUY", 46200.0, 0)]
        def submit(self, order, *, current_price): pass
        def cancel(self): pass
        def has_pending(self): return False
        def pending_order(self): return None
        def poll_fills(self, market):
            out, self.queue = self.queue, []
            return out

    class _Fc:
        forecast_stride_bars, warmup_bars, context_length = 10_000, 10_000, 10
        def forecast(self, history_df): raise AssertionError("not reached")

    broker = _Broker()
    loop = LiveLoop(forecaster=_Fc(), emitter=None, broker=broker,
                    trader=Trader(execution=broker, tick_size=1.0),
                    portfolio=Portfolio())
    loop.on_bar(MarketEvent(timestamp=datetime(2026, 8, 13, 20, 0),
                            price=46200.0, volume=1, session="NIGHT"))
    assert loop.trader.position == 1 and loop.alerter is None
