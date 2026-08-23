"""The contract roll — a soak must not die because the front month moved.

These tests exist because one did (2026-08-19, SHIOAJI_PLAN.md §2.6). `TXFR1` is
an alias resolved once at start-up, so a process that outlives a settlement holds
an object the venue has retired: fifteen FeedWatch repairs re-subscribed the same
dead contract, produced zero ticks, and the run halted on staleness.

Three things are covered here, in the order a roll exercises them: noticing it
(`RollWatch`), warming up across it (the spliced backfill), and carrying the
position through it (`LiveRuntime`).
"""
from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from forecast_eval.live.backfill import (
    collapse_front_month,
    fetch_bars_continuous,
    load_bar_cache,
    save_bar_cache,
)
from forecast_eval.live.contracts import contract_month, dated_months
from forecast_eval.live.roll import (
    RollSchedule,
    RollWatch,
    front_month_at,
    last_trading_day,
    roll_at,
)
from forecast_eval.live.sessions import SessionPolicy


class Recorder:
    def __init__(self):
        self.events = []

    def record(self, event, **fields):
        self.events.append((event, fields))

    def kinds(self):
        return [e for e, _ in self.events]


class FakeContract:
    def __init__(self, code, delivery_month, target_code=None):
        self.code = code
        self.delivery_month = delivery_month
        if target_code is not None:
            self.target_code = target_code


AUG = FakeContract("TXFH6", "202608")
SEP = FakeContract("TXFI6", "202609")
NOW = datetime(2026, 8, 19, 14, 55)


def _watch(sequence, **kw):
    """A watcher whose resolve walks `sequence`, holding the first one's month."""
    seq = list(sequence)
    # The first entry is what the watcher already holds; resolving starts at the
    # second, which is what the venue would report on the next read.
    state = {"i": 1}

    def resolve():
        c = seq[min(state["i"], len(seq) - 1)]
        state["i"] += 1
        return c, f"TXFR1 -> code={c.code} delivery_month={c.delivery_month}"

    kw.setdefault("month", contract_month(seq[0]))
    w = RollWatch(resolve=resolve, **kw)
    return w


# --- noticing the roll ------------------------------------------------------

def test_no_roll_when_the_front_month_is_unchanged():
    rec = Recorder()
    w = _watch([AUG, AUG], recorder=rec)
    assert w.check(NOW) is None
    assert rec.kinds() == []


def test_a_changed_month_returns_the_new_contract_and_both_months():
    w = _watch([AUG, SEP])
    rolled = w.check(NOW)
    assert rolled is not None
    assert rolled.contract is SEP
    assert (rolled.from_month, rolled.to_month) == ("202608", "202609")


def test_the_roll_is_recorded_with_the_reason_that_triggered_it():
    rec = Recorder()
    w = _watch([AUG, SEP], recorder=rec)
    w.check(NOW, reason="feed_missed")
    event, fields = rec.events[0]
    assert event == "contract_rolled"
    assert fields["reason"] == "feed_missed"
    assert (fields["from_month"], fields["to_month"]) == ("202608", "202609")


def test_a_roll_is_adopted_so_the_next_check_is_quiet():
    # The watcher must not keep reporting the same roll: the caller re-subscribes
    # and re-points the broker on every Roll it is handed.
    w = _watch([AUG, SEP], min_recheck=timedelta(0))
    assert w.check(NOW) is not None
    assert w.check(NOW + timedelta(minutes=1)) is None
    assert w.month == "202609"


def test_checks_inside_the_throttle_window_do_not_reach_the_venue():
    # MISSED repeats once a minute; a roll happens eight times a year.
    calls = []

    def resolve():
        calls.append(1)
        return AUG, "note"

    w = RollWatch(resolve=resolve, month="202608")
    w.check(NOW)
    w.check(NOW + timedelta(minutes=1))
    w.check(NOW + timedelta(minutes=4, seconds=59))
    assert len(calls) == 1
    w.check(NOW + timedelta(minutes=5))
    assert len(calls) == 2


def test_a_resolver_that_raises_is_recorded_and_never_propagates():
    # This runs inside a liveness path: a failed read must not end a healthy run.
    rec = Recorder()

    def boom():
        raise RuntimeError("contract table unreachable")

    w = RollWatch(resolve=boom, month="202608", recorder=rec)
    assert w.check(NOW) is None
    assert rec.kinds() == ["roll_check_failed"]
    assert w.summary()["roll_check_errors"] == 1


