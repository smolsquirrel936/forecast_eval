"""Pytest config — make `forecast_eval` importable regardless of where
pytest is invoked from (myPaper/, forecast_eval/, or the tests dir itself).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


import pytest


@pytest.fixture
def cheap_strategy(monkeypatch):
    """Stand a cheap, banded strategy in for a package's real one.

    `LiveRuntime` builds its components through `strategy.wiring.make_strategy`
    (LIVE_STRATEGY_ROUTING_PLAN D2), so the seam live-assembly tests inject at is
    that factory — `LiveConfig.build_forecaster` no longer exists.

    Narrow on purpose: a deterministic forecaster and a short context/stride over
    the *real* `ThresholdEmitter` and `Trader`. These tests are about assembly —
    warm-up, policy filtering, priming, staleness, rolls — and need a strategy
    that reacts predictably. The packages' real forecaster is Toto2 (a
    checkpoint load), and each package's own band differs, so the real stack
    is neither cheap nor predictable enough for those cases.

    Opt in with `pytestmark = pytest.mark.usefixtures("cheap_strategy")`.
    """
    from forecast_eval.live import runtime as runtime_mod
    from forecast_eval.strategy import wiring as strategy_wiring
    from forecast_eval.strategy.night.emitter import ThresholdEmitter
    from forecast_eval.strategy.night.trader import Trader
    from forecast_eval.tests.test_live_runtime import CTX, STRIDE, _Cheap

    def make_strategy(session, *, warmup_bars, tick_size=1.0, **overrides):
        # The band is fixed at what `LiveConfig` used to default to for every
        # policy, NOT read from `params(session)`. These cases assert on a
        # forecast that crosses it, and day's package band (0.001) is wider than
        # night's (0.00045) — reading the package would make the same generated
        # frame emit BUY under one policy and HOLD under the other, which is a
        # property of the strategy, not of the assembly under test.
        p = {**strategy_wiring.params(session), **overrides}
        p.update(checkpoint="cheap", context_length=CTX,
                 horizon_bars=5, stride_bars=STRIDE)
        p.setdefault("trend_filter", True)
        for knob, value in (("buy_threshold", 0.00045),
                            ("sell_threshold", -0.00045),
                            ("min_prob", 0.5),
                            ("full_confidence_prob", 0.9)):
            if knob not in overrides:
                p[knob] = value
        return {
            "forecaster": _Cheap(warmup_bars=warmup_bars),
            "emitter": ThresholdEmitter(
                buy_threshold=p["buy_threshold"],
                sell_threshold=p["sell_threshold"],
                min_prob=p["min_prob"],
                full_confidence_prob=p["full_confidence_prob"],
                trend_filter=p["trend_filter"],
            ),
            "make_trader": lambda execution: Trader(
                execution=execution, tick_size=tick_size,
                aggression_ticks=p["aggression_ticks"],
                max_position=p["max_position"],
                max_aggression_ticks=p["max_aggression_ticks"],
            ),
            "exit_rule": None,
            "forced_close": p["forced_close"],
            "params": p,
        }

    monkeypatch.setattr(runtime_mod.strategy_wiring, "make_strategy", make_strategy)
