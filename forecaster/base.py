"""Abstract Forecaster (SPEC §3) + a look-ahead protection helper (SPEC §6).

Contract: `forecast(history_df)` is given history up to and including the
query time t. The implementation MUST NOT consult any data source outside
of `history_df`. The Forecast it returns must carry `timestamp <= t`.
"""
from abc import ABC, abstractmethod
from typing import Any, List, Optional, Sequence, Union

import pandas as pd

from ..events import Forecast


class Forecaster(ABC):
    # Tech: declares three class-level attributes (warm-up, stride, horizon, all in
    #       bars) and one abstract method, forecast(history_df) -> Forecast.
    # Why:  the run loop reads warmup/stride to schedule forecasts and never touches
    #       model internals — this ABC is the entire contract a model must satisfy
    #       to plug into the harness, keeping the engine model-agnostic.
    warmup_bars: int
    forecast_stride_bars: int
    forecast_horizon_bars: int

    @abstractmethod
    def forecast(self, history_df: pd.DataFrame) -> Forecast:
        # Tech: subclasses turn a history-through-t frame into a Forecast.
        # Why:  abstract so the harness depends only on the interface; concrete
        #       implementations (naive, toto2) supply the actual model.
        ...

    def forecast_series_batch(
        self,
        timestamps: Sequence[Any],
        prices: Sequence[float],
        boundaries: Sequence[int],
        *,
        batch_size: Union[int, str] = "auto",
        progress: bool = False,
    ) -> List[Forecast]:
        # Tech: generic fallback — for each boundary b, rebuild the history-through-b
        #       frame and call forecast(); returns one Forecast per boundary in order.
        #       When progress is set, iterate the boundaries under a lazily-imported
        #       tqdm bar.
        # Why:  lets run.precompute_forecasts work with ANY forecaster (e.g. tests'
        #       baselines), so the batched-precompute plumbing is model-agnostic. This
        #       fallback is O(n) per boundary (it has no fixed context window to slice
        #       to), so it offers no speedup; Toto2 overrides it with a real GPU-batched
        #       implementation. `batch_size` is ignored here (nothing to batch). The
        #       `progress` flag matches Toto2's signature so the caller can forward it
        #       uniformly, and the bar gives feedback during this slow sequential path.
        it: Sequence[int] = boundaries
        if progress:
            from tqdm.auto import tqdm
            it = tqdm(boundaries, total=len(boundaries),
                      desc="precompute", unit="fcst")
        out: List[Forecast] = []
        for b in it:
            hist = pd.DataFrame({
                "timestamp": list(timestamps[: b + 1]),
                "price": list(prices[: b + 1]),
                "volume": [1] * (b + 1),
            })
            out.append(self.forecast(hist))
        return out


def assert_no_lookahead(
    forecaster: Forecaster,
    full_history: pd.DataFrame,
    *,
    k: Optional[int] = None,
) -> None:
    """Assert the forecaster's output at time t depends only on rows <= t.

    Strategy: take a prefix of length k from `full_history`, perturb the
    rows AFTER k (replace prices with garbage), and confirm the forecaster's
    output is unchanged when given the perturbed prefix vs. the clean prefix.
    Since we feed the same prefix in both calls, a leak can only manifest if
    the forecaster reads from an external source it shouldn't — in which case
    this test won't catch it; the structural guarantee of passing only the
    prefix is the real defense. This routine catches the simpler bug of an
    implementation that retains a reference to a previously-seen larger frame.
    """
    # Tech: default k to the midpoint and require it strictly inside the frame.
    # Why:  we need rows both before and after k to construct the "shown a larger
    #       frame in between" scenario; k == len would leave nothing after it.
    if k is None:
        k = max(1, len(full_history) // 2)
    if k >= len(full_history):
        raise ValueError("k must be strictly less than len(full_history)")

    # Tech: take a clean length-k prefix and forecast from it (baseline f1).
    # Why:  this is the reference output the same prefix must reproduce later;
    #       copying defends against a forecaster that mutates its input frame.
    prefix = full_history.iloc[:k].reset_index(drop=True).copy()
    f1 = forecaster.forecast(prefix.copy())

    # Now ask again with the same prefix but after the forecaster has been
    # shown a longer frame — checks against impls that cache the last frame
    # by reference instead of recomputing.
    # Tech: feed a longer (k+5) frame, discard its output, then re-forecast the
    #       original prefix to get f2.
    # Why:  a leaky forecaster that caches the largest frame it has ever seen will
    #       now answer the prefix using rows > k — so f2 will differ from f1, which
    #       is exactly the bug this catches.
    larger = full_history.iloc[: k + 5].reset_index(drop=True).copy()
    _ = forecaster.forecast(larger)
    f2 = forecaster.forecast(prefix.copy())

    # Tech: fail if the same prefix produced a different payload/timestamp across
    #       f1 and f2.
    # Why:  determinism on identical input is the observable signature of no leak;
    #       any divergence means past (larger) frames bled into the answer.
    if f1.payload != f2.payload or f1.timestamp != f2.timestamp:
        raise AssertionError(
            "Look-ahead leak: forecaster output for the same prefix changed "
            "after it was shown a larger frame. Outputs:\n"
            f"  before: ts={f1.timestamp} payload={f1.payload}\n"
            f"  after:  ts={f2.timestamp} payload={f2.payload}"
        )
    # Tech: also fail if the forecast timestamp is past the prefix's last row.
    # Why:  a Forecast must be stamped at or before t (the contract); a future
    #       timestamp is a direct, independent symptom of look-ahead.
    if f1.timestamp > prefix["timestamp"].iloc[-1]:
        raise AssertionError(
            f"Forecast.timestamp {f1.timestamp} is past prefix end "
            f"{prefix['timestamp'].iloc[-1]} — look-ahead leak."
        )