def test_a_failed_refresh_still_lets_the_re_read_happen():
    # The refresh is an optimisation; the re-read is the actual test.
    rec = Recorder()

    def bad_refresh():
        raise RuntimeError("fetch_contracts blew up")

    w = _watch([AUG, SEP], refresh=bad_refresh, recorder=rec)
    assert w.check(NOW) is not None
    assert rec.kinds() == ["roll_refresh_failed", "contract_rolled"]


def test_the_refresh_runs_before_the_re_read():
    order = []
    w = RollWatch(
        resolve=lambda: (order.append("resolve"), (SEP, "note"))[1],
        month="202608",
        refresh=lambda: order.append("refresh"),
    )
    w.check(NOW)
    assert order == ["refresh", "resolve"]


def test_a_backwards_month_is_ignored_rather_than_followed():
    # Following it would re-subscribe something already expired — the exact state
    # this module exists to leave.
    rec = Recorder()
    w = _watch([SEP, AUG], recorder=rec)
    assert w.check(NOW) is None
    assert rec.kinds() == ["roll_backwards_ignored"]
    assert w.month == "202609"


def test_a_contract_without_a_delivery_month_is_not_a_roll():
    w = _watch([AUG, FakeContract("TXFR1", None)])
    assert w.check(NOW) is None


def test_summary_separates_a_quiet_month_from_one_we_could_not_read():
    w = _watch([AUG, AUG], min_recheck=timedelta(0))
    w.check(NOW)
    w.check(NOW + timedelta(minutes=1))
    assert w.summary() == {"roll_checks": 2, "rolls": 0, "roll_check_errors": 0}


def test_dated_months_orders_by_delivery_and_drops_the_alias():
    root = [SEP, FakeContract("TXFR1", "202609"), AUG]
    assert [c.code for c in dated_months(root)] == ["TXFH6", "TXFI6"]


# --- warming up across the roll --------------------------------------------

def _frame(rows, contract="202608"):
    return pd.DataFrame(
        [{"timestamp": pd.Timestamp(t), "price": p, "volume": v,
          "contract": contract}
         for t, p, v in rows])


def test_the_splice_keeps_the_most_traded_contract_for_a_contested_minute():
    # `real_data.load_minute_bars`'s rule, reproduced: the minute belongs to
    # whichever month actually traded it, which is what makes the spliced series
    # the same kind of input the model was evaluated on.
    old = _frame([("2026-08-19 03:00", 100.0, 500), ("2026-08-19 03:01", 101.0, 400)])
    new = _frame([("2026-08-19 03:01", 201.0, 900), ("2026-08-19 03:02", 202.0, 50)])
    out = collapse_front_month([old, new])
    assert list(out["price"]) == [100.0, 201.0, 202.0]


def test_the_calendar_beats_volume_at_the_roll_instant():
    # MEASURED 2026-08-23: the evaluated series splices by calendar, and at the
    # roll moment the expiring month still has the larger volume — so a volume
    # rule would keep feeding the context a contract we had stopped trading.
    old = _frame([("2026-09-15 15:29", 100.0, 900),
                  ("2026-09-15 15:31", 101.0, 900)], contract="202609")
    new = _frame([("2026-09-15 15:29", 200.0, 10),
                  ("2026-09-15 15:31", 201.0, 10)], contract="202610")
    out = collapse_front_month([old, new], front_month_at=front_month_at)
    assert list(out["price"]) == [100.0, 201.0]
    assert list(out["contract"]) == ["202609", "202610"]


def test_a_minute_the_front_month_did_not_print_is_filled_not_dropped():
    # Better filled from the other month than lost: the fallback is volume.
    old = _frame([("2026-09-15 15:31", 101.0, 900)], contract="202609")
    out = collapse_front_month([old], front_month_at=front_month_at)
    assert list(out["price"]) == [101.0]


def test_the_splice_is_chronological_and_has_one_row_per_minute():
    a = _frame([("2026-08-19 03:02", 1.0, 5), ("2026-08-19 03:00", 2.0, 5)])
    b = _frame([("2026-08-19 03:01", 3.0, 5)])
    out = collapse_front_month([a, b])
    assert list(out["timestamp"]) == sorted(out["timestamp"])
    assert out["timestamp"].is_unique


def test_the_splice_of_nothing_is_an_empty_frame_with_the_right_columns():
    out = collapse_front_month([pd.DataFrame(), None])
    assert out.empty
    assert list(out.columns) == ["timestamp", "price", "volume", "contract"]


