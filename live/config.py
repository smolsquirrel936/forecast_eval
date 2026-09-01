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

    def warmup_days_default(self, *, emitter_prime_boundaries: int = 500) -> int:
        # Tech: calendar days of history needed to fully prime the emitter for
        #       *this* policy, derived from its bars-per-session rather than a
        #       flat constant.
        # Why:  DAY_SESSION_PLAN C6, MEASURED — a full prime replays
        #       `context_length + forecast_stride_bars * emitter_prime_boundaries`
        #       bars (3008 + 30*500 = 18008 at the pinned settings); below 3
        #       non-HOLD samples `_check_primed` refuses to start. `30` was
        #       night-sized and silent about it: at NIGHT's 840 bars/session
        #       that default already clears the floor, but DAY's 300
        #       bars/session reaches only 109 boundaries at the same 30 days —
        #       expected ~4 samples against a floor of 3, "one quiet fortnight
        #       from refusing to start." Trading days needed = target / bars
        #       per session; calendar days = that times 7/5 for the weekend,
        #       rounded — chosen over ceil/ceil so NIGHT reproduces its
        #       existing default (30) exactly rather than drifting to 31; DAY
        #       comes out to 84, one under the ~85 the hand-measured table
        #       quoted, which the derivation's own tilde already allows for.
        target_bars = self.context_length + (self.forecast_stride_bars
                                             * emitter_prime_boundaries)
        trading_days = target_bars / self.policy.bars_per_session()
        return round(trading_days * 7 / 5)

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


# Tech: the DAY deployment's configuration, pinned to the best `keep` DAY row in
#       `results.tsv` (commit fdd1348, Calmar 4.6805, 73 trades, net +24 471.87
#       points, max drawdown -5 228.43).
# Why:  DAY_SESSION_PLAN C1/C2. The band and the probability gate are the only
#       knobs that differ from NIGHT, and they differ a long way: +-0.001 against
#       +-0.00045, min_prob 0.65 against 0.5. That is not a tuning nicety. NIGHT's
#       band on the day tape trades 257 times for 18.65 gross points each and
#       hands 35.1 % of the gross back in fees; DAY's own band trades 73 times and
#       pays 1.7 %. The measured cost of running this deployment on NIGHT's values
#       is Calmar 16.3122 -> 0.2671 (W5a), which is why `--config` selects the
#       whole value and there is no `--session` flag: the policy must not be able
#       to move without the parameters that were tuned for it.
#
#       Everything else is deliberately NIGHT's: same checkpoint, context, stride
#       and horizon (the search never moved them), same execution shape, and
#       `forced_close=False` for the same reason -- median hold is 4 days here
#       too, so closing at every session end is a different strategy (C8).
#
#       THREE THINGS THIS VALUE IS NOT, all measured, none of them tuning bugs:
#       (1) It does not beat buy-and-hold. Over the evaluated window it nets
#           +24 472 against a passive +33 745 -- it TRAILS by 9 273 points, where
#           NIGHT beats its own floor by +13 259. The Calmar is earned by a small
#           drawdown (-5 228), not by out-earning the index.
#       (2) It is PROVISIONAL. It was measured against NIGHT's Calmar 16.3122
#           while `TAPE_REBUILD_PLAN.md` is still pending, and that plan
#           invalidates 16.3122. Both W5a and W5b re-measure after its T0-T4.
#       (3) It has never traded, not even in simulation -- W8 has not run.
#
#       So this is what a DAY deployment WOULD trade, wired so it can be dry-run
#       and parity-checked. Sending it at a broker is a separate decision with
#       V4's fill gap in front of it (DAY_SESSION_PLAN 7).
PINNED_DAY = LiveConfig(
    source_commit="fdd1348",
    source_calmar=4.6805,
    source_dataset=r"\\DESKTOP-SQUIRRE\dataset\RPT\TXF_RPT_minute_day.csv",
    source_note=(
        "W5b day search; band 0.001 + min-prob 0.65; 73 trades; "
        "net +24471.87 pts; max dd -5228.43; PROVISIONAL (pre tape-rebuild); "
        "trails eval-window buy-and-hold by -9272.64"
    ),
    policy=SessionPolicy.day(),
    checkpoint="Datadog/Toto-2.0-1B",
    context_length=3008,
    forecast_horizon_bars=30,
    forecast_stride_bars=30,
    buy_threshold=0.001,
    sell_threshold=-0.001,
    min_prob=0.65,
)


# Tech: the deployments this entry point can run, by name.
# Why:  C2 — `cli.py` used to be `cfg = PINNED`, so a DAY deployment could not be
#       expressed at all. A bare `--session` flag would have been worse than the
#       gap: it changes the policy while leaving NIGHT's tuned parameters in
#       place, which runs a night strategy on a day tape and reports nothing
#       unusual while doing it. Selecting the whole config keeps the policy
#       travelling inside the value it belongs to, so the two cannot disagree.
CONFIGS = {
    "night": PINNED,
    "day": PINNED_DAY,
}


def config_named(name: str) -> LiveConfig:
    # Tech: look a deployment up by name, raising on an unknown one.
    # Why:  the caller is a CLI flag with `choices=`, so this should be
    #       unreachable from the command line; it exists so programmatic callers
    #       fail loudly rather than silently defaulting to NIGHT — which is the
    #       failure C2 is about, arriving by a different door.
    try:
        return CONFIGS[name]
    except KeyError:
        raise ValueError(
            f"unknown config {name!r}; known deployments: {sorted(CONFIGS)}"
        ) from None
