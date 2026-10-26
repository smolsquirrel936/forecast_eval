"""HYBRID's forecaster router — the one Forecaster the engine sees.

Each half builds its own forecaster in ``day_half/forecaster.py`` and
``night_half/forecaster.py``. ``RoutedForecaster`` wraps the two and sends each
forecast request to the half whose session the boundary bar is in. Both halves
read the same continuous DAY+NIGHT history (``autoresearch_trading_plan_hybrid_split07.md``
decision 7); they may differ in checkpoint and context length.

Stride and horizon are **shared**: the engine schedules forecasts from one
``forecast_stride_bars``, and ``forecast_cache.py`` decodes every cached
forecast with one ``forecast_horizon_bars``. A per-half value of either needs a
change here, which the plan leaves to a later, human-approved step (its open items).

This file and the two halves' ``forecaster.py`` are what ``results_hybrid.tsv``'s
``forecaster_sha`` hashes (``strategy/wiring.py:forecaster_sha``).
"""
from typing import Any, Dict, List, Sequence, Union

import pandas as pd

from ...events import Forecast
from ...forecaster.base import Forecaster
from .router import session_of

# Tech: the attributes that decide what a forecast *is*, compared between the
#       halves and exposed (as "day=…;night=…") for the cache key.
# Why:  `forecast_cache.cache_key` reads exactly these off the forecaster it is
#       given. Exposing both halves' values in each means a change to either half
#       moves the key, so a cached stream is never served to a changed half.
_CONFIG_ATTRS = ("checkpoint", "context_length", "bar_freq", "signal_step")


def _config(fc: Any) -> tuple:
    return (type(fc),) + tuple(getattr(fc, a, None) for a in _CONFIG_ATTRS) + (
        getattr(fc, "device", None),)


class RoutedForecaster(Forecaster):
    # Tech: hold one forecaster per half; when the two are configured
    #       identically, route both sessions to the day half's instance.
    # Why:  identical configs give identical forecasts on the same history, so
    #       one instance is one model in VRAM and one batched precompute call —
    #       which is also what makes the hybrid reproduce a single-strategy run's
    #       GPU batches exactly (plan, Step 0 gate 2). The comparison includes the
    #       class and every config attribute, so an edit to either half's
    #       construction site (or a copied class body) ends the sharing.
    def __init__(self, day: Any, night: Any, *, warmup_bars: int,
                 stride_bars: int, horizon_bars: int):
        for half, fc in (("day", day), ("night", night)):
            if fc.forecast_horizon_bars != horizon_bars:
                raise ValueError(f"{half} half's horizon {fc.forecast_horizon_bars} "
                                 f"!= the shared horizon_bars {horizon_bars}")
        self.shared = _config(day) == _config(night)
        self.halves: Dict[str, Any] = {"DAY": day, "NIGHT": day if self.shared else night}
        self.warmup_bars = warmup_bars
        self.forecast_stride_bars = stride_bars
        self.forecast_horizon_bars = horizon_bars
        for a in _CONFIG_ATTRS:
            setattr(self, a, f"day={getattr(day, a, None)};night={getattr(night, a, None)}")

    def _ready(self, sess: str) -> Any:
        # Tech: return the half for `sess`, first lending it the other half's
        #       loaded Toto2 model when both use the same checkpoint and device.
        # Why:  halves that differ only in context length would otherwise load the
        #       same 1B checkpoint twice (~4.2 GiB each live). Toto2Forecaster
        #       loads lazily into `_model` / `_torch` / `_patch_size`; the class is
        #       hash-pinned (test_strategy_isolation), so these names cannot move
        #       under this code. A copied class without them is simply not shared.
        fc = self.halves[sess]
        other = self.halves["NIGHT" if sess == "DAY" else "DAY"]
        if (fc is not other and getattr(fc, "_model", 0) is None
                and getattr(other, "_model", None) is not None
                and fc.checkpoint == other.checkpoint
                and getattr(fc, "device", None) == getattr(other, "device", None)):
            fc._model, fc._torch, fc._patch_size = other._model, other._torch, other._patch_size
        return fc

    def forecast(self, history_df: pd.DataFrame) -> Forecast:
        # Tech: forecast with the half of the last history row's session.
        # Why:  the last row is the boundary bar t — the bar the forecast is
        #       stamped at and the one whose session trades on it.
        return self._ready(session_of(history_df["timestamp"].iloc[-1])).forecast(history_df)

    def forecast_series_batch(
        self,
        timestamps: Sequence[Any],
        prices: Sequence[float],
        boundaries: Sequence[int],
        *,
        batch_size: Union[int, str] = "auto",
        progress: bool = False,
    ) -> List[Forecast]:
        # Tech: with a shared instance, one batched call over every boundary;
        #       otherwise split the boundaries by session, batch each half once,
        #       and return the forecasts in the original boundary order.
        # Why:  `run.precompute_forecasts` and `forecast_cache.load_or_compute`
        #       zip the result back onto `boundaries`, so order is the contract.
        #       The shared path is deliberately the same single call a plain
        #       Toto2 run makes, so its batches — and last bits — are the same.
        if self.shared:
            return self.halves["DAY"].forecast_series_batch(
                timestamps, prices, boundaries, batch_size=batch_size, progress=progress)
        by_sess: Dict[str, List[int]] = {"DAY": [], "NIGHT": []}
        for b in boundaries:
            by_sess[session_of(timestamps[b])].append(b)
        out: Dict[int, Forecast] = {}
        for sess, bs in by_sess.items():
            if bs:
                fcs = self._ready(sess).forecast_series_batch(
                    timestamps, prices, bs, batch_size=batch_size, progress=progress)
                out.update(zip(bs, fcs))
        return [out[b] for b in boundaries]


def make_forecaster(*, day: Any, night: Any, warmup_bars: int,
                    stride_bars: int, horizon_bars: int) -> RoutedForecaster:
    # Tech: wrap the two halves' forecasters in the router.
    # Why:  kept as this file's construction site, like every package's
    #       `make_forecaster`, so the forecaster-stream sha covers the routing too.
    return RoutedForecaster(day, night, warmup_bars=warmup_bars,
                            stride_bars=stride_bars, horizon_bars=horizon_bars)
