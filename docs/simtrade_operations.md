# Simtrade operations — the owner and three deployments

**Role:** The operator's guide to a running simulation trade: how to check it,
stop it, pause it, restart it, and what to watch while it runs.
**Shape covered:** one [owner](live/owner_cli.md) plus the day, night and fullday
deployments under `--broker client`, sharing one `outputs/` parent.
**Not covered:** what the strategies trade and why. That is in the run's
branch: `strategy/README.md`, with the launch decisions in
`SIMTRADE_0929_PLAN.md` (both on `simtrade_0929` only, so they are named here,
not linked).

> This is a **task page**, not a 1:1 module page, so it is listed beside
> `tests.md` in `test_docs_coverage.py`'s consolidated set.
>
> Sections 1–8 are **general**: true for any run of this shape. Section 9,
> [Current status](#9-current-status), is **deliberately separate**: a dated
> snapshot of the run in progress, which goes stale. Edit it in place and date
> every entry. Don't let run-specific facts leak into the general sections.

Run every command from the repo root (`night/`), prefixed with
`uv run --no-sync`.

---

## 1. What runs

| Process | Entry point | Role |
|---|---|---|
| owner | `live.owner_cli --out outputs` | the **only** process that places orders; holds the one account book and reconciles it against the venue every 30 s |
| day | `live.cli --strategy day --broker client` | DAY session 08:45–13:45; sends intents to the owner |
| night | `live.cli --strategy night --broker client` | NIGHT session 15:00–05:00 |
| fullday | `live.cli --strategy fullday --broker client` | both sessions, slug `day_night` |

Every deployment still logs in itself, because it needs the market feed to build
bars; only *orders* are delegated. That makes **four sessions on one
credential** ([cli.md](live/cli.md) "`--broker client`").

Each process idles outside its own sessions, so all four can run around the
clock. `simulation=True` is hardcoded in both entry points: no flag reaches a
production account.

## 2. Files under `outputs/`

| File | What it is | Read by |
|---|---|---|
| `fills_<slug>.csv` | the deployment's fill [ledger](live/ledger.md), every soak epoch; absent = never filled | owner start-up seed, `--resume` replay |
| `soak_epoch_<slug>.json` | the current [soak epoch](live/soak_epoch.md) and the retired ones | `--resume` / `--fresh-start` |
| `live_restart_<slug>.json` | [RestartGuard](live/startup.md) state: `run_in_flight`, backoff, backfills today, `last_heartbeat` | every start |
| `live_account_<slug>.json` | the account fingerprint | every start |
| `bars_cache_<slug>.csv` | warm-up bar cache, **rewritten only at start-up** ([seed_cache](live/seed_cache.md)) | every start |
| `intents_<slug>.jsonl` / `events_<slug>.jsonl` | the [wire](live/intents.md): client → owner intents; owner → client fills and `position` events (one a second) | the running pair |
| `live_<slug>_<timestamp>/` | one run's artifacts: `orders`, `fills`, `trades`, `signals`, `forecasts`, `ticks`, `params.json`, `session.jsonl` | review |
| `fatal.jsonl` | every fatal alert, all deployments in one file, **with no deployment field** | review |

**Never move, clean or rename `outputs/` while a soak continues.** `--resume`
and the owner's start-up reconciliation both depend on the files above.

## 3. Checking the state

```powershell
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'forecast_eval.live' } |
    Select-Object ProcessId, CreationDate, CommandLine
```

Each `uv run` shows up as three processes (the `uv` launcher, the venv shim and
the interpreter); that is normal.

| Question | Where to look | Healthy answer |
|---|---|---|
| Is anything held? | last line of `events_<slug>.jsonl` | `"kind":"position","signed_qty":0` means flat |
| Has it ever filled? | `fills_<slug>.csv` | absent means never |
| Has it traded? | `live_<slug>_<ts>/orders.csv`, `trades.csv` | a 40–110 byte file is a header only |
| Is it alive? | `last_heartbeat` in `live_restart_<slug>.json` | seconds old |
| What did it last do? | tail of `live_<slug>_<ts>/session.jsonl` | a `bar`, `relogin` or `verified` event |
| Is a session open? | the clock | DAY 08:45–13:45, NIGHT 15:00–05:00, weekdays. Saturday 00:00–05:00 is still Friday's night session |

## 4. Stopping cleanly

1. **Ctrl-C each deployment first**: day, night, fullday. Wait for each to print
   its summary block (feed, roll, relogin, ledger and alert counters) and exit.
   The same block is recorded at the end of its `session.jsonl`:
   `session_end`, `feed_summary`, `roll_summary`, `relogin_summary`,
   `ledger_summary`, `artifact_summary`.
2. **Ctrl-C the owner last.** It checks its stop flag *between* serve passes, so
   it never stops halfway through placing an order.
3. **Verify:** no `forecast_eval.live` process is left, and every
   `live_restart_<slug>.json` reads `"run_in_flight": false`.

Why it is done this way:

- **Ctrl-C, not the window's close button.** Ctrl-C runs the `finally` teardown
  in [live/cli.py](../live/cli.py): unsubscribe, summary, recorder close,
  logout, `guard.end`. A killed process leaves `run_in_flight: true`. The next
  start treats that as an unclean exit and applies a restart backoff: 60 s
  after a run longer than 600 s, doubling for repeated short-lived crashes, up
  to 1 h.
- **Owner last.** A [ClientBroker](live/broker/client.md) whose owner has gone
  quiet halts instead of reading the silence as a flat account.
- **Stopping never flattens.** Positions stay at the venue exactly as they
  were.

## 5. Restarting

**Owner first, in its own terminal**, then one terminal per deployment:

```powershell
uv run --no-sync python -m forecast_eval.live.owner_cli --out outputs --i-understand-this-places-orders
uv run --no-sync python -m forecast_eval.live.cli --strategy day     --broker client --resume --i-understand-this-places-orders
uv run --no-sync python -m forecast_eval.live.cli --strategy night   --broker client --resume --i-understand-this-places-orders
uv run --no-sync python -m forecast_eval.live.cli --strategy fullday --broker client --resume --i-understand-this-places-orders
```

**Required flags.** Order-placing runs need two separate things, and each
answers a different question:

| Flag | Required on | Without it | What it is |
|---|---|---|---|
| `--i-understand-this-places-orders` | the owner always; a deployment with `--broker shioaji` or `client` | exit 5 before login (`live/owner_cli.py:218`, `live/cli.py:247`) | an acknowledgement, not a setting: it changes nothing about how the run behaves |
| exactly one of `--resume` / `--fresh-start` | a deployment with `--broker shioaji` or `client` | exit 5 | does this start continue the soak or begin a new one (table below) |

The acknowledgement exists because order placement used to be **unreachable**:
`shioaji` was absent from `--broker`'s choices so that it "cannot be reached by
a mistake at 3 a.m.". Re-adding it as one more choice would have made it a
typo away, so it takes a second, deliberate flag (`live/cli.py:234`). `client`
needs it too, because routing orders through the owner is still placing
orders. The read-only brokers (`dryrun`, `sim`) don't need it.

What it does **not** do:

- **It does not choose simulation over production.** `simulation=True` is
  hardcoded in both entry points, and no flag reaches a production account.
  The orders are still real requests to Shioaji's simulation venue.
- **It is not a risk limit.** The loss rails are `--max-daily-loss-points` and
  `--kill-file`.

**When.** Start by about **08:30** for a DAY open. Each start logs in,
backfills and warms the model up before it trades. Starting early also keeps
the start-ups clear of the re-login windows (§7). Night and fullday can start
at the same time; night simply waits for 15:00.

**`--resume` or `--fresh-start`?** Exactly one is required with an
order-placing broker.

| | `--resume` | `--fresh-start` |
|---|---|---|
| soak epoch | continues the current one | retires it and starts a new one |
| a held position | adopted: replayed from the ledger and checked against the owner | **refused** (exit 5) unless *this deployment* is flat |
| use when | continuing the same test across a pause | after a flatten, or to begin a new test |

Every restart writes a **new** `live_<slug>_<timestamp>/` folder, so one soak's
artifacts are split across folders; the soak epoch is what ties them together.

**At start-up the owner seeds** each deployment's position from its
`fills_<slug>.csv`, summed across **all** soak epochs. It then checks the total
against the venue and **refuses (exit 6) on a disagreement**. Where the venue
won't answer, it warns and starts on the ledgers alone.

## 6. Scenarios

### 6.1 Weekend or overnight pause, account flat

Nothing is at risk.

- Stop as in §4, at any time outside a session.
- Don't flatten: there is nothing to flatten, and
  [flatten.py](live/flatten.md) needs session hours to price a close anyway.
- Restart as in §5 before the next open. `--resume` and `--fresh-start` both
  work on a flat deployment; `--resume` keeps the test one soak.

### 6.2 Pause while holding a position

Pick one.

- **Keep the position.** Stop as in §4 and restart later with `--resume`.
  Nothing manages the position in between: no strategy, no exit, no stop-loss.
  Over a weekend that changes little, since the market is closed, but the
  Monday gap lands on it unmanaged. This is the **recommended** path under the
  owner.
- **Flatten.** This must happen in session hours, with **all four processes
  stopped** first. Run `flatten --dry-run --out-parent outputs`, which is
  read-only and names whose lots are held, then the same command without
  `--dry-run`. It must exit 0.

> **Known gap: a flatten under the owner may block the restart. Unverified;
> traced in code, never run.** `flatten.py` closes whatever the *account* holds
> and writes its closing fills to **no** ledger. After a flatten, the
> `fills_<slug>.csv` files still claim the lots while the venue is flat, so the
> owner's start-up reconciliation
> ([owner_cli.py](../live/owner_cli.py) `main`, after `seed_nets`) should refuse
> with exit 6. `--fresh-start` on the deployments doesn't change the owner's
> seed, because it counts every epoch. `strategy/README.md`'s "flatten, then
> `--fresh-start`" predates the owner for this purpose. Until this path has been
> run, expect to reconcile the ledgers by hand after a flatten.

### 6.3 Leave it running through a weekend

This is supported. Each process idles outside its sessions, and the
[re-login scheduler](live/relogin.md) replaces the login token at every daily
anchor, weekends included, so the 24 h token expiry never arrives. Pause only to
free the GPU or the machine, or before a reboot.

### 6.4 Reboot, Windows Update, or a crash

All four die uncleanly.

- Each `live_restart_<slug>.json` still reads `run_in_flight: true`, so the next
  start waits out the backoff (§4), then starts normally.
- **Check positions (§3) before restarting.** If the venue moved while nothing
  was watching (an order resting at the venue that filled after the crash), the
  owner refuses with exit 6. That is a human decision, not a retry.
- Pause Windows Update for the length of a soak, or stop cleanly before an
  update you know about.

### 6.5 Contract expiry

TXF settles monthly, on the third Wednesday, and the deployments roll the
position before that ([roll.md](live/roll.md)). The bar cache is written only
at start-up, and an expired month can no longer be fetched from the broker.
**Restart every deployment shortly before each expiry** (`--resume` if it
holds), so the cache picks up the old month's bars while they are still
served. Otherwise a later warm-up has a hole where that month was.

### 6.6 A deployment refuses at start-up: `emitter priming incomplete`

The warm-up window held fewer than 3 non-HOLD signals, so the trend filter
cannot prime. The usual cause is a fresh `outputs/` without a seeded cache, or
a window too short to reach past a roll.

- **Fullday:** re-run with `--warmup-days 30`. Its default is 13 calendar days
  at stride 15.
- **Day / night:** seed the bar cache from the RPT tape with
  [seed_cache](live/seed_cache.md), or raise `--warmup-days`.

It refuses before trading, so a retry is safe.

### 6.7 Halts and exit codes

A running deployment's [rails](live/state.md) latch on the first problem and
print `HALTED: <reason>`:

| Reason (as printed) | Meaning | Action |
|---|---|---|
| `no market data for …s (limit …s)` | the feed stayed silent past the 900 s backstop, unhealed by [FeedWatch](live/feedwatch.md) | look for a network or Shioaji outage, then restart with `--resume` |
| `position mirror X != venue Y` | the deployment's position and the owner's `net_for(slug)` disagree | **do not restart blindly**: compare `fills_<slug>.csv`, `events_<slug>.jsonl` and the venue |
| `order rejected op_code=…` | the venue rejected an order | read the `op_msg` after it |
| `order rate … exceeds cap …` | a submit storm | treat it as a bug; read `orders.csv` before restarting |

| Exit | Process | Meaning | Action |
|---|---|---|---|
| 0 | either | clean | — |
| 1 | deployment | unhandled exception (also logged to `fatal.jsonl`) | read the traceback |
| 2 | either | credentials missing, or the owner's `--out` is not a directory | fix and restart |
| 3 | deployment | RestartGuard refused: backoff, 10 backfills per trading day, or quota under 50 MB | wait; the cap resets at 08:00 |
| 4 | deployment | **a FLATTEN halt's close did not fill**: a position is open with nothing watching it | the most serious state; act at once |
| 5 | either | refused before the first bar: flags, a non-flat `--fresh-start`, an unadoptable position | read the message |
| 6 | owner | ledgers and venue disagree at start-up | reconcile by hand (§6.2) |
| 7 | owner | `subscribe_trade` refused, so it cannot hear fills | don't start the deployments; retry the owner |

A **running** owner whose 30 s reconciliation fails stops as well, and every
deployment then halts on the silence.

### 6.8 Other work on the machine while VRAM is nearly full

Each deployment loads its own Toto2 copy onto the GPU; the owner uses none.
**High VRAM use is normal here and is not itself a problem.** PyTorch's
caching allocator keeps the peak it reached (mostly from the batched warm-up
priming) reserved for the life of the process. A live forecast is batch 1 with a
fixed context, so it reuses that reserved memory instead of asking for more.
Other programs, however, see that memory as taken.

**Why the peak is so large: warm-up sizes itself to the free VRAM.**
`LiveRuntime._prime_emitter` ([runtime.py](live/runtime.md)) runs up to 900
historical forecasts through `forecast_series_batch` with its default
`batch_size="auto"`. That sizes the batch to **80 % of the VRAM free at that
moment** (`Toto2Forecaster._auto_batch_size`), and nothing calls
`torch.cuda.empty_cache()` afterwards. The deployment started first therefore
claims most of the card, and the later ones over-commit it. Windows (WDDM)
then pages whole processes out of dedicated VRAM, into shared system memory or
out entirely. Read the **Total Committed** counter as well as Dedicated:
dedicated alone hides the over-commit. The start-up order doesn't fix this,
because whichever starts first wins. The fix is a code change: a fixed priming
batch, and/or `empty_cache()` after priming.

| Risk | Where it lands | How likely |
|---|---|---|
| The **other** program runs short of VRAM | that program fails or slows down | high if it needs more than what nvidia-smi reports free |
| A deployment's forecast hits CUDA OOM | **nothing catches it**: `forecast()` is called bare in `live/loop.py` `_on_forecast_boundary`; the error reaches `cli.py`'s `except Exception`, which logs `crashed` to `fatal.jsonl` and exits 1. Positions are left as they are, unmanaged | low, since forecasts reuse reserved memory, but not zero |
| Windows WDDM pages a deployment's VRAM to system RAM under pressure | forecasts slow down (normally ~0.09 s) | possible under a heavy GPU load such as a game |
| A deployment **restarts** while other work holds VRAM | `check_vram` refuses below **5 GiB free** (`MIN_FREE_VRAM_GIB`, `live/cli.py`), so it can't come back | certain if free VRAM is below 5 GiB at restart |

Rules of thumb:

- **Fine:** browser, Office, VS Code, video playback, and the test suite (it is
  GPU-free).
- **Avoid during sessions:** games and any other CUDA or ML work. Above all, a
  research backtest with `--precompute --batch-size auto`, which sizes its
  batch to fill free VRAM.
- **Outside sessions** (05:00–08:45, weekends) no forecast runs, so a crash is
  unlikely. The deployments must still restart into 5 GiB free, so close the
  heavy program before the next open.
- **If you need the GPU for real work, stop the simtrade first (§4).** That
  releases all of it, and it is the clean version of what an OOM crash would
  do anyway.

Read per-process use with Windows counters; `nvidia-smi` shows `N/A` per
process under WDDM:

```powershell
nvidia-smi --query-gpu=memory.used,memory.free --format=csv
(Get-Counter '\GPU Process Memory(*)\Dedicated Usage').CounterSamples |
    Where-Object CookedValue -gt 200MB | Sort-Object CookedValue -Descending |
    Select-Object InstanceName, @{n='MiB';e={[math]::Round($_.CookedValue/1MB)}}
```

The instance name carries the PID. Ignore `dwm`: its counter includes other
processes' shared surfaces and can read above the card's total.

## 7. The daily timeline

| Time | What happens | Watch for |
|---|---|---|
| 08:00 | trading day and quota roll; the backfill cap resets | — |
| 08:37–08:39 | owner re-login | `relogin_failed` (it retries 60 s later, inside the window) |
| 08:40–08:44 | all deployments re-login **in the same second** | `relogin_failed`, `VERIFY_FAILED` |
| 08:45 | DAY opens | first bars arrive; no `feed_*` alerts |
| 13:45 | DAY closes | — |
| 14:52–14:54 | owner re-login | as at 08:37 |
| 14:55–14:59 | deployments re-login | as at 08:40 |
| 15:00 | NIGHT opens | as at 08:45 |
| 05:00 | NIGHT closes; silence until 08:45 is expected | — |

The re-login spike can briefly mean up to 7 connections against Shioaji's
**5 per `person_id`** cap, because `force_refresh=True` opens the new connection
about 30 s before the old one drops. The owner goes first so its spike clears
before the deployments'. What Shioaji does above 5 has never been measured.

## 8. Standing cautions

- **One account, one book.** The owner nets every deployment, so the account
  can hold up to one lot per deployment in the same direction.
- **No exit rules, no stop-losses, no forced close** (`forced_close=False`).
  Positions ride through other sessions, nights and weekends.
- **Four logins share the 500 MiB/day data allowance**; the cost of four tick
  subscriptions is unmeasured. A deployment checks the quota **only at
  start-up** (it refuses below 50 MB); the owner samples it on its 30 s cadence
  and warns below 50 MB but does not stop. **No per-run traffic figure exists:**
  [cli.md](live/cli.md) describes a teardown `traffic this run` report and
  `traffic_start` / `traffic_end` records, but `live/cli.py` does not
  implement them (it imports `usage_bytes` and never calls it). Found
  2026-10-03 during the pause test.
- **Consoles print the account's `person_id`**, a national ID. Read output
  before pasting it anywhere.
- **The host clock must run on exchange local time (TST).** The session and
  staleness checks compare wall clock against exchange hours.
- **Three deployments under one owner, for full sessions,** has no test before
  the 2026-09-29 simtrade (STRATEGY_SPLIT_PLAN §23 lifted the one-instance rule
  for 2 × 1 lot × 10 minutes).

## 9. Current status

> **Snapshot, not reference.** Everything in this section describes one run at
> one time. Update it in place, date every entry, and move anything that turns
> out to be generally true up into §§1–8.

**Run:** `simtrade_0929`, branch `simtrade_0929` (local, not pushed), launched
2026-09-29 08:20–08:25 into a fresh `outputs/` with `--fresh-start`. Settings
are in `strategy/README.md` and decisions in `SIMTRADE_0929_PLAN.md`, both on
that branch only.

| Deployment | Source | Notable |
|---|---|---|
| day | `day_split_iter02` | band ±0.001, `min_prob` 0.65, stride 30 |
| night | `night_iter10` | +0.00065 / −0.00045, `min_prob` 0.55, **long-only**, stride 30 |
| fullday | `fullday_split_iter07` | band ±0.001, `min_prob` 0.50, stride 15 |

**2026-10-03 09:15: running again after a clean pause/resume test (log
below).** Same soak epochs; new run folders `live_<slug>_20261003_0912xx`. The
snapshot that follows is from before the pause.

**As of 2026-10-03 (Sat) 08:40:**

- All four processes alive since 09-29; Saturday's 08:40 re-login succeeded for
  all three deployments.
- **Account flat; zero orders ever placed.** No `fills_*.csv` exists; every
  `orders.csv` and `trades.csv` is a header only.
- Signals so far: day 40 HOLD; night 108 HOLD + 3 SELL (no-ops: long-only and
  flat); fullday 302 HOLD.
- Soak epochs: day `soak_20260929_082537_462229`, night
  `soak_20260929_082008_180784`, fullday `soak_20260929_082029_100149`.

**Next dates:**

| Date | What |
|---|---|
| Mon 2026-10-05, by 08:30 | restart if paused over the weekend (§5) |
| about 2026-10-19 / 10-20 | restart all deployments before the October expiry (§6.5) |
| 2026-10-20 / 10-21 | deployments roll; October contract settles |

**Log:**

| Date | Event |
|---|---|
| 2026-09-29 08:20 | day crashed at start-up with `emitter priming incomplete` (2/3 samples, 5142 bars); day and night caches seeded from the RPT tapes; day relaunched 08:25 |
| 2026-10-01 08:40 | one deployment's re-login timed out (`token_login`, 30 s); recovered on retry |
| 2026-10-03 08:40 | status snapshot above; weekend pause being considered |
| 2026-10-03 08:59 | VRAM 29.5 of 32.6 GB used, 2.6 GB free. Dedicated per process: fullday 11.5 GB, night 8.5 GB, day 5.5 GB (25.5 GB together, against ~4.4 GiB each measured cold in §2.7); the rest is desktop apps |
| 2026-10-03 09:09 | **pause test, stop half: passed.** Ctrl-C in order day 09:09:26 → night 09:09:35 → fullday 09:09:42 → owner (last event 09:09:56). All `run_in_flight: false`, `consecutive_failures: 0`; soak epochs unchanged; every `session.jsonl` ends with the full teardown block; VRAM fell to 4.2 GB used. Run totals: day 1200 bars / 40 forecasts, night 3318 / 111, fullday 4518 / 302; 0 fills replayed or appended; relogins 9 each (day 1 failure, recovered) |
| 2026-10-03 09:11–09:15 | **pause test, resume half: passed.** `--resume`, owner 09:11:50 → day 09:12:01 → night 09:12:28 → fullday 09:13:02. Soak epochs unchanged; `run_in_flight: true`, 0 failures, backfills_today 1 (new trading day); same account digest; all primed with 4 samples (fullday on its default 13-day window, 12 581 bars); subscribed `TXFJ6`; all flat; no new `fatal.jsonl` entry |
| 2026-10-03 09:15 | **VRAM over-committed after the resume** (§6.8): 560 MB free. Dedicated / shared / committed: day 0.08 / 0 / 22.8 GB, night 19.4 / 3.4 / 22.8 GB, fullday 10.2 / 7.4 / 17.7 GB, i.e. 63 GB committed on a 32 GB card. Day is almost fully paged out. Effect on Monday's forecast latency unmeasured; committed figures were not read before the pause, so whether the 09-29 launch was the same is unknown |

**Open questions:**

- **No orders in four days across three deployments.** Day's `min_prob` 0.65
  gate and night's long-only rule are the first suspects; fullday's 302 HOLDs
  at `min_prob` 0.50 point at the band or the trend filter instead.
- **The flatten → restart path under the owner (§6.2) is unverified.**
