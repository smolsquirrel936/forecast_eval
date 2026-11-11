# [live/owner_positions.py](../../live/owner_positions.py)

**Role:** the owner's own record of who holds what, on disk: each deployment's lots and entry, and the account total every Cover is sized against.
**Pipeline:** [owner_process.py](owner_process.md) `poll` → `save_positions` → `outputs/owner_positions.json` → a person edits it → `check_positions_file` → `seed_from`. At start-up: [owner_cli.py](owner_cli.md) `choose_seed` → `load`.
**Depends on:** [live/broker/base.py](broker/base.md) (`HeldPosition`)   **Used by:** [live/owner_process.py](owner_process.md), [live/owner_cli.py](owner_cli.md)

> Built 2026-11-11. On 2026-10-30 night's stop close timed out on our side and
> still filled at 22:42; the owner never heard the deal, its book read +2 against
> the venue's +1, and it froze. Fullday's stop close was rejected at 01:36, and
> the fix meant stopping everything, adding night's ledger row and restarting:
> after 05:00, fullday's close would have waited for Monday's open. The user's
> call: the account total belongs to us, not the venue; record it ourselves,
> correct it by hand while running, and make a venue disagreement an alert.

## Responsibility
The owner's `OrderBook` already knows each deployment's lots and the total
([owner.py](owner.md)); until now only in memory, rebuilt from the ledgers at
each start. This module is the file format and its checks. `OwnerProcess` owns
when it is written and read.

## The file

```json
{
  "updated_at": "2026-11-11T14:30:00",
  "total": {"TXFK6": -3},
  "deployments": {
    "day":       {"lots": {"TXFK6": -1}, "entry": 47025.0},
    "night":     {"lots": {"TXFK6": -1}, "entry": 42571.0},
    "day_night": {"lots": {"TXFK6": -1}, "entry": 42772.0},
    "hybrid":    {"lots": {}, "entry": 0.0}
  }
}
```

- **Every served deployment is listed**, flat ones with empty `lots`, so a
  correction has a place to go.
- **`lots` is by dated month.** Between two deployments' rolls the account holds
  two months, and a lot on one never covers a lot on the other.
- **`total` must equal the sum of the lots, month by month.** It is redundant on
  purpose: a hand edit states the change twice, so a typo in one place is refused
  rather than sizing the next Cover.
- **A held deployment needs its entry.** The deployment adopts its lot at that
  price on `--resume`, and the Portfolio realizes PnL against it.

`parse` refuses, naming the problem: a total that does not add up, a held lot
with no entry, a lot that is not a whole number (`1.0`, `true`), an empty month
code, and lots for a deployment this owner does not serve. A missing deployment
reads as flat, as a missing ledger does. A byte-order mark (Notepad) is dropped.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `POSITIONS_NAME` | const | `"owner_positions.json"` |
| `positions_path(directory)` | function | the file in the owner's `--out` |
| `render(holdings, entries, slugs, now)` | function | the file's content for a book state |
| `write(path, doc)` | function | atomic write (temp file + `os.replace`); returns the text written |
| `parse(doc, slugs)` / `parse_text(text, slugs)` | function | `(holdings, entries)`, or `ValueError` |
| `read_text(path)` / `load(path, slugs)` | function | the text / the validated record; `None` when there is no file |
| `as_seed(holdings, entries, slugs)` | function | `{slug: {code: HeldPosition}}` for `seed_from`; every slug, flat ones `{}` |
| `describe(holdings, entries)` | function | `day TXFK6 -1 @ 47025, …` or `flat` |

`holdings` is `{(slug, code): lots}` with zeros dropped and `entries` is
`{slug: entry}` for the deployments holding, the same normal form
`OrderBook.holdings` / `entries` give, so a file and the book compare with `==`.

## How the owner uses it
See [owner_process.md](owner_process.md) ("The positions file") for the write
and edit rules, and [owner_cli.md](owner_cli.md) for start-up.
[simtrade_operations §6.9](../simtrade_operations.md) is the operator's
procedure for a missed fill.
