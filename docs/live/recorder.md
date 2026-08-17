# [live/recorder.py](../../live/recorder.py)

**Role:** Append-only JSONL audit trail of everything the live loop saw.
**Pipeline:** LiveLoop / SafetyRails / ShioajiBroker → **Recorder** → `*.jsonl`.
**Depends on:** — (stdlib only)   **Used by:** [live/loop.py](loop.md), [live/state.py](state.md), [live/broker/shioaji.py](broker/shioaji.md)

## Responsibility
A sink, not a decision-maker: **nothing it does may influence trading.** Two later
phases depend entirely on this file being complete — shadow-parity (replaying a
live session's bars offline and diffing the *decisions*) and the fill-gap
measurement (comparing, per order, what the sim would have filled against what
the venue reported).

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `Recorder(path, *, simulation, meta=None)` | class | opens for **append**, writes a `meta` header immediately |
| `record(event, **fields)` | method | one JSON object per line, flushed |
| `close()` | method | idempotent |
| context manager | — | `with Recorder(...) as rec:` |

## Event types written
`meta` · `bar` · `forecast` · `signal` · `order` · `fill` · `halt` ·
`broker_submit` · `broker_deal` · `broker_anomaly`

`order` records carry a `reason` (`signal` / `exit` / `forced_close` /
`halt_flatten`), which is what lets a shadow-parity diff line each order up with
the decision that produced it.

## Design constraints (all from "the process may be killed at any moment")
- **One JSON object per line, flushed after every write** — a truncated tail loses
  at most the final event, and every complete line stays parseable. The cost is
  irrelevant next to a forecast.
- **No cross-record state**, so the file reads with a plain `for line in f`.
- **A `meta` header first, carrying the `simulation` flag**, so a file can never be
  mistaken for a production run when read back months later.

## Key points / gotchas
- **Serialization never raises.** Timestamps go out as ISO-8601 (so they re-parse
  losslessly for the diff), numpy scalars unwrap, and anything else is
  stringified — a recorder that can kill the trading loop is worse than a lossy
  one.
- **Opens for append, not truncate**: a restart adds to the session trail instead
  of destroying it.
- `seq` orders events even when several share a bar timestamp; `wall` is the only
  place local time enters the record (bar times come from the feed).

## Related
[live/loop.py](loop.md) · [live/state.py](state.md) · [logging_io.py](../logging_io.md) · [tests/test_live_recorder.py](../tests.md)

## Identifier redaction (§2.26)
`Recorder.record()` scrubs national-ID-shaped tokens (`[A-Z][0-9]{9}`) from the
serialised line before it reaches disk.

**Why it is needed.** The SDK embeds `person_id` *bare* inside session-event
strings — `#P2P/v:…/PYAPI/L1255…/0817/…` — where no field name exists to key on,
and [cli.py](cli.md)'s `_on_session_event` records `info` verbatim. `session.jsonl`
is the file that gets pasted into chats and tickets, and `account_fingerprint`
digests `person_id` precisely so the trail never has to carry one.

**Why it lives here** rather than at the call site: the same reasoning as the
probe's renderer — scrubbing the rendered line means no future `record()` can
reintroduce the leak. Non-identifying context is preserved.

**Found by** the 2026-08-17 dry run. That run's trail was clean only by an
accident of ordering — login events fire before the callbacks are registered — but
§2.24's re-login emits those same events at 08:40 and 14:55 with the callback
live, so the first soak would have written the ID twice a day.

**The first fix was silently disarmed.** It compiled to `'\x08[A-Z][0-9]{9}\x08'`:
`re.sub` interprets escapes in its *replacement* string, so the `\b` boundaries
became literal backspace bytes. `grep` and `.pattern` both rendered it as
correct, because 0x08 is invisible. The regression test asserts the **compiled**
pattern rather than the source text — reading the source is what hid it.

**Out of reach:** the SDK prints that string on its own stdout too. Nothing here
touches that; treat raw live consoles as sensitive.
