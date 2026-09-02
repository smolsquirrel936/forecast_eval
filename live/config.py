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

from .sessions import SessionPolicy


@dataclass(frozen=True)
class LiveConfig:
    # Tech: every knob a live run needs, plus where the values came from.
    # Why:  frozen and complete so a run is described by one value that can be
    #       logged, recorded into the JSONL header, and diffed against the
    #       research log. Provenance fields are part of the config, not a comment,
    #       because a number without a source is exactly how the 313m default
    #       nearly became the live model.
    # Tech: provenance. `source_commit`/`source_calmar` are Optional, and both
    #       being None is the declared meaning of "untuned".
    # Why:  STRATEGY_SPLIT_PLAN §5/D5. FULL-DAY has to be expressible before it
    #       has a `results.tsv` row -- it must be runnable in the backtest and
    #       parity-checkable -- but a config that cannot say it is unprovenanced
    #       would have to borrow DAY's commit and Calmar, which is a lie the
    #       drift guards would then enforce as truth. None is the honest value,
    #       and `is_tuned` below is what the CLI refuses on.
    source_commit: Optional[str]
    source_calmar: Optional[float]
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

    @property
    def is_tuned(self) -> bool:
        # Tech: whether this config carries provenance — a research commit and
        #       the Calmar it was measured at.
        # Why:  the one question the drift guards and the CLI both need to ask.
        #       An untuned config is a legitimate thing to backtest and parity-
        #       check and an illegitimate thing to send at a broker, and that
        #       distinction has to be readable from the value itself rather than
        #       from a name the caller has to remember to special-case.
        return self.source_commit is not None and self.source_calmar is not None

    @property
    def strategy_package(self) -> str:
        # Tech: the `strategy/` package this deployment's components come from,
        #       derived from the policy rather than stored beside it.
        # Why:  STRATEGY_SPLIT_PLAN §2.3. A stored field could disagree with the
        #       policy, and a deployment running one session's policy against
        #       another session's tuned components is exactly the C2 defect that
        #       `--config` exists to make impossible — reintroduced one level
        #       down. Deriving it means the two cannot desync at all.
        sessions = frozenset(self.policy.sessions)
        if sessions == frozenset({"NIGHT"}):
            return "night"
        if sessions == frozenset({"DAY"}):
            return "day"
        if sessions == frozenset({"DAY", "NIGHT"}):
            return "fullday"
        raise ValueError(f"no strategy package for sessions {sorted(sessions)}")

    def build_emitter(self):
        # Tech: construct this deployment's emitter — the class its own strategy
        #       package owns, from the declared fields.
        # Why:  a factory rather than a separately-written construction site, so
        #       the declaration and the object can never disagree. The emitter is
        #       stateful (rolling deques), so this returns a FRESH instance every
        #       call — reusing one across runs silently produces wrong numbers.
        #       Since the split it is the *package's* copy, so a search that
        #       retunes DAY's emitter changes what a DAY live run trades and
        #       nothing else.
        from importlib import import_module
        ThresholdEmitter = import_module(
            f"..strategy.{self.strategy_package}.emitter", __package__
        ).ThresholdEmitter
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
        #
        #       Since the split it routes through the deployment's own package,
        #       which is what makes a per-session forecaster reachable live
        #       (STRATEGY_SPLIT_PLAN D3). The package defers the torch import for
        #       the same reason this method does.
        from importlib import import_module
        make_forecaster = import_module(
            f"..strategy.{self.strategy_package}.forecaster", __package__
        ).make_forecaster
        return make_forecaster(
            warmup_bars=warmup_bars,
            stride_bars=self.forecast_stride_bars,
            horizon_bars=self.forecast_horizon_bars,
            context_length=self.context_length,
            checkpoint=self.checkpoint,
            device=self.device,
            bar_freq=self.bar_freq,
            signal_step=self.signal_step,
        )

    def build_trader(self, *, execution: Any):
        # Tech: construct this deployment's Trader — its own package's copy —
        #       over the execution handle the caller owns.
        # Why:  STRATEGY_SPLIT_PLAN §2.3. The Trader was built inline in
        #       `runtime.py`, which made it the one component a live deployment
        #       could not swap; live and backtest now reach the same per-session
        #       class by the same route. `execution` is a parameter rather than a
        #       field for the reason `warmup_bars` is: it belongs to the run (it
        #       is the broker), not to the strategy. Fresh instance per call, for
        #       the same statefulness reason as `build_emitter`.
        from importlib import import_module
        Trader = import_module(
            f"..strategy.{self.strategy_package}.trader", __package__
        ).Trader
        return Trader(
            execution=execution,
            tick_size=self.tick_size,
            aggression_ticks=self.aggression_ticks,
            max_position=self.max_position,
            max_aggression_ticks=self.max_aggression_ticks,
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
        # Tech: an untuned config renders "UNTUNED" and "calmar=nan" rather than
        #       "None".
        # Why:  `tools/session_artifacts.py` parses this line back out of a
        #       recorded session's `meta` header and calls `float()` on the
        #       calmar group. "nan" survives that; "None" raises. An untuned
        #       config cannot currently reach a recorded live run (the CLI
        #       refuses it), but a parser that crashes on a value this very
        #       module can produce is a landmine, not a safeguard.
        commit = self.source_commit if self.is_tuned else "UNTUNED"
        calmar = self.source_calmar if self.is_tuned else "nan"
        return (
            f"LiveConfig[{commit} calmar={calmar} "
            f"{sorted(self.policy.sessions)}] "
            f"ckpt={self.checkpoint} ctx={self.context_length} "
            f"stride={self.forecast_stride_bars} horizon={self.forecast_horizon_bars} "
            f"band=±{self.buy_threshold} min_prob={self.min_prob} "
            f"trend={self.min_trend_ticks} path={self.path_momentum} "
            f"aggr={self.aggression_ticks} maxpos={self.max_position} "
            f"forced_close={self.forced_close_on_session_end}"
        )


# Tech: the configuration a live run trades, pinned to the best row in
#       `results.tsv` for its session **on the tape vintage it is scored
#       against** (commit 4282d33, Calmar 3.6006, 235 trades, net +24 135.60
#       points, max drawdown -6 703.24, `TXF_RPT_minute_night.csv@2026-09-01`).
# Why:  NIGHT policy because every tuning run used
#       `TXF_RPT_minute_night.csv` — verified from `params.json` and from all 174
#       trades of the latest run being tagged NIGHT (SHIOAJI_PLAN §2.12).
#       `forced_close=False` is kept deliberately: median holding period is 4.5
#       days, so closing at every session boundary would truncate ~89 % of trades
#       and is a different strategy, not a safety setting.
#
#       **RE-PINNED 2026-09-01, TAPE_REBUILD_PLAN T6 (decision D1).** The
#       evaluated tape was rebuilt to splice where the deployment actually rolls,
#       and **not one executable field below changed** — the thresholds, the
#       checkpoint, the cadence and the risk shape are the same values that were
#       pinned as `e44a047`. What changed is the four provenance fields: the same
#       parameters score **16.3122** on the pre-rebuild tape and **3.6006** on the
#       corrected one. D1 chose one vintage over two, so the pin moves forward
#       with the tape.
#
#       **The old pin is history, not a rival.** `e44a047` stays in `results.tsv`
#       at Calmar 13.6742 on `...@2026-05-15`; `tests/test_live_config.py` scopes
#       "the best known row" by vintage precisely so a number that can no longer
#       be reproduced from any file on disk cannot re-pin a live deployment.
#
#       **This does not restart the V5 soak clock**, and that was decided
#       explicitly rather than by default (§6 D1 requires it). The soak's terms
#       are about operational recovery, and re-pinning ordinarily *would* restart
#       them — but only because a re-pin ordinarily changes what the loop
#       executes. Here it changes nothing: `params.json` from the T5 run matches
#       every field of this object, and `source_*` is read by no module outside
#       this one. A live run before and after this commit places identical
#       orders, so there is no behaviour to re-soak. The V4 fill-gap samples
#       accrued so far stay valid for the same reason.
#
#       **What the number does NOT establish:** 3.6006 is the tape correction
#       *and* a fresh forecast-grid phase draw, in unknown proportion — the
#       rebuild inserts bars inside the eval window, and `DAY_SESSION_PLAN.md`
#       §2.2 measured phase alone moving 13.6742 -> 8.7651. See
#       `TAPE_REBUILD_PLAN.md` §5.1 before reading this as a 4.5x decay.
PINNED = LiveConfig(
    source_commit="4282d33",
    source_calmar=3.6006,
    source_dataset=r"\\DESKTOP-SQUIRRE\dataset\RPT\TXF_RPT_minute_night.csv",
    source_note=(
        "TAPE_REBUILD_PLAN T5 re-measure on the rebuilt tape "
        "(@2026-09-01); parameters unchanged from e44a047, tape corrected; "
        "235 trades; net +24135.60 pts; max dd -6703.24"
    ),
    policy=SessionPolicy.night(),
    checkpoint="Datadog/Toto-2.0-1B",
    context_length=3008,
    forecast_horizon_bars=30,
    forecast_stride_bars=30,
)


# Tech: the DAY deployment's configuration, pinned to the best `keep` DAY row in
#       `results.tsv` (commit 3c3ae38, Calmar 1.6937, 67 trades, net +9 748.01
#       points, max drawdown -5 755.50, on `TXF_RPT_minute_day.csv@2026-09-01`).
# Why:  DAY_SESSION_PLAN C1/C2. The band and the probability gate are the only
#       knobs that differ from NIGHT, and they differ a long way: +-0.001 against
#       +-0.00045, min_prob 0.65 against 0.5. That is not a tuning nicety. NIGHT's
#       band on the day tape trades 230 times and scores 0.4129 against this
#       cell's 1.6937 (W5a, re-measured), which is why `--config` selects the
#       whole value and there is no `--session` flag: the policy must not be able
#       to move without the parameters that were tuned for it.
#
#       Everything else is deliberately NIGHT's: same checkpoint, context, stride
#       and horizon (the search never moved them), same execution shape, and
#       `forced_close=False`.
#
#       **The reason given for that last one was wrong, MEASURED 2026-09-02.**
#       It read "median hold is 4 days here too, so closing at every session end
#       is a different strategy". It is not 4 days: over this cell's 67 trades
#       the median hold is **20 days**, p90 57 days, max 173, and **100 %** span
#       more than one calendar day against NIGHT's 90 % at 4 d 00 h. So the
#       conclusion may still hold -- forced-closing a 20-day position at 13:45
#       every day is certainly a different strategy -- but it no longer rests on
#       the two deployments being alike, because they are not. A DAY position
#       rides ~20 calendar days of night sessions and weekends unmanaged, which
#       is DAY_SESSION_PLAN C8 and is still open.
#
#       **Re-measured, not re-selected (TAPE_REBUILD_PLAN T7, 2026-09-01.)** The
#       parameters did not move; the tape under them did. On the rebuilt day tape
#       this cell scores **1.6937** where the retired `@2026-08-21` vintage said
#       4.6805, and its worst-half score -- the number W5b actually selected on --
#       fell 3.1557 -> 0.9967. The `0.00125/0.65` cell is now the better one
#       (worst 1.1664), and the pin was deliberately NOT moved to it: that would
#       be a parameter change dressed as a re-measurement, and D1 chose the
#       opposite discipline for NIGHT. The old row survives in `results.tsv` as
#       history; this pin is the forward policy.
#
#       THREE THINGS THIS VALUE IS NOT, all measured, none of them tuning bugs:
#       (1) It does not beat buy-and-hold, and the gap got worse. Over the
#           evaluated window it nets +9 748 against a passive +34 167 -- it
#           TRAILS by 24 419, where the pre-rebuild shortfall was 9 273. NIGHT no
#           longer beats its own floor either (-9 205, was +13 259). The Calmar is
#           earned by a small drawdown (-5 755), not by out-earning the index.
#       (2) It carries the phase confound, not just a tape correction. The
#           rebuild inserts bars inside the eval window, so the forecast grid
#           necessarily re-phased; `DAY_SESSION_PLAN.md` 2.2 measured phase ALONE
#           moving 13.6742 -> 8.7651 on NIGHT. Read 4.6805 -> 1.6937 with
#           `TAPE_REBUILD_PLAN.md` 5.1 before calling it a 2.8x decay.
#       (3) It has never traded, not even in simulation -- W8 has not run.
#
#       So this is what a DAY deployment WOULD trade, wired so it can be dry-run
#       and parity-checked. Sending it at a broker is a separate decision with
#       V4's fill gap in front of it (DAY_SESSION_PLAN 7).
PINNED_DAY = LiveConfig(
    source_commit="3c3ae38",
    source_calmar=1.6937,
    source_dataset=r"\\DESKTOP-SQUIRRE\dataset\RPT\TXF_RPT_minute_day.csv",
    source_note=(
        "TAPE_REBUILD_PLAN T7 re-measure of the W5b day search on the rebuilt "
        "tape (@2026-09-01); band 0.001 + min-prob 0.65 unchanged, tape "
        "corrected; 67 trades; net +9748.01 pts; max dd -5755.50; has never "
        "traded (W8 unrun); trails eval-window buy-and-hold by -24418.57"
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
# Tech: the FULL-DAY deployment. It carries no `source_commit` and no
#       `source_calmar`, which is what `is_tuned` reads as "unprovenanced", and
#       its band and probability gate are DAY's — inherited, not measured.
# Why:  STRATEGY_SPLIT_PLAN §5 / D5. Full-day needs to exist as a value before it
#       has a tuned row: the backtest runs it today (`--strategy fullday`), the
#       parity machinery can check it, and `strategy/fullday/` has somewhere to
#       point. What it must NOT do is trade, and the honest way to arrange that
#       is a config that says it is untuned and an entry point that refuses it
#       (`live/cli.py`), rather than plausible-looking numbers borrowed from
#       another session.
#
#       WHY DAY'S NUMBERS AND NOT NIGHT'S: full-day trades DAY, so it rolls at
#       12:45 like DAY (`roll_at_time`, extended 2026-09-01; DAY_SESSION_PLAN §4
#       C4 has a FULL-DAY row). Inheriting the band from the deployment it shares
#       a roll instant with is the least arbitrary choice available — but it is
#       still arbitrary, and it is not evidence. W5a measured what transferring a
#       band across sessions is worth: NIGHT's band on the day tape scores 0.4129
#       against 3.6006. There is no reason to expect DAY's band to transfer to
#       full-day any better.
#
#       THE TAPE TO TUNE ON is `TXF_RPT_minute_fullday.csv` (2 364 417 bars,
#       2018-01-02 -> 2026-08-21), NOT the combined `TXF_RPT_minute.csv`, whose
#       first 15.6 % is day-only because TAIFEX had no night session before
#       2017-05-15 (TAPE_REBUILD_PLAN §2.5). Running `tools/band_sweep.py` there
#       is the work that replaces this stub.
#
#       ALSO UNANSWERED, and bigger than the band: full-day is not a third
#       independent strategy, it is a SUPERSET session that overlaps both others
#       by construction. Running it beside DAY and NIGHT on one account is
#       STRATEGY_SPLIT_PLAN §7.1 — the reconciliation rail halts every deployment
#       the moment two of them hold at once — and §7.4, which is a risk decision
#       nobody has made.
PINNED_FULLDAY = LiveConfig(
    source_commit=None,
    source_calmar=None,
    source_dataset=r"\\DESKTOP-SQUIRRE\dataset\RPT\TXF_RPT_minute_fullday.csv",
    source_note=(
        "UNTUNED (STRATEGY_SPLIT_PLAN D5, 2026-09-02). No results.tsv row "
        "exists for the FULL-DAY session and this configuration has never "
        "traded, in simulation or otherwise. Band 0.001 / min_prob 0.65 are "
        "DAY's, inherited because full-day shares DAY's 12:45 roll instant -- "
        "they were never measured on the full-day tape. live/cli.py refuses "
        "--config fullday until a tuned row exists; the backtest runs it via "
        "`real_data.py --strategy fullday`."
    ),
    policy=SessionPolicy.both(),
    checkpoint="Datadog/Toto-2.0-1B",
    context_length=3008,
    forecast_horizon_bars=30,
    forecast_stride_bars=30,
    buy_threshold=0.001,
    sell_threshold=-0.001,
    min_prob=0.65,
)


CONFIGS = {
    "night": PINNED,
    "day": PINNED_DAY,
    "fullday": PINNED_FULLDAY,
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
