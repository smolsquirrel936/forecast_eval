"""live/status_report.py — collection rules on a synthetic outputs folder.

GPU-free and process-free: processes and hardware are injected, so nothing here
touches a running soak.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from forecast_eval.live import status_report as sr
from forecast_eval.metrics import trading_metrics
from forecast_eval.live.status_report import Proc

NOW = datetime(2026, 10, 1, 18, 30, 0)
UTC = timezone.utc
HW = {"procs": [{"pid": 300, "ded": 8000.0, "shr": 0.0, "ws": 400, "priv": 14000, "cpu": 60,
                 "threads": 10, "handles": 100}],
      "gpu": {"name": "GPU", "used": 30000.0, "total": 32000.0, "util": 0.0, "temp": 50.0,
              "power": 50.0, "power_limit": 500.0, "driver": "1"},
      "mem": {"ram": 128.0, "ram_free": 64.0, "commit": 130.0, "commit_free": 60.0, "cpu": "CPU"}}
# The headers `logging_io.write_fills` / `write_trades` produce.
FILLS_HEAD = "timestamp,side,fill_price,quantity,fee"
TRADES_HEAD = ("entry_ts,exit_ts,side,entry_price,exit_price,pnl_points,fees_points,"
               "net_pnl_points,session,bars_held,rolled")


def _procs():
    cmd = "python -m forecast_eval.live.cli --strategy night --broker client"
    own = "python -m forecast_eval.live.owner_cli --out outputs"
    return [Proc(100, 1, None, "uv run --no-sync " + cmd), Proc(200, 100, None, cmd),
            Proc(300, 200, None, cmd), Proc(400, 1, None, own), Proc(401, 400, None, own)]


def _write_jsonl(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _outputs(tmp_path):
    out = tmp_path / "outputs"
    run = out / "live_night_20260930_080000"
    run.mkdir(parents=True)
    soak = "soak_20260930_080000_000000"
    (out / "soak_epoch_night.json").write_text(json.dumps(
        {"soak_id": soak, "started_at": "2026-09-30T08:00:00", "retired": [
            {"soak_id": "soak_old", "started_at": "2026-09-30T07:50:00",
             "retired_at": "2026-09-30T08:00:00"}]}))
    (out / "live_restart_night.json").write_text(json.dumps(
        {"consecutive_failures": 0, "backfills_today": 1, "last_heartbeat": "2026-10-01T18:29:50"}))
    (run / "params.json").write_text(json.dumps({
        "soak_id": soak, "strategy_package": "night", "sessions": ["NIGHT"],
        "buy_threshold": 0.001, "sell_threshold": -0.001, "min_prob": 0.6,
        "horizon_bars": 2, "stride_bars": 2, "context_length": 10, "checkpoint": "x/y",
        "max_position": 1, "git_commit": "abc", "git_dirty": False, "strategy_sha": "s"}))
    # Night session that opened 09-30: bars at 15:00..15:03 and 00:01 (next calendar
    # day, same session). 15:02 has a trade in ticks.csv but no bar — a lost minute.
    bar_ts = ["2026-09-30T15:00:00", "2026-09-30T15:01:00", "2026-09-30T15:03:00",
              "2026-09-30T15:04:00", "2026-10-01T00:01:00"]
    prices = [100.0, 101.0, 102.0, 99.0, 98.0]
    events = [{"event": "warm_up", "bars": 50, "first": "a", "last": "b", "emitter_primed": 4}]
    events += [{"event": "bar", "bar_idx": i, "timestamp": t, "price": p, "position": 0}
               for i, (t, p) in enumerate(zip(bar_ts, prices))]
    # forecast 0: up 0.5 %, realised +2 % (hit), crossed, prob ok -> BUY emitted
    # forecast 2: down 0.2 %, realised -3.9 % (hit), crossed, prob 0.5 fails min_prob
    events += [
        {"event": "forecast", "bar_idx": 0, "timestamp": bar_ts[0], "payload": {
            "predicted_return": 0.005, "prob_up": 0.8, "last_price": 100.0, "predicted_price": 100.5}},
        {"event": "signal", "timestamp": bar_ts[0], "direction": "BUY"},
        {"event": "forecast", "bar_idx": 2, "timestamp": bar_ts[2], "payload": {
            "predicted_return": -0.002, "prob_up": 0.5, "last_price": 102.0, "predicted_price": 101.8}},
        {"event": "signal", "timestamp": bar_ts[2], "direction": "HOLD"},
        {"event": "relogin", "when": "2026-10-01T14:55:00"},
        {"event": "relogin_failed", "wall": "2026-10-01T08:40:30", "backoff_seconds": 60.0},
        {"event": "verified", "token_age_seconds": 240.2},
        {"event": "feed_check", "verdict": "quiet"},
    ]
    _write_jsonl(run / "session.jsonl", events)
    (run / "session.jsonl").open("a", encoding="utf-8").write('{"truncated": ')  # mid-write line
    ticks = ["timestamp,received,code,price,volume,simtrade",
             "2026-09-30T14:59:00,2026-09-30T14:59:01,TXF,100,1,1"]
    for m in ("15:00:10", "15:01:10", "15:02:30", "15:03:10", "15:04:10"):
        ticks.append(f"2026-09-30T{m},2026-09-30T{m}.500000,TXF,100,2,0")
    (run / "ticks.csv").write_text("\n".join(ticks) + "\n", encoding="utf-8")
    for name, head in (("orders.csv", "timestamp,side"), ("fills.csv", FILLS_HEAD),
                       ("trades.csv", TRADES_HEAD)):
        (run / name).write_text(head + "\n", encoding="utf-8")
    _write_jsonl(out / "events_night.jsonl", [{"kind": "position", "signed_qty": 0}] * 3)
    _write_jsonl(out / "fatal.jsonl", [{"kind": "crashed", "when": "2026-09-30T07:55:00", "message": "boom"}])
    log = tmp_path / "shioaji.log"
    log.write_text(
        "[pid:300] 2026-09-30T00:00:01.000000Z  INFO shioaji::auth: Stored new token in slot 1\n"
        "[pid:300] 2026-09-30T06:55:00.000000Z  INFO shioaji::auth: No cached token available, performing new login\n"
        "[pid:300] 2026-09-30T06:55:01.000000Z  INFO shioaji::auth: Stored new token in slot 1\n"
        "[pid:401] 2026-09-30T00:00:00.000000Z ERROR rsolace::solclient: send msg to channel error: ReceiveClosed\n"
        "[pid:999] 2026-09-30T00:00:02.000000Z  INFO shioaji::auth: Stored new token in slot 2\n",
        encoding="utf-8")
    return out, log


def test_find_processes_takes_the_leaf_of_the_uv_chain():
    found = sr.find_processes(_procs())
    assert found["deployments"]["night"].pid == 300
    assert found["owner"].pid == 401


def test_session_of_labels_a_night_session_by_the_date_it_opened():
    assert sr.session_of(datetime(2026, 10, 1, 2, 0))["label"] == "NIGHT 09-30"
    assert sr.session_of(datetime(2026, 10, 1, 9, 0))["label"] == "DAY 10-01"
    assert sr.session_of(datetime(2026, 10, 1, 14, 0)) is None


def _fake_packages(tmp_path, monkeypatch):
    # The real packages differ by branch (night gained `long_only` on simtrade_0929),
    # so the tests read a fixture package tree instead.
    root = tmp_path / "pkg"
    for name, body in (("night", '{"buy_threshold": 0.001, "long_only": True}'),
                       ("day", '{"buy_threshold": 0.001, "max_aggression_ticks": None}')):
        (root / "strategy" / name).mkdir(parents=True)
        (root / "strategy" / name / "wiring.py").write_text(
            "import torch  # never imported by the report\n"
            "from typing import Any, Dict\n"
            f"PARAMS: Dict[str, Any] = {body}\n", encoding="utf-8")
    monkeypatch.setattr(sr, "PACKAGE_ROOT", root)


def test_package_params_reads_the_literal_without_importing(tmp_path, monkeypatch):
    _fake_packages(tmp_path, monkeypatch)
    assert sr.package_params("night").get("long_only") is True
    assert sr.package_params("day") == {"buy_threshold": 0.001, "max_aggression_ticks": None}
    assert sr.package_params("missing") == {}


def test_collect_counts_sessions_ticks_decisions_and_logins(tmp_path, monkeypatch):
    _fake_packages(tmp_path, monkeypatch)
    out, log = _outputs(tmp_path)
    data = sr.collect(out, log, NOW, processes=_procs(), hardware=HW, tz=UTC)
    (r,) = data["runs"]
    assert r["running"] and r["pid"] == 300
    assert r["params"]["long_only"] is True                 # from the fixture wiring.py
    (s,) = r["sessions"]
    assert s["label"] == "NIGHT 09-30" and s["bars"] == 5 and s["complete"]
    assert s["lost_minutes"] == ["2026-09-30T15:02"]        # trade on tape, no bar
    assert r["ticks"]["total"] == 6 and r["ticks"]["trial"] == 1
    assert r["ticks"]["by_session"] == {"outside": 1, "NIGHT 09-30": 5}
    d = r["decisions"]
    assert (d["forecasts"], d["band_pass"], d["prob_pass"], d["nonhold"]) == (2, 2, 1, 1)
    assert (d["hit"], d["n_eval"]) == (2, 2)
    assert r["position"]["owner_events"] == 3 and r["position"]["changes"] == 0
    assert data["logins"]["300"]["logins"] == ["2026-09-30T00:00:01", "2026-09-30T06:55:01"]
    assert data["logins"]["300"]["attempts"] == 1
    assert len(data["logins"]["401"]["errors"]) == 1
    assert "999" not in data["logins"]                      # a PID that is not running
    assert data["fatal"][0]["attributed"].startswith("night: soak soak_old")


def _resumed(out):
    """A `--resume` restart: a second run folder under the same soak id.

    The new folder starts its own `bar_idx` at 0, re-seeds the soak's fills
    (the ledger replay), and has no orders of its own yet."""
    old = out / "live_night_20260930_080000"
    new = out / "live_night_20261001_090000"
    new.mkdir()
    (new / "params.json").write_text((old / "params.json").read_text())
    # the old folder: one order and its fill, plus a forecast on its LAST bar,
    # whose 2-bar horizon can only be scored from the new folder's bars
    (old / "orders.csv").write_text("timestamp,side\n2026-09-30T15:01:00,BUY\n", encoding="utf-8")
    (old / "fills.csv").write_text(f"{FILLS_HEAD}\n2026-09-30T15:01:05,BUY,100,1,0.02\n", encoding="utf-8")
    with (old / "session.jsonl").open("a", encoding="utf-8") as f:
        f.write("\n" + json.dumps({"event": "forecast", "bar_idx": 4, "timestamp": "2026-10-01T00:01:00",
                                   "payload": {"predicted_return": -0.003, "prob_up": 0.1,
                                               "last_price": 98.0, "predicted_price": 97.7}}) + "\n")
    _write_jsonl(new / "session.jsonl", [
        {"event": "warm_up", "bars": 77, "first": "c", "last": "d", "emitter_primed": 5},
        {"event": "bar", "bar_idx": 0, "timestamp": "2026-10-01T15:00:00", "price": 97.0, "position": 0},
        {"event": "bar", "bar_idx": 1, "timestamp": "2026-10-01T15:01:00", "price": 96.0, "position": 0},
    ])
    (new / "ticks.csv").write_text(
        "timestamp,received,code,price,volume,simtrade\n"
        "2026-10-01T15:00:10,2026-10-01T15:00:10.500000,TXF,97,1,0\n", encoding="utf-8")
    (new / "orders.csv").write_text("timestamp,side\n", encoding="utf-8")
    (new / "fills.csv").write_text(f"{FILLS_HEAD}\n2026-09-30T15:01:05,BUY,100,1,0.02\n", encoding="utf-8")
    (new / "trades.csv").write_text(TRADES_HEAD + "\n", encoding="utf-8")
    return old, new


def test_collect_covers_every_run_folder_of_the_soak(tmp_path, monkeypatch):
    """MEASURED 2026-10-03: after a weekend `--resume` the page showed 0
    forecasts, because it read only the newest folder; 453 were on disk."""
    _fake_packages(tmp_path, monkeypatch)
    out, log = _outputs(tmp_path)
    old, new = _resumed(out)
    (dep,) = sr.find_deployments(out)
    assert dep.run_dir == new and dep.run_dirs == (old, new)
    (r,) = sr.collect(out, log, NOW + timedelta(days=1), processes=_procs(), hardware=HW, tz=UTC)["runs"]
    assert r["dir"] == new.name and r["dirs"] == [old.name, new.name]
    assert [s["label"] for s in r["sessions"]] == ["NIGHT 09-30", "NIGHT 10-01"]
    assert r["bars"] == 7 and r["ticks"]["total"] == 7
    d = r["decisions"]
    assert d["forecasts"] == 3                      # all from the old folder
    assert len(d["rows"]) == 3
    assert d["orders"] == 1                         # summed: 1 old + 0 new
    assert d["fills"] == 1                          # newest only: it holds the replay
    assert r["warm_up"]["emitter_primed"] == 5      # the newest run's warm-up


def test_a_forecast_whose_horizon_crosses_a_restart_is_still_scored(tmp_path, monkeypatch):
    """`bar_idx` restarts at 0 per folder: the old folder's bar 4 + 2 must land
    on the new folder's bar 1 (96.0), not on the old folder's missing bar 6 or
    on the new folder's bar_idx 6."""
    _fake_packages(tmp_path, monkeypatch)
    out, log = _outputs(tmp_path)
    _resumed(out)
    (r,) = sr.collect(out, log, NOW + timedelta(days=1), processes=_procs(), hardware=HW, tz=UTC)["runs"]
    last = r["decisions"]["rows"][-1]
    assert last["ts"] == "2026-10-01T00:01:00"
    assert abs(last["realized"] - (96.0 / 98.0 - 1)) < 1e-12
    assert (r["decisions"]["hit"], r["decisions"]["n_eval"]) == (3, 3)


