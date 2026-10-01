# [live/status_report.py](../../live/status_report.py)

**Role:** render one self-contained HTML status page for the live simtrades. It is read-only and runs on demand.
**Pipeline:** `find_processes` + `find_deployments` → `collect_run` per deployment → `parse_shioaji_log` + `read_hardware` → `findings` → `render` into `status_report_template.html`.
**Depends on:** [live/artifacts.py](artifacts.md) (`strategy_sha`), [live/sessions.py](sessions.md) (`MINUTES_PER_SESSION`), [environment.py](../environment.md) (session hours, `classify_session`).   **Used by:** the operator

```powershell
# from the repo root (night/); writes simtrade_status_report.html there
uv run --no-sync python -m forecast_eval.live.status_report
# also dump the collected data; pin the report time for a reproducible build
uv run --no-sync python -m forecast_eval.live.status_report --json status.json --now 2026-10-01T18:30:00
```

## Responsibility
It answers "how are the running soaks doing?" without anyone reading logs by
hand. It covers basic info, data received, decisions, position, stability
(logins) and hardware (VRAM and commit charge). The same inputs and `--now`
always give the same page, except the hardware section, which is a live
snapshot by nature.

## Where each number comes from
| Section | Source |
|---|---|
| Which deployments exist | each `soak_epoch_<slug>.json` in `--outputs`, matched to the newest `live_*` folder with the same `soak_id` |
| Running or stopped, PID | `python.exe` processes running `-m forecast_eval.live.cli --strategy X`. Only the leaf of the `uv` → venv shim → interpreter chain counts |
| Sessions, bars, decisions, re-logins, alerts | the run folder's `session.jsonl` |
| Ticks | the run folder's `ticks.csv` |
| Heartbeat, failures | `live_restart_<slug>.json` |
| Owner's position view | `events_<slug>.jsonl` |
| Successful logins, SDK errors | `shioaji.log` (“Stored new token” per PID, UTC converted to local time) |
| VRAM / RAM per process | Windows `GPU Process Memory` counters + `Get-Process` (nvidia-smi shows N/A per process on WDDM) |
| Knobs missing from `params.json` (`long_only`) | the package's `PARAMS` literal, parsed with `ast` |

## What it deliberately does not do
- **Never imports a strategy or the broker.** `PARAMS` is parsed, not
  imported, because importing `wiring.py` pulls in the forecaster and torch.
  Nothing in it is imported by `cli.py` or `owner_cli.py`, so adding or editing
  it cannot change a running soak.
- **Writes nothing except `--output` (and `--json`).** All other files are
  only read. A line being written mid-read is skipped, not fatal.
- **No free-form commentary.** Every key finding is a fixed rule with a
  threshold: VRAM ≥ 85 %, commit ≥ 80 %, heartbeat older than 5 min, any lost
  minute, failed or unconfirmed re-logins, SDK errors. An unattended page can't
  rely on someone's judgement of the numbers.
- **No history.** Hardware is one reading. Nothing records it over time.

## Two definitions worth knowing
- **Lost minute:** a minute with a trade in `ticks.csv` but no bar. A session
  short of 300/840 bars is normally a quiet market, because a bar is only built
  for a minute with a print. Minutes at or after the newest bar are excluded,
  since that bar may not be flushed yet.
- **Decision funnel:** the band and `min_prob` stages are rebuilt from each
  forecast's `predicted_return` / `prob_up`. "Vetoed by trend/path" is what is
  left once emitted signals are counted.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `main(argv)` | function | CLI: collect, render, print the findings |
| `collect(outputs, log, now, *, processes, hardware, tz)` | function | every input as one JSON-able dict. `processes`/`hardware` are injectable |
| `render(data, template)` | function | the HTML page, with the data embedded and `</` escaped |
| `find_processes(procs)` | function | owner + one leaf process per `--strategy` |
| `find_deployments(outputs)` | function | `Deployment`s, keyed by soak id |
| `collect_run(dep, outputs, now)` | function | one deployment's sessions, ticks, decisions, positions, stability |
| `parse_shioaji_log(path, pids, tz)` | function | logins, attempts and errors per PID |
| `read_hardware(pids)` | function | VRAM/RAM per PID, GPU totals, commit charge. Each probe degrades to `None` on its own |
| `findings(data)` | function | the rule-based key findings |
| `session_of(ts)` | function | the session a timestamp belongs to, labelled by the date it opened |
| `package_params(package)` | function | the package's `PARAMS` literal, without importing it |
