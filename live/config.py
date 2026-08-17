"""LiveConfig — the one declared, provenanced configuration a live run trades.

Before this module the tuned configuration existed only as a **command string in
`results.tsv`** and a scatter of `params.json` files. Nothing in importable code
held it, and three separate defaults contradicted it:

| knob | `Toto2Forecaster` default | `real_data.py` CLI default | tuned value |
|---|---|---|---|
| checkpoint | `Toto-2.0-2.5B` | `Toto-2.0-313m` | **`Toto-2.0-1B`** |
| buy / sell threshold | — | ±0.0005 | **±0.00045** |
| `min_prob` | `None` | `None` | **0.5** |

`strategy/wiring.py` disagrees on all three as well; it is the *autoresearch*
seam, whose entrypoint no longer exists, and the winning run was produced through
`real_data.py` CLI flags instead. So a live run wired from any of those sources
would have traded an untuned configuration — silently, since none of the
mismatches raises.

`PINNED` below is that configuration, and `tests/test_live_config.py` asserts it
against the `results.tsv` row it claims to come from. If either drifts, the test
fails — which is the point.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from ..strategy.threshold import ThresholdEmitter
from .sessions import SessionPolicy


@dataclass(frozen=True)
class LiveConfig:
    # Tech: every knob a live run needs, plus where the values came from.
    # Why:  frozen and complete so a run is described by one value that can be
    #       logged, recorded into the JSONL header, and diffed against the
    #       research log. Provenance fields are part of the config, not a comment,
    #       because a number without a source is exactly how the 313m default
    #       nearly became the live model.
    source_commit: str
    source_calmar: float
    source_dataset: str
    source_note: str

    policy: SessionPolicy

    # --- forecaster ---
    checkpoint: str
    context_length: int
    forecast_horizon_bars: int
    forecast_stride_bars: int
    device: str = "auto"
    bar_freq: Optional[str] = None
    signal_step: str = "last"

    # --- emitter ---
    buy_threshold: float = 0.00045
    sell_threshold: float = -0.00045
    min_prob: Optional[float] = 0.5
    full_confidence_prob: float = 0.9
    short_window: int = 3
    min_trend_ticks: float = 3.0
    path_momentum: bool = True
    trend_filter: bool = True

    # --- trader / execution ---
    tick_size: float = 1.0
    aggression_ticks: int = 0
    max_position: int = 1
    max_aggression_ticks: Optional[int] = None
    fee_rate: float = 0.00015
    contract_multiplier: float = 200.0
    forced_close_on_session_end: bool = False

    def build_emitter(self) -> ThresholdEmitter:
        # Tech: construct the emitter from the declared fields.
        # Why:  a factory rather than a separately-written construction site, so
        #       the declaration and the object can never disagree. The emitter is
        #       stateful (rolling deques), so this returns a FRESH instance every
        #       call — reusing one across runs silently produces wrong numbers.
        return ThresholdEmitter(
            buy_threshold=self.buy_threshold,
            sell_threshold=self.sell_threshold,
            min_prob=self.min_prob,
            full_confidence_prob=self.full_confidence_prob,
            short_window=self.short_window,
            min_trend_ticks=self.min_trend_ticks,
            path_momentum=self.path_momentum,
            trend_filter=self.trend_filter,
        )

    def build_forecaster(self, *, warmup_bars: int) -> Any:
        # Tech: construct the Toto2 forecaster; `warmup_bars` is passed in rather
        #       than stored on the config.
        # Why:  in the backtest `warmup_bars` is the *train/eval split* — a
        #       property of that experiment, not of the strategy. Live, warm-up
        #       comes from the `kbars` backfill instead, so how many bars must
        #       elapse before the first live forecast is a decision for the entry
        #       point (V3), not something to pin from a research run. Imported
        #       here so this module stays importable without torch installed.
        from ..forecaster.toto2 import Toto2Forecaster
        return Toto2Forecaster(
            warmup_bars=warmup_bars,
            forecast_stride_bars=self.forecast_stride_bars,
            forecast_horizon_bars=self.forecast_horizon_bars,
            context_length=self.context_length,
            checkpoint=self.checkpoint,
            device=self.device,
            bar_freq=self.bar_freq,
            signal_step=self.signal_step,
        )

    def describe(self) -> str:
        # Tech: one-line human summary, provenance first.
        # Why:  goes on the console banner and into the Recorder's `meta` header,
        #       so any artifact can be traced back to the research row that
        #       justified it without opening the code.
        return (
            f"LiveConfig[{self.source_commit} calmar={self.source_calmar} "
            f"{sorted(self.policy.sessions)}] "
            f"ckpt={self.checkpoint} ctx={self.context_length} "
            f"stride={self.forecast_stride_bars} horizon={self.forecast_horizon_bars} "
            f"band=±{self.buy_threshold} min_prob={self.min_prob} "
            f"trend={self.min_trend_ticks} path={self.path_momentum} "
            f"aggr={self.aggression_ticks} maxpos={self.max_position} "
            f"forced_close={self.forced_close_on_session_end}"
        )


# Tech: the configuration a live run trades, pinned to the best row in
#       `results.tsv` (commit e44a047, Calmar 13.6742, 185 trades, net +39 075.63
#       points, max drawdown -2 856.95).
# Why:  NIGHT policy because every tuning run used
#       `TXF_RPT_minute_night.csv` — verified from `params.json` and from all 174
#       trades of the latest run being tagged NIGHT (SHIOAJI_PLAN §2.12).
#       `forced_close=False` is kept deliberately: median holding period is 4.5
#       days, so closing at every session boundary would truncate ~89 % of trades
#       and is a different strategy, not a safety setting.
PINNED = LiveConfig(
    source_commit="e44a047",
    source_calmar=13.6742,
    source_dataset=r"\\DESKTOP-SQUIRRE\dataset\RPT\TXF_RPT_minute_night.csv",
    source_note=(
        "+median_path mid-horizon momentum gate (path_momentum=True); "
        "185 trades; net +39075.63 pts; max dd -2856.95"
    ),
    policy=SessionPolicy.night(),
    checkpoint="Datadog/Toto-2.0-1B",
    context_length=3008,
    forecast_horizon_bars=30,
    forecast_stride_bars=30,
)
