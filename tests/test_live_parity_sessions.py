"""W7 — offline parity for a *single-session deployment*, V0-style.

`test_live_parity.py` proves `LiveLoop` and `run_backtest` decide identically
over one stream. It is session-blind: it drives `LiveLoop.on_bar` directly, so
nothing in it exercises `SessionPolicy`, and the only session content is a
forced-close config that deliberately crosses DAY<->NIGHT.

A *deployment* is narrower than that, and the difference is where a day-session
port can break. Live, the bars reaching the loop have already been filtered by
`TickAggregator`, which drops anything outside `policy.sessions`
(`live/bars.py:227-234`). Offline, `run_backtest` consumes whatever frame it is
handed. So the claim W7 has to establish is not "the loop transcription is
right" — V0 settled that — but:

    given a tape containing BOTH sessions, a deployment pinned to one of them
    reaches the same decisions as a backtest over that session's bars alone.

That is exactly what `results.tsv`'s DAY rows assume: they were measured by
`run_backtest` over `TXF_RPT_minute_day.csv`, and a DAY deployment has to
reproduce them from a live feed carrying night prints it must ignore.

The ticks go through the **real** `TickAggregator`, not a reimplementation of
its filter — testing an emulation of the thing that does the dropping would
leave the actual dropper untested. `flush_grace=0` is the offline-replay
setting the aggregator documents: it makes the aggregator a pure function of
its ticks.

Both deployments in `CONFIGS` are covered, with each one's *own pinned emitter*
— so this is parity for the configuration that would really be deployed, not
for arbitrary thresholds.
"""
from datetime import timedelta

import pandas as pd
import pytest

from forecast_eval.environment import Environment
from forecast_eval.execution import Execution
from forecast_eval.live.bars import TickAggregator
from forecast_eval.live.broker.sim import SimBroker
from forecast_eval.live.config import CONFIGS, config_named
from forecast_eval.live.loop import LiveLoop
from forecast_eval.portfolio import Portfolio
from forecast_eval.run import run_backtest
from forecast_eval.trader import Trader

from forecast_eval.tests.test_live_parity import (
    FEE_RATE, HISTORY_WINDOW, MULTIPLIER, _assert_identical, _make_forecaster,
    _ticks,
)


def _deployment_bars(ticks: pd.DataFrame, policy):
    """The bars a deployment on `policy` actually sees, via the real aggregator.

    Returns (frame, aggregator) — the frame is what the backtest is given, and
    the aggregator is returned so the test can assert it really dropped
    something rather than passing everything through.
    """
    agg = TickAggregator(policy=policy, drop_simtrade=True,
                         drop_non_session=True, flush_grace=timedelta(0))
    events = []
    for row in ticks.itertuples(index=False):
        events.extend(agg.add_tick(row.timestamp, float(row.price),
                                   int(row.volume)))
    # Close the final open bucket the way an offline replay ends.
    events.extend(agg.flush(ticks["timestamp"].iloc[-1] + timedelta(minutes=2)))
    frame = pd.DataFrame({
        "timestamp": [e.timestamp for e in events],
        "price": [e.price for e in events],
        "volume": [e.volume for e in events],
    })
    return frame, agg, events


def _run_live_over(events, *, emitter, **trader_kw):
    execution = Execution(fee_rate=FEE_RATE)
    broker = SimBroker(execution)
    portfolio = Portfolio(contract_multiplier=MULTIPLIER)
    trader = Trader(execution=broker, tick_size=1.0, **trader_kw)
    loop = LiveLoop(
        forecaster=_make_forecaster(),
        emitter=emitter,
        broker=broker,
        trader=trader,
        portfolio=portfolio,
        exit_rule=None,
        forced_close_on_session_end=False,
        history_window=HISTORY_WINDOW,
    )
    for market in events:
        loop.on_bar(market)
    return loop.result()


def _run_backtest_over(frame, *, emitter, **trader_kw):
    return run_backtest(
        frame,
        forecaster=_make_forecaster(),
        emitter=emitter,
        exit_rule=None,
        forced_close_on_session_end=False,
        tick_size=1.0,
        fee_rate=FEE_RATE,
        contract_multiplier=MULTIPLIER,
        history_window=HISTORY_WINDOW,
        **trader_kw,
    )


# Tech: the deployments that trade exactly one session.
# Why:  STRATEGY_SPLIT_PLAN S4 registered `fullday`, whose `both()` policy filters
#       nothing out of a mixed tape -- so the two tests below, which assert that
#       the OTHER session was removed, are meaningless for it rather than merely
#       failing. Full-day's own invariant is the opposite one and is asserted
#       separately at the end of this file.
SINGLE_SESSION = sorted(n for n, c in CONFIGS.items() if len(c.policy.sessions) == 1)


