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
> Sections 1–10 are **general**: true for any run of this shape. Section 11,
> [Current status](#11-current-status), is **deliberately separate**: a dated
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
| `fatal.jsonl` | every fatal alert, all deployments in one file, **with no deployment field**; the owner's alerts go to `owner_session.jsonl` instead | review |
| `telegram_level.json` | the Telegram alert level chosen with `/settings`, shared by all four processes (§9); absent = the `[telegram]` default | every alert |
| `owner_session.jsonl` | the owner's trail, appended across runs: placements with their `ordno` and slug, deals, order events, pending/failed reconciles, freezes, anomalies (since 2026-10-07) | review |

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
| Is it alive? | `last_heartbeat` in `live_restart_<slug>.json`; to the owner, the last `heartbeat` in `intents_<slug>.jsonl` (every 30 s, since 2026-10-07) | seconds old; under 30 s |
| What did it last do? | tail of `live_<slug>_<ts>/session.jsonl` | a `bar`, `relogin` or `verified` event |
| Is a session open? | the clock | DAY 08:45–13:45, NIGHT 15:00–05:00, weekdays. Saturday 00:00–05:00 is still Friday's night session |

## 4. Stopping cleanly

1. **Ctrl-C each deployment first**: day, night, fullday. Wait for each to print
   its summary block (feed, roll, relogin, ledger, alert and Telegram
   counters) and exit.
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

**A held lot resumes under a `stop_pct`** (since 2026-10-14). Adoption seeds
the mirrors from the venue's average entry, and `EntryPercentStop` reads only
side and entry price, so the stop is armed at the true entry from the first
bar. `adopt_position` still refuses (exit 5) any exit rule that is not
`adoptable` — one that counts bars held, since the entry bar index belongs to
the previous process ([runtime.py](live/runtime.md) `adopt_position`,
[exits/base.py](exits/base.md)). From 2026-10-12 to 10-14 every rule was
refused, so a held lot had to be flattened.

Every restart writes a **new** `live_<slug>_<timestamp>/` folder, so one soak's
artifacts are split across folders; the soak epoch is what ties them together.

**At start-up the owner seeds** each deployment's position from its
`fills_<slug>.csv`, summed across **all** soak epochs. It then checks the total
against the venue and **refuses (exit 6) on a disagreement**. Where the venue
won't answer, it warns and starts on the ledgers alone.

**Neither side replays the wire** (since 2026-10-07). A restarted owner skips
the intents already in `intents_<slug>.jsonl` and prints how many. A restarted
deployment skips the old `events_<slug>.jsonl` and keeps only the owner's latest
`position`. Before that, an owner restart re-placed every intent ever sent, and
a deployment restart re-booked every fill on top of its ledger replay.

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
  owner. A declared `stop_pct` resumes armed at the venue's entry (§5); only
  an exit rule that is not `adoptable` refuses (exit 5) and forces a flatten.
- **Flatten.** This must happen in session hours, with **all four processes
  stopped** first. Run `flatten --dry-run --out-parent outputs`, which is
  read-only and names whose lots are held, then the same command without
  `--dry-run`. It must exit 0.

> **Known gap: a flatten under the owner may block the restart. Unverified;
> traced in code, never run.** `flatten.py` closes whatever the *account* holds
> and writes its closing fills to **no** ledger. After a flatten, the
> `fills_<slug>.csv` files still claim the lots while the venue is flat, so the
> owner's start-up reconciliation
> ([owner_cli.py](../live/owner_cli.py) `main`, after `seed_positions`) should refuse
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

**The owner follows each deployment's month** (fixed 2026-10-24 on `main`,
after the 10-20 roll). Every intent names its month, and the owner places it
there, so the owner needs no restart at a roll.

**A restarted owner seeds each lot on the month its ledger row names** (also
2026-10-24 on `main`). Each `fills_<slug>.csv` row carries a `contract` column,
written by the deployment. The owner's own `--contract` now only covers rows
with no month. A deployment gives its old rows the month it holds the first
time it starts on this code ([ledger](live/ledger.md)). Until every deployment
has done that, the old rule still applies: **between a deployment's roll and
the old month's expiry, restart the owner with the dated code the deployments
hold** (e.g. `--contract TXFK6`; `TXF202611` does not resolve). `TXFR1` still
names the expiring month in that window, and an unstamped seed is then refused
with `account mirror … != venue …`.

**The deployments still need `--contract` in that window.** A deployment
resolves its own month from `--contract` alone, so one restarted on `TXFR1`
between its roll and the expiry would trade the old month (and stamp its old
rows with it).

**A missed roll no longer leaves the owner with a desync.** If the scheduled
roll lapses and `RollWatch` catches the expiry, the deployment settles its
mirror, writes the settlement to its ledger, and sends the owner a `settle`
intent. The owner takes the lot off its book before the re-open is sized
([owner_process](live/owner_process.md)), and its reconcile does not compare a
month that has expired. One case is not covered: a deployment that is **not
running** at the expiry sends no `settle`. Restart the owner before that
deployment, and the restarted owner drops the expired lot when seeding.

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
| `position mirror X != venue Y` | the deployment's own position and the owner's `net_for(slug)` disagree. On code before 2026-10-07 09:00, X was the sum of **every** ledger, so a peer's lot alone triggered it | **do not restart blindly**: compare `fills_<slug>.csv`, `events_<slug>.jsonl` and the venue |
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
| 6 | owner | ledgers and venue disagreed: refused at start-up, or a running owner **froze** and was then stopped | reconcile by hand (§6.2, §6.9) |
| 7 | owner | `subscribe_trade` refused, so it cannot hear fills | don't start the deployments; retry the owner |

A **running** owner whose 30 s reconciliation fails **freezes**
(`RECONCILIATION FAILED — OWNER FROZEN`). It places nothing and rejects every
submit, so each deployment halts when it next tries to trade. It keeps booking
deals and publishing positions until you Ctrl-C it. A difference that orders
in flight explain is not a failure for up to 120 s; it prints
`reconcile pending: …`.

