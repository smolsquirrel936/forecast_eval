# [live/flatten.py](../../live/flatten.py)

**Role:** Close whatever the account is holding, and confirm it.
**Pipeline:** standalone — the manual remedy when something else left a position behind.
**Depends on:** [live/contracts.py](contracts.md)   **Used by:** run by hand, after a failure

```powershell
uv run --no-sync python -m forecast_eval.live.flatten --dry-run   # show, do nothing
uv run --no-sync python -m forecast_eval.live.flatten
```

## Responsibility
Every other tool here **refuses to act** when a position already exists, because
none of them can tell its own order from one that was already there — see
[cli.py](cli.md)'s preflight and [v4_probe.py](v4_probe.md). That is the right
default, and it leaves a gap: something has to be able to close a position left
by a killed process, a failed `FLATTEN` halt, or a probe whose close did not
fill. This is that something.

## Exit codes
| Code | Meaning |
|---|---|
| `0` | flat (or already was) |
| `2` | no credentials |
| `3` | no reference price — a close cannot be priced; retry during a session |
| `4` | **still holding** after every attempt — close it in the broker's front end |

## Key invariants / gotchas
- **The credential comes from `config/secrets.toml`, like everything else.**
  `--credential` picks the section (default `night`); all three name the same
  futures account (STRATEGY_SPLIT_PLAN §13), so it only chooses the login.
  Falls back to `SHIOAJI_API_KEY`/`SHIOAJI_SECRET_KEY`. Until 2026-09-07 this
  was the one live entry point that read the environment *only* — so the
  emergency tool was the single tool that could not start on a machine
  configured the way every other one here expects (§22.6).
- **Working orders are cancelled first.** A resting close is invisible in
  `list_positions`, so placing a second `Cover` on top of it can fill twice
  against a single lot and turn a long into a **short** — a worse state than the
  one being repaired. Reachable only since the flatten became ROD (§2.19).
- **An unrecognised order status counts as *not* working.** The concrete
  literals have been wrong before (§7.2 found `FORDER`/`FDEAL` where the docs
  showed `FuturesOrder`/`FuturesDeal`), so the worst case must be a cancel that
  is skipped — never a position mistaken for flat. `Status.Submitted` is matched
  as well as `Submitted`, since `str(enum)` prefixes the type.
- **Positions are matched on the resolved code**, via
  [contract_codes](contracts.md). Filtering on the `TXFR1` alias alone finds
  nothing and reports flat while holding — the one reading this tool must never
  produce.
- **LMT + ROD, priced 30 points through the touch.** Far more aggressive than the
  live flatten's 5 ticks because this runs *attended*, after something has
  already gone wrong: filling now matters more than the last tick. Still a
  bounded limit, so the worst price is knowable before sending. ROD rather than
  IOC for the §2.19 reason — a stationary market has no prints to match against,
  and a resting order fills on the next one where an expiring order gets nothing.
- **It re-reads the position between attempts** and re-derives side and size from
  what is actually held, so a partial close converges instead of over-selling.
- **"Submitted a close" is not "flat" (§2.12).** The position is read back and a
  non-zero exit is the answer if anything remains.

## Related
[live/cli.py](cli.md) · [live/v4_probe.py](v4_probe.md) · [live/contracts.py](contracts.md) · [live/state.py](state.md) · [SHIOAJI_PLAN.md §2.19](../../SHIOAJI_PLAN.md)

## It stays account-wide, and now says so (§18.3 item 8)

This tool reads `list_positions`, which on a shared book is the **sum** of what
every deployment holds ([§14](../../STRATEGY_SPLIT_PLAN.md)). So closing it
closes other people's positions too, and their mirrors will not know.

**That is still the right behaviour.** It exists precisely for when nothing else
can be trusted to close a position — a flatten that could only close a third of
the account would be useless in the one situation it is for.

What it must not do is stay silent. Given `--out-parent`, it reads the
per-deployment fill ledgers ([ledger.py](ledger.md)'s `per_deployment_nets`) and,
when **more than one** deployment is holding, prints whose lots are about to be
closed and what to do afterwards:

```
  *** THIS CLOSES THE WHOLE ACCOUNT, AND MORE THAN ONE DEPLOYMENT IS HOLDING ***
      day        +1 lot(s)
      night      -1 lot(s)
      venue      +0 lot(s)  <- what will be closed
  Their position mirrors will NOT know. After this, restart each of
  them with --fresh-start, or they halt on the first bar.
```

**Silence when only one deployment is holding is deliberate.** The
single-deployment case is still the normal one, and a warning with nothing behind
it trains an operator to ignore warnings.
