"""LiveConfig — the one declared, provenanced configuration a live run trades.

Before this module the tuned configuration existed only as a **command string in
`results.tsv`** and a scatter of `params.json` files. Nothing in importable code
held it, and three separate defaults contradicted it:

| knob | `Toto2Forecaster` default | `real_data.py` CLI default | tuned value |
|---|---|---|---|
| checkpoint | `Toto-2.0-2.5B` | `Toto-2.0-313m` | **`Toto-2.0-1B`** |
| buy / sell threshold | — | ±0.0005 | **±0.00045** |
| `min_prob` | `None` | `None` | **0.5** |

`strategy/wiring.py` disagreed on all three as well; it was the *autoresearch*
seam, whose entrypoint no longer existed, and the winning run was produced through
`real_data.py` CLI flags instead. So a live run wired from any of those sources
would have traded an untuned configuration — silently, since none of the
mismatches raises.

**That is the history, and it is why this module no longer holds any strategy
values (LIVE_STRATEGY_ROUTING_PLAN D2, 2026-09-09).** The table above is a list of
declarations that could contradict each other; the fix was not to add a fourth
but to delete this one. `strategy/<session>/wiring.py:PARAMS` is now the single
source, `strategy.wiring.make_strategy` the single construction site, and
[live/runtime.py] builds from it — so what a live run trades IS what
`real_data.py --strategy <session>` backtests.

The copy that was here caused the failure twice: the 2026-08-11 policy/band
defect, and then `build_emitter` importing `.emitter.ThresholdEmitter` *by name*,
which made `strategy/fullday/combine.py` unreachable from live for a whole
research loop while `build_trader` *did* route through the package.

What remains is a deployment's identity: which sessions it trades, where its
provenance came from, and the run-level values that are not strategy
(`tick_size`, `fee_rate`, `contract_multiplier`). `tests/test_live_config.py`
still asserts each pin against the `results.tsv` row it claims — provenance can
drift; the strategy no longer can.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .sessions import SessionPolicy


# Tech: the packages that trade each session set, and the halves of a package
#       that puts one strategy per session behind a router.
# Why:  `fullday` and `hybrid` both trade DAY+NIGHT, so the sessions alone no
#       longer name the package; a deployment that names one is checked against
#       this table instead. A routed package has no single emitter, band or
#       checkpoint, so `describe()` renders it half by half.
PACKAGE_SESSIONS = {
    "night": frozenset({"NIGHT"}),
    "day": frozenset({"DAY"}),
    "fullday": frozenset({"DAY", "NIGHT"}),
    "hybrid": frozenset({"DAY", "NIGHT"}),
}
HALVES = {"hybrid": ("day", "night")}


def _emitter_signature_defaults(package: str) -> dict:
    # Tech: the declared defaults of the two emitter knobs `PARAMS` omits, read
    #       from the package's own emitter class signature.
    # Why:  `strategy/<session>/wiring.py` states that `short_window`,
    #       `min_trend_ticks`, `path_momentum` and `trend_filter` are left to the
    #       emitter's class defaults so recorded runs reproduce exactly -- so the
    #       class *is* where those values live, and `describe()` has nowhere else
    #       truthful to read them from. Constructing an emitter would be the
    #       alternative, but only `make_strategy` builds one and it builds the
    #       forecaster too, which is far too much work for a log line.
    import inspect
    from importlib import import_module
    # Tech: a half is addressed as "<package>.<half>_half" (hybrid's layout).
    cls = import_module(f"..strategy.{package}.emitter", __package__).ThresholdEmitter
    sig = inspect.signature(cls.__init__).parameters
    return {name: sig[name].default
            for name in ("short_window", "min_trend_ticks",
                         "path_momentum", "trend_filter")}


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
    # Tech: the `strategy/` package, when the sessions do not name it; None
    #       derives it (`strategy_package`).
    # Why:  hybrid trades DAY+NIGHT like fullday. `__post_init__` checks a named
    #       package against `PACKAGE_SESSIONS`, so the policy still cannot drift
    #       from the components.
    package: Optional[str] = None

    # Tech: no strategy fields. The band, the probability gate, the checkpoint,
    #       the cadence, the filter knobs and the trader's risk shape all live in
    #       `strategy/<session>/wiring.py:PARAMS` and are read from there.
    # Why:  LIVE_STRATEGY_ROUTING_PLAN D2. This dataclass used to re-declare ~19
    #       values the package already owned, and a copy that can disagree
    #       eventually does: it produced the 2026-08-11 policy/band defect this
    #       module's docstring describes, and then `build_emitter` hard-coding
    #       `.emitter.ThresholdEmitter` made `strategy/fullday/combine.py`
    #       unreachable from live while the package's Trader *was* picked up --
    #       a live run that was one package's emitter on another's trader. The
    #       copy is gone, so neither is expressible.

    # --- deployment, not strategy ---
    tick_size: float = 1.0            # the contract's tick grid
    fee_rate: float = 0.0002          # what the broker charges, not what to trade
    contract_multiplier: float = 200.0  # TXF point value

    def __post_init__(self) -> None:
        # Tech: refuse a named package whose sessions differ from the policy's.
        # Why:  the C2 defect `strategy_package` exists to prevent -- one
        #       session's policy on another's components -- must stay impossible
        #       when the package is named rather than derived.
        if self.package is not None:
            want = PACKAGE_SESSIONS.get(self.package)
            if want != frozenset(self.policy.sessions):
                raise ValueError(
                    f"package {self.package!r} trades {sorted(want or [])}, "
                    f"not the policy's {sorted(self.policy.sessions)}")

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
        #       down. Deriving it means the two cannot desync at all; a named
        #       `package` is checked against the policy in `__post_init__`.
        if self.package is not None:
            return self.package
        sessions = frozenset(self.policy.sessions)
        if sessions == frozenset({"NIGHT"}):
            return "night"
        if sessions == frozenset({"DAY"}):
            return "day"
        if sessions == frozenset({"DAY", "NIGHT"}):
            return "fullday"
        raise ValueError(f"no strategy package for sessions {sorted(sessions)}")

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
        # Tech: the cadence comes from this deployment's strategy package, not
        #       from a field on this config.
        # Why:  D2 -- `context_length` and `stride_bars` are strategy values, and
        #       a search that changes either changes how much history a live run
        #       must replay before it can trade. Reading them here means that
        #       stays true automatically instead of needing a second edit.
        from ..strategy import wiring as strategy_wiring
        params = strategy_wiring.params(self.strategy_package)
        target_bars = params["context_length"] + (params["stride_bars"]
                                                  * emitter_prime_boundaries)
        trading_days = target_bars / self.policy.bars_per_session()
        return round(trading_days * 7 / 5)

    def describe(self, params: Optional[dict] = None) -> str:
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
        # Tech: every strategy value on this line is read from the deployment's
        #       package -- `PARAMS` for the declared knobs, and the emitter
        #       class's own signature defaults for `min_trend_ticks` and
        #       `path_momentum`, which `PARAMS` deliberately does not declare.
        # Why:  D2. The format is unchanged on purpose: `tools/session_artifacts.py`
        #       parses this exact line back out of a recorded `meta` header with
        #       `_DESCRIBE_RE`, so only the *source* of the values moves. Reading
        #       the two undeclared knobs from the signature rather than printing
        #       3.0/True as literals keeps the line truthful if a search edits the
        #       package's `emitter.py` -- which is precisely what that file is for.
        # Tech: render the RUN's resolved knobs when the caller has them; fall
        #       back to the package's declared ones when it does not.
        # Why:  LIVE_STRATEGY_ROUTING_PLAN Phase 5. Reading the package
        #       unconditionally made this line lie under `--param`: it printed the
        #       declared band while the run traded the typed one, on the banner
        #       AND in the recorder's `meta` header. The caller that knows is the
        #       one that resolved the overrides. The fallback keeps every existing
        #       call site working, and is right for them -- they describe a
        #       deployment, not a run.
        from ..strategy import wiring as strategy_wiring
        p = params if params is not None else strategy_wiring.params(
            self.strategy_package)
        # Tech: a routed package renders its shared knobs once and each half's
        #       band, gate and filter in a `day[...]` / `night[...]` group.
        # Why:  it has no single band or checkpoint (its unprefixed knobs are
        #       None), and its halves live in `<half>_half/`. The line then does
        #       not match `tools/session_artifacts.py:_DESCRIBE_RE`, whose rebuild
        #       returns None for it -- the run's own `params.json` is the record.
        halves = HALVES.get(self.strategy_package)
        if halves:
            parts = []
            for h in halves:
                d = _emitter_signature_defaults(f"{self.strategy_package}.{h}_half")
                parts.append(
                    f"{h}[ckpt={p[f'{h}_checkpoint']} ctx={p[f'{h}_context_length']} "
                    f"band=±{p[f'{h}_buy_threshold']} min_prob={p[f'{h}_min_prob']} "
                    f"trend={d['min_trend_ticks']} path={d['path_momentum']} "
                    f"aggr={p[f'{h}_aggression_ticks']}]")
            return (
                f"LiveConfig[{commit} calmar={calmar} "
                f"{sorted(self.policy.sessions)}] {self.strategy_package} "
                f"ctx={p['context_length']} stride={p['stride_bars']} "
                f"horizon={p['horizon_bars']} {' '.join(parts)} "
                f"maxpos={p['max_position']} forced_close={p['forced_close']}"
            )
        emitter_defaults = _emitter_signature_defaults(self.strategy_package)
        return (
            f"LiveConfig[{commit} calmar={calmar} "
            f"{sorted(self.policy.sessions)}] "
            f"ckpt={p['checkpoint']} ctx={p['context_length']} "
            f"stride={p['stride_bars']} horizon={p['horizon_bars']} "
            f"band=±{p['buy_threshold']} min_prob={p['min_prob']} "
            f"trend={emitter_defaults['min_trend_ticks']} "
            f"path={emitter_defaults['path_momentum']} "
            f"aggr={p['aggression_ticks']} maxpos={p['max_position']} "
            f"forced_close={p['forced_close']}"
        )


# Tech: SIMTRADE_0929 — the NIGHT deployment's provenance is cleared
#       (`source_commit`/`source_calmar` None), because its package now trades
#       `night_iter30` (`4cbc1e6`, branch `autoresearch_night_1004`, whole-data
#       Calmar 7.2974, 376 trades), a row that lives only in that branch's
#       `results_night.tsv`. It replaced `night_iter10` (`6159a4c`, Calmar
#       6.5793) on 2026-10-05.
# Why:  SIMTRADE_0929_PLAN step 5. `results.tsv` has no row for these settings,
#       so the 4282d33 pin below would name a strategy this deployment no longer
#       trades. None is the honest value (the same one FULL-DAY used); the source
#       is carried in `source_note`. The history below describes the old pin.
#
# Tech: the configuration a live run traded until SIMTRADE_0929, pinned to the
#       best row in `results.tsv` for its session **on the tape vintage it is
#       scored against** (commit 4282d33, Calmar 3.6006, 235 trades, net
#       +24 135.60 points, max drawdown -6 703.24,
#       `TXF_RPT_minute_night.csv@2026-09-01`).
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
    source_commit=None,
    source_calmar=None,
    source_dataset=r"\\DESKTOP-SQUIRRE\dataset\RPT\TXF_RPT_minute_night.csv",
    source_note=(
        "SIMTRADE_0929: night_iter30 (4cbc1e6, autoresearch_night_1004); "
        "Calmar 7.2974 on the WHOLE eval window (no held-out tail), 376 "
        "trades; recorded in that branch's results_night.tsv only, not "
        "results.tsv. band +-0.0004, min_prob 0.6, min_trend_ticks 10, "
        "stride 15, two-sided"
    ),
    policy=SessionPolicy.night(),
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
)


# Tech: the deployments this entry point can run, by name.
# Why:  C2 — `cli.py` used to be `cfg = PINNED`, so a DAY deployment could not be
#       expressed at all. A bare `--session` flag would have been worse than the
#       gap: it changes the policy while leaving NIGHT's tuned parameters in
#       place, which runs a night strategy on a day tape and reports nothing
#       unusual while doing it. Selecting the whole config keeps the policy
#       travelling inside the value it belongs to, so the two cannot disagree.
# Tech: the FULL-DAY deployment. It carries no `source_commit` and no
#       `source_calmar`, which `is_tuned` reads as "no results.tsv row"; its
#       settings are `fullday_split_iter07` (`394ce85`, branch
#       `autoresearch_split_0.7_fullday`): band +-0.001, min_prob 0.50, stride 15.
# Why:  SIMTRADE_0929_PLAN step 5. Until then this was a stub trading DAY's band
#       and gate, "inherited, not measured" (STRATEGY_SPLIT_PLAN D5). The split
#       loop has since measured these values on `TXF_RPT_minute_fullday.csv` --
#       but only on the TRAIN 70 % of the eval window (Calmar 2.8213, 134
#       trades), recorded in `results_fullday.tsv`, which `results.tsv`-scoped
#       drift guards do not read. None stays the honest value; the source lives
#       in `source_note`. The CLI no longer refuses an unprovenanced config
#       (`live/cli.py`, LIVE_STRATEGY_ROUTING_PLAN D1).
#
#       STILL TRUE: full-day is a SUPERSET session that overlaps DAY and NIGHT by
#       construction. On one account the three run under the owning process
#       (`live/owner_cli.py`, STRATEGY_SPLIT_PLAN §18), and the exposure question
#       (§7.4) is a risk decision, not an engineering one.
PINNED_FULLDAY = LiveConfig(
    source_commit=None,
    source_calmar=None,
    source_dataset=r"\\DESKTOP-SQUIRRE\dataset\RPT\TXF_RPT_minute_fullday.csv",
    source_note=(
        "SIMTRADE_0929 since 2026-10-27: fullday_rev_iter34 "
        "(autoresearch_fullday_reversal_1026, via candidate_fullday_iter34); "
        "train-70% Calmar 4.4463, 181 trades; held-out approx Calmar 4.06 but "
        "read after selection (optimistic). stride 10, band 0.001, min_prob "
        "0.50, entry_min_prob 0.60, stop 0.05, lock_after_pct 0.03, "
        "reentry_cooldown_signals 16. Replaced fullday_split_iter07 + stop 0.05"
    ),
    policy=SessionPolicy.both(),
)


# Tech: the HYBRID deployment -- the DAY half trades the DAY session and the
#       NIGHT half the NIGHT session, one position between them. It names its
#       package and its file slug, because fullday also trades DAY+NIGHT.
# Why:  autoresearch_trading_plan_hybrid_split07.md "Going live" step 1. Never
#       slug `day_night`: that is fullday's ledger, bar cache and wire. On
#       `main` the package is Step 0 (the reset day and night settings,
#       `eaaad1d`), untuned; a live branch that adopts a loop result restates
#       `source_note`, as it does for the others.
PINNED_HYBRID = LiveConfig(
    source_commit=None,
    source_calmar=None,
    source_dataset=r"\\DESKTOP-SQUIRRE\dataset\RPT\TXF_RPT_minute_fullday.csv",
    source_note=(
        "SIMTRADE_0929 since 2026-10-27: hybrid_split_iter22 "
        "(autoresearch_split_0.7_hybrid, 9c0741f; code placed by 58eb8b5); "
        "train-70% Calmar 12.8738, 86 trades; held-out approx Calmar 6.9773 "
        "vs baseline 5.7634 (day solo 0.371). day half band 0.0005, min_prob "
        "0.60, no opens 09:30-13:00; night half band 0.0007, closes only "
        "(night_allow_open False); stride 30, Toto2-1B. results_hybrid.tsv"
    ),
    policy=SessionPolicy.both(slug="hybrid"),
    package="hybrid",
)


CONFIGS = {
    "night": PINNED,
    "day": PINNED_DAY,
    "fullday": PINNED_FULLDAY,
    "hybrid": PINNED_HYBRID,
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
