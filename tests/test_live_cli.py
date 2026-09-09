"""The live entry point's guard rails — the parts testable without a feed.

The live loop itself is covered by test_live_runtime.py; what matters here is
that the CLI cannot be talked into something dangerous: placing real orders, or
starting without the VRAM the model needs.
"""
import json
import pathlib
from datetime import datetime, timedelta

import pytest

from forecast_eval.live import cli, soak_epoch
from forecast_eval.live.config import CONFIGS, PINNED, PINNED_DAY, config_named
from forecast_eval.live.recorder import Recorder
from forecast_eval.live.runtime import LiveRuntime
from forecast_eval.live.sessions import SessionPolicy
from forecast_eval.live.state import HaltAction, RailsConfig, SafetyRails


def _params(cfg):
    """The strategy knobs this deployment resolves to.

    LIVE_STRATEGY_ROUTING_PLAN D2: `LiveConfig` no longer holds a copy of them --
    the deployment's `strategy/<session>/` package does, and live builds from
    there. These assertions kept their meaning; only where they read moved.
    """
    from forecast_eval.strategy import wiring as _wiring
    return _wiring.params(cfg.strategy_package)




# ---- reaching the live broker takes two deliberate flags (V4) ---------------
#
# Through V3 the protection here was structural: `shioaji` was absent from
# BROKERS entirely, so it "could not be reached by a mistake at 3 a.m." V4 needs
# it, and re-adding it as one word among three would spend that protection for
# nothing — so the acknowledgement flag now carries it.

def test_the_default_broker_still_sends_nothing():
    args = cli.build_parser().parse_args([])
    assert args.broker == "dryrun"
    assert args.i_understand_this_places_orders is False


def test_selecting_shioaji_alone_is_refused():
    """One flag must not be enough. The refusal happens before subscribing."""
    args = cli.build_parser().parse_args(["--broker", "shioaji"])
    refusal = cli._preflight_orders(args)
    assert refusal and "i-understand-this-places-orders" in refusal


def test_both_flags_together_are_not_enough_on_their_own():
    """`--broker shioaji` + `--i-understand-this-places-orders` used to be
    sufficient by themselves; state retention (below) now also gates this."""
    args = _order_args()
    refusal = cli._preflight_orders(args)
    assert refusal and "--fresh-start" in refusal and "--resume" in refusal


# ---- state retention across a restart is also a deliberate choice -----------

def test_shioaji_with_fresh_start_alone_is_accepted():
    args = cli.build_parser().parse_args(
        ["--broker", "shioaji", "--i-understand-this-places-orders",
         "--fresh-start"])
    assert cli._preflight_orders(args) is None


def test_shioaji_with_resume_alone_is_accepted():
    args = cli.build_parser().parse_args(
        ["--broker", "shioaji", "--i-understand-this-places-orders",
         "--resume"])
    assert cli._preflight_orders(args) is None


def test_fresh_start_and_resume_together_are_refused():
    args = cli.build_parser().parse_args(
        ["--broker", "shioaji", "--i-understand-this-places-orders",
         "--fresh-start", "--resume"])
    refusal = cli._preflight_orders(args)
    assert refusal and "mutually exclusive" in refusal


# ---- a refused launch must not touch the epoch on disk ----------------------
#
# MEASURED 2026-08-31. `soak_epoch.resolve` runs ~100 lines before
# `_preflight_orders` refuses anything, and it *writes* on both branches:
# `--fresh-start` retires the epoch on disk, `--resume` bootstraps one when
# there is none. So the guard deciding whether to call it has to agree with the
# preflight exactly. It did not — it caught both-flags and missed neither-flag,
# and the neither-flag launch minted a phantom epoch on its way to being
# refused. These two tests pin the agreement rather than the symptom.

