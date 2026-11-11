"""Catch-up — the plan arithmetic, the venue guard and the leg loop, without a login.

The tool's one rule is that the ledger only ever holds venue fills: it moves the
venue and the ledger together, or neither. These tests pin the pieces that rule
rests on — the target and the order, the refusal on a mismatched book, the
owner-style Cover/New legs, and that every fill that arrives is returned even
when the run cannot finish.
"""
from datetime import datetime

import pandas as pd
import pytest

from forecast_eval.events import FillEvent, MarketEvent
from forecast_eval.exits.profit_trail import ProfitTrail
from forecast_eval.live import catchup
from forecast_eval.live.broker.base import HeldPosition
from forecast_eval.live.catchup import (
    CatchupRefused, check_venue, exit_state_after, extend_tape, gap_since,
    lot_opened_at, order_for, overlap_mismatches, record_gap, trade_to_target,
)


def _fill(ts, side, price, qty=1):
    return FillEvent(timestamp=pd.Timestamp(ts), side=side, fill_price=price,
                     quantity=qty, fee=price * 0.0002)


def _rpt(rows):
    return pd.DataFrame(
        [{"datetime": t, "contract": "202611", "open": c, "high": c, "low": c,
          "close": c, "volume": 10.0} for t, c in rows])


def _bars(rows):
    return pd.DataFrame({"timestamp": pd.to_datetime([t for t, _ in rows]),
                         "price": [float(c) for _, c in rows],
                         "volume": [5.0] * len(rows),
                         "contract": ["202611"] * len(rows)})


# ---- the tape ---------------------------------------------------------------

def test_extend_tape_appends_only_past_the_last_minute_in_rpt_schema():
    rpt = _rpt([("2026-11-06 13:43:00", 100.0), ("2026-11-06 13:44:00", 101.0)])
    bars = _bars([("2026-11-06 13:44:00", 999.0), ("2026-11-09 08:45:00", 102.0)])
    ext, added = extend_tape(rpt, bars)
    assert added == 1
    assert list(ext.columns) == list(rpt.columns)
    # The RPT minute is kept as the backtests read it, not overwritten.
    assert ext["close"].tolist() == [100.0, 101.0, 102.0]
    last = ext.iloc[-1]
    assert last["datetime"] == "2026-11-09 08:45:00"
    assert last["open"] == last["high"] == last["low"] == last["close"] == 102.0
    assert last["contract"] == "202611"


def test_overlap_counts_minutes_whose_close_differs():
    rpt = _rpt([("2026-11-06 13:43:00", 100.0), ("2026-11-06 13:44:00", 101.0)])
    same = _bars([("2026-11-06 13:43:00", 100.0), ("2026-11-06 13:44:00", 101.0)])
    off = _bars([("2026-11-06 13:43:00", 100.0), ("2026-11-06 13:44:00", 105.0)])
    assert overlap_mismatches(rpt, same) == (2, 0)
    assert overlap_mismatches(rpt, off) == (2, 1)


# ---- the plan ---------------------------------------------------------------

def test_order_for_takes_held_to_target_through_flat():
    assert order_for(0, 0) is None
    assert order_for(1, 1) is None
    assert order_for(0, 1) == ("BUY", 1)
    assert order_for(1, 0) == ("SELL", 1)
    assert order_for(1, -1) == ("SELL", 2)
    assert order_for(-1, 1) == ("BUY", 2)


def test_lot_opened_at_is_the_fill_that_left_flat_or_crossed_it():
    fills = [_fill("2026-10-28 10:46", "SELL", 47025.0),
             _fill("2026-11-06 09:15", "BUY", 38427.0),
             _fill("2026-11-11 11:54", "BUY", 43829.0)]
    assert lot_opened_at(fills) == pd.Timestamp("2026-11-11 11:54")
    assert lot_opened_at(fills[:2]) is None
    crossed = [_fill("2026-11-01 09:00", "SELL", 100.0),
               _fill("2026-11-02 09:00", "BUY", 101.0, qty=2)]
    assert lot_opened_at(crossed) == pd.Timestamp("2026-11-02 09:00")


def test_downtime_starts_at_the_later_of_last_fill_and_heartbeat():
    fills = [_fill("2026-11-06 09:15", "BUY", 38427.0)]
    assert gap_since(fills, "2026-11-11T14:04:18") == pd.Timestamp("2026-11-11 14:04:18")
    assert gap_since(fills, None) == pd.Timestamp("2026-11-06 09:15")
    assert gap_since([], None) is None


