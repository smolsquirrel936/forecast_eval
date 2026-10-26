"""The contract roll — a soak must not die because the front month moved.

These tests exist because one did (2026-08-19, SHIOAJI_PLAN.md §2.6). `TXFR1` is
an alias resolved once at start-up, so a process that outlives a settlement holds
an object the venue has retired: fifteen FeedWatch repairs re-subscribed the same
dead contract, produced zero ticks, and the run halted on staleness.

Three things are covered here, in the order a roll exercises them: noticing it
(`RollWatch`), warming up across it (the spliced backfill), and carrying the
position through it (`LiveRuntime`).
"""
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from functools import partial

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
    ROLL_WINDOW,
    RollSchedule,
    RollWatch,
    front_month_at,
    front_month_series,
    last_trading_day,
    roll_at,
    roll_calendar,
    roll_at,
)
from forecast_eval.live.sessions import SessionPolicy

# These build a `LiveRuntime`, which now assembles its components from the
# session package; the fixture stands a cheap deterministic strategy in its
# place so a short synthetic frame is still a valid context (see conftest).
pytestmark = pytest.mark.usefixtures("cheap_strategy")

# The splice rule bound to NIGHT's policy, for the cases whose subject is the
# rule rather than the deployment. `front_month_at` has required a policy since
# TAPE_REBUILD_PLAN 5.3 -- an unbound one is what let a DAY run splice its
# history at 15:30 while rolling its position at 12:45 -- and NIGHT's policy is
# the module constant the old default meant, so nothing these cases assert moved.
_night_front = partial(front_month_at, policy=SessionPolicy.night())


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
    out = collapse_front_month([old, new], front_month_at=_night_front)
    assert list(out["price"]) == [100.0, 201.0]
    assert list(out["contract"]) == ["202609", "202610"]


def test_a_minute_the_front_month_did_not_print_is_filled_not_dropped():
    # Better filled from the other month than lost: the fallback is volume.
    old = _frame([("2026-09-15 15:31", 101.0, 900)], contract="202609")
    out = collapse_front_month([old], front_month_at=_night_front)
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

    def trade(self, kind, message, **fields):
        self.sent.append(("trade", kind))

    def info(self, kind, message, **fields):
        self.sent.append(("info", kind))

    def fatal(self, kind, message, **fields):
        self.sent.append(("fatal", kind))


def _rolling_runtime(policy=None, start="2026-08-18 16:00"):
    """A warm runtime holding one long lot, as a roll would find it.

    Defaults to NIGHT; `policy`/`start` let the DAY mirror be built the same
    way, which is what C3's regression needs."""
    from forecast_eval.tests.test_live_runtime import _bars, _runtime
    rec = Recorder()
    rt = _runtime(policy, recorder=rec, alerter=Alerter())
    rt.warm_up(_bars(60, start=start))
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
    assert _night_front(datetime(2026, 9, 15, 15, 29)) == "202609"
    assert _night_front(datetime(2026, 9, 15, 15, 30)) == "202610"


def test_the_front_month_rolls_across_a_year_boundary():
    assert _night_front(datetime(2026, 12, 16, 20, 0)) == "202701"


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


# --- roll legs and a trader's entry filters (2026-10-26) ---------------------
#
# A roll re-opens as an OPEN from flat on the side it just closed -- exactly what
# a same-side re-entry cooldown blocks. Found on the fullday reversal loop: the
# settled leg's single-shot re-entry was dropped, and the ladder's rungs each
# burned one cooldown count. The runtime now enters the trader's optional
# `rolling()` context around every call it makes on a roll's behalf.

class _Market:
    def __init__(self, ts, price):
        self.timestamp = ts
        self.price = price