def test_a_hold_past_band_and_min_prob_names_its_veto(tmp_path, monkeypatch):
    """The emitter's only gates after min_prob are the trend filter, then the
    path check. A HOLD whose median path agrees with the signal is the trend
    filter's; one whose path disagrees could be either, so it says so. An
    emitted signal and a min_prob failure carry no veto."""
    _fake_packages(tmp_path, monkeypatch)
    out, log = _outputs(tmp_path)
    run = out / "live_night_20260930_080000"
    sell = {"predicted_return": -0.005, "prob_up": 0.1, "last_price": 99.0}
    _write_jsonl(run / "session.jsonl", [
        json.loads(l) for l in (run / "session.jsonl").read_text(encoding="utf-8").splitlines()
        if not l.startswith('{"truncated"')] + [
        {"event": "forecast", "bar_idx": 3, "timestamp": "2026-09-30T15:04:00",
         "payload": {**sell, "median_path": [99.0, 98.0, 97.0]}},       # path agrees
        {"event": "signal", "timestamp": "2026-09-30T15:04:00", "direction": "HOLD"},
        {"event": "forecast", "bar_idx": 4, "timestamp": "2026-10-01T00:01:00",
         "payload": {**sell, "median_path": [99.0, 100.0, 97.0]}},      # path disagrees
        {"event": "signal", "timestamp": "2026-10-01T00:01:00", "direction": "HOLD"},
    ])
    data = sr.collect(out, log, NOW, processes=_procs(), hardware=HW, tz=UTC)
    d = data["runs"][0]["decisions"]
    assert [f["veto"] for f in d["rows"]] == [None, None, "trend", "trend or path"]
    assert d["vetoes"] == {"trend": 1, "trend or path": 1}
    text = " ".join(f["html"] for f in sr.findings(data))
    assert "1 vetoed by the trend filter, 1 by the trend filter or path check" in text