@pytest.fixture(scope="module")
def mixed_tape():
    """A tape carrying both sessions — the thing a live feed delivers."""
    return _ticks()


@pytest.mark.parametrize("name", SINGLE_SESSION)
def test_single_session_deployment_matches_its_backtest(name, mixed_tape):
    """W7's acceptance test, for every pinned deployment."""
    cfg = config_named(name)
    session = next(iter(cfg.policy.sessions))
    frame, agg, events = _deployment_bars(mixed_tape, cfg.policy)

    # The filter must have actually removed the other session, or the whole
    # premise of this test is untested.
    assert agg.n_bars_dropped_off_session > 0, (
        f"{name}: nothing was filtered — the tape is not carrying both sessions"
    )
    assert len(events) > 0
    assert {e.session for e in events} == {session}, (
        f"{name} deployment saw sessions {sorted({e.session for e in events})}"
    )

    trader_kw = dict(aggression_ticks=cfg.aggression_ticks,
                     max_position=cfg.max_position)
    live = _run_live_over(events, emitter=cfg.build_emitter(), **trader_kw)
    back = _run_backtest_over(frame, emitter=cfg.build_emitter(), **trader_kw)
    _assert_identical(live, back)

    # PnL is booked to the deployment's own session and to no other. Both keys
    # always exist -- `Portfolio` seeds {"DAY": 0.0, "NIGHT": 0.0} -- so the
    # assertion is that the other bucket was never written to, not that it is
    # absent.
    other = "NIGHT" if session == "DAY" else "DAY"
    assert back.portfolio.realized_pnl_by_session[session] != 0.0
    assert back.portfolio.realized_pnl_by_session[other] == 0.0
    assert back.portfolio.fees_by_session[other] == 0.0


@pytest.mark.parametrize("name", SINGLE_SESSION)
def test_the_deployment_never_sees_the_other_session(name, mixed_tape):
    """C2's invariant, measured on the bars rather than argued from the config.

    A night-only deployment that let day bars through would feed the model a
    session it was never evaluated on, and the bar counts the forecast stride
    depends on would be wrong too."""
    cfg = config_named(name)
    mine = next(iter(cfg.policy.sessions))
    _frame, _agg, events = _deployment_bars(mixed_tape, cfg.policy)

    stamps = {e.timestamp for e in events}
    # Every bar the *other* deployment would see must be absent from this one.
    other = next(n for n in SINGLE_SESSION if n != name)
    _f2, _a2, other_events = _deployment_bars(mixed_tape,
                                              config_named(other).policy)
    assert stamps.isdisjoint({e.timestamp for e in other_events})
    assert all(cfg.policy.label(e.timestamp) == mine for e in events)


def test_fullday_sees_both_sessions_and_is_the_union_of_the_other_two(mixed_tape):
    """Full-day's invariant is the inverse of the two above, and it is worth
    asserting rather than assuming.

    The single-session tests check that the *other* session was filtered out;
    full-day must filter nothing and must see exactly what DAY and NIGHT see
    between them. This is also the measurement behind STRATEGY_SPLIT_PLAN §7.4:
    full-day is not a third independent strategy, it is a superset session that
    overlaps both others by construction, so three deployments on one account is
    a risk question before it is an engineering one."""
    frame, agg, events = _deployment_bars(mixed_tape,
                                          config_named("fullday").policy)
    assert {e.session for e in events} == {"DAY", "NIGHT"}

    _df, day_agg, day_ev = _deployment_bars(mixed_tape,
                                            config_named("day").policy)
    _nf, night_agg, night_ev = _deployment_bars(mixed_tape,
                                                config_named("night").policy)
    # It still drops the genuinely off-hours bars -- `both()` is not "no filter"
    # -- but strictly fewer than either single-session deployment, because it
    # keeps what each of those throws away as "the other session".
    assert 0 < agg.n_bars_dropped_off_session < min(
        day_agg.n_bars_dropped_off_session,
        night_agg.n_bars_dropped_off_session,
    )
    assert ({e.timestamp for e in events}
            == {e.timestamp for e in day_ev} | {e.timestamp for e in night_ev})
    assert len(events) == len(day_ev) + len(night_ev)


