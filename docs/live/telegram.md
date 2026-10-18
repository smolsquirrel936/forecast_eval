# [live/telegram.py](../../live/telegram.py)

**Role:** The Telegram transport for [alerts.py](alerts.md), and the `/settings` buttons that choose what it sends.
**Pipeline:** [cli.py](cli.md) and [owner_cli.py](owner_cli.md) call `attach` straight after building their `Alerter`; from then on every alert passes through it.
**Depends on:** [live/alerts.py](alerts.md), [live/credentials.py](credentials.md)   **Used by:** [live/cli.py](cli.md), [live/owner_cli.py](owner_cli.md), [tests/test_live_telegram.py](../tests.md)

## Responsibility
One bot, one chat, every deployment and the owner. Each `live/cli.py` process,
and the owner, registers a `TelegramNotifier` as an alerter sink, and every
message is prefixed with who sent it (`owner` for the owner):

```
[night] [TRADE] position_opened: LONG 1 @ 46200
[day] [FATAL] halt: position mirror disagrees with the venue | action=flatten simulation=True
```

**The level is a button, not a constant.** Send `/settings` to the bot (it is
also in the bot's menu) and it answers with four buttons:

| Button | Sends |
|---|---|
| All (INFO) | everything — trades, milestones, warnings, fatals |
| Trades + WARN + FATAL | fills, warnings and fatals; drops the routine re-logins |
| WARN + FATAL | warnings and fatals |
| FATAL only | fatals |

The choice is written to `outputs/telegram_level.json`, beside `fatal.jsonl`.
That is **one file for every deployment**: each process re-reads it on every
alert, so a press changes what all of them send, without a restart, and it
survives restarts. `level` in `[telegram]` applies only until the first press.
**FATAL is always sent**: no button mutes it.

## Setup
1. Create a bot with [@BotFather](https://t.me/BotFather) and copy its token.
2. Put it in `config/secrets.toml` (gitignored, and refused if git ever tracks it):
   ```toml
   [telegram]
   bot_token = "123456:ABC..."
   chat_id = "REPLACE_ME"
   level = "info"          # starting level: info | trade | warn | fatal
   ```
3. Send the bot any message, then run
   `uv run --no-sync python -m forecast_eval.live.telegram --find-chat` and copy
   the `chat_id` it prints into the section.
4. `uv run --no-sync python -m forecast_eval.live.telegram` sends a test message.

A section that still holds `REPLACE_ME` does not stop a run: `attach` raises a
`telegram_disabled` warning on the console and the run carries on without
Telegram. `--no-telegram` turns it off for one run.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `attach(alerter, *, deployment, level_file)` | function | load `[telegram]`, start the notifier, subscribe it to every level; None when not configured |
| `load_config()` | function | the `[telegram]` section as a `TelegramConfig`, or None; raises on placeholders |
| `TelegramNotifier(cfg, *, deployment, store, call=None, ...)` | class | the sink (`__call__`), the sender and poller threads, `close()`, `summary()` |
| `LevelStore(path, default)` | class | the shared setting: `get()`, `set(level)`, `allows(severity)` |
| `TelegramConfig` | dataclass | `token` (redacted in `repr`), `chat_id`, `level` |
| `main([--find-chat])` | CLI | find the chat id, or send a test message |

## Key invariants / gotchas
- **The sink only enqueues.** It runs inside halt paths and exception handlers,
  so the network is reached from a background thread with a 10 s timeout and
  three attempts. The queue is bounded (200), and overflow is counted as
  `telegram_dropped` rather than blocking.
- **Send failures are counted here, not in `alert_sink_failures`.** Sends are
  asynchronous, so the alerter never sees them fail. `telegram_failed` in the
  end-of-run summary is the number that tells a quiet night from a dead
  transport.
- **`close()` drains before exit.** `crashed` and `flatten_incomplete` are raised
  moments before the process ends, and both threads are daemons. `cli.main`
  closes the notifier at the end of its `finally`, and `attach` also registers
  `close` with `atexit` for the early `return`s that come before the `finally`.
- **Only the configured chat can change the level.** Anyone who finds a bot's
  name can message it, so a message or button press from any other chat is
  ignored. In a group chat, every member can press the buttons.
- **One poller per bot.** Telegram answers a second concurrent `getUpdates` with
  409. With two deployments running, they take turns, back off 5 s doubling to
  60 s, and count `telegram_poll_conflicts`. A button press is answered by
  whichever process holds the poll, and since the setting is shared it does not
  matter which. Sending is unaffected. `--find-chat` run during a live run hits
  the same 409.
- **A press made while nothing is polling** is picked up when the next run
  starts. The level is applied even though Telegram by then refuses to answer
  the stale button.
- **The token never reaches a message.** It is redacted in `TelegramConfig`'s
  `repr`, and nothing here formats the request URL into an error.
- **Tests never reach the real bot.** `tests/conftest.py` patches `load_config`
  to None for every test, because the trading box's `secrets.toml` carries a
  real section and the `cli.main` tests stub the venue but not that file.

## Related
[notifications.md](../notifications.md) (the usage manual) · [live/alerts.py](alerts.md) · [live/cli.py](cli.md) · [live/owner_cli.py](owner_cli.md) · [live/credentials.py](credentials.md) · [simtrade_operations.md §9](../simtrade_operations.md)