### 6.8 Other work on the machine while VRAM is nearly full

Each deployment loads its own Toto2 copy onto the GPU; the owner uses none.
**Expect about 4.2 GiB per deployment, about 13 GiB for three.** Warm-up
priming runs at a fixed batch of 32 and then releases the GPU cache
([runtime.md](live/runtime.md), `PRIME_BATCH_SIZE`). A live forecast is batch
1 with a fixed context and needs nothing beyond the model (measured 104 ms,
4.22 GiB).

**Code without that fix sizes the warm-up to the free VRAM.** Before
`PRIME_BATCH_SIZE`, `_prime_emitter` ran its up-to-900 forecasts with
`batch_size="auto"`, which takes **80 % of the VRAM free at that moment**, and
nothing released it afterwards. The first deployment started claimed most of the
card and the later ones over-committed it. MEASURED 2026-10-03: 63 GB committed
on a 32 GB card, with one deployment paged off the GPU entirely. A checkout
whose `live/runtime.py` lacks `PRIME_BATCH_SIZE` still behaves this way, so check
before relying on the 4.2 GiB figure. Read Windows' **Total Committed** counter
as well as Dedicated: dedicated alone hides an over-commit.

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

### 6.9 A fill the ledgers never got (owner refuses with exit 6)

The 2026-10-06 shape: an order filled at the venue, but the fill never reached
its deployment's `fills_<slug>.csv`. The start-up then refuses with
`account mirror N != venue M`.

1. **Stop everything** (§4). Run
   `flatten --dry-run --show-orders --out-parent outputs` (read-only). It lists
   this session's orders with `ordno`, side, price and `dealt`, plus the
   account's position.
2. **Find whose order it was.** On code from 2026-10-07 on, read `owner_placed`
   in `owner_session.jsonl` (it carries `ordno` and `slug`). Before that, the
   only record is the deployment whose `intents_<slug>.jsonl` holds a matching
   submit.
3. **Add the row by hand** to that deployment's `fills_<slug>.csv`, with that
   deployment's current `soak_id`
   (`timestamp,side,fill_price,quantity,fee,soak_id`; fee = price × 0.0002 ×
   qty). Keep a copy of the file first.
4. Restart as in §5 with `--resume`. The owner's seed now matches the venue,
   and the deployment adopts the lot from its ledger.

Or flatten instead (§6.2) and leave the ledgers alone. Expect §6.2's known
gap: the ledgers still claim lots the venue no longer holds.

### 6.10 A paper candidate beside the live deployments

**Use case:** forward-testing a candidate config on new data before adoption. It
runs on the same market feed as live, but **places nothing and never touches
the owner or the account.**

**Shape:** `--broker dryrun`, run from the candidate's own worktree (for
example `night/fullday_cand34/`). That worktree also gets its own `outputs/`,
and four things follow from that:

- **Ledgers and state are separate.** Every file in §2 is keyed by the run
  folder's parent, so the candidate gets its own `fills_<slug>.csv`, restart
  guard, soak epoch and bar cache, even with the same slug as a live deployment
  (fullday is `day_night`).
- **Fills are modelled the backtest's way.** The dry-run broker fills from the
  live prints with the backtest's fill model (SPEC §4.5), so its record is the
  forward-test number to compare with the backtest.
- **No owner involvement.** The owner neither seeds nor reconciles the
  candidate, and the candidate's fills never reach the account.
- **It costs one more login.** That is a fifth session on the deployment's
  credential: about 12 MB/day of the 500 MiB traffic allowance
  ([cli.md](live/cli.md), "The traffic meter"). A model resident in VRAM
  takes ~4.1 GiB, and `check_vram` wants 5 GiB free at start.

**One-time setup**, in the candidate worktree:

1. Copy `forecast_eval/config/secrets.toml` from the main checkout into the
   worktree's `forecast_eval/config/`. It is git-ignored, so a worktree starts
   without it, and the CLI reads it from the package's own `config/`.
2. Copy `forecast_eval/CLAUDE.md` too (also ignored).
   `test_docs_coverage.py::test_internal_doc_links_resolve` fails without it.

**Start** (PowerShell). **The `cd` is not optional:** `python -m` imports
`forecast_eval` from the current folder, and the default `--out` is relative to
it.

```powershell
cd D:\Users\squirrel\Documents\paper\myPaper\forecast_eval_folder\night\fullday_cand34
uv run --no-sync python -m forecast_eval.live.cli --strategy fullday --broker dryrun --resume --warmup-days 15 --no-telegram
```

**Check the banner before leaving it running.**
- **Right folder:** the `LiveConfig[...]` line shows the candidate's settings
  (for the iter34 candidate, `stride=10 ... min_prob=0.5`). On a first start
  there is no `ledger:` line and no `resume: N inherited fill(s)` line.