def test_the_two_deployments_partition_the_tape(mixed_tape):
    """Together DAY and NIGHT must account for every in-session bar — neither
    losing bars to a gap between the policies nor double-counting one."""
    day_f, _da, day_ev = _deployment_bars(mixed_tape, config_named("day").policy)
    night_f, _na, night_ev = _deployment_bars(mixed_tape,
                                              config_named("night").policy)

    # `Environment` is the reference for "in session at all".
    everything = list(Environment(mixed_tape).stream())
    assert len(day_ev) + len(night_ev) == len(everything), (
        f"day {len(day_ev)} + night {len(night_ev)} != "
        f"{len(everything)} in-session bars"
    )
    assert len(day_ev) > 0 and len(night_ev) > 0


def test_day_and_night_reach_different_decisions_on_the_same_tape(mixed_tape):
    """Non-vacuity for the whole file. If the two deployments produced the same
    trades, every assertion above would hold for the wrong reason — and W5a's
    finding (night's parameters are wrong for day) would be invisible here."""
    results = {}
    for name in ("day", "night"):
        cfg = config_named(name)
        frame, _agg, _ev = _deployment_bars(mixed_tape, cfg.policy)
        results[name] = _run_backtest_over(
            frame, emitter=cfg.build_emitter(),
            aggression_ticks=cfg.aggression_ticks, max_position=cfg.max_position,
        )
    assert results["day"].n_bars != results["night"].n_bars
    assert (results["day"].portfolio.net_pnl()
            != results["night"].portfolio.net_pnl())


# ---- non-vacuity ------------------------------------------------------------
#
# V0's parity was "verified non-vacuous by two mutation checks" (SHIOAJI_PLAN
# V0). The same discipline applies here, and it matters more: every assertion
# above is an equality, and an equality that cannot fail proves nothing. These
# two break the deployment in the exact ways W7 exists to catch, and assert the
# harness notices.


def test_parity_fails_when_the_session_filter_is_bypassed(mixed_tape):
    """Mutation 1 — the DAY deployment is fed the unfiltered tape.

    This is the failure `TickAggregator`'s policy filter prevents: night bars
    reaching a day deployment. The model would be handed a session it was never
    evaluated on, and the bar counts the forecast stride depends on would be
    wrong. Parity must not survive it."""
    day = config_named("day")
    frame, _agg, day_events = _deployment_bars(mixed_tape, day.policy)
    _f, _a, night_events = _deployment_bars(mixed_tape,
                                            config_named("night").policy)
    unfiltered = sorted(day_events + night_events, key=lambda e: e.timestamp)

    live = _run_live_over(unfiltered, emitter=day.build_emitter(),
                          aggression_ticks=0, max_position=1)
    back = _run_backtest_over(frame, emitter=day.build_emitter(),
                              aggression_ticks=0, max_position=1)
    with pytest.raises(AssertionError):
        _assert_identical(live, back)


def test_parity_fails_when_the_deployments_swap_emitters(mixed_tape):
    """Mutation 2 — night's band on the day deployment, which is W5a's measured
    mistake (Calmar 16.3122 -> 0.2671) expressed as a parity break.

    If this passed, the harness would be blind to the single defect C2 exists to
    prevent: a policy moving without the parameters tuned for it."""
    day, night = config_named("day"), config_named("night")
    frame, _agg, events = _deployment_bars(mixed_tape, day.policy)

    live = _run_live_over(events, emitter=night.build_emitter(),
                          aggression_ticks=0, max_position=1)
    back = _run_backtest_over(frame, emitter=day.build_emitter(),
                              aggression_ticks=0, max_position=1)
    with pytest.raises(AssertionError):
        _assert_identical(live, back)


def test_each_deployment_trades_enough_for_parity_to_mean_something(mixed_tape):
    """Scale, asserted rather than assumed. `_assert_identical` already refuses a
    run with no forecasts or no fills; this pins the order of magnitude so a
    future change that quietly shrinks the fixture is visible."""
    seen = {}
    for name in sorted(CONFIGS):
        cfg = config_named(name)
        frame, agg, events = _deployment_bars(mixed_tape, cfg.policy)
        back = _run_backtest_over(frame, emitter=cfg.build_emitter(),
                                  aggression_ticks=cfg.aggression_ticks,
                                  max_position=cfg.max_position)
        seen[name] = (len(events), agg.n_bars_dropped_off_session,
                      back.n_forecasts, len(back.fills))
        assert back.n_forecasts >= 100, f"{name}: only {back.n_forecasts} forecasts"
        assert len(back.fills) >= 20, f"{name}: only {len(back.fills)} fills"
        # more was dropped than kept for DAY, and a lot was dropped for NIGHT
        assert agg.n_bars_dropped_off_session > 0

    # DAY is the shorter session, so it must see strictly fewer bars.
    assert seen["day"][0] < seen["night"][0]
