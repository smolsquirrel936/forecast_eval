"""HYBRID's router — one engine-facing object per role, two halves behind each.

``run.py:run_backtest`` takes one emitter, one trader factory and one exit rule.
HYBRID has two of each, so the objects here stand in for them and send every call
to the half whose session it is (``environment.classify_session``):

  * ``RoutedEmitter``   — both halves see every forecast; the decision is the
    half's of ``forecast.timestamp``'s session;
  * ``RoutedTrader``    — ``on_signal`` / ``submit_exit`` by ``market.session``;
    ``on_fill`` to **both**, so the halves always agree on the one position;
  * ``RoutedExitRule``  — by ``market.session``; a half with no rule never exits.

The forecaster's router is in ``forecaster.py``, beside the construction site
that ``forecaster_sha`` hashes. This file is frozen once the loop starts
(``autoresearch_trading_plan_hybrid.md``, "The freeze line"): an edit here moves
both halves at once.
"""
from typing import Any, Dict, Literal, Optional

from ...environment import classify_session
from ...events import Forecast, MarketEvent, OrderEvent, Signal
from ...exits.base import ExitRule, PositionState
from ..base import SignalEmitter

Direction = Literal["BUY", "SELL", "HOLD"]


def session_of(ts: Any) -> str:
    # Tech: the TXF session ('DAY' / 'NIGHT') a timestamp falls in; raises for an
    #       off-hours one.
    # Why:  every routing decision reduces to this. The engine drops off-hours
    #       prints before they reach a strategy, so None here means a caller fed
    #       the router something the run loop never would — failing beats silently
    #       sending it to either half.
    sess = classify_session(ts)
    if sess is None:
        raise ValueError(f"{ts} is outside both TXF sessions; cannot route it")
    return sess


class RoutedEmitter(SignalEmitter):
    # Tech: hold one emitter per half; hand EVERY forecast to both, and return
    #       the decision of the half whose session the forecast is in.
    # Why:  a half's emitter keeps state across forecasts (the trend filter's
    #       stored prices). Both halves forecast from the continuous DAY+NIGHT
    #       tape (plan decision 7), so their state follows the same continuous
    #       stream; only the decision is per session. Feeding a half only its own
    #       session's forecasts was tried first and split that memory at every
    #       session change: with identical settings it diverged from a single
    #       emitter on 251 of 78 648 boundaries (Step 0 parity, 2026-10-26).
    def __init__(self, day: SignalEmitter, night: SignalEmitter):
        self.halves: Dict[str, SignalEmitter] = {"DAY": day, "NIGHT": night}

    def _session(self, forecast: Optional[Forecast]) -> str:
        # Tech: the session this forecast's decision belongs to; refuse a missing one.
        # Why:  a None forecast carries no timestamp to route by, and HYBRID always
        #       runs with a forecaster, so None can only be a wiring error.
        if forecast is None:
            raise ValueError("RoutedEmitter needs a forecast to route by")
        return session_of(forecast.timestamp)

    def emit(self, forecast: Optional[Forecast]) -> Direction:
        sess = self._session(forecast)
        out = {s: em.emit(forecast) for s, em in self.halves.items()}
        return out[sess]

    def emit_signal(self, forecast: Optional[Forecast]) -> Signal:
        sess = self._session(forecast)
        out = {s: em.emit_signal(forecast) for s, em in self.halves.items()}
        return out[sess]


class RoutedTrader:
    # Tech: hold one Trader per half over the same Execution; decisions go to the
    #       half of `market.session`, fills go to both.
    # Why:  there is one account position, so both halves must see every fill —
    #       that is what lets a lot opened by one half be managed (and closed) by
    #       the other after a session change. Only decisions are split: the
    #       incoming half owns an inherited lot from its session's first tick.
    #       Both share the run's Execution, so a signal from either half cancels
    #       the one resting order (SPEC §4.4) exactly as a single Trader would.
    def __init__(self, day: Any, night: Any):
        self.halves: Dict[str, Any] = {"DAY": day, "NIGHT": night}

    @property
    def position(self) -> int:
        # Tech: the signed position, read from the day half after checking the
        #       night half agrees.
        # Why:  the run loop reads `trader.position` to book fills; a disagreement
        #       would mean a fill reached one half only, which would make the two
        #       halves trade different books. Loud beats a silent desync.
        day, night = self.halves["DAY"].position, self.halves["NIGHT"].position
        if day != night:
            raise RuntimeError(f"hybrid halves disagree on position: day {day}, night {night}")
        return day

    def on_signal(
        self, direction: Direction, market: MarketEvent, *, strength: float = 1.0
    ) -> Optional[OrderEvent]:
        return self.halves[market.session].on_signal(direction, market, strength=strength)

    def submit_exit(self, market: MarketEvent) -> Optional[OrderEvent]:
        return self.halves[market.session].submit_exit(market)

    def on_fill(self, side: str, quantity: int, **kw: Any) -> None:
        # Tech: fold the fill into both halves' position mirrors.
        # Why:  see the class comment — one position, two mirrors kept in lockstep.
        for half in self.halves.values():
            half.on_fill(side, quantity, **kw)

    def position_state(self) -> Optional[PositionState]:
        # Tech: the open position's snapshot, from the day half.
        # Why:  both halves fold the same fills, so their snapshots are equal;
        #       `position` above is the check that they still are.
        self.position
        return self.halves["DAY"].position_state()


class RoutedExitRule(ExitRule):
    # Tech: hold an optional exit rule per half and query the one for
    #       `market.session`; a half without a rule never fires.
    # Why:  each half owns its risk exit like the rest of its strategy, and an
    #       inherited lot is checked by the incoming half's rule from its first
    #       tick. `adoptable` is True only if every rule present is, because a live
    #       restart adopts the lot under whichever half's session it resumes in.
    def __init__(self, day: Optional[ExitRule], night: Optional[ExitRule]):
        self.halves: Dict[str, Optional[ExitRule]] = {"DAY": day, "NIGHT": night}
        self.adoptable = all(r.adoptable for r in self.halves.values() if r is not None)

    def should_exit(
        self, position: PositionState, market: MarketEvent, *, bar_idx: int
    ) -> bool:
        rule = self.halves[market.session]
        return False if rule is None else rule.should_exit(position, market, bar_idx=bar_idx)
