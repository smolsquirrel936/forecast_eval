"""FULL-DAY's forecast-figure combination — the stride consensus (iter03/iter05).

Toto2 fires one forecast per stride boundary and stride == horizon (30), so
consecutive forecasts are successive vantage points on overlapping windows.
This module combines Toto2's two headline figures across the last few
boundaries before the banded emitter's gates see them: ``predicted_return``
and ``prob_up`` are each averaged over the last ``RET_K`` / ``PROB_K``
boundaries, and the averaged figures — not the current boundary's — must clear
the band and the probability gate.

Provenance (scratch probes on the full-day tape at the swept band, fee 0.0002,
2026-09-09, replayed off one precompute each). iter03 (two-sided trader): K=1/1
6.139, **K=2/2 9.505**, K=3/3 7.156, K=4/4 3.844; return-only 6.263, prob-only
4.363 — the lift needs BOTH figures averaged jointly. iter04 made the trader
long-only (side split: longs avg +318/trade vs shorts +71, plus the no-flip
freeing effect), which shifted the consensus optimum deeper: K=2/2 10.971,
**K=3/3 12.939** (n 77, net +35050, dd -2709, worse half 3.616 — improved on
both halves), K=4/4 7.089, K=5/5 7.714. The band grid at K=3 keeps 0.00075
(0.0006/0.0007/0.0008 -> 7.05/8.54/9.99).
"""
from collections import deque
from typing import Any, Deque, Optional, Tuple

from ...events import Forecast
from .emitter import Direction, ThresholdEmitter

# Tech: how many recent stride boundaries each figure is averaged over.
# Why:  probed as a family on the full-day tape (2026-09-09). Two-sided
#       trader: K=1/1 is the iter02 baseline (6.139), 2/2 the iter03 winner
#       (9.505), 3/3 7.156, 4/4 3.844. Long-only trader (iter04): 2/2
#       10.971, 3/3 the iter05 winner (12.939), 4/4 7.089, 5/5 7.714 — and the
#       split cells (ret-only 6.263, prob-only 4.363 at two-sided) show the
#       lift needs BOTH figures averaged. Stated as module constants rather
#       than PARAMS keys because `real_data.py`'s resolver requires a
#       package's knob set to equal the CLI's twelve exactly.
RET_K = 3
PROB_K = 3


class ConsensusThresholdEmitter(ThresholdEmitter):
    """The banded emitter over *combined* forecast figures.

    A thin subclass: it rewrites ``predicted_return`` / ``prob_up`` to the mean
    of the last ``ret_k`` / ``prob_k`` boundaries' values and delegates the
    decision to the inherited gates (band, probability, trend filter, path
    momentum) unchanged, so every other payload field reaches the parent as-is.
    """

    def __init__(self, *, ret_k: int = RET_K, prob_k: int = PROB_K, **kw: Any):
        # Tech: validate the two window sizes and store them; build the rolling
        #       history deque at the larger of the two.
        # Why:  ret_k/prob_k below 1 would combine nothing and a zero/negative
        #       window is a caller bug that must fail loudly, not silently act
        #       like the uncombined emitter. One deque serves both figures so
        #       the two windows can never drift out of alignment.
        if ret_k < 1 or prob_k < 1:
            raise ValueError(f"ret_k/prob_k must be >= 1, got {ret_k}/{prob_k}")
        super().__init__(**kw)
        self.ret_k = ret_k
        self.prob_k = prob_k
        self._hist: Deque[Tuple[Optional[float], Optional[float]]] = deque(
            maxlen=max(ret_k, prob_k)
        )

    def _decide(self, forecast: Optional[Forecast]) -> Tuple[Direction, float]:
        # Tech: no forecast / no predicted_return -> HOLD without touching the
        #       history, so a boundary with a missing figure never dilutes the
        #       consensus.
        # Why:  the parent treats both cases as HOLD; the extra guard exists
        #       only to keep the deque clean of uninformative boundaries.
        if forecast is None:
            return "HOLD", 0.0
        payload = forecast.payload if isinstance(forecast.payload, dict) else {}
        r = payload.get("predicted_return")
        if r is None:
            return "HOLD", 0.0

        # Tech: append this boundary's (predicted_return, prob_up), then average
        #       the trailing ret_k returns and the trailing prob_k probabilities
        #       (over what has been seen so far — fewer than k during the first
        #       boundaries after warm-up).
        # Why:  the consensus thesis — a trade needs the *average* forecast to
        #       be strong, not the freshest one. Averaging over what is seen
        #       (rather than HOLDing until k samples exist) matches the probed
        #       behavior exactly and only affects the first boundary or two.
        self._hist.append((r, payload.get("prob_up")))
        recent = list(self._hist)
        rs = [x for x, _ in recent[-self.ret_k:] if x is not None]
        r_c = sum(rs) / len(rs)
        p_c = payload.get("prob_up")
        ps = [p for _, p in recent[-self.prob_k:] if p is not None]
        if ps:
            p_c = sum(ps) / len(ps)

        # Tech: rewrite only the two combined figures in the payload and
        #       delegate to the parent's decision with a Forecast carrying them.
        # Why:  every inherited gate (band, min_prob, strength ramp, trend
        #       filter, path momentum) then reads the consensus figures while
        #       the tape-level fields (last_price, median_path) stay per-
        #       boundary, which is exactly the probed configuration.
        new_payload = dict(payload)
        new_payload["predicted_return"] = r_c
        new_payload["prob_up"] = p_c
        return super()._decide(
            Forecast(
                timestamp=forecast.timestamp,
                horizon_bars=forecast.horizon_bars,
                payload=new_payload,
            )
        )