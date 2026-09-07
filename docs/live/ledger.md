# [live/ledger.py](../../live/ledger.py)

**Role:** the deployment's fill history, carried across restarts, so a resumed run's record covers the soak rather than one segment.
**Pipeline:** `--resume` → `FillLedger.replay()` → seeds `LiveLoop.fills` → `build_trades` pairs correctly → [artifacts](artifacts.md).
**Depends on:** [events.py](../events.md), [live/sessions.py](sessions.md)   **Used by:** [live/cli.py](cli.md)

## The bug it exists for

A restart adopts the venue's **position** (§2.20) but starts with an empty fill
list, and `build_trades` walks fills from flat. So the inherited position's close
was booked as an **open**, and every pair after it shifted. Measured on a
two-trade example:

| | reality | resumed run's `trades.csv`, before |
|---|---|---|
| trades | 2 | **1** |
| direction | LONG, LONG | **SHORT** |
| PnL | +145, +300 | **+100** |
| final fill | closes trade 2 | **dangling, dropped** |

**The trading was never affected** — the `Portfolio` is seeded on adoption, so
realized PnL was always right. Only the record derived from fills was wrong, and
only once [artifacts](artifacts.md) started writing `trades.csv` from a live run.

It is also not an edge case. §2.20 puts it plainly: *"the strategy holds a median
4.5 days, so **any** restart during a soak lands mid-position."*

## Three properties

**Session-sensitive.** The path is `fills_<policy.cache_slug()>.csv` —
`fills_night.csv`, `fills_day.csv` — derived from the policy rather than passed
in, so a new deployment cannot forget to change it. This is C5 one file over: a
night trade replayed into a day run would put a position in `trades.csv` that the
day deployment never took, and then argue with the venue about a lot belonging to
somebody else.

**Soak-scoped.** Only rows carrying the live `soak_id` are replayed, which is what
makes `--fresh-start` mean a clean slate in the *artifacts* and not only in the
state file. Retired soaks stay in the file and stay readable; they are simply not
this run's history.

