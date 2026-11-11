# [live/catchup.py](../../live/catchup.py)

**Role:** Put a deployment where its strategy would be after downtime, at the venue.
**Pipeline:** standalone — run by hand while the owner and every deployment are stopped, before a `--resume`.
**Depends on:** [real_data.py](../real_data.md) · [live/backfill.py](backfill.md) · [live/owner.py](owner.md) (`plan_legs`) · [live/broker/shioaji.py](broker/shioaji.md) · [live/ledger.py](ledger.md) · [live/exit_state.py](exit_state.md) · [live/owner_positions.py](owner_positions.md)   **Used by:** run by hand

```powershell
# Plan only: fetch, replay, print the order a catch-up needs. Sends nothing, records nothing.
uv run --no-sync python -m forecast_eval.live.catchup --config day --warmup-bars 6000
# PLACES ORDERS: trade the venue to the target, record the real fills.
uv run --no-sync python -m forecast_eval.live.catchup --config day --warmup-bars 6000 --apply
```

## Responsibility
When a simtrade was not running, its strategy kept "trading" on paper. This tool
finds out where it would be now and moves the **venue** there:

1. **Tape.** Reads the package's RPT tape and appends every bar after its last
   minute, fetched with `kbars` (front month plus the month before, spliced by the
   deployment's roll rule, as `cli.py`'s warm-up does). It prints how many closes
   differ where the fetch overlaps the tape (2026-11-12, day: 0 of 300).
2. **Replay.** Runs `real_data --strategy <package>` on the extended tape, so the
   replay is the package's own backtest: same loop, fill model and exit rule.
3. **Plan.** The replay's open position at the end of the tape is the **target**.
   The ledger's open position is what the deployment holds. Their difference is
   the order. The replay's trades after the downtime began are listed. The
   downtime starts at the later of the ledger's last fill and `last_heartbeat`
   in `live_restart_<slug>.json`.
4. **Apply** (only with `--apply`). Checks the venue, places the order as
   owner-style legs, appends the **real fills** to `fills_<slug>.csv`, updates
   `owner_positions.json` if it exists, writes or clears
   `exit_state_<slug>.json`, and appends the replay's downtime trades to
   `catchup_<slug>.csv`.

Each run writes its tape, replay and `plan.json` to `outputs/catchup_<slug>_<timestamp>/`.

## The rule: the ledger only ever holds venue fills
A catch-up moves the venue and the ledger together, or neither. The owner's
start-up reconcile compares the ledgers' total with the venue and refuses on any
difference ([owner_cli.py](owner_cli.md)), so a ledger edited to show trades the
venue never made stops every deployment from starting. The strategy's would-be
trades are kept in `catchup_<slug>.csv` instead, which nothing live reads. Rows
already recorded (same timestamp, side and price) are not written twice.

## Exit codes
| Code | Meaning |
|---|---|
| `0` | plan printed, or the venue reached the target |
| `2` | bad arguments, or no credential |
| `4` | the venue did not reach the target; **every fill that arrived is in the ledger** — reconcile before starting |
| `5` | refused before any order: working orders, venue ≠ the ledgers' total, or a held lot not on the front month |
| `6` | the replay failed; see `replay.log` in the run folder |

## Key invariants / gotchas
- **`--warmup-bars` sets the forecast schedule.** Use the package's adopted run
  (day: 6000). The replay's schedule is the backtest's, not live's: on
  2026-10-27/28 the backtest entered day's short a day before live did. A
  catch-up can therefore disagree with what live would have done. There is no
  record of live's forecasts for a gap, so this cannot be avoided.
- **No forecast cache.** Where a branch has one (`forecast_cache.py`, on
  `simtrade_0929`), its key hashes the whole tape, so an extended tape never hits. A
  day replay recomputes every Toto2 forecast (a few minutes on the 5090). Run it
  while the simtrades are stopped.
- **The venue must hold exactly the ledgers' total**, checked before any order.
  The legs are sized on the account's net (`owner.plan_legs`), and a Cover sized
  against lots that are not there is unreliable (STRATEGY_SPLIT_PLAN §18.12:
  clamped at flat, or crossed to a short).
- **Cover/New is decided on the account's net, not the deployment's.** Day going
  flat → long while night is short is a `Cover` at the venue (§18.9).
- **Legs are LMT + ROD, 30 points through a fresh snapshot**, as in
  [flatten.py](flatten.md). An unfilled leg is cancelled and re-priced for what
  remains, up to `--attempts`. If a leg still has lots left, nothing after it is
  sent.
- **Every fill is written to the ledger before anything else can fail**, under
  the deployment's current `soak_id` and the front month's code.
- **Working orders refuse, not cancel.** Unlike `flatten`, this tool has no
  emergency that would excuse withdrawing an order it did not place.
- **Exit-rule state.** A lot opened by the catch-up starts its state at the real
  fill. A lot kept from before gets the state the rule would have after every
  in-session bar since its entry: the rule's own `should_exit` is run over those
  bars on a fresh rule object. If the rule would have fired during the downtime,
  the plan says when. Nothing acts on it: the live loop re-checks the rule on its
  first bar. When the deployment ends flat, any stale exit-state file is removed.
- **A held lot on an older month refuses.** Moving it is the roll's job.
- **One login per phase.** The fetch logs in and out before the replay, and
  `--apply` logs in again, so a long replay never holds a session.

## Related
[simtrade_operations.md, "Catching up after downtime"](../simtrade_operations.md) · [live/flatten.py](flatten.md) · [live/owner_cli.py](owner_cli.md) · [live/exit_state.py](exit_state.md) · [real_data.py](../real_data.md)
