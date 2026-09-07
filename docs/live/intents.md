# [live/intents.py](../../live/intents.py)

**Role:** the wire between the three deployments and the owning process — line-delimited JSON, one file per direction per deployment.
**Pipeline:** deployment → `intents_<slug>.jsonl` → owner → `events_<slug>.jsonl` → deployment.
**Depends on:** nothing in this package   **Used by:** S9b's owner and `ClientBroker` (being built; [STRATEGY_SPLIT_PLAN §18](../../STRATEGY_SPLIT_PLAN.md))

## What it is for

Option (b) puts one process in front of the account: the deployments stop calling
`place_order` and submit **intents** instead, and the owner reports back what
happened. This module carries those messages. It is deliberately protocol-free —
it moves dicts, and what the kinds mean lives with the two ends that speak them.

Why one process at all: [§14](../../STRATEGY_SPLIT_PLAN.md) measured that deals
are per-session but the position book is **shared**, so three deployments on one
account see each other's positions and none of their fills. One owner, one book,
one `_match`.

## Why files and not a socket

Decided in §18.4. Files match every existing idiom here — the
[fill ledger](ledger.md), the bar cache, the run directory — add no dependency,
and leave a **durable record readable after a crash**, which a socket does not.
The loop runs on one-minute bars, so polling latency is irrelevant.

**The cost it accepts:** a dead writer's file looks exactly like a quiet one. So
every record carries a monotonic `seq` and a wall-clock `ts`, and `age_seconds()`
exists for the reader to notice silence. A deployment whose owner has gone quiet
must **halt**, not trade blind (§18.3 item 6) — the same rule
[broker/base.py](broker/base.md) already states for a venue that will not answer:
degrade to "cannot compare", never to a confident guess.

## Public surface

| | |
|---|---|
| `intent_path(dir, slug)` | `intents_<slug>.jsonl` — deployment writes, owner reads |
| `event_path(dir, slug)` | `events_<slug>.jsonl` — owner writes, deployment reads |
| `JsonlStream(path).append(kind, **fields) -> seq` | one record, flushed |
| `.read_new() -> list[dict]` | every **complete** record since the last call |
| `.resume_seq() -> int` | continue numbering past an existing file |
| `.age_seconds() -> float \| None` | seconds since the newest record; `None` if there are none |

Paths are keyed by `cache_slug()` — the same rule that names the fill ledger, and
for the same reason: C5 is this repo's record of what happens when two
deployments share one path. One file per direction means each file has exactly
**one writer**, which is what makes an append safe to rely on and `seq`
meaningful.

## The three rules that matter

**1. Only complete lines are records.** `read_new` advances its byte offset by
the bytes it actually consumed, stopping at the last newline. A trailing partial
write stays unconsumed and is returned whole on a later call. A reader can
therefore never see half an order — and the offset is a byte position rather
than a line count precisely so the partial can be re-read.

**2. A complete line that will not parse raises.** That is damage, not a partial
write (the newline rule already covers partials). Skipping it would drop an order
silently, which is the one failure this wire cannot have. The caller halts
instead — the same choice `ledger.account_net_position` makes when it returns
`None` rather than a partial sum.

**3. `append` flushes.** The reader is a different process. An intent sitting in
this process's buffer is an intent the owner cannot act on;
[§16](../../STRATEGY_SPLIT_PLAN.md)'s freshness caveat is the same problem one
file over.

## `age_seconds` uses the record's own `ts`, not the file's mtime

mtime moves for reasons that are not a record — a touch, a copy, a backup — and
the question being asked is *"when did the far end last say something"*.

Two further details, each pinned by a test:

- **`None` is not zero.** Never having spoken is a different state from having
  gone quiet, and a reader must be able to tell them apart.
- **A future timestamp floors to `0.0`.** Two processes' clocks are not
  identical; a small negative age is skew, not a message from the future.

## `resume_seq` before the first append on an existing file

A restarted writer that began again at `1` would reissue sequence numbers the far
end has already seen, and a gap check would read the restart as a duplicate
stream rather than a continuation. `resume_seq()` reads the last complete record
and continues from it — ignoring a trailing partial, which is not a record yet.

## Related
[live/ledger.py](ledger.md) · [live/broker/base.py](broker/base.md) · [live/loop.py](loop.md) · [STRATEGY_SPLIT_PLAN.md](../../STRATEGY_SPLIT_PLAN.md)