def test_performance_is_the_per_run_reports_metrics_and_charts(tmp_path):
    """The table is `metrics.trading_metrics` over trades.csv, in its own key
    order; the charts are the per-run report's plotly figures, drawn against the
    soak's capture; and nothing is written under outputs/ — the soak price
    cache belongs to the live process."""
    out, log = _outputs(tmp_path)
    run = out / "live_night_20260930_080000"
    (run / "trades.csv").write_text(
        TRADES_HEAD + "\n"
        "2026-09-30T15:00:30,2026-09-30T15:01:30,LONG,100,103,3,0.04,2.96,NIGHT,1,0\n"
        "2026-09-30T15:03:30,2026-09-30T15:04:30,SHORT,102,103,-1,0.04,-1.04,NIGHT,1,0\n",
        encoding="utf-8")
    (run / "fills.csv").write_text(
        FILLS_HEAD + "\n2026-09-30T15:00:30,BUY,100,1,0.02\n2026-09-30T15:01:30,SELL,103,1,0.02\n"
        "2026-09-30T15:03:30,SELL,102,1,0.02\n2026-09-30T15:04:30,BUY,103,1,0.02\n", encoding="utf-8")
    before = sorted(str(p) for p in out.rglob("*"))
    data = sr.collect(out, log, NOW, processes=_procs(), hardware=HW, tz=UTC)
    assert sorted(str(p) for p in out.rglob("*")) == before
    perf = data["runs"][0]["performance"]
    assert [k for k, _ in perf["metrics"]] == list(trading_metrics([]))
    m = dict(perf["metrics"])
    assert m["n_trades"] == 2 and m["net_pnl_points"] == pytest.approx(1.92)
    assert m["hit_rate"] == 0.5 and m["max_drawdown_points"] == pytest.approx(-1.04)
    assert perf["point_value"] == 200.0                     # the per-run report's TXF default
    assert "strategy" in {t["name"] for t in perf["figs"]["equity"]["data"]}
    assert [t["name"] for t in perf["figs"]["price"]["data"]] == ["price", "BUY", "SELL"]