@pytest.mark.parametrize("broker", cli.ORDER_PLACING_BROKERS)
@pytest.mark.parametrize("flags", [
    [],                              # neither — refused
    ["--fresh-start"],               # accepted
    ["--resume"],                    # accepted
    ["--fresh-start", "--resume"],   # both — refused
])
def test_only_a_launch_the_preflight_accepts_resolves_an_epoch(flags, broker):
    """The guard and the refusal are one condition, so they cannot drift."""
    args = cli.build_parser().parse_args(
        ["--broker", broker, "--i-understand-this-places-orders", *flags])
    accepted = cli._preflight_orders(args) is None
    assert cli._resolves_soak_epoch(args) is accepted


def test_a_refused_launch_never_writes_a_first_ever_epoch_file(tmp_path):
    """The regression itself, on disk: no epoch file, a launch that will be
    refused, and nothing left behind for the next `--fresh-start` to retire or
    the next `--resume` to adopt as the soak's identity."""
    path = tmp_path / "soak_epoch.json"
    for flags in ([], ["--fresh-start", "--resume"]):
        args = cli.build_parser().parse_args(
            ["--broker", "shioaji", "--i-understand-this-places-orders", *flags])
        assert cli._preflight_orders(args) is not None, "expected a refusal"
        if cli._resolves_soak_epoch(args):            # what `main` guards on
            soak_epoch.resolve(path, fresh_start=args.fresh_start,
                               now=datetime(2026, 8, 31, 20, 39, 23))
        assert not path.exists(), f"{flags or ['(neither)']} minted an epoch"


def test_neither_flag_matters_for_offline_brokers():
    """dryrun/sim have no venue ledger — the choice is meaningless there."""
    for name in ("dryrun", "sim"):
        args = cli.build_parser().parse_args(["--broker", name])
        assert args.fresh_start is False and args.resume is False


# ---- the staleness window is reachable without editing code (§2.21) ---------

def test_the_live_staleness_default_is_a_backstop_not_a_detector():
    """§2.22 — the CLI's default deliberately no longer matches `RailsConfig`'s.

    `FeedWatch` polls the venue every 60 s and repairs what it finds, so the
    rails' timer stopped being the detector and became the thing that halts when
    the repair did not work. 120 s is inside normal night behaviour and would
    fire on a thin tape (§2.21); a halt wants minutes of *unhealed* silence.

    `RailsConfig`'s own 120 s stays put — it is the offline/test default and V0
    parity depends on it."""
    args = cli.build_parser().parse_args([])
    assert args.staleness_seconds == 900.0
    assert args.feed_check_seconds == 60.0
    assert args.feed_check_seconds < args.staleness_seconds, (
        "the check must run — and get a chance to repair — before the backstop "
        "halts, or the repair path is unreachable")
    assert RailsConfig().quote_staleness_seconds == 120.0


def test_the_staleness_window_reaches_the_rails():
    """REGRESSION (§2.21). A measured 151 s gap between prints halted a healthy
    23-hour soak at the 120 s limit, 31 s before the market traded again. The
    window has to be settable from the command line — a run that must be edited
    to survive the night is a run nobody restarts at 03:00.

    Goes through `_rails_config`, the same call `main` makes, so a flag that is
    parsed but never wired reads as a failure rather than a pass."""
    args = cli.build_parser().parse_args(["--staleness-seconds", "300"])
    cfg = cli._rails_config(args, PINNED)
    assert cfg.quote_staleness_seconds == 300.0
    rails = SafetyRails(cfg, simulation=True, policy=SessionPolicy.night())

    t0 = datetime(2026, 8, 13, 2, 35, 5)
    rails.note_data(t0)
    rails.check_staleness(t0 + timedelta(seconds=151))   # the real gap
    assert not rails.halted, "300 s window must survive the gap that halted the soak"
    rails.check_staleness(t0 + timedelta(seconds=301))
    assert rails.halted and rails.halt_action is HaltAction.STOP


def test_the_banner_states_the_consequence_not_the_flag():
    """`dryrun` and `shioaji` differ by one word on the command line and by
    everything in effect."""
    live = cli._broker_banner(_order_args(), "TXFR1 -> TXFH6")
    off = cli._broker_banner(cli.build_parser().parse_args([]), "TXFR1 -> TXFH6")
    assert "SIMULATED-LIVE" in live and "ROD" in live
    assert "ORDERS: NONE" in off and "nothing is sent" in off


