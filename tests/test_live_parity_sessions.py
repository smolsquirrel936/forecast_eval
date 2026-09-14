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

def _emitter_for(cfg):
    """This deployment's package emitter, banded at the package's own values.

    Deliberately NOT `make_strategy(...)["emitter"]`. These cases are about the
    live loop's session filtering -- they feed a synthetic Toto2-shaped forecast
    and assert the two loops reach identical decisions -- and the packages'
    `make_strategy` stack would bring a real Toto2 forecaster along with it. (It
    was first written while day and night built RSI, whose emitter reads different
    payload keys and would emit nothing: "no fills — parity would be trivially
    true".)

    That live and the backtest reach the *same* factory is pinned by
    `test_live_config.py::test_live_builds_the_package_the_backtest_would_run`;
    this file pins the session behaviour on top of a strategy that reacts.
    """
    from importlib import import_module
    from forecast_eval.strategy import wiring as _wiring
    p = _wiring.params(cfg.strategy_package)
    cls = import_module(
        f"forecast_eval.strategy.{cfg.strategy_package}.emitter").ThresholdEmitter
    return cls(buy_threshold=p["buy_threshold"], sell_threshold=p["sell_threshold"],
               min_prob=p["min_prob"],
               full_confidence_prob=p["full_confidence_prob"])


def _params(cfg):
    """The strategy knobs this deployment resolves to.

    LIVE_STRATEGY_ROUTING_PLAN D2: `LiveConfig` no longer holds a copy of them --
    the deployment's `strategy/<session>/` package does, and live builds from
    there. These assertions kept their meaning; only where they read moved.
    """
    from forecast_eval.strategy import wiring as _wiring
    return _wiring.params(cfg.strategy_package)


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

    trader_kw = dict(aggression_ticks=_params(cfg)["aggression_ticks"],
                     max_position=_params(cfg)["max_position"])
    live = _run_live_over(events, emitter=_emitter_for(cfg), **trader_kw)
    back = _run_backtest_over(frame, emitter=_emitter_for(cfg), **trader_kw)
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
            frame, emitter=_emitter_for(cfg),
            aggression_ticks=_params(cfg)["aggression_ticks"], max_position=_params(cfg)["max_position"],
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

    live = _run_live_over(unfiltered, emitter=_emitter_for(day),
                          aggression_ticks=0, max_position=1)
    back = _run_backtest_over(frame, emitter=_emitter_for(day),
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

    live = _run_live_over(events, emitter=_emitter_for(night),
                          aggression_ticks=0, max_position=1)
    back = _run_backtest_over(frame, emitter=_emitter_for(day),
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
        back = _run_backtest_over(frame, emitter=_emitter_for(cfg),
                                  aggression_ticks=_params(cfg)["aggression_ticks"],
                                  max_position=_params(cfg)["max_position"])
        seen[name] = (len(events), agg.n_bars_dropped_off_session,
                      back.n_forecasts, len(back.fills))
        assert back.n_forecasts >= 100, f"{name}: only {back.n_forecasts} forecasts"
        assert len(back.fills) >= 20, f"{name}: only {len(back.fills)} fills"
        # more was dropped than kept for DAY, and a lot was dropped for NIGHT
        assert agg.n_bars_dropped_off_session > 0

    # DAY is the shorter session, so it must see strictly fewer bars.
    assert seen["day"][0] < seen["night"][0]


# ---- the assertion the old suite structurally could not make ----------------

@pytest.mark.parametrize("session", ["night", "day", "fullday"])
def test_the_backtest_cli_and_live_resolve_the_same_strategy(session):
    """`real_data.py --strategy X` and a live run of X must be the same strategy.

    This is the defect LIVE_STRATEGY_ROUTING_PLAN was written for, stated as a
    test. `live/config.py` used to import `.emitter.ThresholdEmitter` **by name**,
    so a package whose `make_strategy` built anything else was invisible to live —
    `strategy/fullday/combine.py` carried a whole research loop (Calmar 6.14 →
    12.94) while being unreachable from a live run, and night/day have built an
    RSI strategy since `a8ddd85` that live never saw.

    The old parity tests could not catch it: they built **both** sides with
    `cfg.build_emitter()`, comparing the copy against itself. This compares the
    two *entry points* — the backtest CLI's knob resolution against the live
    config's — which is where the two paths actually diverged.
    """
    import argparse

    from forecast_eval.live.config import config_named
    from forecast_eval.real_data import _resolve_strategy_knobs
    from forecast_eval.strategy import wiring

    # what `real_data.py --strategy <session>` resolves
    args = argparse.Namespace(strategy=session, param=[])
    origins = _resolve_strategy_knobs(args)
    backtest_knobs = {knob: getattr(args, knob) for knob in origins}

    # what a live run of the deployment whose policy maps to that package resolves
    cfg = next(config_named(n) for n in ("night", "day", "fullday")
               if config_named(n).strategy_package == session)
    live_knobs = wiring.make_strategy(cfg.strategy_package, warmup_bars=0,
                                      tick_size=cfg.tick_size)["params"]

    assert backtest_knobs == live_knobs, (
        f"{session}: the backtest CLI and a live run disagree about the strategy")


@pytest.mark.parametrize("session", ["night", "day", "fullday"])
def test_live_takes_its_components_from_the_seam_and_nowhere_else(session):
    """The structural pin. Comparing `make_strategy` against `make_strategy`
    would be a tautology — it cannot catch a live path that bypasses the seam,
    which is precisely what the old `build_emitter` did.

    So this asserts the *route*: replace `make_strategy` with a sentinel stack
    and check that is what a `LiveRuntime` ends up holding. Against the old code
    it fails — `build_emitter` imported `.emitter.ThresholdEmitter` by name and
    would have returned a real emitter, not the sentinel.
    """
    from forecast_eval.live import runtime as runtime_mod
    from forecast_eval.live.config import config_named
    from forecast_eval.live.runtime import LiveRuntime
    from forecast_eval.strategy import wiring

    cfg = next(config_named(n) for n in ("night", "day", "fullday")
               if config_named(n).strategy_package == session)

    # Sentinels carry the attributes `LiveRuntime.__init__` reads and nothing
    # else — identity is what is being asserted, not behaviour.
    import types as _types
    sentinel = {
        "forecaster": _types.SimpleNamespace(
            forecast_stride_bars=30, forecast_horizon_bars=30,
            context_length=40, warmup_bars=0, checkpoint="sentinel",
            bar_freq=None, signal_step="last"),
        "emitter": _types.SimpleNamespace(trend_filter=False, path_momentum=False),
        "make_trader": lambda execution: _types.SimpleNamespace(
            position=0, max_position=1),
        "exit_rule": None,
        "forced_close": False,
        "params": {**wiring.params(session), "context_length": 40},
    }
    seen = {}

    def fake_make_strategy(pkg, **kwargs):
        seen["package"], seen["kwargs"] = pkg, kwargs
        return sentinel

    original = runtime_mod.strategy_wiring.make_strategy
    runtime_mod.strategy_wiring.make_strategy = fake_make_strategy
    try:
        rt = LiveRuntime(cfg)
    finally:
        runtime_mod.strategy_wiring.make_strategy = original

    assert seen["package"] == session, "live must ask for its own package"
    assert seen["kwargs"]["warmup_bars"] == 0, (
        "live seeds context via seed_history; a split would delay trading")
    assert rt.emitter is sentinel["emitter"]
    assert rt.forecaster is sentinel["forecaster"]
    assert rt.strategy_params is sentinel["params"]