def _same_side_filter(rt, *, hook=True):
    """Make rt.trader refuse a BUY from flat unless it is inside `rolling()`.

    Records, per trader call, whether the call was made inside the hook."""
    tr = rt.trader
    state = {"rolling": False, "calls": []}
    if hook:
        @contextmanager
        def rolling():
            state["rolling"] = True
            try:
                yield
            finally:
                state["rolling"] = False
        tr.rolling = rolling
    for name in ("on_fill", "submit_exit"):
        def wrapped(*a, _orig=getattr(tr, name), _name=name, **kw):
            state["calls"].append((_name, state["rolling"]))
            return _orig(*a, **kw)
        setattr(tr, name, wrapped)
    orig_signal = tr.on_signal

    def on_signal(direction, market, **kw):
        state["calls"].append(("on_signal", state["rolling"]))
        if tr.position == 0 and direction == "BUY" and not state["rolling"]:
            return None
        return orig_signal(direction, market, **kw)
    tr.on_signal = on_signal
    return state


def _resumed(rec):
    return [f for e, f in rec.events if e == "roll_exposure_resumed"]


def test_a_settled_leg_is_re_entered_through_a_same_side_filter():
    rt, rec = _rolling_runtime()
    state = _same_side_filter(rt)
    rt.settle_rolled_leg(price=17600.0, timestamp=datetime(2026, 8, 19, 14, 55))
    rt._resume_rolled_exposure(_Market(datetime(2026, 8, 19, 16, 0), 17610.0))
    assert _resumed(rec)[0]["quantity"] == 1
    assert ("on_fill", True) in state["calls"]       # the settlement fill
    assert ("on_signal", True) in state["calls"]     # the re-entry
    assert state["rolling"] is False                 # the context was left


def test_without_the_hook_the_filter_drops_the_settled_leg():
    # The defect the hook exists for: the single-shot re-entry is lost.
    rt, rec = _rolling_runtime()
    _same_side_filter(rt, hook=False)
    rt.settle_rolled_leg(price=17600.0, timestamp=datetime(2026, 8, 19, 14, 55))
    rt._resume_rolled_exposure(_Market(datetime(2026, 8, 19, 16, 0), 17610.0))
    assert _resumed(rec)[0]["quantity"] is None
    assert rt._roll_entry == 0


def test_a_scheduled_re_open_goes_out_on_the_first_rung_through_the_filter():
    rt, rec = _rolling_runtime()
    state = _same_side_filter(rt)
    rt.request_roll()
    rt.trader.position = 0                     # the close has filled
    rt.portfolio.position = 0
    rt._roll_state = "switch"                  # the caller has moved the contract
    rt.resume_roll()
    rt._resume_rolled_exposure(_Market(datetime(2026, 9, 16, 15, 31), 17500.0))
    assert _resumed(rec)[0]["quantity"] == 1
    assert rt._ladder_attempt == 1
    assert ("on_signal", True) in state["calls"]


def test_the_scheduled_close_is_submitted_inside_the_hook():
    rt, _rec = _rolling_runtime()
    state = _same_side_filter(rt)
    rt.request_roll()
    for ts in pd.date_range("2026-09-15 15:30", periods=120, freq="1s"):
        rt.on_tick(ts, 17500.0, 1)
    assert rt.roll_state == "closing"
    assert ("submit_exit", True) in state["calls"]


def test_strategy_signals_are_never_inside_the_hook():
    # Only the roll's own calls are marked; the strategy's signals during the
    # same ticks must still meet the trader's filters.
    rt, _rec = _rolling_runtime()
    state = _same_side_filter(rt)
    rt.trader.position = 0
    rt.portfolio.position = 0
    for ts in pd.date_range("2026-08-19 16:00", periods=600, freq="1s"):
        rt.on_tick(ts, 17610.0, 1)
    signals = [inside for name, inside in state["calls"] if name == "on_signal"]
    assert signals and not any(signals)


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


# ---- C3: the settled leg's session (DAY_SESSION_PLAN W1) --------------------
#
# `settle_rolled_leg` booked to the literal "NIGHT". That is right for one
# deployment and silently wrong for the other: on a DAY run the rolled
# round-trip accrued into `realized_pnl_by_session["NIGHT"]`, corrupting the
# day/night split with no error anywhere. These fail against the literal.

