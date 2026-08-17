"""The live entry point's guard rails — the parts testable without a feed.

The live loop itself is covered by test_live_runtime.py; what matters here is
that the CLI cannot be talked into something dangerous: placing real orders, or
starting without the VRAM the model needs.
"""
from datetime import datetime, timedelta

import pytest

from forecast_eval.live import cli
from forecast_eval.live.config import PINNED
from forecast_eval.live.recorder import Recorder
from forecast_eval.live.runtime import LiveRuntime
from forecast_eval.live.sessions import SessionPolicy
from forecast_eval.live.state import HaltAction, RailsConfig, SafetyRails


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


def test_both_flags_together_are_accepted():
    args = _order_args()
    assert cli._preflight_orders(args) is None


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


def _runtime(tmp_path, **kw):
    # A *copy* of PINNED, never PINNED itself — it is a module-level frozen
    # singleton, and patching it in place would leak into test_live_config.py.
    # trend_filter is off because these tests are about shutdown reporting, not
    # priming; with it on, a constant-forecast fixture yields only HOLDs and the
    # priming guard (rightly) refuses to start.
    import dataclasses
    cfg = dataclasses.replace(PINNED, trend_filter=False, context_length=60,
                              forecast_stride_bars=5)
    object.__setattr__(cfg, "build_forecaster", lambda **k: _Cheap(**k))
    rt = LiveRuntime(cfg, recorder=Recorder(tmp_path / "r.jsonl",
                                            simulation=True), **kw)
    rt.warm_up(_night_bars(cfg.context_length + 20, rt.policy))
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
    longest observed HOLD run is 385 boundaries)."""
    assert cli.build_parser().parse_args([]).warmup_days >= 30


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