def test_the_cache_round_trips(tmp_path):
    bars = _frame([("2026-08-19 03:00", 100.0, 5)])
    path = tmp_path / "bars_cache.csv"
    save_bar_cache(path, bars)
    assert load_bar_cache(path).equals(bars)


def test_a_cache_written_before_the_contract_column_still_loads(tmp_path):
    # The cache is the only copy of a delisted month's bars; a schema change must
    # never be a reason to throw one away.
    path = tmp_path / "old.csv"
    path.write_text("timestamp,price,volume\n2026-08-19 03:00:00,100.0,5\n",
                    encoding="utf-8")
    out = load_bar_cache(path)
    assert list(out.columns) == ["timestamp", "price", "volume", "contract"]
    assert out["contract"].iloc[0] == ""


def test_a_missing_or_unreadable_cache_degrades_to_empty(tmp_path):
    # The cache is an optimisation, never a dependency — a half-written file is
    # an expected state for a process that can be killed at any moment.
    assert load_bar_cache(tmp_path / "absent.csv").empty
    bad = tmp_path / "bad.csv"
    bad.write_text("not,a,bar\n1,2,3\n", encoding="utf-8")
    assert load_bar_cache(bad).empty


def test_the_cache_is_capped_to_its_tail(tmp_path):
    bars = _frame([(f"2026-08-19 03:{m:02d}", float(m), 5) for m in range(10)])
    path = tmp_path / "c.csv"
    save_bar_cache(path, bars, keep=3)
    assert list(load_bar_cache(path)["price"]) == [7.0, 8.0, 9.0]


class FakeApi:
    """`kbars` per contract; a delisted month raises, as the venue does."""

    def __init__(self, by_contract):
        self.by_contract = by_contract

    def kbars(self, contract, start, end):
        rows = self.by_contract.get(contract.code)
        if rows is None:
            raise RuntimeError(f"{contract.code} is not listed")
        return {
            "ts": [pd.Timestamp(t).value for t, _, _ in rows],
            "Close": [p for _, p, _ in rows],
            "Volume": [v for _, _, v in rows],
        }


NIGHT = SessionPolicy(sessions=("NIGHT",))
FETCHED_AT = datetime(2026, 8, 20, 6, 0)


def test_the_backfill_splices_two_listed_months_on_volume():
    api = FakeApi({
        "TXFH6": [("2026-08-19 03:01", 100.0, 900), ("2026-08-19 03:02", 101.0, 800)],
        "TXFI6": [("2026-08-19 03:02", 201.0, 50), ("2026-08-19 03:03", 202.0, 900)],
    })
    out = fetch_bars_continuous(api, [AUG, SEP], "2026-08-18", "2026-08-19",
                                policy=NIGHT, now=FETCHED_AT)
    # kbars are right-labelled, so each bar lands one minute earlier.
    assert list(out["price"]) == [100.0, 101.0, 202.0]


def test_a_delisted_month_is_skipped_instead_of_killing_the_backfill():
    # §2.8 Finding D: expired contracts are delisted. The month that just settled
    # is *expected* to fail, and the front month is the one that matters.
    api = FakeApi({"TXFI6": [("2026-08-19 03:03", 202.0, 900)]})
    out = fetch_bars_continuous(api, [AUG, SEP], "2026-08-18", "2026-08-19",
                                policy=NIGHT, now=FETCHED_AT)
    assert list(out["price"]) == [202.0]


def test_the_cache_carries_the_expired_month_the_venue_can_no_longer_serve(tmp_path):
    # The whole point of the cache: after the roll, only the cache still holds
    # the bars from before it.
    path = tmp_path / "bars_cache.csv"
    api = FakeApi({"TXFH6": [("2026-08-19 03:01", 100.0, 900)]})
    fetch_bars_continuous(api, [AUG], "2026-08-18", "2026-08-19",
                          policy=NIGHT, cache_path=path, now=FETCHED_AT)

    delisted = FakeApi({"TXFI6": [("2026-08-19 03:03", 202.0, 900)]})
    out = fetch_bars_continuous(delisted, [AUG, SEP], "2026-08-18", "2026-08-19",
                                policy=NIGHT, cache_path=path, now=FETCHED_AT)
    assert list(out["price"]) == [100.0, 202.0]
    assert list(load_bar_cache(path)["price"]) == [100.0, 202.0]


# --- carrying the position through the roll ---------------------------------

