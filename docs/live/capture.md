# [live/capture.py](../../live/capture.py)

**Role:** The raw tick feed, written down **before** anything filters it.
**Pipeline:** quote callback → **TickCapture** → `ticks.csv` (and, separately, → [bars.py](bars.md)).
**Depends on:** — (stdlib `csv` + pandas)   **Used by:** [live/cli.py](cli.md), [live/runtime.py](runtime.md)

## Responsibility
The input half of **V1's live validation**. The offline half already proved
[TickAggregator](bars.md) reproduces the RPT convention; the open question is
whether the *live tick stream* aggregates to the same bars `kbars` reports for
the same session. Answering that needs the ticks exactly as they arrived.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `TickCapture(path, *, flush_every=500)` | class | opens the CSV and writes the header immediately |
| `write(ts, price, volume=1, *, simtrade=False, code="", received=None)` | method | one row; flushes on cadence; `received` defaults to now |
| `close()` | method | flush + close; safe to call twice (also a context manager) |
| `read_ticks(path)` | function | read a capture back **typed**, for the aggregation diff |
| `FIELDS` | constant | `timestamp, received, code, price, volume, simtrade` |

## Two clocks per row
`timestamp` is the **exchange's**; `received` is **ours**, stamped when the loop
could first act on the tick. Bars are built on the first and closed on the second
([bars.py](bars.md)'s `flush`), and the gap between them is exactly what the
§2.16 flush grace must cover — so the file records both rather than leaving that
gap unobservable.

`received` is taken at *write* time, not enqueue time, and that is the meaningful
instant: `write` is called from `LiveRuntime.on_tick`, on the same single
consumer thread as `on_clock`, so it is what actually races the flush. It
therefore includes queue backlog as well as network lag — an enqueue-time stamp
would miss the backlog half.

## Key invariants / gotchas
- **Captured before filtering, deliberately.** simtrade prints and out-of-session
  ticks are written too, even though [the aggregator](bars.md) drops both. A file
  containing only what survived the filter **cannot be used to audit the filter** —
  and "were the simtrade prints really dropped?" is a question V1 has to answer.
- **Buffered, not flushed per line — the opposite of [Recorder](recorder.md).**
  That contrast is intentional: the recorder flushes every line so a `kill -9`
  loses at most one event, which is right for a few hundred decisions per session
  and wrong for a tick stream. Losing the tail of a diagnostic file is acceptable
  in a way that losing a fill record is not.
- **CSV, not JSONL.** The consumer is a bar-by-bar diff in pandas and a tick
  stream is uniform, so JSONL's per-event flexibility buys nothing and costs size.
- **`code` is recorded per tick, not once per session.** The contract behind an
  alias can change at rollover (V1b), and a session that spanned one would
  otherwise be indistinguishable from one that did not. The V3 run confirmed this
  matters: `TXFR1` arrives stamped `TXFH6`.
- **`read_ticks` coerces `simtrade` back to `bool`.** Left as the 0/1 ints CSV
  round-trips to, a replay would treat *every* tick as genuine — silently
  disabling the filter the replay exists to check.
- **`read_ticks` parses ISO-8601 explicitly** (`format="ISO8601"`). `_iso` omits
  the fraction when microseconds are zero, so a real session mixes
  `...T15:00:00` with `...T15:00:00.024000`; bare `pd.to_datetime` infers a format
  from the first row and raises on the first row that differs, which made every
  real capture unreadable while uniform fixtures passed (§2.17).
- **`received` is optional on read.** Four sessions were captured before the
  column existed — including the only one holding observed simtrade prints — and
  the V1 diff still reads those files, so a capture is never rejected for
  predating the schema.
- **`close()` matters more than it looks.** The final partial buffer holds the
  session's last minutes, which is precisely where end-of-session behaviour gets
  audited.

## Replay gotcha (found the hard way)
When replaying a capture through [TickAggregator](bars.md) to diff against
`kbars`, **flush only as far as the last tick the live process actually saw**.
Flushing past it fabricates a bar the live run never completed, and the diff then
reports a mismatch that exists only in the replay.

## Related
[live/bars.py](bars.md) · [live/recorder.py](recorder.md) · [live/runtime.py](runtime.md) · [live/cli.py](cli.md) · [SHIOAJI_PLAN.md §2.5](../../SHIOAJI_PLAN.md)
