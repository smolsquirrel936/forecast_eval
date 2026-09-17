"""The DAY package's forecaster construction site.

PER-SESSION SEAM (STRATEGY_SPLIT_PLAN S1, §3.2). This starts as a thin
construction over the shared, frozen ``forecaster/toto2.py`` rather than a copy
of it: the file is 23 KB and most searches move *parameters*, not model
internals. To change the internals, copy the class body in here first — the
shared original is hash-guarded by ``tests/test_strategy_isolation.py``.

This file is what ``results_day.tsv``'s ``forecaster_sha`` column hashes, so
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
) -> Any:
    # Tech: build the Toto2 forecaster for this session from the resolved knobs,
    #       importing it inside the function rather than at module scope.
    # Why:  the lazy import keeps this package importable without torch installed,
    #       which is what lets `strategy/wiring.py` be imported by the isolation
    #       test and by `--help` on a machine with no GPU. `LiveConfig.
    #       build_forecaster` defers the same import for the same reason.
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