def _booked_to(rt):
    """Which session buckets actually received PnL.

    Both keys are pre-seeded to 0.0, so membership proves nothing — only a
    non-zero value does."""
    return {k for k, v in rt.portfolio.realized_pnl_by_session.items() if v != 0.0}


def test_a_night_deployment_books_the_settled_leg_to_night():
    rt, _rec = _rolling_runtime()
    rt.settle_rolled_leg(price=17600.0, timestamp=datetime(2026, 8, 19, 14, 55))
    assert _booked_to(rt) == {"NIGHT"}


def test_a_day_deployment_books_the_settled_leg_to_day():
    """The regression. Against the old literal this books to NIGHT — a session
    the run never traded — and nothing raises."""
    rt, _rec = _rolling_runtime(SessionPolicy.day(), start="2026-08-18 09:00")
    rt.settle_rolled_leg(price=17600.0, timestamp=datetime(2026, 8, 19, 9, 30))
    assert _booked_to(rt) == {"DAY"}
    assert rt.portfolio.realized_pnl_by_session["NIGHT"] == 0.0


def test_the_settled_leg_follows_the_deployment_not_the_clock():
    """C4's interaction: `ROLL_AT` is 15:30, which is outside a DAY session. The
    closing leg still belongs to the DAY round-trip its opening leg started —
    booking it by the wall clock would split one trade across two buckets."""
    rt, _rec = _rolling_runtime(SessionPolicy.day(), start="2026-08-18 09:00")
    at_1530 = datetime(2026, 8, 19, 15, 30)
    from forecast_eval.environment import classify_session
    assert classify_session(at_1530) == "NIGHT", "precondition: 15:30 is a NIGHT time"
    rt.settle_rolled_leg(price=17600.0, timestamp=at_1530)
    assert _booked_to(rt) == {"DAY"}


def test_the_settlement_records_the_session_it_booked():
    """The trail has to say where the PnL went, or the attribution is only
    checkable by re-deriving it."""
    rt, rec = _rolling_runtime(SessionPolicy.day(), start="2026-08-18 09:00")
    rt.settle_rolled_leg(price=17600.0, timestamp=datetime(2026, 8, 19, 9, 30))
    _event, fields = [e for e in rec.events if e[0] == "roll_leg_settled"][0]
    assert fields["session"] == "DAY"


def test_an_explicit_session_still_wins():
    """The parameter is kept so a caller can force a value; no production caller
    does."""
    rt, _rec = _rolling_runtime()
    rt.settle_rolled_leg(price=17600.0, timestamp=datetime(2026, 8, 19, 14, 55),
                         session="DAY")
    assert _booked_to(rt) == {"DAY"}


# ---- C5: the bar cache is keyed by policy (DAY_SESSION_PLAN W1) -------------
#
# `cli.py` hardcoded one `outputs/bars_cache.csv` for every deployment, so a DAY
# run wrote DAY bars into the file a NIGHT run reads back as model context.
# These fail against the shared literal.

def test_two_deployments_do_not_share_a_bar_cache(tmp_path):
    from forecast_eval.live.cli import _bars_cache_path
    night = _bars_cache_path(tmp_path, SessionPolicy.night())
    day = _bars_cache_path(tmp_path, SessionPolicy.day())
    assert night != day, (
        "a DAY run would write its bars into the file the NIGHT run reads"
    )
    assert night.parent == day.parent == tmp_path


def test_the_cache_name_states_which_deployment_owns_it(tmp_path):
    """The filename has to be readable on its own — an operator looking at
    `outputs/` should not have to infer which run wrote which cache."""
    from forecast_eval.live.cli import _bars_cache_path
    assert _bars_cache_path(tmp_path, SessionPolicy.night()).name == "bars_cache_night.csv"
    assert _bars_cache_path(tmp_path, SessionPolicy.day()).name == "bars_cache_day.csv"


