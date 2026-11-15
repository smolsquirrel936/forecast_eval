# FORECAST_SERVER_PLAN.md — one forecast model for all live deployments

**Status: DEFERRED 2026-11-12** (user decision). No code written. Was planned for the weekend of 2026-11-14.
Moved out of `CLAUDE.md` on 2026-11-15, unchanged except for the headings.

## Decision

**Today every live deployment is its own process and loads its own copy of any Toto2 checkpoint it uses.** Not fixed, and **deferred on 2026-11-12** by the user's decision. No code was written.

- **Why deferred:** the live trading gains nothing from it, because the forecasts are meant to be identical. The only gain is free VRAM for research. The cost is a new shared point of failure: today one crashed deployment leaves the others trading, but a server crash or hang would stop day, fullday and hybrid together. There are also new ways to fail (sockets, timeouts, queued warm-ups). It is about 2–4 days of work including the checks below. Live runs fine at about 14–15 GB.
- **Until then:** for research, reuse `--forecast-cache` so most runs need no model on the GPU, and run cold Toto2 runs (about 27 GB peak) one at a time while the simtrades are stopped.
- **Revisit when** GPU contention actually blocks research. If it is built, make it opt-in per deployment, so a flag returns any deployment to its own in-process model with no code change. Move one deployment at a time (fullday first, for example), not all three at once.

## The plan, kept for when it is revisited

- **Footprint (2026-11-09, `simtrade_0929`):** day, fullday and hybrid each hold one Toto-2.0-1B, about 4.8 GB each, so about 14–15 GB of the 32 GB card. Hybrid's two halves already share one model inside their process (`RoutedForecaster._ready`, [strategy/hybrid/forecaster.py](strategy/hybrid/forecaster.py)). Night runs the CPU price ridge (`38b5bf6`) and loads none; since `0d98b6f` it also skips `check_vram` (`forecaster_needs_gpu`, [live/cli.py](live/cli.py)).
- **Why it matters:** sharing would free about 9–10 GB, room for research Toto2 runs next to the simtrade. On 2026-11-08 two research runs at once already ran out of memory.
- **Constraint:** PyTorch's CUDA IPC (sharing GPU tensors between processes) is Linux-only, so on Windows separate processes cannot share the weights directly. Running all deployments in one process is rejected: it gives up the per-deployment isolation (separate accounts, separate processes).
- **Planned design: a forecast server.** One process loads the model once. In the live layer (not the strategy packages), each deployment's forecaster is swapped for a client that forwards `forecast()` / `forecast_series_batch()` over a local socket. The server keeps one `Toto2Forecaster` per deployment config, all pointing at the shared `_model` (the same trick as `RoutedForecaster._ready`). The packages, the hash-pinned `forecaster/toto2.py` and `forecaster_sha` stay unchanged, so backtests and provenance are unaffected. Requests are never batched across deployments, so forecasts stay bit-identical to today's.
- **To settle before shipping:** a server crash stops day, fullday and hybrid at once, so it needs a heartbeat and alert like the owner process has, plus a rule for what a deployment does while the server is down. Simultaneous requests queue (about 0.09 s each; warm-ups, about 5 s each, run one after another). The VRAM floor moves to the server's start. Check the outputs are identical to in-process forecasts on a replay. Then do a sim run before live. Commit on `main`, then merge into `simtrade_0929` while the simtrades are stopped.
