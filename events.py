"""Event dataclasses (SPEC §3)."""
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

# Tech: string-literal type aliases reused across every event below.
# Why:  centralizing them means the legal vocabulary (sessions, directions,
#       sides, intents) is declared once; a typo like "SEL" is then a type error
#       at every call site instead of a silent string mismatch.
Session = Literal["DAY", "NIGHT"]
Direction = Literal["BUY", "SELL", "HOLD"]
Side = Literal["BUY", "SELL"]
Intent = Literal["OPEN", "CLOSE"]


@dataclass
class MarketEvent:
    # Tech: one trade print replayed by the Environment — when, at what price and
    #       size, and which trading session it fell in.
    # Why:  this is the single unit the whole per-tick loop turns on; carrying the
    #       pre-classified session avoids re-deriving DAY/NIGHT in every consumer.
    timestamp: datetime
    price: float
    volume: int
    session: Session


@dataclass
class Forecast:
    # Tech: a model's output at `timestamp`, predicting `horizon_bars` ahead, with
    #       a model-defined `payload` (e.g. predicted_return / median_path).
    # Why:  payload is intentionally `Any` so the SignalEmitter API stays
    #       model-agnostic — the harness never inspects model internals, it just
    #       hands the Forecast to whichever emitter is configured (SPEC §3). 
    timestamp: datetime
    horizon_bars: int
    payload: Any


@dataclass
class SignalEvent:
    # Tech: a discrete trading intent (BUY/SELL/HOLD) stamped at a time.
    # Why:  the clean boundary between "what the model thinks" (Forecast) and
    #       "what we decide to do" (SignalEvent); keeps strategy logic swappable.
    timestamp: datetime
    direction: Direction


@dataclass
class OrderEvent:
    # Tech: a limit order: side, price, and whether it opens or closes a position.
    # Why:  `intent` (OPEN vs CLOSE) lets downstream code distinguish entries from
    #       exits without re-deriving it from position state — needed for logging
    #       and for the no-flip accounting.
    timestamp: datetime
    side: Side
    limit_price: float
    intent: Intent


@dataclass
class FillEvent:
    # Tech: a realized execution — the price it actually traded at, size, and the
    #       per-side fee charged on this leg.
    # Why:  fills are the only events Portfolio trusts for PnL; fee is stored on
    #       the fill (not recomputed) so booking is exact even if fee_rate changes.
    timestamp: datetime
    side: Side
    fill_price: float
    quantity: int
    fee: float