def test_a_multi_session_policy_gets_its_own_cache_too(tmp_path):
    from forecast_eval.live.cli import _bars_cache_path
    both = _bars_cache_path(tmp_path, SessionPolicy.both())
    assert both.name == "bars_cache_day_night.csv"
    assert both != _bars_cache_path(tmp_path, SessionPolicy.night())
    assert both != _bars_cache_path(tmp_path, SessionPolicy.day())


def test_the_pre_c5_cache_is_not_adopted_automatically(tmp_path):
    """The legacy `bars_cache.csv` holds NIGHT bars. Auto-adopting it would give
    them to whichever deployment ran first — a DAY run inheriting a night cache
    is the corruption C5 exists to prevent, arriving by a different door."""
    from forecast_eval.live.cli import _bars_cache_path
    legacy = tmp_path / "bars_cache.csv"
    legacy.write_text("timestamp,price,volume,contract" + chr(10), encoding="utf-8")
    day = _bars_cache_path(tmp_path, SessionPolicy.day())
    assert not day.exists()
    assert legacy.exists(), "the legacy file must be left alone, not consumed"


def test_cache_slug_is_stable_and_filesystem_safe():
    assert SessionPolicy.night().cache_slug() == "night"
    assert SessionPolicy.day().cache_slug() == "day"
    assert SessionPolicy.both().cache_slug() == "day_night"
    # order-independent: the slug is derived from a set
    assert SessionPolicy.of(["NIGHT", "DAY"]).cache_slug() == "day_night"


# ---- C4: ROLL_AT is carried per deployment (DAY_SESSION_PLAN W2) ------------
#
# 15:30 was measured for a NIGHT strategy and is OUTSIDE a DAY session: the
# clock still fires, so the ladder runs against a tape the run is not
# following. DECIDED 2026-08-25 -- DAY 12:45 (one hour before the 13:45 close,
# so ROLL_WINDOW fills the session exactly), NIGHT unchanged at 15:30.

def test_the_two_deployments_do_not_share_a_roll_time():
    assert SessionPolicy.day().roll_at_time() != SessionPolicy.night().roll_at_time()


def test_night_keeps_the_measured_1530():
    from forecast_eval.live.roll import roll_at
    assert roll_at("202609", SessionPolicy.night()) == datetime(2026, 9, 15, 15, 30)


def test_day_rolls_at_1245():
    from forecast_eval.live.roll import roll_at
    assert roll_at("202609", SessionPolicy.day()) == datetime(2026, 9, 15, 12, 45)


def test_the_day_roll_window_fits_inside_the_day_session():
    """The point of 12:45. ROLL_WINDOW is an hour and the session closes 13:45,
    so the window ends exactly at the close -- it can neither overrun onto a
    dead tape nor leave the schedule firing after hours."""
    from forecast_eval.live.roll import roll_at, ROLL_WINDOW
    start = roll_at("202609", SessionPolicy.day())
    end = start + ROLL_WINDOW
    assert start.time() == time(12, 45)
    assert end.time() == time(13, 45)
    assert end.date() == start.date()


def test_the_night_roll_instant_is_inside_the_night_session():
    from forecast_eval.live.roll import roll_at
    from forecast_eval.environment import classify_session
    assert classify_session(roll_at("202609", SessionPolicy.night())) == "NIGHT"


def test_a_day_deployment_would_have_rolled_outside_its_session_before_this():
    """The defect, stated as a test: night's 15:30 is not a DAY moment at all."""
    from forecast_eval.environment import classify_session
    assert classify_session(datetime(2026, 9, 15, 15, 30)) == "NIGHT"
    assert classify_session(datetime(2026, 9, 15, 12, 45)) == "DAY"