def test_the_offline_brokers_remain_order_free():
    assert {"dryrun", "sim"} <= set(cli.BROKERS)
    for name in ("dryrun", "sim"):
        args = cli.build_parser().parse_args(["--broker", name])
        assert "NONE" in cli._broker_banner(args, "x")


def _order_args():
    return cli.build_parser().parse_args(
        ["--broker", "shioaji", "--i-understand-this-places-orders"])


# ---- VRAM floor -------------------------------------------------------------

def test_vram_check_refuses_below_the_floor(monkeypatch):
    """Measured: model + context needs ~4.4 GiB. A research run with
    --batch-size auto sizes to fill free VRAM, so starting it first can leave
    live unable to load."""
    import torch
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(torch.cuda, "mem_get_info",
                        lambda *a: (int(1.0 * 2**30), int(32 * 2**30)))
    with pytest.raises(RuntimeError, match="VRAM free"):
        cli.check_vram(min_free_gib=5.0)


def test_vram_check_passes_with_headroom(monkeypatch):
    import torch
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(torch.cuda, "mem_get_info",
                        lambda *a: (int(26 * 2**30), int(32 * 2**30)))
    assert cli.check_vram(min_free_gib=5.0) == pytest.approx(26.0, abs=0.1)


def test_vram_check_requires_a_gpu(monkeypatch):
    import torch
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    with pytest.raises(RuntimeError, match="no CUDA device"):
        cli.check_vram()


# ---- shutdown reporting -----------------------------------------------------

class _Cheap:
    warmup_bars = 0
    forecast_stride_bars = 5
    forecast_horizon_bars = 5

    def __init__(self, **kw):
        self._model = None

    def forecast(self, history_df):
        from forecast_eval.events import Forecast
        self._model = "loaded"
        last = float(history_df["price"].iloc[-1])
        return Forecast(timestamp=history_df["timestamp"].iloc[-1],
                        horizon_bars=5,
                        payload={"predicted_return": 0.0, "last_price": last})

    def forecast_series_batch(self, ts, px, boundaries, **kw):
        import pandas as pd
        return [self.forecast(pd.DataFrame({"timestamp": ts[:b + 1],
                                            "price": px[:b + 1],
                                            "volume": [1] * (b + 1)}))
                for b in boundaries]


def _night_bars(n, policy):
    """n bars that actually lie in the night session.

    A naive 1-min range runs straight through 05:00 into the inter-session gap,
    which `warm_up` rightly rejects — so filter through the policy and take the
    trailing n, spanning as many nights as that needs.
    """
    import pandas as pd
    ts = pd.date_range("2026-04-01 15:00", periods=n * 3, freq="1min")
    keep = [t for t in ts if policy.includes(t)][-n:]
    return pd.DataFrame({"timestamp": keep, "price": [17500.0] * len(keep),
                         "volume": [1] * len(keep)})


# `_runtime` below builds a real `LiveRuntime`, which assembles from the
# session package; the shared fixture stands a cheap banded strategy in its
# place so a short synthetic frame is a valid context and `trend_filter` --
# which `PARAMS` does not declare -- stays overridable (see conftest).
pytestmark = pytest.mark.usefixtures("cheap_strategy")


def _runtime(tmp_path, **kw):
    # A *copy* of PINNED, never PINNED itself — it is a module-level frozen
    # singleton, and patching it in place would leak into test_live_config.py.
    # trend_filter is off because these tests are about shutdown reporting, not
    # priming; with it on, a constant-forecast fixture yields only HOLDs and the
    # priming guard (rightly) refuses to start. It, the short context and the
    # stride are strategy knobs now, so they arrive through the override seam
    # (LIVE_STRATEGY_ROUTING_PLAN D2/D7) rather than as `LiveConfig` fields.
    import dataclasses
    cfg = dataclasses.replace(PINNED)
    overrides = {"trend_filter": False}
    rt = LiveRuntime(cfg, recorder=Recorder(tmp_path / "r.jsonl",
                                            simulation=True),
                     strategy_overrides=overrides, **kw)
    rt.warm_up(_night_bars(rt.strategy_params["context_length"] + 20, rt.policy))
    return rt