def test_performance_without_trades_is_valid_json(tmp_path):
    """No round trip leaves the ratios undefined; they must reach the page as
    null, since NaN is not JSON."""
    out, log = _outputs(tmp_path)
    data = sr.collect(out, log, NOW, processes=_procs(), hardware=HW, tz=UTC)
    m = dict(data["runs"][0]["performance"]["metrics"])
    assert m["n_trades"] == 0 and m["hit_rate"] is None
    json.dumps(data, default=str, allow_nan=False)


def test_findings_flag_gaps_failed_relogins_and_vram(tmp_path):
    out, log = _outputs(tmp_path)
    data = sr.collect(out, log, NOW, processes=_procs(), hardware=HW, tz=UTC)
    text = {f["html"].split("</b>")[0]: f["level"] for f in data["findings"]}
    assert text["<b>Data gaps found."] == "warn"
    assert any(k.startswith("<b>VRAM 94% used") and v == "warn" for k, v in text.items())
    assert any(k.startswith("<b>Logins") and v == "warn" for k, v in text.items())


def test_a_deployment_without_a_process_is_reported_stopped(tmp_path):
    out, log = _outputs(tmp_path)
    data = sr.collect(out, log, NOW, processes=[], hardware=HW, tz=UTC)
    assert data["runs"][0]["running"] is False and data["owner"] is None
    assert data["findings"][0]["level"] == "warn"