def test_the_schedule_uses_its_policys_time():
    from forecast_eval.live.roll import RollSchedule
    day = RollSchedule(month="202609", policy=SessionPolicy.day())
    assert day.due(datetime(2026, 9, 15, 12, 45))
    assert not day.due(datetime(2026, 9, 15, 15, 30)), (
        "a DAY schedule must not come due at NIGHT's roll time"
    )
    night = RollSchedule(month="202609", policy=SessionPolicy.night())
    assert night.due(datetime(2026, 9, 15, 15, 30))
    assert not night.due(datetime(2026, 9, 15, 12, 45))


def test_a_schedule_with_no_policy_keeps_the_module_default():
    """Back-compat: every existing call site behaves exactly as before."""
    from forecast_eval.live.roll import RollSchedule, roll_at, ROLL_AT
    assert roll_at("202609") == roll_at("202609", SessionPolicy.night())
    assert ROLL_AT == time(15, 30)
    assert RollSchedule(month="202609").due(datetime(2026, 9, 15, 15, 30))


def test_the_splice_rule_requires_a_policy_and_night_is_still_the_old_default():
    """This test used to assert the opposite, and the assertion was the defect.

    It read: "threading a policy into `front_month_at` would move a live
    deployment's warm-up context as a side effect of a day-session decision, so
    it is pinned to the module default". That protected NIGHT and broke DAY --
    `live/cli.py` passed the function to the warm-up backfill **unbound**, so a
    DAY run rolled its position at 12:45 and spliced its history at 15:30, for
    183 of 183 DAY rolls that traded (TAPE_REBUILD_PLAN 5.3). The parameter is
    required now, so that call cannot be written by omission.

    What the old default protected is still true and is asserted here: NIGHT's
    policy *is* the module constant, so the pinned deployment does not move."""
    assert front_month_at(datetime(2026, 9, 15, 15, 29), NIGHT) == "202609"
    assert front_month_at(datetime(2026, 9, 15, 15, 31), NIGHT) == "202610"
    assert front_month_at(datetime(2026, 9, 15, 12, 46), NIGHT) == "202609"
    # ...and DAY, which the old default answered wrong in silence, moves at 12:45
    assert front_month_at(datetime(2026, 9, 15, 12, 46),
                          SessionPolicy.day()) == "202610"
    # omitting it is now unrepresentable rather than merely wrong
    with pytest.raises(TypeError):
        front_month_at(datetime(2026, 9, 15, 15, 29))


# ---- the roll instant is set by the clearance-day constraint ----------------
#
# DECIDED 2026-09-01. The expiring month stops trading at 13:30 on the clearance
# day (the third Wednesday). A deployment's roll must complete while it is awake
# AND that contract is still alive, so the instant is not a preference — it is
# the last such moment, minus the retry budget.


def test_any_policy_that_trades_day_rolls_inside_the_day_session():
    """A `both()` deployment on NIGHT's 15:30 would be rolling at an instant its
    day half cannot reach in time. 12:45 puts the whole ROLL_WINDOW inside the
    day session."""
    for p in (SessionPolicy.day(), SessionPolicy.both()):
        assert p.roll_at_time() == time(12, 45), sorted(p.sessions)


def test_night_alone_keeps_1530_because_it_cannot_reach_1245():
    """Not an inconsistency. A night-only deployment is asleep 05:00-15:00, so
    12:45 on any day is a moment it is not running."""
    assert SessionPolicy.night().roll_at_time() == time(15, 30)


def test_the_roll_window_of_a_day_trading_policy_ends_at_the_close():
    """12:45 + ROLL_WINDOW = 13:45, exactly the day-session close, so the retry
    budget fills the rest of the session and cannot overrun it."""
    for p in (SessionPolicy.day(), SessionPolicy.both()):
        start = datetime.combine(date(2026, 9, 15), p.roll_at_time())
        assert (start + ROLL_WINDOW).time() == time(13, 45)


def test_every_deployment_rolls_before_the_expiring_month_stops_trading():
    """The constraint the instant exists to satisfy, asserted rather than
    argued. The 202609 contract dies 2026-09-16 13:30."""
    clear = datetime(2026, 9, 16, 13, 30)
    for p in (SessionPolicy.day(), SessionPolicy.night(), SessionPolicy.both()):
        end_of_window = roll_at("202609", p) + ROLL_WINDOW
        assert end_of_window < clear, (
            f"{sorted(p.sessions)} finishes rolling at {end_of_window}, "
            f"after the contract dies at {clear}"
        )