- **Wrong folder (`night\`):** the same command runs the **main checkout's**
  config and writes into **live's `outputs\`**: live's ledger, restart guard,
  bar cache and run folders. Its paper fills would land in the ledger the
  owner seeds live from.
  - **Happened 2026-10-26 11:50,** caught before any fill. Signs: banner
    `stride=30 ... min_prob=0.65`, `ledger: 6 row(s) without a month stamped
    TXFK6`, `resume: 6 inherited fill(s)`.
  - **Recovery:**
    1. Ctrl-C.
    2. Confirm `fills_appended 0` in the summary, or remove any paper rows
       from the live ledger.
    3. Confirm `run_in_flight: false`.
    4. Move the stray `live_<slug>_<ts>/` folder out of live's `outputs\`.

- **`--no-telegram`:** its alerts would carry the same deployment name as live
  fullday. Read its state from its own `outputs/` (§3) instead.
- **`--warmup-days`:** must cover the emitter's prime at the candidate's
  stride. The default is sized for 500 boundaries, while the prime replays up
  to 900. For a stride-10 fullday that is 10 calendar days by default and 15
  for the full 900. Measured on the stride-10 backtest's forecasts: 900
  boundaries never fell below 15 band-clearing samples in 2026 (floor 3); 500
  did in 0.4 % of windows.
- **`--resume`:** replays its own ledger and adopts the paper position.

**Restart limit: a held paper position cannot be resumed under a stateful
exit rule.** `adopt_position` refuses (exit 5) an exit rule that is not
`adoptable`, and fullday's break-even lock is not: its best price belongs to
the dead process. So:

- **Restart the candidate only while it is flat.** Leave it running through
  weekends and pauses (§6.3); it idles outside its sessions.
- **If it must stop while holding,** restart it without `--resume`. It then
  starts flat, and the forward record has a gap at that point, which must be
  noted where the result is read.

**Rolls:** it rolls on its own, like any deployment (§6.5), through the same
runtime path. A trader with entry filters needs the runtime's `rolling()` hook
([runtime.md](live/runtime.md), "Roll legs and trader filters").

## 7. The daily timeline

| Time | What happens | Watch for |
|---|---|---|
| 08:00 | trading day and quota roll; the backfill cap resets | — |
| 08:37–08:39 | owner re-login | `relogin_failed` (it retries 60 s later, inside the window) |
| 08:40–08:44 | deployments re-login 40 s apart: day 08:40:00, night 08:40:40, fullday 08:41:20; all verify at 08:44 | `relogin_failed`, `VERIFY_FAILED` |
| 08:45 | DAY opens | first bars arrive; no `feed_*` alerts |
| 13:45 | DAY closes | — |
| 14:52–14:54 | owner re-login | as at 08:37 |
| 14:55–14:59 | deployments re-login (14:55:00 / 14:55:40 / 14:56:20) | as at 08:40 |
| 15:00 | NIGHT opens | as at 08:45 |
| 05:00 | NIGHT closes; silence until 08:45 is expected | — |

A `force_refresh=True` login opens its new connection about 30 s before the old
one drops, against Shioaji's **5 per `person_id`** cap with four processes
connected. Until 2026-10-18 the three deployments re-logged in the same second,
briefly up to 7. They are now 40 s apart (`relogin.DEPLOYMENT_STAGGER`), so each
spike clears before the next and the peak is 5; the owner still goes first.
**Above 5 was measured on 2026-10-26** (a holiday, every live process stopped):
`multi_deploy_probe --mode sessions --sessions 6` opened 6 simulation logins,
`usage()` reported `connections=6`, and the first session was still usable.
So a sixth connection is neither refused nor evicts an older one, which is the
peak a fourth deployment (hybrid) makes during each re-login. 7 or more is
still unmeasured.

## 8. Reports while the simtrade runs

Two reports can be generated at any time while all four processes run. Neither
loads the model or touches the GPU, and neither sends anything to the venue.

| | Status page | Per-run report |
|---|---|---|
| Answers | how all the soaks are doing right now: running, data, decisions, position, logins, hardware | one deployment's trading result: equity and drawdown, fills on the price line, signals against realized PnL, settings |
| Command | `live.status_report` | `forecast_eval.reports <run_dir>` |
| Reads | `outputs/`, `shioaji.log`, process list, Windows GPU counters | the run folder's CSVs and `params.json`, plus every `ticks.csv` of the soak |
| Writes | `night/simtrade_status_report_<YYYYMMDD_HHMMSS>.html` only (and `--json` if given); a new file per run | `<run_dir>/report.html` and `<run_dir>/report/*.png`; the price cache `outputs/prices_<slug>.csv` |
| Time (2026-10-03) | about 6 s | about 1 s for a run with no trades yet |

### 8.1 The status page — `live/status_report.py`

```powershell
uv run --no-sync python -m forecast_eval.live.status_report
# options: --outputs <dir> (default outputs), --log <path> (default shioaji.log),
#          --output <html> to choose the file, --json <file> to dump the data,
#          --now <ISO> to pin the time (the filename stamp follows it)
```

It prints the key findings to the console and writes one self-contained HTML
page, `simtrade_status_report_<YYYYMMDD_HHMMSS>.html` at the repo root, stamped
with the report time. Each run keeps its own page, so earlier pages stay
available for comparison. Nothing deletes them; clear old ones by hand. The findings are fixed rules with thresholds, not commentary: VRAM ≥ 85 %,
commit charge ≥ 80 %, a heartbeat older than 5 min, any lost minute (a minute
with a trade on the tape but no bar), failed or unconfirmed re-logins, and SDK
errors. See [status_report.md](live/status_report.md) for where each number
comes from.

Read it with these limits in mind:

- **Counts cover the whole soak:** every `live_<slug>_*` folder with the
  soak's id, oldest first, so a restart doesn't reset them. Fills, trades and
  PnL come from the newest folder, which holds the replayed history
  ([status_report.md](live/status_report.md)). Before 2026-10-03 only the
  newest folder was read, and a fresh `--resume` showed 0 forecasts.
- **"Up" is the soak's age, not the process's.** It counts from
  `soak_epoch_<slug>.json`'s `started_at`, the same span the counts cover. It
  read "4d 1h" for processes started ten minutes earlier.
- **Logins** are still counted per *current* PID from `shioaji.log`, so the
  login count resets at a restart. Re-login events and failures come from
  `session.jsonl` and cover the soak.
- **Hardware is a single snapshot** taken when the page is built; nothing keeps
  a history.
- It never imports a strategy or the broker, and nothing the live processes run
  imports it, so generating it cannot change a running soak.

### 8.2 The per-run report — `reports.py`

Pick the deployment's newest run folder, then render it:

```powershell
$run = (Get-ChildItem outputs -Directory -Filter 'live_night_2*' | Sort-Object Name | Select-Object -Last 1).FullName
uv run --no-sync python -m forecast_eval.reports $run
```

Use `live_day_2*` / `live_day_night_2*` for the other two. The trailing `2`
keeps `live_day_*` from also matching `live_day_night_*`. Open
`<run_dir>\report.html`.

How a running folder feeds it:

- **The live process refreshes the CSVs** (`fills`, `orders`, `forecasts`,
  `signals`, `trades`) at most **once a minute**, and only when something
  changed ([artifacts.md](live/artifacts.md)). The report is therefore up to a
  minute behind, and during 05:00–08:45 and weekends nothing changes at all. If
  a render fails on a CSV caught mid-write, run it again.
- **The price line spans the whole soak**, not just this folder
  ([prices.md](live/prices.md)): every `ticks.csv` of the soak, one row a
  minute. Finished segments are cached in `outputs/prices_<slug>.csv`; the live
  one is re-read each time.
- **Fills and trades include earlier segments** after a `--resume`, because the
  [ledger](live/ledger.md) replays them into the new run. Signals and forecasts
  are this folder's only.
- **A clean stop writes the final report itself** (`report=True` at teardown,
  [cli.py](live/cli.md)), overwriting any copy rendered by hand. It is the same
  renderer, so nothing is lost.
- A stopped run's folder already holds its shutdown report. Re-rendering it works
  the same way.

### 8.3 Not while running: `tools/session_artifacts.py`

[session_artifacts](tools/session_artifacts.md) rebuilds artifacts from
`session.jsonl` for runs that ended **before** live runs wrote their own. It
refuses a folder that already has `fills`/`trades`/`signals` CSVs, which every
current run does. Its `--seed-ledger` appends to the live fill ledger, which
**must never** happen while a deployment is writing to it.

## 9. Alerts on your phone

All four processes send their alerts to one Telegram chat. The full manual —
every alert and what to do about it, delivery, troubleshooting — is
[notifications.md](notifications.md). Each message starts with who sent it:
`[owner]`, `[day]`, `[night]` or `[fullday]` (the `--strategy` name, not the
`day_night` file slug). The alerts also go to the
console and the trails, so a dead Telegram loses nothing.

**Setup, once.** Fill in `[telegram]` in `config/secrets.toml` (`bot_token`
from @BotFather, `chat_id` from `live.telegram --find-chat`), then send a test
message:

```powershell
uv run --no-sync python -m forecast_eval.live.telegram --find-chat
uv run --no-sync python -m forecast_eval.live.telegram
```

A section still holding `REPLACE_ME` doesn't stop a start: it prints a
`telegram_disabled` warning and runs without Telegram. `--no-telegram` turns it
off for one process.

**What arrives:**

| Level | From the deployments | From the owner |
|---|---|---|
| FATAL | any rails halt, `flatten_incomplete`, a refused start, `crashed`, no credentials, `session_unverified`, an unsettled roll | `owner_frozen`, a refused start (exit 6 or 7), `crashed`, no credentials, `session_unverified` |
| WARN | `feed_missed` / `feed_unreachable`, `session_down`, `relogin_failed` / `relogin_abandoned`, `session_relogin_failed`, `account_changed`, `contract_rolled`, roll trouble | `deployment_silent`, `traffic_low`, `venue_unanswered`, `relogin_failed` / `relogin_abandoned` |
| TRADE | `position_opened`, `position_closed` with this trade's PnL | |
| INFO | `resumed`, `relogin`, `session_relogin`, `roll_started` | `relogin` |
| recovery | `feed_recovered`; `relogin`, `session_relogin`, `roll_started` after a failure | `owner_agrees_again`; `relogin`, `session_relogin` after a failure |

A recovery is WARN when it ends a warning and INFO otherwise
([notifications.md §6.5](notifications.md)), so a setting that showed the
problem shows the all-clear too.

The owner's console repeats its traffic and silent-deployment warnings every
30 s; it alerts only when they change.

**Choosing the level.** Send `/settings` to the bot and press *All (INFO)*,
*Trades + WARN + FATAL*, *WARN + FATAL* or *FATAL only*. The choice is saved in
`outputs/telegram_level.json` and applies to **all four processes**, without a
restart, and it is kept across restarts. FATAL is always sent. The default is
INFO, which means a message per fill plus a `relogin` notice from each process
before every open (08:40 and 14:55, weekends included): eight a day across the
four. *Trades + WARN + FATAL* keeps the fills and drops those.

**After a stop**, each process prints its `telegram_*` counters with the rest
of its summary (§4). `telegram_failed` above 0 means messages were not
delivered, and is how a dead transport differs from a quiet night.
`telegram_poll_conflicts` grows normally: four processes share one bot and take
turns reading button presses, so a press can take up to about a minute to be
answered.

## 10. Standing cautions

- **One account, one book.** The owner nets every deployment, so the account
  can hold up to one lot per deployment in the same direction.
- **No forced close** (`forced_close=False`); a stop-loss only as the package
  declares one. Each session package's `PARAMS` carries a `stop_pct` knob — an
  entry-anchored `EntryPercentStop`, checked every bar against the entry price,
  closing via a marketable limit. Where it is `None` (`main`'s default) there
  is no exit at all and positions ride through other sessions, nights and
  weekends; where a width is declared, a held lot resumes with the stop armed
  at the venue's entry (§5), and a crash leaves the position unmanaged until
  the restart's first bar. Which
  branch declares which width is recorded in each package's `wiring.py`.
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

## 11. Current status

> **Snapshot, not reference.** Everything in this section describes one run at
> one time. Update it in place, date every entry, and move anything that turns
> out to be generally true up into §§1–10.

**Run:** `simtrade_0929`, branch `simtrade_0929` (local, not pushed), launched
2026-09-29 08:20–08:25 into a fresh `outputs/` with `--fresh-start`. Settings
are in `strategy/README.md` and decisions in `SIMTRADE_0929_PLAN.md`, both on
that branch only.

| Deployment | Source | Notable |
|---|---|---|
| day | `day_split_iter02` | band ±0.001, `min_prob` 0.65, stride 30 |
| night | `night_iter10` | +0.00065 / −0.00045, `min_prob` 0.55, **long-only**, stride 30 |
| fullday | `fullday_split_iter07` | band ±0.001, `min_prob` 0.50, stride 15 |

**2026-10-26: live fullday adopts `fullday_rev_iter34`.**
`candidate_fullday_iter34` was merged into `simtrade_0929` (user decision), and
fullday trades it from the 2026-10-27 restart.

- **Config:** stride 10, `entry_min_prob` 0.60, stop 0.05, `lock_after_pct`
  0.03, `reentry_cooldown_signals` 16. Details: `strategy/README.md`.
- **Live roll fixes it carries:** the `rolling()` hook (main `825f069`); the
  cooldown lets a roll's re-open through; the re-open keeps the original entry.
- **Restart rule:** fullday **cannot `--resume` into a held lot**, because the
  lock is not adoptable (exit 5). Stop it only while flat, or flatten first
  and use `--fresh-start`.
- **The held-out numbers are optimistic.** The config was chosen with the
  held-out window (`autoresearch_trading_plan_fullday_reversal.md`, phase 2
  Result), so this soak is its first clean test.
- **The 10-27 start** follows "Resuming on the 2026-10-24 code" below, with
  two differences for fullday:
  - **The old strategy's records are deleted.** On 2026-10-26 they were moved
    out of `outputs/`, and the archive was then deleted at the user's request:
    - `fills_day_night.csv` (6 rows, net 0);
    - `soak_epoch_day_night.json`;
    - the wire: `intents_day_night.jsonl` and `events_day_night.jsonl`;
    - `prices_day_night.csv`;
    - the six `live_day_night_*` run folders;
    - the `_backup_roll_entryfix_20261024` copies.

    Kept in `outputs/`: `bars_cache_day_night.csv` (warm-up market data) and
    the restart and account state files. The owner therefore seeds
    `day_night` as flat (no ledger). Back up `fills_day.csv` and
    `fills_night.csv` as step 3 says.
  - **Fullday starts with `--fresh-start`, not `--resume`.** It is flat, and a
    new soak epoch keeps the new config's record apart from the old one's:

    ```powershell
    uv run --no-sync python -m forecast_eval.live.cli --strategy fullday --broker client --fresh-start --i-understand-this-places-orders
    ```

    Expect the banner to read `stride=10 ... min_prob=0.5`, with no
    `resume:` line.
  - **No `--contract` on any of the four.** The October contract expired on
    10-21, so `TXFR1` now resolves to November: both 2026-10-26 start-ups
    printed `contract TXFR1 -> ... delivery_month=202611`. Steps 4–5 below
    were written with `--contract TXFK6`, which is equivalent now; it can be
    dropped. Night's ledger is still unstamped, so its first start stamps its
    rows 202611, the month it holds.
  - **`--contract` is needed again only in the roll window** (11-17 12:45 →
    11-18 13:30, table above). There, a restarted deployment that has rolled
    takes `--contract TXFL6`, and so does the owner while any seed row is
    unstamped (§6.5).
- **Superseded:** the paper candidate (§6.10, worktree `night/fullday_cand34/`,
  started 2026-10-26 11:56) runs the same config, so it duplicates live
  fullday from here on.

**2026-10-12 ~05:50: not running.** All four processes have been dead since
2026-10-08 ~21:35 — an abrupt kill, no teardown — with three lots held
unmanaged at the venue; a clean restart is planned (see the log).

**2026-10-05: night replaced by `night_iter30`** (`4cbc1e6`, branch `autoresearch_night_1004`): band ±0.0004, `min_prob` 0.6, `min_trend_ticks` 10 (emitter default), stride 15, **two-sided**; Calmar 7.2974, 376 trades (whole data). The night row above, and the observations below dated before it, describe `night_iter10`. Current settings: `strategy/README.md`.

**2026-10-03 09:51: running, with the warm-up VRAM fix** (`simtrade_0929` @
`7e57912`, `main` merged in). Same soak epochs; latest run folders
`live_<slug>_20261003_0949xx/0950xx`. The snapshot that follows is from before
the first pause.

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
| Mon 2026-10-12, 08:45+ | flatten the three held lots, then the clean restart (log, 2026-10-12) |
| about 2026-10-19 / 10-20 | restart all deployments before the October expiry (§6.5) |
| 2026-10-20 / 10-21 | deployments roll; October contract settles |
| Sat–Sun 2026-10-24 / 10-25 | **done 10-24:** the three roll defects and both follow-ups fixed on `main`; night's and fullday's entry prices corrected. Resume with the steps below |
| until the 11-17 roll | restart with `--contract TXFK6`. October expired 10-21 13:30, so `TXFR1` should name November too, but the dated code removes the doubt |
| about 2026-11-16 / 11-17 morning | restart all deployments before the November expiry, for the bar cache (§6.5) |
| 2026-11-17 12:45 / 15:30 | day and fullday, then night, roll `TXFK6` → `TXFL6` (December). First live run of the roll fixes |
| 11-17 roll → 11-18 13:30 | a deployment that has rolled restarts with **`--contract TXFL6`**, not `TXFK6` (§6.5). The owner needs no `--contract` once its seed is all stamped (step 7 below) |

**Resuming on the 2026-10-24 code** (written 2026-10-24; no `forecast_eval.live`
process was running then). This is the first start on the ledger-month code,
which rewrites the ledgers, and the first on any of the roll fixes.

1. **Commit the 10-24 follow-ups on `main`,** then run the suite there.
   *Done 2026-10-24:* `a45357d`, 1716 passed, 2 skipped.
2. **Merge `main` into `simtrade_0929` and test the branch.** *Done
   2026-10-24*; the only conflict was this section. Run the processes from a
   `simtrade_0929` checkout, not `main`.
3. **Before starting anything:**
   - Confirm nothing is running (§3), or stop it (§4: deployments first, owner
     last).
   - Read each deployment's position (§3): the last `position` line in
     `events_<slug>.jsonl`, and `fills_<slug>.csv`. Per the 10-24 log row, the
     re-priced lots (night +1 @ 46581, fullday −1 @ 46588) had already closed;
     the nets were **night −1 @ 45621, fullday flat**. Day's net is not in that
     row; read it from its ledger.
   - **Back up the three ledgers,** e.g. to
     `outputs/_backup_ledger_months_20261024/`. The first deployment start
     rewrites each file to add the `contract` column (§6.5).
4. **Owner first, with the dated code.** Its seed is still unstamped this
   once, so `--contract` decides the month:

   ```powershell
   uv run --no-sync python -m forecast_eval.live.owner_cli --out outputs --contract TXFK6 --i-understand-this-places-orders
   ```

   Expect `seeded from ledgers:` to show step 3's positions with no month
   suffix (e.g. `night: -1 @ 45621.00`), an `account=` equal to their sum, and
   **no refusal**. An
   exit 6 means the ledgers and the venue disagree: stop and use §6.9. Don't
   flatten.
5. **Then the three deployments,** each with `--resume` and the dated code:

   ```powershell
   uv run --no-sync python -m forecast_eval.live.cli --strategy day     --broker client --resume --contract TXFK6 --i-understand-this-places-orders
   uv run --no-sync python -m forecast_eval.live.cli --strategy night   --broker client --resume --contract TXFK6 --i-understand-this-places-orders
   uv run --no-sync python -m forecast_eval.live.cli --strategy fullday --broker client --resume --contract TXFK6 --i-understand-this-places-orders
   ```

   Expect, per deployment:
   - `contract … code=TXFK6`;
   - `ledger: N row(s) without a month stamped TXFK6` (absent for a deployment
     with no ledger);
   - `roll: 202611 until 2026-11-17 12:45` (night: `15:30`);
   - a held deployment adopts from the owner at step 3's entry, and its
     `resume: … inherited fill(s) seeded` net agrees, with no
     `fills_replay_refused`.
6. **Check after the first bars:**
   - every `fills_<slug>.csv` has a `contract` column, filled `TXFK6`;
   - no new entry in `fatal.jsonl`, and no `owner_frozen` or `reconcile pending`
     that lasts;
   - the status page (§8.1) shows all four alive.
7. **From then on, the owner restarts without `--contract`** (its seed is
   stamped). The deployments still need it, per the dates above.

If the deployments are restarted again before the November expiry (§6.5),
repeat step 5 only; the ledgers are already stamped.

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
| 2026-10-03 09:19 | paused again cleanly (Ctrl-C, owner last) for the fix |
| 2026-10-03 09:21 | **warm-up VRAM fix on `main`**: priming at a fixed batch of 32 (`PRIME_BATCH_SIZE`) plus `empty_cache()` after it. Measured on the day bar cache, 900 boundaries: auto → batch 1024, 21.5 GiB held; 32 → 5.0 s, 4.61 GiB peak, 4.13 GiB after release; outputs equal to float noise. Not on `simtrade_0929` until `main` is merged into it |
| 2026-10-03 09:26 | `main` merged into `simtrade_0929` (`7e57912`, no conflicts; `strategy/` and `live/config.py` untouched). Suite on the branch: 1501 passed, 11 skipped, 0 failed |
| 2026-10-03 09:49–09:50 | **restart with the fix, `--resume`: passed.** Owner 09:49:34, then day 09:49:43, night 09:50:06, fullday 09:50:30. Soak epochs unchanged; all primed with 4 samples; all flat; no new `fatal.jsonl` entry. **VRAM: each deployment 4.8 GB dedicated / 5.0 GB committed, nothing paged out; 16.5 GB used, 15.7 GB free** (was 63 GB committed). Login-to-warm-up: day 17 s, night 18 s, fullday 15 s (fullday took 101 s before the fix) |

| 2026-10-06 09:13–09:16 | **owner stopped on an in-flight fill; day and fullday halted.** Fullday's first order, BUY `14AD46` @49887 (intent 09:13:02), filled @49884 at 09:14:04.5. The owner's 30 s reconcile at ~09:14:07 read venue +1 against book 0, before the deal callback arrived, and the owner exited 6. Its logout (`shioaji.log`, pid 20596, 09:14:07.09) cut the deal stream, so the fill reached no ledger. Day and fullday halted at 09:16:02 on `owner silent for 115s`. Night idled and was stopped cleanly at 13:48 |
| 2026-10-06 ~13:50–16:00 | restart attempts on uncommitted fixes (since discarded). Start-up refused `account mirror 0 != venue 1`. Two day restarts at 15:13 crashed on `PermissionError(13)`, file unrecorded. The row `2026-10-06 09:14:04.511207,BUY,49884.0,1,9.9768,soak_20260929_082029_100149` was added by hand to `fills_day_night.csv` at 14:45 |
| 2026-10-07 | venue confirmed: one order, `14AD46` Buy 1 @49887, Filled dealt=1; account +1. Hand-added row kept. **Fix on `main`:** 120 s grace for fills in flight, freeze instead of exit, no wire replay on restart (owner and deployments), `owner_session.jsonl`, crash records name the file. `owner_ack_day_night.json` is a leftover of the discarded fix; no code reads it |
| 2026-10-07 08:00–08:02 | restart with the fix, `--resume`: owner skipped fullday's old intent, seeded `day_night` +1, agreed with the venue. **But fullday adopted "+1 @ 0.00"**: the owner's seed carried no entry price, so it published `avg_price` 0.0. That defect predates the fix and first showed on this resume while holding. Fixed on `main` (`seed_positions`); owner and fullday restarted to re-adopt @49884 |
| 2026-10-07 08:12–08:13 | all four restarted on the entry fix: owner seeded `day_night` +1 @ 49884.00; fullday adopted +1 @ 49884.00, ledger replay agreed. Owner kept printing `no intents from ['day_night'] for 180s`: nothing ever called `heartbeat()`. **Heartbeat timer on `main`** (every 30 s from the clock block; `age_seconds` reads only the file's tail). Running processes pick it up at their next restart |
| 2026-10-07 08:35–08:39 | restart on the heartbeat code. Day's first start died at once on `OMP: Error #135: Cannot set thread affinity mask` (Intel OpenMP vs Process Lasso narrowing the deployments to the V-Cache CCD, `0x0000FFFF`, mid-start-up); the retry was refused by the 60 s backoff, the next one ran. If it recurs: `$env:KMP_AFFINITY = "disabled"` |
| 2026-10-07 08:45 | **day halted on its first bar: `position mirror 1 != venue 0`.** Under `--broker client` the rail compared §16's account-wide ledger sum (+1, fullday's lot) against the owner's per-deployment `net_for('day')` (0). Latent since the owner shipped; first fired because a ledger was non-zero. Night would have halted the same way at 15:00, and fullday the moment a peer held anything. Fixed on `main` (`reconcile_ledger_dir`: no ledger sum under the owner); all three deployments restarted |
| 2026-10-12 ~05:50 | **all four found dead; dead since 2026-10-08 ~21:35.** No `forecast_eval.live` process; the last run folders (`live_<slug>_20261008_2132xx`) end mid-night-session with no teardown block, and `fatal.jsonl` holds nothing after 10-07 08:46 — an abrupt kill, not a clean stop. Held unmanaged for four days: day +1 @ 49936 (10-07 13:25), night −1 @ 49440 (10-07 22:16), fullday +1 @ 49884 (10-06 09:14). `live_restart_<slug>.json` reads `run_in_flight: true`, so a same-parent restart waits out the 60 s backoff |
| 2026-10-12 | **`stop_pct` adopted on all three packages, user go** (on `simtrade_0929`): day 0.10 (`aace5c7`), night 0.10 (`260a033`, tightened from the inert 0.20 `9a29ae4`), fullday 0.10 (`c7e2178`). Sweeps: `PCTSTOP_POSTMORTEM.md`, `NIGHT_PCTSTOP_POSTMORTEM.md`, `FULLDAY_PCTSTOP_POSTMORTEM.md`; gates: the adopt rows in `results_*.tsv`. Consequence: a held lot can no longer be resumed (§5) — flatten it |
| 2026-10-12 | **plan: clean restart.** Next session (08:45+): `flatten --dry-run --out-parent outputs`, then for real (must exit 0); archive `outputs/` (ends the 09-29 soaks, dissolves §6.2's ledger gap and the dead guards' backoff); seed the day and night caches (§6.6); owner first, then all three `--fresh-start`. The stops arm on every **new** entry — the flattened lots are not resumed, so the adoption guard never fires |
| 2026-10-14 05:01–05:06 | **owner lost its session** (found 2026-10-18 in `owner_session.jsonl`): a TCP connection error at 05:01:55, then 11 `SessionNotEstablished` reconciles until the owner stopped at 05:06:47; restarted 08:04 and 08:30 |
| 2026-10-14 08:05 | **night and fullday refused `--resume`** (exit 5, `startup_refused`, gate `adopt_position`): both held lots opened after the 10-12 clean restart — night −1 @ 48760 (10-13 22:04), fullday +1 @ 48972 (10-12 22:36) — and the blanket exit-rule guard refused them under `stop_pct` 0.10. **Guard narrowed, user go:** `ExitRule.adoptable` (default False); `EntryPercentStop` and `FixedStopLoss` opt in, since they read only side and entry price. A held lot now resumes with the stop armed at the venue's entry (§5) |
| 2026-10-15 09:52–10:08 | **broker session lost; day and fullday halted.** All three deployments' last print 09:52:25; no `session_down` event, nothing in `shioaji.log`. From 09:54 every FeedWatch check was `unreachable` (`SessionNotEstablished: Unable to wait for session '(c3,s1)_sinopac' to be established`, 14 in a row), which only warns, so nothing re-logged in. Day and fullday halted at 10:08:01 on `no market data for 900.6s`, losing 10:08–13:45 of the day session (no orders either way; fullday held +1 @ 48972). Night, outside its session, stayed alive and deaf for 5 h; its scheduled 14:55 `force_refresh` re-login healed it, the 14:59 check passed and bars ran from 15:00. **The SDK did not reconnect by itself in 5 h; our re-login did.** Day and fullday restarted 14:42 (`--resume`, fullday re-adopted +1 @ 48972). **The owner lost its session too** (read from `owner_session.jsonl` 2026-10-18): `list_positions` failed from 09:54:01, `Session connect timeout` then `SessionNotEstablished`, 597 reconciles in a row until its 14:52 anchor re-login; night's 19:34 and 20:34 orders then placed and filled. Fix deferred to the weekend — see open questions |
| 2026-10-20 12:45–15:41 | **October → November roll: the deployments rolled, the owner did not.** Day (flat) switched to `TXFK6` at 12:45:02. Fullday closed its short BUY @46424 (2nd ladder attempt; @46399 cancelled), switched 12:48:01, re-opened SELL limit 46413 → filled **@46421**. Night closed its long SELL @46420 (2nd attempt, filled 15:39:32; −429 pts), switched 15:40:02, re-opened BUY limit 46419 → filled **@46418** in 3 s while November traded ~46578. The owner resolves `--contract` once at start-up (`owner_cli.py:495`, here `TXFR1` → `TXFJ6` on 10-19 04:29) and places every order on it (`owner_process.py:337`), so **both re-open legs were October trades**: night's BUY closed fullday's SELL at the venue. Venue account flat on both months; `reconcile` cannot see it (venue `TXFJ6` 0 = ledgers +1 −1). Books: night +1 @46418, fullday −1 @46421, October prices against November, **~160 pts wrong each, opposite signs**. Separately, the re-open leg is priced from the last **old-month** bar (46413, 46419), so it would be ~160 pts off even with a rolled owner |
| 2026-10-20 14:59 | **false FATAL `session_unverified` from day and fullday.** `_verify` (`live/cli.py:1201`) requires a tick between the 14:55/14:56 re-login and 14:59; held on `TXFK6`, the first trial prints came at 14:59:50 and 14:59:55, after the check (14:59:00) and its recovery (~14:59:21). Not a dead feed: `usage()` passed, `TXFK6` had no trial print from 14:50 either (before the re-login), and normal ticks resumed at 15:00 with no repair. Night, still on `TXFJ6` (~12 trial prints a minute), passed. `roll_backwards_ignored` at the same re-logins is correct: `TXFR1` still names 202610 until expiry |
| 2026-10-20 16:17–16:18 | **owner restarted alone on November.** Ctrl-C 16:17:24. `--contract TXF202611` refused at once (`RuntimeError: contract 'TXF202611' not found via api.Contracts.Futures.TXF`; nothing placed). **`--contract TXFK6` works** (the container is keyed by code): serving 16:18:43, seeded day 0, night +1 @46418, day_night −1 @46421, account +0, agreed with the venue. 79 s silent, under the deployments' 90 s `owner_timeout`: no halt. Ticks checked after: all three on `TXFK6`, per-minute counts identical, lag ~1.07 s |
| 2026-10-24 (nothing running) | **roll re-open entries re-priced to November** in `fills_night.csv` and `fills_day_night.csv`. Each is set to the last `TXFK6` print at or before its fill instant in the run's `ticks.csv`, and the fee is recomputed at 0.0002: night BUY 15:40:04.583 46418 → **46581** (print 15:40:04.304), fullday SELL 12:48:10.742 46421 → **46588** (print 12:48:08.762). Both lots had already closed, so this changes realized P&L. Night's LONG to 46828 goes from +410 to **+247** gross (net 391.35 → 228.32). Fullday's SHORT to 47408 goes from −987 to **−820** (net −1005.77 → −838.80). The newest run folders' `fills.csv`/`trades.csv` (`live_night_20261020_233853`, `live_day_night_20261020_233915`) were corrected to match and checked against `build_trades`. The 10-19 run folders are kept as recorded. Nets unchanged: night −1 @45621, fullday flat. Originals in `outputs/_backup_roll_entryfix_20261024/` |

**Open questions:**

- **Roll defects found 2026-10-20 (log rows above); fix deferred to the weekend
  of 10-24/25, must land before the 11-18 roll.** Unfixed:
  1. The owner never rolls its order contract. It must follow the deployments'
     roll, or refuse an intent for a month it does not hold.
  2. The roll's re-open leg is priced from the last old-month bar, not the new
     month.
  3. `_verify`'s tick half false-fails on a held month that is not yet the
     front one (sparse trial prints). It may fire again on 10-21 at 08:44 and
     14:59.

  **All three fixed on `main` 2026-10-24 and merged into `simtrade_0929`;
  not yet demonstrated live.**
  1. Each intent names its month, and the owner places, sizes and reconciles
     per month ([owner_process.md](live/owner_process.md)).
  2. `LiveRuntime.switch_contract` drops the old month's half-built bar,
     queued prints and ladder reference. The re-open's rung 1 goes out on the
     first new-month bar ([roll.md](live/roll.md)).
  3. The tick half is skipped while the held month is later than the
     exchange's front month ([cli.md](live/cli.md)).

  The live code takes effect only after every process (owner and deployments)
  is restarted on `simtrade_0929`.

  The October entry prices (night +1 @46418, fullday −1 @46421) were re-priced
  to November on 2026-10-24 (log row above). For which `--contract` to restart
  with, and when, see **Next dates** and the resume steps above; `TXF202611`
  does not resolve.

  **Two follow-ups fixed on `main` 2026-10-24 (`a45357d`) and merged into
  `simtrade_0929`; not yet demonstrated live:**
  1. Restart rule: ledger rows carry their month (`contract`), and the owner
     seeds per month from them. Rows written before the column get the
     deployment's month the first time it starts on this code. The deployments
     themselves still need `--contract` (§6.5).
  2. A roll that lapsed into `RollWatch`'s backup path: the deployment sends a
     `settle` intent, the owner drops the lot from the expired month and
     answers `settled`, and its reconcile skips expired months. The settlement
     now also reaches the ledger and `trades.csv`. Not covered: a deployment
     not running at the expiry (§6.5).
- **The 2026-10-06 `PermissionError`** on the day restart came from uncommitted
  code, and nothing recorded which file. If a restart on committed code hits it,
  the `crashed` record and the console traceback now name the file.

- **No orders in four days across three deployments.** Day's `min_prob` 0.65
  gate and night's long-only rule are the first suspects; fullday's 302 HOLDs
  at `min_prob` 0.50 point at the band or the trend filter instead.
- **The flatten → restart path under the owner (§6.2) is unverified.**
- **Fixed in code 2026-10-18, not yet seen live: a lost session is re-logged
  into.** FeedWatch now calls the anchors' `_relogin` after 3 consecutive
  checks whose snapshot *raised* (~3 min in session), and again every 3 more
  while it keeps failing — 4 attempts before the 900 s backstop
  ([feedwatch.md](live/feedwatch.md)). Alerts: `session_relogin` (INFO, WARN after
  a failed attempt) or `session_relogin_failed` (WARN). The two follow-ups, also 2026-10-18:
  the deployments' re-logins are **staggered** (anchors 40 s apart; lost-session
  attempts a check apart, at checks 3/4/5 then every 3), and the **owner**
  re-logs in after 3 unanswered reconciles (31 s to ~90 s) when `usage()`
  fails too, up to 4 times an outage, then FATAL `owner_session_lost`
  ([owner_cli.md](live/owner_cli.md)). The owner did lose its session on 10-15
  (09:54:01 to 14:52) and on 10-14 05:01 (log rows above). Until a live
  recovery is seen, a `feed_unreachable` with no `session_relogin` within
  ~6 min means restart by hand.