class Alerter:
    def __init__(self):
        self.sent = []

    def warn(self, kind, message, **fields):
        self.sent.append(("warn", kind))

    def info(self, kind, message, **fields):
        self.sent.append(("info", kind))

    def fatal(self, kind, message, **fields):
        self.sent.append(("fatal", kind))


def _rolling_runtime():
    """A warm NIGHT runtime holding one long lot, as a roll would find it."""
    from forecast_eval.tests.test_live_runtime import _bars, _runtime
    rec = Recorder()
    rt = _runtime(recorder=rec, alerter=Alerter())
    rt.warm_up(_bars(60, start="2026-08-18 16:00"))
    rt.trader.position = 1
    rt.trader.entry_price = 17500.0
    rt.portfolio.position = 1
    rt.portfolio.avg_entry_price = 17500.0
    return rt, rec


def test_the_settled_leg_leaves_the_mirrors_flat():
    # The venue cash-settles the expiring month before the alias moves, so the
    # mirrors must let go of a contract that no longer exists.
    rt, _rec = _rolling_runtime()
    out = rt.settle_rolled_leg(price=17600.0, timestamp=datetime(2026, 8, 19, 14, 55))
    assert out["position"] == 1
    assert rt.trader.position == 0
    assert rt.portfolio.position == 0


def test_the_settled_leg_realizes_its_pnl_like_any_other_close():
    rt, _rec = _rolling_runtime()
    out = rt.settle_rolled_leg(price=17600.0, timestamp=datetime(2026, 8, 19, 14, 55))
    assert out["realized_points"] > 0
    assert rt.portfolio.realized_pnl > 0


def test_the_settlement_price_is_recorded_as_an_estimate():
    # The true number is the venue's final settlement auction, which this process
    # never sees; the trail must not imply otherwise.
    rt, rec = _rolling_runtime()
    rt.settle_rolled_leg(price=17600.0, timestamp=datetime(2026, 8, 19, 14, 55))
    event, fields = [e for e in rec.events if e[0] == "roll_leg_settled"][0]
    assert fields["price_is_estimated"] is True
    assert fields["settled_at"] == 17600.0


def test_settling_a_flat_book_does_nothing():
    from forecast_eval.tests.test_live_runtime import _bars, _runtime
    rt = _runtime()
    rt.warm_up(_bars(60, start="2026-08-18 16:00"))
    assert rt.settle_rolled_leg(price=1.0, timestamp=NOW) is None


def test_the_exposure_is_re_opened_on_the_first_bar_of_the_new_contract():
    # "Roll the exposure": the settled size comes back as a real order on the new
    # month, through the ordinary signal path.
    rt, rec = _rolling_runtime()
    rt.settle_rolled_leg(price=17600.0, timestamp=datetime(2026, 8, 19, 14, 55))
    assert rt._roll_entry == 1

    for ts in pd.date_range("2026-08-19 16:00", periods=120, freq="1s"):
        rt.on_tick(ts, 17610.0, 1)             # enough ticks to close one bar
    resumed = [f for e, f in rec.events if e == "roll_exposure_resumed"]
    assert resumed and resumed[0]["direction"] == "BUY"
    assert rt._roll_entry == 0


def test_a_short_rolls_back_as_a_short():
    rt, rec = _rolling_runtime()
    rt.trader.position = -1
    rt.portfolio.position = -1
    rt.settle_rolled_leg(price=17400.0, timestamp=datetime(2026, 8, 19, 14, 55))
    for ts in pd.date_range("2026-08-19 16:00", periods=120, freq="1s"):
        rt.on_tick(ts, 17390.0, 1)
    resumed = [f for e, f in rec.events if e == "roll_exposure_resumed"]
    assert resumed and resumed[0]["direction"] == "SELL"


def test_the_re_entry_is_disarmed_even_when_it_cannot_be_placed():
    # Disarming before submitting is what keeps a broker failure from re-firing
    # this on every later bar.
    rt, _rec = _rolling_runtime()
    rt.settle_rolled_leg(price=17600.0, timestamp=datetime(2026, 8, 19, 14, 55))
    rt.trader.position = 1                     # something else opened first
    rt._resume_rolled_exposure(type("M", (), {"timestamp": NOW, "price": 1.0})())
    assert rt._roll_entry == 0


# --- the scheduled roll -----------------------------------------------------

def test_the_last_trading_day_is_the_third_wednesday():
    assert last_trading_day("202608") == date(2026, 8, 19)
    assert last_trading_day("202609") == date(2026, 9, 16)
    assert last_trading_day("202701") == date(2027, 1, 20)