def test_a_full_day_deployment_rolls_on_the_same_schedule_as_day():
    """DECIDED 2026-09-01. Left on NIGHT's instant the full-day deployment would
    be scheduled past the point where rolling is still possible for the half of
    it that trades the day session -- the position would expire rather than be
    rolled."""
    assert (roll_at("202609", SessionPolicy.both())
            == roll_at("202609", SessionPolicy.day()))
    assert (roll_at("202609", SessionPolicy.both())
            != roll_at("202609", SessionPolicy.night()))


# ---- TAPE_REBUILD_PLAN T0 — one splice rule, shared by the tape and the cache -
#
# T0's acceptance is two claims: the rule agrees with `front_month_at` on every
# splice 2017-2026, and it never names a contract that has stopped trading.
# Both are asserted here over the tape's full span rather than sampled.

T0_FIRST, T0_LAST = "201705", "202612"          # the tape's span, plus runway


def _all_months(first=T0_FIRST, last=T0_LAST):
    out = [first]
    while out[-1] < last:
        y, m = int(out[-1][:4]), int(out[-1][4:6])
        out.append(f"{y + 1}01" if m == 12 else f"{y}{m + 1:02d}")
    return out


def test_roll_calendar_is_ascending_and_covers_every_month():
    bounds, months = roll_calendar(T0_FIRST, T0_LAST)
    assert months == _all_months()
    assert bounds == sorted(bounds), "roll instants must be ascending"
    assert len(set(bounds)) == len(bounds), "two months cannot roll at once"
    # one roll per delivery month, and TAIFEX lists one per calendar month
    assert len(bounds) == len(months)


@pytest.mark.parametrize("policy", [None, SessionPolicy.night(),
                                    SessionPolicy.day(), SessionPolicy.both()])
def test_vectorised_rule_equals_the_scalar_one_at_every_splice(policy):
    """T0's first acceptance criterion. Checked at the splice instants and the
    minutes either side of them -- where the two forms could disagree -- for
    every month 2017-2026."""
    probes = []
    for m in _all_months():
        r = roll_at(m, policy)
        probes += [r - timedelta(minutes=1), r, r + timedelta(minutes=1)]
    got = front_month_series(probes, policy)
    for ts, g in zip(probes, got):
        assert g == front_month_at(ts, policy), f"{ts} under {policy}"


def test_vectorised_rule_equals_the_scalar_one_across_the_whole_span():
    """Not just at the splices: every 6 h from 2017 to 2026."""
    ts = pd.date_range("2017-05-15", "2026-12-31", freq="6h")
    got = front_month_series(ts, NIGHT)
    for t, g in zip(ts, got):
        assert g == _night_front(t)


def test_the_rule_never_names_a_contract_that_has_stopped_trading():
    """T0's second acceptance criterion -- "no dead-contract minutes".

    The expiring month stops trading at 13:30 on its last trading day. A splice
    rule that ever returned a month already past that instant would attribute
    prints to a contract that cannot produce them, which is Defect A (0.2) in
    rule form rather than in the data."""
    ts = pd.date_range("2017-05-15", "2026-12-31", freq="1h")
    got = front_month_series(ts, NIGHT)
    for t, month in zip(ts, got):
        dies = datetime.combine(last_trading_day(month), time(13, 30))
        assert t.to_pydatetime() < dies, (
            f"{t} was attributed to {month}, which stopped trading {dies}"
        )


def test_every_policy_splices_before_the_contract_it_leaves_dies():
    """The same claim from the other side: each roll instant precedes the death
    of the month being left, with the whole ROLL_WINDOW still to spare."""
    for policy in (None, SessionPolicy.night(), SessionPolicy.day(),
                   SessionPolicy.both()):
        for m in _all_months():
            dies = datetime.combine(last_trading_day(m), time(13, 30))
            assert roll_at(m, policy) + ROLL_WINDOW < dies, (m, policy)