def test_clean_session_reports_no_flatten_failure(tmp_path):
    rt = _runtime(tmp_path)
    status = rt.shutdown()
    assert status["flatten_incomplete"] is False
    assert status["position_at_close"] == 0


def test_position_open_after_a_flatten_halt_is_flagged(tmp_path):
    """'Submitted a close' is not 'flat'. ROD orders die at session end, so a
    late flatten can silently fail — it must never look like a clean run."""
    rt = _runtime(tmp_path)
    rt.trader.position = 1
    rt.rails.halt("daily loss", HaltAction.FLATTEN)
    status = rt.shutdown()
    assert status["flatten_incomplete"] is True
    assert status["halt_action"] == "flatten"


def test_stop_halt_with_a_position_is_not_a_flatten_failure(tmp_path):
    """A STOP halt deliberately leaves the position alone — that is correct
    behaviour, not an incomplete flatten."""
    rt = _runtime(tmp_path)
    rt.trader.position = 1
    rt.rails.halt("frozen feed", HaltAction.STOP)
    assert rt.shutdown()["flatten_incomplete"] is False


# ---- argument surface -------------------------------------------------------

def test_capture_is_on_by_default():
    """V1's live half needs the raw stream."""
    assert cli.build_parser().parse_args([]).no_capture is False


def test_warmup_window_defaults_wide_enough_for_priming():
    """Priming needs ~21 night sessions worth of boundaries (measured: the
    longest observed HOLD run is 385 boundaries).

    W6 moved *where* this default is resolved: the parser now leaves it None and
    `main` fills it from the selected config, because a parser-build-time default
    is always NIGHT's regardless of --config. The property being protected is
    unchanged, so it is asserted on the resolved value -- and on every registered
    deployment, since DAY's shorter sessions need a wider window for the same
    number of boundaries, not the same one."""
    assert cli.build_parser().parse_args([]).warmup_days is None
    assert config_named("night").warmup_days_default() >= 30
    for name, cfg in CONFIGS.items():
        got = cfg.warmup_days_default()
        # >= 21 sessions of boundaries, expressed in calendar days for this policy
        assert got * 5 / 7 * cfg.policy.bars_per_session() >= 21 * 840, (
            f"{name}: {got} days is too narrow to prime"
        )


# ---- feed repair budget (§2.24) --------------------------------------------

def test_repair_budget_is_derived_from_staleness_over_interval():
    """Not a picked number: it is exactly how many repairs fit before the
    backstop halts the run. At the shipped defaults (900 s / 60 s) the old fixed
    3 covered only the first 3 of 15 minutes, leaving twelve in which the feed
    was known broken and nothing was attempted."""
    assert cli._repair_budget(900.0, 60.0) == 15
    assert cli._repair_budget(120.0, 60.0) == 2


def test_repair_budget_tracks_the_defaults():
    args = cli.build_parser().parse_args([])
    assert cli._repair_budget(args.staleness_seconds,
                              args.feed_check_seconds) == 15


def test_repair_budget_never_disarms_the_repair():
    """A budget of 0 would leave FeedWatch reporting MISSED while healing
    nothing — the silent-disarm shape SANE_AFTER exists to prevent."""
    assert cli._repair_budget(10.0, 60.0) == 1
    assert cli._repair_budget(900.0, 0.0) == 1
    assert cli._repair_budget(900.0, -1.0) == 1


# ---- W6 / C2: the entry point can express a DAY deployment ------------------


def test_config_flag_selects_the_whole_deployment_not_just_a_policy():
    """C2. The failure this replaces is a `--session day` flag: it moves the
    policy and leaves NIGHT's band and probability gate in place, which is a
    night strategy on a day tape and raises nothing. W5a measured that at
    Calmar 16.3122 -> 0.2671."""
    night = config_named(cli.build_parser().parse_args([]).config)
    day = config_named(cli.build_parser().parse_args(["--config", "day"]).config)

    assert night is PINNED
    assert day is PINNED_DAY
    # the policy moved *and* so did the parameters tuned for it
    assert sorted(night.policy.sessions) == ["NIGHT"]
    assert sorted(day.policy.sessions) == ["DAY"]
    assert _params(day)["buy_threshold"] != _params(night)["buy_threshold"]
    assert _params(day)["min_prob"] != _params(night)["min_prob"]