def test_the_roll_lands_in_the_expiring_contract_s_final_night_session():
    # The final night session starts 15:00 the day *before* the last trading day;
    # 15:30 is its most liquid half hour and ~13h clear of the contract's end.
    assert roll_at("202609") == datetime(2026, 9, 15, 15, 30)


def test_the_front_month_follows_the_same_instant_the_stream_switches_on():
    assert front_month_at(datetime(2026, 9, 15, 15, 29)) == "202609"
    assert front_month_at(datetime(2026, 9, 15, 15, 30)) == "202610"


def test_the_front_month_rolls_across_a_year_boundary():
    assert front_month_at(datetime(2026, 12, 16, 20, 0)) == "202701"


def test_the_window_opens_at_the_roll_instant_and_closes_an_hour_later():
    s = RollSchedule(month="202609")
    assert not s.due(datetime(2026, 9, 15, 15, 29))
    assert s.due(datetime(2026, 9, 15, 15, 30))
    assert s.due(datetime(2026, 9, 15, 16, 29))
    assert not s.due(datetime(2026, 9, 15, 16, 31))


def test_a_completed_roll_does_not_fire_again_inside_its_window():
    # The window is a retry budget for two orders, not a repeat instruction.
    s = RollSchedule(month="202609")
    assert s.due(datetime(2026, 9, 15, 15, 30))
    s.done("202609", "202610", datetime(2026, 9, 15, 15, 31))
    assert not s.due(datetime(2026, 9, 15, 15, 32))
    assert s.month == "202610"


def test_an_unrolled_window_that_closes_is_reported_as_lapsed():
    # Not silence: the plan did not happen, and the run is now relying on
    # RollWatch catching the expiry, which settles rather than trades.
    s = RollSchedule(month="202609")
    assert not s.lapsed(datetime(2026, 9, 15, 16, 0))
    assert s.lapsed(datetime(2026, 9, 15, 16, 31))
    s.done("202609", "202610", datetime(2026, 9, 15, 16, 0))
    assert not s.lapsed(datetime(2026, 9, 15, 16, 31))


# --- the scheduled roll sequence, end to end --------------------------------
#
# close on the old month -> the caller moves the contract -> re-open on the new.
# Every transition is driven by something observed, never by a timer.

def test_a_flat_book_goes_straight_to_the_switch():
    rt, _rec = _rolling_runtime()
    rt.trader.position = 0
    rt.portfolio.position = 0
    rt.request_roll()
    for ts in pd.date_range("2026-09-15 15:30", periods=120, freq="1s"):
        rt.on_tick(ts, 17500.0, 1)
    assert rt.roll_state == "switch"


def test_a_held_position_submits_a_close_before_the_switch():
    rt, rec = _rolling_runtime()
    rt.request_roll()
    for ts in pd.date_range("2026-09-15 15:30", periods=120, freq="1s"):
        rt.on_tick(ts, 17500.0, 1)
    assert rt.roll_state == "closing"
    assert "roll_close_submitted" in [e for e, _ in rec.events]


def test_the_switch_waits_for_the_close_to_actually_fill():
    # A roll whose close does not fill must stay visible as "closing" rather than
    # advancing into a switch that would strand the old contract's lot.
    rt, _rec = _rolling_runtime()
    rt.request_roll()
    for ts in pd.date_range("2026-09-15 15:30", periods=600, freq="1s"):
        rt.on_tick(ts, 17500.0, 1)
    assert rt.trader.position != 0 or rt.roll_state == "switch"


def test_resume_arms_the_re_entry_only_after_the_caller_has_switched():
    rt, rec = _rolling_runtime()
    rt.request_roll()
    rt._roll_state = "switch"          # the caller has moved the contract
    rt.resume_roll()
    assert rt.roll_state == "reopening"
    assert rt._roll_entry == 1


def test_a_flat_roll_needs_no_re_entry():
    rt, _rec = _rolling_runtime()
    rt.trader.position = 0
    rt.request_roll()
    rt._roll_state = "switch"
    rt.resume_roll()
    assert rt.roll_state is None
    assert rt._roll_entry == 0


def test_a_settled_leg_is_marked_as_a_roll_fill():
    # So `build_trades` books the settle-and-resume pair as one round-trip too.
    rt, _rec = _rolling_runtime()
    ts = datetime(2026, 9, 16, 14, 55)
    rt.settle_rolled_leg(price=17600.0, timestamp=ts)
    assert ts in rt.roll_fills