**Never a dependency.** Read best-effort, exactly like the bar cache ("an
optimisation, never a dependency"). A missing ledger is ordinary — the first
resume after this shipped has none. Every failure path falls back to the
synthetic fill below, so the cost of a bad ledger is a *shorter* history, never a
wrong one and never a stopped run.

## The reconciliation, which is the load-bearing part

Before seeding, `net_position(replayed)` is compared with what the venue actually
holds. If they disagree the history is wrong — a manual flatten, a crash between
a fill and its sync, a different account — and seeding it would produce a
confidently wrong `trades.csv`. The ledger is then **refused**
(`fills_replay_refused`, with both numbers) and the fallback takes over.

## The fallback: one synthetic opening fill

When no history can be replayed but a position *is* held, `cli.py` books a single
opening fill at the venue's own average price. It cannot reconstruct the history,
but it guarantees the pairing, so `trades.csv` is never mis-paired even on the
very first resume after this shipped — which is the case that is guaranteed to
happen, since no ledger exists yet.

Two things about it are deliberate and worth knowing:

- **`entry_ts` is the resume moment, not the original open.** The venue reports
  an average *price*, not a time. This is the same approximation
  `trader.entry_timestamp` already makes on adoption.
- **It is never written to the ledger.** `mark_written` holds it back, because it
  stands in for an execution this process never saw; recording it would make the
  *next* resume replay a fill that never happened.

## Two ways the mark is used

`mark_written(n)` declares that the first `n` entries of the loop's fill list
must never be appended — for opposite reasons in the two seeding paths. Replayed
fills are *already* in the file, so re-appending would duplicate the whole
history on the first sync of every resumed run. The synthetic fill is
deliberately not a ledger row at all.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `FillLedger(path, *, soak_id)` | class | append this deployment's fills; replay the soak's |
| `FillLedger.replay()` | method | the current soak's fills, in file order |
| `FillLedger.sync(fills)` | method | append everything past the high-water mark |
| `FillLedger.mark_written(n)` | method | hold the first `n` back from the file |
| `FillLedger.summary()` | method | `fills_replayed` / `fills_appended` / `ledger_failures` |
| `ledger_path(out_parent, policy)` | function | `fills_<slug>.csv`, keyed like the bar cache |
| `net_position(fills)` | function | the signed lots a fill sequence leaves |
| `open_position(fills)` | function | the lot a fill sequence leaves open, and its entry price |
| `account_net_position(out_parent)` | function | signed lots **every** deployment's ledger says the ACCOUNT holds; `None` when absent or unreadable (§16) |
| `LEDGER_COLUMNS` | constant | a `FillEvent`'s columns, plus `soak_id` |

## Why the schema check comes before the emptiness check

Measured, and the order is not cosmetic. An empty ledger is ordinary and must not
raise the failure count. But corruption need not *raise* either: `read_csv` is
lenient enough to parse a truncated write into a header-only frame with garbage
column names, which is empty and would slip past an emptiness check placed first.
Asking the more specific question first separates "nothing recorded yet" from
"this is not our file". Both replay nothing; only the second is a failure.

## `open_position` — the ledger's answer to `broker_position_detail`

`net_position` says *how many* lots a sequence leaves; the reconciliation needs
nothing more. Adopting **from** the ledger needs *at what price* as well, because
the entry is what the Portfolio realizes PnL against — a run that invented one
would book a fictional number on the closing fill. So `open_position` returns the
same `HeldPosition` the venue would have, which is what lets a broker with no
venue (`--broker dryrun`) use the ledger in its place — see
[live/runtime.md](runtime.md).

FIFO, not an average over every row: a sequence that goes short, closes, then
goes long holds **only** the long, and averaging across all three would price the
residual from a trade that is already closed and booked. At `max_position=1` the
residual is always one lot and the weighting is a no-op; it is written generally
so it does not quietly become wrong if that limit moves.

## Reconciliation now compares the ACCOUNT, not one deployment (§16)
**MEASURED 2026-09-02** (STRATEGY_SPLIT_PLAN §14): a deployment's order callback
hears **only its own** fills, while `list_positions` returns the **combined**
book for the account. Comparing one deployment's mirror against the whole
account's size is not a desync check — it is a category error, and it halts every
deployment the first time two of them hold at once, with nothing actually wrong.

So the reconciliation mirror is now **what we recorded for the whole account**:
`ledger.account_net_position(dir)` sums the signed quantity of every row of every
`fills_*.csv` in the run's parent directory, across deployments and across soak
epochs. Both sides of the comparison then mean "the account", and a disagreement
means what the rail was written to catch: a fill missed or counted twice.

**It degrades, it does not guess.** No ledger directory, no ledger files, or one
that cannot be parsed all return `None`, and the loop falls back to this
deployment's own position — the previous behaviour exactly. A partial sum would
be a confident wrong number, which is the failure mode this removes.

**It does not make concurrent deployments safe.** It stops the spurious halt; it
does not stop two deployments' orders netting into one book. See
STRATEGY_SPLIT_PLAN §15 — one instance at a time — which stands until the venue
gives each deployment its own book.

## `per_deployment_nets` — the same rows, split by who owns them (§18.3 item 8)

`account_net_position` answers *"what does the account hold"*.
`per_deployment_nets` answers *"who holds it"*, as `{slug: signed lots}` from
each `fills_<slug>.csv`.

It exists for [flatten.py](flatten.md), which closes the account's **whole** net.
On a shared book that is the sum of every deployment's position, so an operator
about to flatten needs "closing day's 1 and night's 1", not "closing 2 lots".

**The two functions degrade in opposite directions, on purpose:**

| | unreadable ledger | because |
|---|---|---|
| `account_net_position` | returns `None` for the whole set | it feeds a safety rail, where a partial sum is a confident wrong number |
| `per_deployment_nets` | skips that file, keeps the rest | it feeds a warning, where naming two of three beats naming none |

The CSV-reading rule lives here beside `account_net_position` rather than being
spelled a second time at the call site — two readers of one format are how they
drift.