def test_the_run_directory_names_the_deployment():
    """A directory of DAY artifacts and one of NIGHT artifacts used to be
    distinguishable only by opening `params.json`, and they are the two things
    most likely to be compared side by side."""
    now = datetime(2026, 9, 2, 1, 32, 4)
    night = cli._run_dir(config_named("night"), now)
    day = cli._run_dir(config_named("day"), now)
    assert night.name == "live_night_20260902_013204"
    assert day.name == "live_day_20260902_013204"
    assert night != day


def test_the_run_directory_shares_the_slug_with_the_cache_and_the_ledger():
    """One token per deployment across every file it owns, so a reader can see
    which belong together without opening any of them."""
    from pathlib import Path as _P

    from forecast_eval.live.ledger import ledger_path
    cfg = config_named("day")
    assert "day" in cli._run_dir(cfg).name
    assert cli._bars_cache_path(_P("outputs"), cfg.policy).name == "bars_cache_day.csv"
    assert ledger_path("outputs", cfg.policy).name == "fills_day.csv"


def test_start_up_state_is_keyed_by_policy_not_shared():
    """DAY_SESSION_PLAN §2.5/W9. C5 keyed the data a deployment owns and left its
    START-UP STATE on one path. The RestartGuard caps backfills per trading day
    (10, protecting the venue quota), so one shared file means one shared budget:
    MEASURED 2026-09-02, a single DAY run was charged to the same counter as two
    NIGHT ones, and whichever deployment restarts first can refuse the other for
    the rest of the day. `soak_epoch` and `live_account` share the same fault."""
    from pathlib import Path as _P

    night, day = config_named("night"), config_named("day")
    for name in ("live_restart", "soak_epoch", "live_account"):
        n = cli._state_path(_P("outputs"), night.policy, name)
        d = cli._state_path(_P("outputs"), day.policy, name)
        assert n != d, f"{name} is shared between deployments"
        assert n.name == f"{name}_night.json" and d.name == f"{name}_day.json"


def test_start_up_state_shares_the_slug_with_the_run_directory():
    """Same token across every file a deployment owns, state included."""
    from pathlib import Path as _P

    cfg = config_named("day")
    assert "day" in cli._run_dir(cfg).name
    assert "day" in cli._state_path(_P("outputs"), cfg.policy, "live_restart").name


def test_the_legacy_unkeyed_state_files_are_not_adopted():
    """The pre-C5 `bars_cache.csv` was deliberately never auto-adopted, because
    handing NIGHT's file to whichever deployment starts first is the corruption
    the keying prevents. The same holds for the state files: no policy resolves
    to the bare legacy name."""
    from pathlib import Path as _P

    legacy = {"live_restart.json", "soak_epoch.json", "live_account.json"}
    for cfg in CONFIGS.values():
        for name in ("live_restart", "soak_epoch", "live_account"):
            assert cli._state_path(_P("outputs"), cfg.policy, name).name not in legacy


def test_an_explicit_out_still_wins_over_the_derived_name():
    """`--out` is how a probe or a one-off run names its own directory; deriving
    a default must not take that away."""
    args = cli.build_parser().parse_args(["--out", "outputs/whatever"])
    assert args.out == pathlib.Path("outputs/whatever")


