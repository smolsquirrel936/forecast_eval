"""HYBRID's router — one engine-facing object per role, two halves behind each.

``run.py:run_backtest`` takes one emitter, one trader factory and one exit rule.
HYBRID has two of each, so the objects here stand in for them and send every call
to the half whose session it is (``environment.classify_session``):

  * ``RoutedEmitter``   — both halves see every forecast; the decision is the
    half's of ``forecast.timestamp``'s session;
  * ``RoutedTrader``    — ``on_signal`` / ``submit_exit`` by ``market.session``;
    ``on_fill`` to **both**, so the halves always agree on the one position;
    a live roll's re-open restores the original entry in both (2026-11-11);
  * ``RoutedExitRule``  — by ``market.session``; a half with no rule never exits.

The forecaster's router is in ``forecaster.py``, beside the construction site
that ``forecaster_sha`` hashes. This file is frozen once the loop starts
(``autoresearch_trading_plan_hybrid_split07.md``, "The freeze line"): an edit here moves
both halves at once.
"""
from contextlib import ExitStack, contextmanager
from typing import Any, Dict, Iterator, Literal, Optional

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
        self._rolling: bool = False         # inside rolling(): a roll leg
        self._roll_reopen: bool = False     # the resting OPEN is a roll's re-open
        self._closed_entry = None           # (side, price, bar_idx, ts) of the last flatten

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

    # Tech: assigning `position` or an `entry_*` field writes it to both halves;
    #       reading an `entry_*` field reads the day half's.
    # Why:  a live `--resume` seeds the trader by assignment
    #       (`LiveRuntime.adopt_position`). The adopted lot belongs to both
    #       halves, like any lot after a fill, because either may manage it next.
    #       Nothing in the backtest assigns these, so it is unchanged.
    @position.setter
    def position(self, value: int) -> None:
        for half in self.halves.values():
            half.position = value

    def _both(name: str) -> property:
        def get(self: "RoutedTrader") -> Any:
            return getattr(self.halves["DAY"], name)

        def set_(self: "RoutedTrader", value: Any) -> None:
            for half in self.halves.values():
                setattr(half, name, value)
        return property(get, set_)

    entry_price = _both("entry_price")
    entry_bar_idx = _both("entry_bar_idx")
    entry_timestamp = _both("entry_timestamp")
    del _both

    @contextmanager
    def rolling(self) -> Iterator[None]:
        # Tech: enter every half's `rolling()` hook (those that define one) for
        #       the duration of a live contract roll's calls.
        # Why:  the runtime looks the hook up on the trader it holds, which is
        #       this router (`live/runtime.py:_trader_roll_leg`). A half that
        #       filters entries must see the roll's re-open marked as one, or a
        #       filter (e.g. a time-of-day open block) refuses the re-open and
        #       the roll leaves the account flat.
        # Tech: also mark the router itself as inside a roll leg.
        # Why:  the roll's re-open is submitted here (on_signal below), and the
        #       router, not a half, carries the entry across the roll (on_fill).
        with ExitStack() as stack:
            for half in self.halves.values():
                hook = getattr(half, "rolling", None)
                if hook is not None:
                    stack.enter_context(hook())
            self._rolling = True
            try:
                yield
            finally:
                self._rolling = False

    def on_signal(
        self, direction: Direction, market: MarketEvent, *, strength: float = 1.0
    ) -> Optional[OrderEvent]:
        # Tech: route the decision; then mark a resting OPEN placed inside
        #       rolling() as the roll's re-open.
        # Why:  every signal cancels the one resting order first (§4.4), so any
        #       earlier mark is void; the re-open's fill arrives later, through
        #       the run loop and outside rolling(), so on_fill needs the mark.
        self._roll_reopen = False
        order = self.halves[market.session].on_signal(direction, market, strength=strength)
        self._roll_reopen = (self._rolling and order is not None
                             and order.intent == "OPEN")
        return order

    def submit_exit(self, market: MarketEvent) -> Optional[OrderEvent]:
        # Tech: an exit on a held position cancels any roll mark.
        # Why:  submit_exit cancels the resting order only when there is a
        #       position to close; when flat it does nothing, so neither do we.
        if self.position != 0:
            self._roll_reopen = False
        return self.halves[market.session].submit_exit(market)

    def on_fill(self, side: str, quantity: int, **kw: Any) -> None:
        # Tech: fold the fill into both halves' position mirrors; remember the
        #       entry a flatten closed, and when a roll's re-open fills on that
        #       side, write the closed entry (price, bar, time) back to both.
        # Why:  see the class comment — one position, two mirrors kept in
        #       lockstep. The carry lives here, not in a half: only the half
        #       that placed the re-open could tell it was a roll's, and carrying
        #       in one mirror would split them. The backtest's spliced tape never
        #       closes a position at a roll, so its stops and trail keep the
        #       original entry; this makes live do the same. Used once.
        prev = self.position
        before = (self.entry_price, self.entry_bar_idx, self.entry_timestamp)
        for half in self.halves.values():
            half.on_fill(side, quantity, **kw)
        new = self.position
        if prev != 0 and new == 0:
            self._closed_entry = (1 if prev > 0 else -1, *before)
        elif prev == 0 and new != 0:
            carried = self._closed_entry
            if (self._roll_reopen and carried is not None
                    and carried[0] == (1 if new > 0 else -1)):
                _side, self.entry_price, self.entry_bar_idx, self.entry_timestamp = carried
            self._roll_reopen = False
            self._closed_entry = None

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