def test_exit_state_is_the_rules_own_best_price_over_the_bars_since_entry():
    """The 2026-11-12 case: a long @ 43829 at 11:54, best close 44230 at 13:42.
    Bars before the entry minute must not count."""
    events = [MarketEvent(pd.Timestamp(t), p, 1, "DAY") for t, p in [
        ("2026-11-11 11:00", 99999.0),           # before entry - ignored
        ("2026-11-11 11:54", 43829.0),
        ("2026-11-11 13:42", 44230.0),
        ("2026-11-11 13:44", 44091.0)]]
    state, fired = exit_state_after(ProfitTrail(0.15, trail_pct=0.02),
                                    HeldPosition(1, 43829.0),
                                    pd.Timestamp("2026-11-11 11:54:00"), events)
    assert state == {"best_price": 44230.0}
    assert fired is None


def test_exit_state_reports_when_the_rule_would_have_fired():
    events = [MarketEvent(pd.Timestamp(t), p, 1, "DAY") for t, p in [
        ("2026-11-11 09:00", 100.0), ("2026-11-11 09:01", 120.0),
        ("2026-11-11 09:02", 110.0)]]
    state, fired = exit_state_after(ProfitTrail(0.15, trail_pct=0.02),
                                    HeldPosition(1, 100.0),
                                    pd.Timestamp("2026-11-11 09:00"), events)
    assert state == {"best_price": 120.0}
    assert fired == pd.Timestamp("2026-11-11 09:02")


def test_gap_record_skips_rows_already_recorded_and_writes_crlf(tmp_path):
    path = tmp_path / "catchup_day.csv"
    gap = [_fill("2026-11-11 11:54", "BUY", 43829.0)]
    assert record_gap(path, gap, "run1") == 1
    assert record_gap(path, gap + [_fill("2026-11-12 09:00", "SELL", 44000.0)],
                      "run2") == 1
    df = pd.read_csv(path)
    assert df["catchup_run"].tolist() == ["run1", "run2"]
    raw = path.read_bytes()
    assert raw.count(b"\r\n") == raw.count(b"\n") == 3


# ---- the venue --------------------------------------------------------------

def test_venue_must_hold_exactly_what_the_ledgers_add_up_to():
    check_venue(1, {"day": 1, "night": 0})
    check_venue(0, {"day": 1, "night": -1})
    with pytest.raises(CatchupRefused, match="ledgers add up to"):
        check_venue(0, {"day": 1, "night": 0})
    with pytest.raises(CatchupRefused, match="did not answer"):
        check_venue(None, {"day": 0})


class _Clock:
    def __init__(self):
        self.now = 0.0

    def time(self):
        return self.now

    def sleep(self, s):
        self.now += s


class _Broker:
    """Fills each order with the lots scripted for it, then nothing."""

    def __init__(self, script):
        self.script = list(script)
        self.submitted, self.cancels, self._queue = [], 0, []

    def submit(self, order, *, current_price):
        self.submitted.append(order)
        got = self.script.pop(0) if self.script else 0
        if got:
            self._queue.append(FillEvent(datetime(2026, 11, 12, 9, 0), order.side,
                                         order.limit_price, got, 0.0))

    def poll_fills(self, market):
        out, self._queue = self._queue, []
        return out

    def cancel(self):
        self.cancels += 1


def _trade(broker, side, qty, account_net, attempts=3):
    return trade_to_target(broker, side, qty, account_net, lambda: 44000.0,
                           aggression=30.0, attempts=attempts, wait_seconds=1.0,
                           clock=_Clock())


def test_a_buy_while_a_peer_is_short_is_a_cover_then_a_new_leg():
    """§18.9: Cover/New is a property of the ACCOUNT. Day going flat -> +1
    while night is short -1 is a Cover at the venue, not a New."""
    broker = _Broker([1])
    fills = _trade(broker, "BUY", 1, account_net=-1)
    assert [(o.intent, o.quantity) for o in broker.submitted] == [("CLOSE", 1)]
    assert len(fills) == 1
    broker = _Broker([1, 1])
    _trade(broker, "BUY", 2, account_net=-1)
    assert [(o.intent, o.quantity) for o in broker.submitted] == [("CLOSE", 1),
                                                                  ("OPEN", 1)]


def test_a_leg_is_priced_through_the_touch():
    broker = _Broker([1])
    _trade(broker, "BUY", 1, account_net=0)
    assert broker.submitted[0].limit_price == 44030.0
    broker = _Broker([1])
    _trade(broker, "SELL", 1, account_net=0)
    assert broker.submitted[0].limit_price == 43970.0


def test_an_unfilled_leg_is_cancelled_and_repriced_for_what_remains():
    broker = _Broker([0, 1])
    fills = _trade(broker, "BUY", 1, account_net=0)
    assert broker.cancels == 1
    assert len(broker.submitted) == 2
    assert sum(f.quantity for f in fills) == 1