def test_the_missing_flag_is_reported_before_the_restart_guard(monkeypatch, tmp_path):
    """MEASURED 2026-09-02. A command missing --resume was refused by the BACKFILL
    CAP instead, because the guard gated at start-up while this check sat next to
    the broker it protects, 150 lines later. The operator was told "something is
    restarting this process repeatedly" when the answer was one missing flag."""
    monkeypatch.setenv("SHIOAJI_API_KEY", "test-key")
    monkeypatch.setenv("SHIOAJI_SECRET_KEY", "test-secret")
    state = tmp_path / "live_restart.json"
    state.write_text(json.dumps({
        "run_in_flight": False, "scored": True, "consecutive_failures": 0,
        "trading_day": cli.RestartGuard.trading_day(datetime.now()),
        "backfills_today": 99,          # the cap is long spent
    }), encoding="utf-8")

    rc = cli.main(["--broker", "shioaji", "--i-understand-this-places-orders",
                   "--out", str(tmp_path / "run"), "--state-file", str(state)])
    assert rc == 5, "5 is the preflight refusal; 3 would be the restart guard"


def test_there_is_no_bare_session_flag():
    """The flag C2 rejects must not exist. If someone adds one later, the
    pairing this whole design protects is breakable again."""
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["--session", "day"])


def test_night_remains_the_default_deployment():
    """NIGHT is the deployment that has soaked; DAY has never traded. A missing
    flag must not silently start the unproven one."""
    assert cli.build_parser().parse_args([]).config == "night"


def test_warmup_days_default_follows_the_selected_config_not_pinned():
    """DAY_SESSION_PLAN C6, and the regression for where W6 could have undone it.

    Argparse evaluates defaults when the parser is *built*, before --config has
    been read, so a baked `default=PINNED.warmup_days_default()` is always
    NIGHT's 30. A DAY run inheriting 30 reaches ~109 emitter boundaries and about
    4 non-HOLD samples against `_check_primed`'s floor of 3."""
    for argv, expected in ([], 30), (["--config", "day"], 84):
        args = cli.build_parser().parse_args(argv)
        # unresolved at parse time -- that is the point
        assert args.warmup_days is None
        cfg = config_named(args.config)
        assert cfg.warmup_days_default() == expected


def test_an_explicit_warmup_days_still_wins_over_the_derived_default():
    args = cli.build_parser().parse_args(["--config", "day", "--warmup-days", "7"])
    assert args.warmup_days == 7


def test_config_named_refuses_an_unknown_deployment():
    """Reachable only programmatically -- the CLI has `choices=` -- but a silent
    fallback to NIGHT would be C2's failure arriving by another door."""
    with pytest.raises(ValueError, match="unknown config"):
        config_named("weekend")


def test_every_TUNED_config_trades_exactly_one_session():
    """A `both()` policy would make "the best row for this deployment"
    undefined, and `results.tsv` is scoped by a single session name.

    Scoped to *tuned* configs by STRATEGY_SPLIT_PLAN S4. `fullday` is a
    `both()` policy and is deliberately registered while unprovenanced, so the
    original blanket assertion is no longer true — but its reason still is, and
    this is where it bites: the day full-day acquires a `source_commit`, this
    fails, and whoever tuned it has to say which `results.tsv` session name the
    pin is scoped by before it can trade. That question should not be answerable
    by accident."""
    for name, cfg in CONFIGS.items():
        if not cfg.is_tuned:
            continue
        assert len(cfg.policy.sessions) == 1, f"{name} trades {cfg.policy.sessions}"


def test_an_untuned_config_is_no_longer_refused():
    """LIVE_STRATEGY_ROUTING_PLAN D1 — the gate that stood here is gone.

    `main` used to return 5 for any config whose `is_tuned` was False, before the
    alerter or the credentials check. It was removed because the broker it
    protected is hardcoded `simulation=True` (not a flag), so it guarded an
    account that cannot lose money while blocking the one workflow simtrade is
    for: running what the research loop just produced.

    Not asserted by calling `main()` — past this point it makes a run directory
    and reaches for credentials. What is asserted is that the property survives
    and still marks the run, which is what replaced the refusal: enforcement
    became a record.
    """
    cfg = config_named("fullday")
    assert cfg.is_tuned is False
    assert "UNTUNED" in cfg.describe()
    assert "calmar=nan" in cfg.describe(), (
        "tools/session_artifacts.py calls float() on this group; 'None' would raise")
    # The parser accepts it — there is nothing left to refuse it at.
    assert cli.build_parser().parse_args(["--strategy", "fullday"]).config == "fullday"