def test_render_is_deterministic_and_escapes_script_end(tmp_path):
    out, log = _outputs(tmp_path)
    data = sr.collect(out, log, NOW, processes=_procs(), hardware=HW, tz=UTC)
    data["fatal"][0]["message"] = "</script><b>x"
    a, b = sr.render(data), sr.render(json.loads(json.dumps(data, default=str)))
    assert a == b
    assert "__STATUS_DATA__" not in a and "__PLOTLY_JS__" not in a
    assert a.count("</script>") == 2                # plotly.js + the page's own script


def test_main_writes_the_page_and_json(tmp_path, monkeypatch):
    out, log = _outputs(tmp_path)
    monkeypatch.setattr(sr, "list_processes", _procs)
    monkeypatch.setattr(sr, "read_hardware", lambda pids: HW)
    page, js = tmp_path / "r.html", tmp_path / "r.json"
    assert sr.main(["--outputs", str(out), "--log", str(log), "--output", str(page),
                    "--json", str(js), "--now", (NOW + timedelta(0)).isoformat()]) == 0
    assert page.read_text(encoding="utf-8").startswith("<!doctype html>")
    assert json.loads(js.read_text(encoding="utf-8"))["generated"] == NOW.isoformat()


def test_default_output_is_stamped_with_the_report_time(tmp_path):
    """One fixed name meant every run overwrote the last page."""
    assert sr.default_output(NOW, tmp_path) == tmp_path / "simtrade_status_report_20261001_183000.html"
    later = sr.default_output(NOW + timedelta(seconds=1), tmp_path)
    assert later != sr.default_output(NOW, tmp_path)
    assert sorted([later.name, sr.default_output(NOW, tmp_path).name])[0].endswith("183000.html")


def test_main_without_output_writes_the_stamped_page(tmp_path, monkeypatch):
    """The stamp follows --now, so a pinned build gets a pinned name too."""
    out, log = _outputs(tmp_path)
    monkeypatch.setattr(sr, "list_processes", _procs)
    monkeypatch.setattr(sr, "read_hardware", lambda pids: HW)
    monkeypatch.setattr(sr, "REPO_ROOT", tmp_path)
    assert sr.main(["--outputs", str(out), "--log", str(log), "--now", NOW.isoformat()]) == 0
    page = tmp_path / "simtrade_status_report_20261001_183000.html"
    assert page.read_text(encoding="utf-8").startswith("<!doctype html>")
    assert not (tmp_path / "simtrade_status_report.html").exists()


def test_explicit_output_still_wins(tmp_path, monkeypatch):
    out, log = _outputs(tmp_path)
    monkeypatch.setattr(sr, "list_processes", _procs)
    monkeypatch.setattr(sr, "read_hardware", lambda pids: HW)
    monkeypatch.setattr(sr, "REPO_ROOT", tmp_path)
    page = tmp_path / "mine.html"
    assert sr.main(["--outputs", str(out), "--log", str(log), "--output", str(page),
                    "--now", NOW.isoformat()]) == 0
    assert page.exists()
    assert list(tmp_path.glob("simtrade_status_report_*.html")) == []