def test_a_re_entry_beaten_by_a_real_signal_is_recorded_not_silent():
    rt, rec = _rolling_runtime()
    rt.settle_rolled_leg(price=17600.0, timestamp=datetime(2026, 9, 16, 14, 55))
    rt.trader.position = 1                      # something else opened first
    rt._resume_rolled_exposure(
        type("M", (), {"timestamp": NOW, "price": 1.0})())
    assert "roll_resume_skipped" in [e for e, _ in rec.events]


# --- the escalating-aggression ladder (§2.6) --------------------------------
#
# A roll leg that does not fill is the only failure that matters: missing the
# close means holding an expiring contract into a cash settlement whose price we
# do not control. So the price escalates until it goes through — in equal 5-tick
# steps, on the clock rather than on bars, because a bar is a minute wide and the
# whole point is to retry inside one.

def test_the_ladder_steps_five_ticks_at_a_time_and_then_stops():
    from forecast_eval.live.runtime import ROLL_LADDER_MAX_TICKS, ladder_ticks
    assert [ladder_ticks(i) for i in range(1, 6)] == [5, 10, 15, 20, 25]
    assert ladder_ticks(50) == ROLL_LADDER_MAX_TICKS


def test_the_ladder_is_capped_because_aggression_stops_helping():
    # §2.19: the venue matches on prints. Past a point more aggression buys
    # nothing and only gives up the bound that makes this a limit and not an MKT.
    from forecast_eval.live.runtime import ladder_ticks
    assert ladder_ticks(6) == ladder_ticks(600)


def test_the_ladder_does_nothing_when_no_roll_leg_is_outstanding():
    rt, _rec = _rolling_runtime()
    assert rt.advance_roll_ladder() is None


def test_the_ladder_waits_out_its_interval_between_rungs():
    from forecast_eval.live import runtime as rtmod
    rt, _rec = _rolling_runtime()
    rt.request_roll()
    for ts in pd.date_range("2026-09-15 15:30", periods=120, freq="1s"):
        rt.on_tick(ts, 17500.0, 1)
    assert rt.roll_state == "closing"          # rung 1 went out with the close
    # The offline broker rests its limit; an IOC would have expired by now, which
    # is the state the ladder is designed around.
    rt.broker.has_pending = lambda: False
    assert rt.advance_roll_ladder() is None    # rung 2 is not due yet

    rt._ladder_last -= rtmod.ROLL_LADDER_INTERVAL + 1
    assert rt.advance_roll_ladder() == 10      # ... and now it is


def test_a_pending_order_blocks_the_next_rung():
    # The gate the broker's local IOC deadline exists to release: an expiry the
    # venue never reports would otherwise block every rung for the whole window.
    from forecast_eval.live import runtime as rtmod
    rt, _rec = _rolling_runtime()
    rt.request_roll()
    for ts in pd.date_range("2026-09-15 15:30", periods=120, freq="1s"):
        rt.on_tick(ts, 17500.0, 1)
    rt._ladder_last -= rtmod.ROLL_LADDER_INTERVAL + 1
    rt.broker.has_pending = lambda: True
    assert rt.advance_roll_ladder() is None


def test_each_rung_is_recorded_with_its_aggression():
    from forecast_eval.live import runtime as rtmod
    rt, rec = _rolling_runtime()
    rt.request_roll()
    for ts in pd.date_range("2026-09-15 15:30", periods=120, freq="1s"):
        rt.on_tick(ts, 17500.0, 1)
    rt._ladder_last -= rtmod.ROLL_LADDER_INTERVAL + 1
    rt.broker.has_pending = lambda: False
    rt.advance_roll_ladder()
    rungs = [f for e, f in rec.events if e == "roll_ladder_attempt"]
    assert rungs and rungs[-1]["ticks_through"] == 10
    assert rungs[-1]["leg"] == "closing"


def test_the_second_leg_starts_its_ladder_from_the_bottom():
    # The close's escalation says nothing about the open's; starting the second
    # leg at the cap would pay for the first leg's problem twice.
    rt, _rec = _rolling_runtime()
    rt.request_roll()
    for ts in pd.date_range("2026-09-15 15:30", periods=120, freq="1s"):
        rt.on_tick(ts, 17500.0, 1)
    rt._ladder_attempt = 4                     # the close needed four rungs
    rt._roll_state = "switch"
    rt.resume_roll()
    assert rt._ladder_attempt == 0