def test_day_and_full_day_splice_together_and_night_does_not():
    """TAPE_REBUILD_PLAN 2.3 as amended -- three tapes, two splice points."""
    day, night, both = (SessionPolicy.day(), SessionPolicy.night(),
                        SessionPolicy.both())
    for m in _all_months():
        assert roll_at(m, day) == roll_at(m, both)
        assert roll_at(m, night) != roll_at(m, day)
        # night splices later on the same date
        assert roll_at(m, night) > roll_at(m, day)
        assert roll_at(m, night).date() == roll_at(m, day).date()


def test_the_two_splice_points_disagree_by_2h45m_on_every_roll_date():
    """The window in which the day/combined tapes and the night tape hold
    DIFFERENT delivery months: 12:45-15:30 on the roll date. Only 15:00-15:30 of
    it is inside a traded session, so it costs 30 traded minutes a roll -- but
    the tapes disagree for the whole 2h45m, which is what a reconciliation
    between them has to allow for."""
    day, night = SessionPolicy.day(), SessionPolicy.night()
    for m in _all_months():
        gap = roll_at(m, night) - roll_at(m, day)
        assert gap == timedelta(hours=2, minutes=45)


def test_night_policy_is_the_module_constant_so_the_pinned_context_does_not_move():
    """The property the removed default existed to protect, kept as an explicit
    assertion — this is what makes requiring the argument a no-op for NIGHT.
    `None` still *means* the module constant; what changed is that a caller has
    to say so."""
    for m in _all_months():
        assert roll_at(m) == roll_at(m, SessionPolicy.night())
    ts = pd.date_range("2020-01-01", "2026-12-31", freq="7h")
    a = front_month_series(ts, None)
    b = front_month_series(ts, SessionPolicy.night())
    assert list(a) == list(b)


def test_front_month_series_handles_an_empty_column():
    assert len(front_month_series([], NIGHT)) == 0


# ---- 2026-10-20: the re-open priced from the old month ----------------------
#
# simtrade_operations §11. The switch happened at 15:40:02 and the ladder's
# rung 1 went out the same second at 46419, the last October bar's price, while
# November traded ~46578. Prints are scaled down here; the gap is what matters.

OLD, NEW = "TXFJ6", "TXFK6"


def _switched_runtime():
    """A runtime at the scheduled roll's switch: old leg closed, one bar of the
    old month half-built, and a ladder reference left over from that month."""
    rt, rec = _rolling_runtime()
    rt.request_roll()
    rt.trader.position = 0
    rt.portfolio.position = 0
    for ts in pd.date_range("2026-10-20 15:38:00", periods=119, freq="1s"):
        rt.on_tick(ts, 17500.0, 1, code=OLD)
    assert rt.roll_state == "switch"
    assert rt._ladder_market is not None and rt._ladder_market.price == 17500.0
    rt.broker.cancel()            # the cheap strategy's own order, not the roll's
    assert not rt.broker.has_pending()
    rt.switch_contract({NEW})
    rt.resume_roll()
    return rt, rec


def test_the_switch_leaves_the_ladder_nothing_old_to_price_from():
    rt, rec = _switched_runtime()
    rt.broker.has_pending = lambda: False
    assert rt.advance_roll_ladder() is None
    assert [f for e, f in rec.events if e == "roll_ladder_attempt"] == []


def test_the_re_open_is_priced_from_the_first_new_month_bar():
    rt, rec = _switched_runtime()
    rt.on_tick(pd.Timestamp("2026-10-20 15:40:01"), 17500.0, 1, code=OLD)
    for ts in pd.date_range("2026-10-20 15:40:02", periods=63, freq="1s"):
        rt.on_tick(ts, 17660.0, 1, code=NEW)
    resumed = [f for e, f in rec.events if e == "roll_exposure_resumed"]
    assert resumed and resumed[0]["direction"] == "BUY"
    assert resumed[0]["timestamp"] == pd.Timestamp("2026-10-20 15:40")
    assert resumed[0]["price"] is None or resumed[0]["price"] >= 17660.0 - 25
    assert rt._ladder_market.price == 17660.0
    assert rt.n_foreign_dropped == 1


