"""The FULL-DAY package's forecaster construction site.

PER-SESSION SEAM (STRATEGY_SPLIT_PLAN S1, §3.2). This starts as a thin
construction over the shared, frozen ``forecaster/toto2.py`` rather than a copy
of it: the file is 23 KB and most searches move *parameters*, not model
internals. To change the internals, copy the class body in here first — the
shared original is hash-guarded by ``tests/test_strategy_isolation.py``.

This file is what ``results_fullday.tsv``'s ``forecaster_sha`` column hashes, so
a change here is visible in the research log as a forecast-stream change
(STRATEGY_SPLIT_PLAN §2.4). The pre-split ledger ``results.tsv`` carries the
column too, but every row in it reads ``pre-split``.
"""
from typing import Any, Optional


def make_forecaster(
    *,
    warmup_bars: int,
    stride_bars: int,
    horizon_bars: int,
    context_length: int,
    checkpoint: str,
    device: str = "auto",
    bar_freq: Optional[str] = None,
    signal_step: str = "last",
    forecaster: str = "toto2",
) -> Any:
    # Tech: build this session's forecaster from the resolved knobs — the
    #       incumbent Toto2 wrapper, or (branch forecaster_eval_1116) the
    #       package's own Timestral-209M adapter — importing lazily.
    # Why:  the lazy imports keep this package importable without
    #       torch/timestral installed, which is what lets `strategy/wiring.py`
    #       be imported by the isolation test and by `--help` on a machine
    #       with no GPU. `LiveConfig.build_forecaster` defers the same
    #       imports for the same reason. The `forecaster` knob selects the
    #       model (STRATEGY_SPLIT_PLAN D4/D6: package-declared, reachable
    #       via `--param forecaster=timestral`); `checkpoint` must name a
    #       checkpoint of that model.
    if forecaster == "toto2":
        from ...forecaster.toto2 import Toto2Forecaster

        return Toto2Forecaster(
            warmup_bars=warmup_bars,
            forecast_stride_bars=stride_bars,
            forecast_horizon_bars=horizon_bars,
            context_length=context_length,
            checkpoint=checkpoint,
            device=device,
            bar_freq=bar_freq,
            signal_step=signal_step,
        )
    if forecaster == "timestral":
        from .timestral import TimestralForecaster

        return TimestralForecaster(
            warmup_bars=warmup_bars,
            forecast_stride_bars=stride_bars,
            forecast_horizon_bars=horizon_bars,
            context_length=context_length,
            checkpoint=checkpoint,
            device=device,
            signal_step=signal_step,
        )
    raise ValueError(
        f"unknown forecaster {forecaster!r}; this package declares "
        f"'toto2' and 'timestral'"
    )
