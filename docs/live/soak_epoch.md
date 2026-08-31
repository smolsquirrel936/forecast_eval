# [live/soak_epoch.py](../../live/soak_epoch.py)

**Role:** track the identity of one continuous soak across restarts, from the operator's explicit `--fresh-start`/`--resume` choice.
**Pipeline:** [cli.py](cli.md) `main` → `resolve(soak_epoch.json, fresh_start=…)` → `SoakEpoch` → `session.jsonl`'s `meta.soak_id`.
**Depends on:** nothing in `live/` — pure JSON state, no broker, no clock beyond what's passed in.   **Used by:** [live/cli.py](cli.md)

> SHIOAJI_PLAN.md's "Open items" §3 (2026-08-24). Built 2026-08-31, right after
> the first genuine kill-restart against a strategy-opened position proved
> `adopt_position` recovers the *position* correctly — this module is what was
> still missing: nothing recorded whether the operator meant that restart to
> continue the soak or start a new one.

## Responsibility
Before this existed, `adopt_position` ([runtime.md](runtime.md)) ran
unconditionally on every launch — no way to ask it not to — and every launch
got a fresh `outputs/live_<ts>/session.jsonl` with nothing linking it to the
run before it. Two scenarios an operator actually has want different
behavior: *"all bugs fixed, start clean"* and *"the process died mid-soak,
keep going"*. Neither was distinguishable at start-up.

This module does not touch either of those things. It tracks one fact — a
`soak_id` — so a restart can say which soak it belongs to. `--resume` carries
the id on disk forward (or mints the first one, if none exists yet).
`--fresh-start` mints a new one and retires the old one into the same file's
`retired` list, rather than discarding it silently.

## What it deliberately does not do
- **Does not touch the position.** `adopt_position` still runs unconditionally
  on every launch either way — this is orthogonal, not a replacement.
- **Does not flatten anything.** `--fresh-start` refuses at start-up
  ([cli.md](cli.md)) when the account is not already flat; it only checks.
  [flatten.py](flatten.md) stays the only path that places a closing order.
- **Does not reconcile `list_profit_loss`.** The `soak_id` is what a future
  reconciliation tool would filter on — SHIOAJI_PLAN.md's §3 still calls that
  "downstream of this and still applies once the marker exists." Not built.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `SoakEpoch` | dataclass | `soak_id`, `started_at` — the identity of one continuous soak |
| `load(path)` | function | the epoch on disk, or `None` if there isn't one (missing or corrupt file both read this way) |
| `resolve(path, *, fresh_start, now)` | function | the epoch this launch belongs to; mints, carries forward, or retires-and-mints, per `fresh_start` |

## Why `resolve` reads the file once, not twice
`--fresh-start` needs to both retire whatever is on disk *and* decide what to
retire it as — reading once and branching on the same value keeps the two
CLI flags from ever disagreeing about what "currently on disk" means if the
file changed between two separate reads. There is only one read, in both
branches.

## Why a corrupt file is treated as no epoch, not an error
Unlike [`RestartGuard`](startup.md) — where a corrupt state file is charged a
failure, because backoff exists specifically to survive a damaged file — a
soak epoch has no safety property riding on it. A restart that only wants its
*position* back must not be blocked by a damaged id file; `resolve` bootstraps
a fresh epoch instead, the same as a genuinely first-ever launch.