def test_the_re_open_starts_the_ladder_at_rung_one():
    # The close leg's shape: rung 2 waits out the interval rather than firing on
    # the next clock second behind a plain order.
    rt, _rec = _switched_runtime()
    for ts in pd.date_range("2026-10-20 15:40:02", periods=63, freq="1s"):
        rt.on_tick(ts, 17660.0, 1, code=NEW)
    assert rt._ladder_attempt == 1
    rt.broker.has_pending = lambda: False
    assert rt.advance_roll_ladder() is None


def test_prints_without_a_code_still_count_after_a_switch():
    # A source that stamps no code (replays, tests) must not be silenced.
    rt, _rec = _switched_runtime()
    for ts in pd.date_range("2026-10-20 15:40:02", periods=63, freq="1s"):
        rt.on_tick(ts, 17660.0, 1)
    assert rt.n_foreign_dropped == 0
    assert rt._ladder_market.price == 17660.0


def test_no_print_is_filtered_before_a_roll():
    from forecast_eval.tests.test_live_runtime import _bars, _runtime
    rt = _runtime()
    rt.warm_up(_bars(60, start="2026-08-18 16:00"))
    for ts in pd.date_range("2026-08-19 16:00", periods=10, freq="1s"):
        rt.on_tick(ts, 17500.0, 1, code="ANYTHING")
    assert rt.n_foreign_dropped == 0


# ---- 2026-10-20: the exchange's front month vs ours -------------------------

def test_the_exchange_front_month_moves_when_the_expiring_month_stops():
    from forecast_eval.live.roll import exchange_front_month
    assert exchange_front_month(datetime(2026, 10, 20, 14, 59)) == "202610"
    assert exchange_front_month(datetime(2026, 10, 21, 8, 44)) == "202610"
    assert exchange_front_month(datetime(2026, 10, 21, 13, 29)) == "202610"
    assert exchange_front_month(datetime(2026, 10, 21, 13, 30)) == "202611"
    assert exchange_front_month(datetime(2026, 10, 21, 14, 59)) == "202611"
    assert exchange_front_month(datetime(2026, 12, 31, 23, 0)) == "202701"


def test_the_exchange_front_month_trails_our_roll_by_about_a_day():
    from forecast_eval.live.roll import exchange_front_month
    day = SessionPolicy.day()
    ts = datetime(2026, 10, 20, 14, 59)
    assert front_month_at(ts, day) == "202611"
    assert exchange_front_month(ts) == "202610"


def test_a_settled_leg_joins_the_fill_list_the_ledger_appends():
    # Left out, the ledger read the settled lot and its re-open as two open
    # lots, and `roll_fills` named a timestamp no fill carried.
    rt, _rec = _rolling_runtime()
    ts = datetime(2026, 9, 16, 14, 55)
    rt.settle_rolled_leg(price=17600.0, timestamp=ts)
    (fill,) = [f for f in rt.loop.fills if f.timestamp == ts]
    assert (fill.side, fill.fill_price, fill.quantity) == ("SELL", 17600.0, 1)


def test_a_month_expires_at_its_last_trading_days_close():
    from forecast_eval.live.roll import month_expired
    # October 2026's third Wednesday is the 21st; it stops trading at 13:30.
    assert not month_expired("202610", datetime(2026, 10, 21, 13, 29))
    assert month_expired("202610", datetime(2026, 10, 21, 13, 30))
    assert not month_expired("202611", datetime(2026, 10, 21, 13, 30))
    assert not month_expired("", datetime(2026, 10, 21, 13, 30))
