# [live/ioc_probe.py](../../live/ioc_probe.py)

**Role:** Answer the one question that blocks the roll's escalating-aggression ladder — does an expired IOC report anything?
**Pipeline:** Standalone. Run by the operator inside a session; reports a paste-back block.
**Depends on:** [contracts.py](contracts.md), [v72_probe.py](v72_probe.md) for the unfillable-price helper   **Used by:** nothing — it informs [live/roll.py](roll.md) and [broker/shioaji.py](broker/shioaji.md)

## Why it exists
The scheduled roll (§2.6) wants an IOC priced through the touch, re-priced every
few seconds until it fills. IOC is the right time-in-force: there is nothing
resting to cancel, so the retry *is* the re-price and §2.2's cancel/replace race
never happens.

One measured fact blocks it (§2.19, 2026-08-12):

```
an expired IOC left has_pending() True
```

If that still holds, every retry in the ladder is blocked by the pending check
and the ladder is **inert while logging beautifully** — the same failure shape as
the plain-`login()` no-op that would have made the §2.24 fix hollow. So this is
measured before anything is built.

| | Question |
|---|---|
| **Q1** | does an IOC that expires unfilled produce an order callback at all? |
| **Q2** | what does it carry — `op_type`, `op_code`, `status`? Anything `_handle_order` could key on? |
| **Q3** | how long until it arrives? The ladder's retry interval cannot be shorter, or attempt N+1 races attempt N's expiry |
| **Q4** | is it consistent, or was 2026-08-12 a one-off? |

## What it measured — 2026-08-24, day session
| | Answer |
|---|---|
| **Q1** | **No.** The only callback is the submission ack, `operation.op_type='New'` / `op_code='00'`. Nothing follows it through 10 s and an `update_status()` |
| **Q2** | Moot — there is no expiry message to carry anything |
| **Q3** | Ack in 0.029–0.043 s; a *cancel* reports in 0.109 s, so the reporting path is ~100 ms when the venue uses it |
| **Q4** | Consistent — 3/3, twice |

So `has_pending()` would stay True forever on the callback path, `IOC_TTL_SECONDS`
is load-bearing, and the ladder is unchanged
([SHIOAJI_PLAN §2.6](../../SHIOAJI_PLAN.md), *MEASURED 2026-08-24*). It proves the
venue never **pushes** an expiry; whether `trade.status` shows one after
`update_status()` is untested and unused.

## Running it
**It must run inside a trading session.** Off-hours the venue *rejects* an order
rather than expiring it, and "rejected because the market is closed" is a
different answer to "expired because nothing matched" — one that looks like a
measurement while telling us nothing. The probe aborts if the snapshot returns no
usable close.

```powershell
$env:SHIOAJI_API_KEY="..."; $env:SHIOAJI_SECRET_KEY="..."
uv run --no-sync python -m forecast_eval.live.ioc_probe --i-understand-this-places-orders
```

| Flag | Default | |
|---|---|---|
| `--attempts` | 3 | how many IOCs to place |
| `--wait` | 10 | seconds to listen after each |
| `--offset` | 200 | points **below** the market to price the BUY |
| `--control-rod` | off | also place an unfillable ROD and cancel it, to compare a cancellation against an expiry |

`--control-rod` is worth one extra order: if an expiry reports the same way a
cancellation does, the adapter may already retire it and the ladder needs no
change at all. That is the cheapest possible outcome.

## Safety
- **`simulation=True` is passed to the constructor, and that is the guarantee.**
  `api.simulation` reads back as `None` — shioaji 1.7.2 has no such attribute
  (§7.2) — so the guard aborts only on an explicit `False` and can never
  actually fire, exactly as in [v72_probe](v72_probe.md). It read `is not True`
  until 2026-08-24, which aborted every run before an order was placed.
- **Every order is unfillable by construction**: a BUY priced 200 points *below*
  the market, one lot. An IOC there cannot fill — it can only expire, which is
  the thing being measured. 200 points on a ~45 000 index is 0.4 %: far outside
  any plausible ten-second print, nowhere near the ±10 % daily limit that would
  get it rejected instead of accepted.
- **Two refusal gates before anything happens** —
  `--i-understand-this-places-orders`, then the credential check.
- The ROD control is cancelled in a `finally`, so nothing is left resting.

## The output is redacted; the lines above it are not
`redact()` runs two passes — identifying fields, then any bare national-ID-shaped
token — because **the leak arrived by both routes** on the §2.24 probe. The
paste-back block says so explicitly, because the SDK prints its own banner above
it carrying `#P2P/…/L1255…/…`, and that is not ours to redact.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `redact(text)` | function | field pass, then shape pass |
| `message_facts(msg)` | function | the fields `_handle_order` dispatches on, plus the raw repr |
| `verdict(events, waited)` | function | one attempt's callbacks reduced to reported / latency / op types |
| `summarise(findings)` | function | the paste-back block |
| `DEFAULT_OFFSET` | constant | 200 points below the market |

`verdict` deliberately says what arrived and how fast, and **does not judge
whether it is enough** — whether a message can retire an order is a reading made
against the adapter, not here. A probe that decides the design is a probe that
can be wrong twice (§2.19).

## Related
[live/roll.py](roll.md) · [live/broker/shioaji.py](broker/shioaji.md) · [live/v72_probe.py](v72_probe.md) · [live/relogin_probe.py](relogin_probe.md) · [SHIOAJI_PLAN.md §2.6](../../SHIOAJI_PLAN.md)