def test_an_incomplete_run_still_returns_every_fill_and_stops_at_that_leg():
    """Each fill is a real lot; the caller writes them all to the ledger."""
    broker = _Broker([1, 0, 0, 0])
    fills = _trade(broker, "SELL", 2, account_net=1, attempts=3)
    assert sum(f.quantity for f in fills) == 1
    # The Cover leg filled; the New leg never did and nothing follows it.
    assert [o.intent for o in broker.submitted] == ["CLOSE", "OPEN", "OPEN", "OPEN"]


def test_no_reference_price_refuses():
    broker = _Broker([1])
    with pytest.raises(CatchupRefused, match="no reference price"):
        trade_to_target(broker, "BUY", 1, 0, lambda: None, aggression=30.0,
                        attempts=1, wait_seconds=1.0, clock=_Clock())


def test_owner_positions_follows_the_ledger_and_keeps_the_other_deployments(tmp_path):
    """The owner refuses to start when owner_positions.json and the ledgers
    differ; a catch-up's new fill must reach both."""
    import json
    from forecast_eval.live import owner_positions
    from forecast_eval.live.catchup import sync_owner_positions
    from forecast_eval.live.owner_cli import seed_positions
    assert sync_owner_positions(tmp_path, "day", "TXFK6") is None   # no file, no-op
    (tmp_path / "fills_day.csv").write_bytes(
        b"timestamp,side,fill_price,quantity,fee,soak_id,contract\r\n"
        b"2026-11-12 09:00:00,BUY,44000.0,1,8.8,s1,TXFK6\r\n")
    (tmp_path / "fills_night.csv").write_bytes(
        b"timestamp,side,fill_price,quantity,fee,soak_id,contract\r\n"
        b"2026-11-11 16:00:00,SELL,43000.0,1,8.6,s2,TXFK6\r\n")
    slugs = ["day", "night", "day_night", "hybrid"]
    owner_positions.write(
        owner_positions.positions_path(tmp_path),
        owner_positions.render({("night", "TXFK6"): -1}, {"night": 43000.0}, slugs))

    sync_owner_positions(tmp_path, "day", "TXFK6")

    holdings, entries = owner_positions.load(
        owner_positions.positions_path(tmp_path), slugs)
    assert holdings == {("day", "TXFK6"): 1, ("night", "TXFK6"): -1}
    assert entries == {"day": 44000.0, "night": 43000.0}
    doc = json.loads(owner_positions.positions_path(tmp_path).read_text("utf-8"))
    assert doc["total"] == {}                          # +1 and -1 net to flat
    ledgers = seed_positions(tmp_path, ["day"])["day"]
    assert {c: h.signed_qty for c, h in ledgers.items()} == {"TXFK6": 1}


# ---- plan mode end to end ----------------------------------------------------

def test_plan_mode_reads_a_replay_and_changes_nothing_live(tmp_path, capsys):
    out = tmp_path / "outputs"
    out.mkdir()
    ledger = out / "fills_day.csv"
    ledger.write_bytes(
        b"timestamp,side,fill_price,quantity,fee,soak_id,contract\r\n"
        b"2026-10-28 10:46:03,SELL,47025.0,1,9.4,s1,TXFK6\r\n"
        b"2026-11-06 09:15:00,BUY,38427.0,1,7.7,s1,TXFK6\r\n")
    ledger_bytes = ledger.read_bytes()
    tape = tmp_path / "tape.csv"
    _rpt([("2026-11-11 11:54:00", 43829.0),
          ("2026-11-11 13:44:00", 44091.0)]).to_csv(tape, index=False)
    replay = tmp_path / "replay"
    replay.mkdir()
    (replay / "fills.csv").write_text(
        "timestamp,side,fill_price,quantity,fee\n"
        "2026-10-27 12:54:00,SELL,46296.0,1,9.3\n"
        "2026-11-06 09:15:00,BUY,38427.0,1,7.7\n"
        "2026-11-11 11:54:00,BUY,43829.0,1,8.8\n", encoding="utf-8")
    before = sorted(p.name for p in out.iterdir())

    rc = catchup.main(["--config", "day", "--out-parent", str(out),
                       "--tape", str(tape), "--replay-dir", str(replay)])

    assert rc == 0
    text = capsys.readouterr().out
    assert "replay trades during the downtime: 1" in text
    assert "order needed: BUY 1" in text
    after = sorted(p.name for p in out.iterdir())
    # Only the catch-up's own work folder is new; no ledger, exit state or
    # gap record was touched.
    assert [n for n in after if n not in before] == [
        n for n in after if n.startswith("catchup_day_")]
    assert ledger.read_bytes() == ledger_bytes