def test_strategy_and_config_are_two_spellings_of_one_flag():
    """D5. They have always selected the same thing; `--config` stays an alias so
    recorded soak commands keep running."""
    for spelling in ("--strategy", "--config"):
        assert cli.build_parser().parse_args([spelling, "day"]).config == "day"
    assert cli.build_parser().parse_args([]).config == "night"
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["--strategy", "weekend"])


def test_param_is_repeatable_and_defaults_empty():
    """D7. The live CLI carries the same `--param` the backtest does; the values
    are coerced against the deployment package's PARAMS and merged by
    `make_strategy`."""
    assert cli.build_parser().parse_args([]).param == []
    args = cli.build_parser().parse_args(
        ["--param", "min_prob=0.6", "--param", "aggression_ticks=1"])
    assert args.param == ["min_prob=0.6", "aggression_ticks=1"]

    from forecast_eval.param_override import parse_overrides
    from forecast_eval.strategy import wiring
    got = parse_overrides(args.param, wiring.params("night"))
    assert got == {"min_prob": 0.6, "aggression_ticks": 1}


def test_the_banner_carries_the_selected_deployments_provenance():
    """`cfg.describe()` goes on the console banner and into the recorder's meta
    header, so a running process can be identified from its own output."""
    text = PINNED_DAY.describe()
    assert "DAY" in text
    assert "3c3ae38" in text          # the commit that produced the row
    assert "1.6937" in text           # its Calmar
    assert "0.001" in text            # the band that differs from NIGHT
    assert "NIGHT" not in text


# ---- every order-placing broker passes through every gate (§18) -------------
#
# `client` places nothing itself; it submits intents to the owning process,
# which does. Four gates used to be spelled `== "shioaji"` — the preflight, the
# soak-epoch resolution, the `--fresh-start` flatness check and the banner — and
# a mode that sends orders through one more hop is still a mode that sends
# orders. These are written over `ORDER_PLACING_BROKERS` rather than over
# "client" so a fifth mode cannot be added without facing them.

@pytest.mark.parametrize("broker", cli.ORDER_PLACING_BROKERS)
def test_an_order_placing_broker_alone_is_refused(broker):
    """One flag must never be enough, whichever way the orders reach the venue."""
    args = cli.build_parser().parse_args(["--broker", broker, "--fresh-start"])
    refusal = cli._preflight_orders(args)
    assert refusal and "--i-understand-this-places-orders" in refusal
    assert broker in refusal, "the refusal names the mode the operator typed"


@pytest.mark.parametrize("broker", cli.ORDER_PLACING_BROKERS)
def test_an_order_placing_broker_needs_a_retention_choice(broker):
    args = cli.build_parser().parse_args(
        ["--broker", broker, "--i-understand-this-places-orders"])
    refusal = cli._preflight_orders(args)
    assert refusal and "--fresh-start or --resume" in refusal


@pytest.mark.parametrize("broker", cli.ORDER_PLACING_BROKERS)
def test_an_order_placing_broker_says_so_in_its_banner(broker):
    """§5 — the mode must be unmistakable in the log and on the console."""
    args = cli.build_parser().parse_args(
        ["--broker", broker, "--i-understand-this-places-orders"])
    banner = cli._broker_banner(args, "TXFR1 -> TXFH6")
    assert "ORDERS: NONE" not in banner
    assert "nothing is sent" not in banner


def test_the_client_banner_names_the_owner_not_the_venue():
    """An operator reading 'via Shioaji' here would debug the wrong process."""
    args = cli.build_parser().parse_args(
        ["--broker", "client", "--i-understand-this-places-orders"])
    banner = cli._broker_banner(args, "TXFR1 -> TXFH6")
    assert "VIA OWNER" in banner
    assert "places nothing directly" in banner


def test_the_offline_brokers_are_not_order_placing():
    assert set(cli.ORDER_PLACING_BROKERS).isdisjoint({"dryrun", "sim"})
    assert set(cli.ORDER_PLACING_BROKERS) <= set(cli.BROKERS)
