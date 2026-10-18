# Notifications — the usage manual

**Role:** Everything an operator needs to get alerts from a live run onto a
phone, choose how many arrive, read them, and tell when delivery is broken.
**Covers:** the Telegram transport ([live/telegram.py](live/telegram.md)) on top
of the alert system ([live/alerts.py](live/alerts.md)), as used by the four
simtrade processes: the [owner](live/owner_cli.md) and the day, night and
fullday deployments ([live/cli.py](live/cli.md)).
**Not covered:** what to *do* about a halt or a refused start in depth. That is
[simtrade_operations.md](simtrade_operations.md) §6, which this page links to
where it matters.

> This is a **task page**, not a 1:1 module page, so it is listed beside
> `tests.md` in `test_docs_coverage.py`'s consolidated set. The module pages
> hold the design reasoning; this page holds the procedures.

Run every command from the repo root (`night/`), prefixed with
`uv run --no-sync`.

---

## 1. How it works

```
 rails halt, fill, re-login, crash, ...
              │
              ▼
          Alerter.emit ──► console (stderr for FATAL/WARN, stdout for INFO)
              │        ──► session.jsonl / owner_session.jsonl   (always)
              │        ──► fatal.jsonl                           (deployments' FATALs)
              ▼
     TelegramNotifier ── level check (telegram_level.json) ──► queue
                                                                 │
                                       background sender ◄──────┘
                                                 │
                                                 ▼
                                      Telegram Bot API ──► your chat

     /settings, button presses ──► background poller ──► telegram_level.json
```

- **Every process sends to the same bot and the same chat.** Each message starts
  with the process it came from: `[owner]`, `[day]`, `[night]` or `[fullday]`.
- **Telegram is an addition, never a replacement.** The console and the trails
  get every alert whether Telegram works or not, so a dead Telegram loses
  nothing that was not already on disk.
- **One setting steers all four.** The level you pick with `/settings` is one
  file, `outputs/telegram_level.json`, which every process re-reads on every
  alert.
- **It cannot stall a run.** Sending happens on a background thread; the
  trading loop only drops a message into a queue.

## 2. Quick start

About five minutes, once.

1. **Create a bot.** In Telegram, open **@BotFather** (blue check mark), send
   `/newbot`, give it a display name and a username ending in `bot`. Copy the
   **token** it replies with (`8123456789:AAH...`). Treat it like a password.
2. **Add the token** to the `[telegram]` section at the bottom of
   `config/secrets.toml`:
   ```toml
   [telegram]
   bot_token = "8123456789:AAH..."
   chat_id = "REPLACE_ME"
   level = "info"
   ```
3. **Find your chat id.** Open your bot in Telegram, press **Start** (or send it
   anything), then:
   ```powershell
   uv run --no-sync python -m forecast_eval.live.telegram --find-chat
   ```
   It prints `chat_id = 123456789    your_username`. Copy the number into
   `chat_id`.
4. **Send a test message:**
   ```powershell
   uv run --no-sync python -m forecast_eval.live.telegram
   ```
   It prints `sent — check the chat`, and the chat shows
   `[test] [INFO] telegram_test: forecast_eval can reach this chat...`.
5. **Start the simtrade as usual** ([simtrade_operations.md §5](simtrade_operations.md#5-restarting)).
   No extra flag is needed: the owner and every deployment attach Telegram on
   their own when the section is filled in.

Do steps 3 and 4 while nothing is running (§10 explains why).

## 3. Configuration reference

### 3.1 The `[telegram]` section

It lives in `config/secrets.toml`, the same file as the Shioaji keys.
`config/secrets.example.toml` carries a commented template.

| Key | Required | Value | Notes |
|---|---|---|---|
| `bot_token` | yes | the token from @BotFather | never printed: `TelegramConfig`'s `repr` shows `<redacted>` |
| `chat_id` | yes | the number `--find-chat` prints | quoted or bare both work (`"123"` or `123`); a group's id is negative |
| `level` | no | `info` (default), `warn` or `fatal` | **the starting level only** — used until the first `/settings` press, then the level file wins (§4) |

### 3.2 What happens with a bad or missing section

| State of the section | What a process does |
|---|---|
| absent | runs without Telegram, silently. This is the opt-out |
| `bot_token` or `chat_id` still `REPLACE_ME` (or empty) | prints `[WARN] telegram_disabled: [telegram] in ... still holds the example placeholder...` and runs without Telegram |
| `level` not one of the three | prints `[WARN] telegram_disabled: [telegram] level='...' is not one of [...]` and runs without Telegram |
| present and valid, but wrong (bad token, wrong chat) | runs, attaches Telegram, and every send fails: `telegram_failed` counts up (§8). Catch this with the test message before a run |
| `secrets.toml` tracked by git | refused: the same guard as the Shioaji keys ([credentials.md](live/credentials.md)). Rotate the bot token too |

A Telegram problem **never** refuses a start. It is the one transport that can
silently fail, so it is never allowed to be a precondition.

### 3.3 Turning it off

| Scope | How |
|---|---|
| one process, one run | add `--no-telegram` to its command. Both `live.cli` and `live.owner_cli` accept it |
| everything | delete the `[telegram]` section, or comment it out |
| only the noise | keep it on and press **FATAL only** in `/settings` (§4) |

### 3.4 A group chat instead of a private one

Add the bot to the group, post any message in the group, then run
`--find-chat`. The group's id is negative (`-1001234567890`). Everyone in the
group receives the alerts, and **everyone in the group can press the level
buttons**: the access check is the chat, not the person.

## 4. Choosing what is sent: `/settings`

Send `/settings` to the bot (or `/start`, or pick **settings** from the bot's
menu button — the entry appears once any process has started). It answers:

```
Alerts sent to this chat: All (INFO).
FATAL is always sent. The setting is shared by every deployment and survives restarts.
 [ ✓ All (INFO) ]
 [ WARN + FATAL ]
 [ FATAL only   ]
```

| Button | Sends | Typical use |
|---|---|---|
| All (INFO) | everything: fills, milestones, warnings, fatals | the default; a normal day is a message per fill plus re-login notices |
| WARN + FATAL | warnings and fatals | you trust the trading, want to hear about trouble |
| FATAL only | fatals | overnight, when only "act now" should buzz |

How the setting behaves:

- **FATAL is always sent.** No button mutes a halt, a crash or an exposed
  position.
- **Shared by all four processes.** A press is caught by whichever process is
  reading the bot at that moment and written to `outputs/telegram_level.json`;
  the others read that file on their next alert. No restart.
- **Kept across restarts.** The `level` in `secrets.toml` is consulted only
  while the file does not exist. A fresh `outputs/` therefore starts again at
  that default.
- **The buttons redraw** to move the ✓, and a small toast confirms
  `Now sending: ...`.
- **A press needs a running process to answer it.** With nothing running the
  press waits at Telegram and is applied when the next process starts —
  Telegram keeps unread updates for **24 hours**, so a press older than that is
  lost. With processes running, an answer can take **up to about a minute**
  (§10).
- **Only the configured chat counts.** Messages and presses from any other
  chat are ignored and counted, never acted on.

## 5. Reading a message

```
[night] [INFO] position_closed: closed @ 46350 — +150 points | side=SELL quantity=1 price=46350.0 points=150.0 position_after=0 bar_idx=812
└──┬──┘ └─┬──┘ └─────┬──────┘ └──────────────┬──────────────┘ └────────────────────────────┬────────────────────────────────┘
 sender  level      kind                 message                               detail (key=value, when present)
```

- **sender** — `owner`, `day`, `night` or `fullday` (the `--strategy` name, not
  the file slug: fullday's files say `day_night`, its messages say `fullday`).
- **level** — `FATAL`, `WARN` or `INFO` (§6).
- **kind** — a fixed machine tag; the catalogue in §6 lists every one.
- **message** — the sentence meant for a human.
- **detail** — structured fields, appended after `|`. Prices are in index
  points; `points` is that trade's own PnL, not the running total.

Messages are plain text (no Markdown), and Telegram's 4 096-character cap
applies: a longer one — a crash with a long path, say — is cut at 4 096.

## 6. Alert catalogue

What each alert means and what to do. "Ops" links point into
[simtrade_operations.md](simtrade_operations.md).

### 6.1 FATAL — act now

| Kind | Sender | Means | Do |
|---|---|---|---|
| `halt` | deployment | the safety rails latched. The message is the reason; `action=stop` or `action=flatten`. Reasons include `no market data for …s`, `position mirror X != venue Y`, `order rejected op_code=…`, `order rate … exceeds cap …`, `daily loss … >= limit …`, `kill file present: …`, and under the owner `client broker: owner silent for …s` | read the reason against Ops §6.7. Sent **once** per halt, not once per bar |
| `flatten_incomplete` | deployment | a FLATTEN halt's close did not fill; the process exited (code 4) **holding a position with nothing watching it** | the most serious alert there is. Close the position in the broker's front end, now |
| `crashed` | both | an unhandled exception ended the process. Deployments add `error_type` and `where` (file:line); the owner adds `error_type` | read the console traceback; positions are left as they were, unmanaged |
| `startup_refused` | both | a start was refused before trading. `gate=` says which: `preflight` (flags), `RestartGuard` (backoff, backfill cap, quota), `adopt_position` (cannot adopt the held lot, e.g. an exit rule is declared), `subscribe_trade` (owner exit 7), `reconcile` (owner exit 6: ledgers and venue disagree); also a non-flat `--fresh-start` | the message names the fix; Ops §5, §6.2, §6.9 |
| `owner_frozen` | owner | a running reconcile disagreed; the owner places nothing more, and each deployment halts when it next submits | reconcile by hand (Ops §6.7, §6.9), then restart everything |
| `owner_session_lost` | owner | the owner's venue calls kept failing through 4 re-logins: its session is dead, so **no deployment can place or hear an order** | restart the owner, then check the deployments |
| `session_unverified` | both | the pre-open check failed and recovery failed: the token or feed is dead as the session opens. From the owner it means **no deployment can place an order** | restart the process (owner first if it is the owner) |
| `no_credentials`, `bad_credentials` | deployment; owner `no_credentials` | no usable Shioaji key for this process | fix `config/secrets.toml` |
| `roll_unsettled` | deployment | the contract rolled with a position open and no price to settle it at; the position mirror is stale | compare the venue with the ledgers before trusting the run (Ops §6.5) |
| `roll_no_next_month` | deployment | the old leg closed and no later month is listed; the run is flat and cannot re-enter | restart after the new month lists |

### 6.2 WARN — handled, but you would want to know

| Kind | Sender | Means |
|---|---|---|
| `feed_missed` | deployment | the venue traded without us; resubscribing. Sent when the feed's verdict *changes*, not every minute |
| `feed_unreachable` | deployment | cannot reach the venue to check the feed. If it lasts 3-5 checks (~3-5 min; day, night, fullday in that order) the deployment re-logs in by itself |
| `session_relogin_failed` | both | a re-login after a lost session failed. A deployment retries after 3 more failed checks, and its 900 s backstop halts if none heals it; the owner retries after 3 more unanswered reconciles, up to 4 times, then sends `owner_session_lost` |
| `session_down` | deployment | the quote session dropped |
| `relogin_failed` | both | a re-login attempt failed; it retries inside the window |
| `relogin_abandoned` | both | the re-login window closed with no fresh token — expect `session_unverified` at the open |
| `account_changed` | deployment | the account behind this run differs from the previous run's (digests and broker fields only) |
| `fills_replay_refused` | deployment | the ledger replays to a different position than the venue holds; the replay was skipped |
| `contract_rolled` | deployment | front month changed; re-subscribed and re-pointed |
| `roll_deferred` | deployment | the incoming month is not printing yet; not rolling on this tick |
| `roll_window_lapsed` | deployment | the roll window closed without rolling; the fallback settles the position instead of trading it |
| `roll_exposure_resumed` | deployment | re-opening the position on the new contract after the roll |
| `deployment_silent` | owner | no intents from a deployment for the timeout; its positions are still open and counted. Sent when the set of silent deployments changes; the console repeats it every 30 s |
| `traffic_low` | owner | the daily data allowance is below the floor; the next start-up will be refused. Sent once until the headroom recovers |
| `venue_unanswered` | owner | the venue did not answer the start-up reconcile; the owner started on the ledgers alone |
| `telegram_disabled` | both | the `[telegram]` section is incomplete. Console only — Telegram is, by definition, not attached |

### 6.3 INFO — milestones and trades

| Kind | Sender | Means |
|---|---|---|
| `position_opened` | deployment | a fill opened a position: `LONG 1 @ 46200`. Sent on the **fill**, never on the order |
| `position_closed` | deployment | a fill closed it, with **this trade's** PnL in points |
| `resumed` | deployment | `--resume` adopted a held position (from the venue or the ledger) |
| `relogin` | both | the token was refreshed before an open: deployments at about 08:40 and 14:55, the owner at about 08:37 and 14:52, weekends included |
| `session_relogin` | both | a lost session was re-logged into between the anchors: a deployment after 3-5 failed feed checks, the owner after 3 unanswered reconciles with `usage()` failing too |
| `feed_recovered` | deployment | the feed is delivering again after a `feed_*` warning |
| `roll_started` | deployment | the scheduled roll to the next month is beginning |
| `owner_agrees_again` | owner | the venue and the book match again while frozen; still frozen until restarted |

What is deliberately **not** sent: process start and clean stop, every bar,
every forecast and signal, orders that have not filled, and the owner's
console-only refusals (missing `--i-understand-this-places-orders`, a missing
`--out` directory) — those are typos at a terminal someone is looking at.

## 7. Delivery: what is guaranteed and what is not

Delivery is **best-effort, by decision**: the alert most worth sending is often
about a network fault, which is exactly when sending fails. Losing a message is
acceptable; losing the run to the notifier is not.

| Stage | Behaviour |
|---|---|
| queueing | instant; never blocks. The queue holds **200** messages; beyond that a message is dropped and counted |
| sending | one background thread, **10 s** timeout per attempt, **3 attempts** with 2 s and 4 s pauses; then counted as failed |
| ordering | messages from one process arrive in the order raised. Between processes, no order is promised |
| shutdown | the queue gets up to **15 s** to drain before the process exits — after a Ctrl-C, a crash, a refused start, or `flatten_incomplete`. A message raised after that drain is dropped |
| killed process | a process killed outright (window closed, Task Manager, reboot) drains nothing: whatever was still queued is lost. The trails still hold it |

Where to look when a message may have been lost: the deployment's
`live_<slug>_<ts>/session.jsonl` (`"kind": "alert"` records), `fatal.jsonl` for
deployment FATALs, and `owner_session.jsonl` for the owner.

## 8. The counters

Every process prints these with its end-of-run summary
([simtrade_operations.md §4](simtrade_operations.md#4-stopping-cleanly)), after
the drain, so they are final:

| Counter | Counts | Healthy |
|---|---|---|
| `telegram_sent` | messages Telegram accepted | > 0 on any run that raised an alert at your level |
| `telegram_failed` | messages that failed all 3 attempts | **0**. Above 0, messages were lost — see §11 |
| `telegram_filtered` | alerts held back by the `/settings` level | anything; it is your choice working |
| `telegram_dropped` | queue full, or raised after the drain | 0 |
| `telegram_poll_conflicts` | button polls refused because another process was reading the bot | grows normally with several processes (§10) |
| `telegram_poll_errors` | button polls that failed for another reason, or a malformed update | small. A steady climb means the network or the token |

`alert_sink_failures` (printed just above) does **not** include Telegram
failures: Telegram sends from a background thread, so they never reach the
alerter. `telegram_failed` is the number that tells a quiet night from a dead
transport.

## 9. Files

| File | Written by | Content |
|---|---|---|
| `config/secrets.toml` `[telegram]` | you | token, chat id, starting level. Gitignored |
| `outputs/telegram_level.json` | the process that caught a `/settings` press | `{"level": "warn", "by": "night", "set_at": "2026-10-12T09:01:33"}`; `by` is the process that caught the press, not who pressed. Safe to delete: the next alert falls back to `level` in `secrets.toml` |
| `outputs/fatal.jsonl` | deployments | every deployment FATAL, all deployments in one file, whether or not Telegram delivered it |
| `outputs/owner_session.jsonl` | owner | the owner's trail; its alerts appear as `"kind": "alert"` records. Owner FATALs are **not** in `fatal.jsonl`: the status page attributes that file's entries to deployment soaks by timing |
| `live_<slug>_<ts>/session.jsonl` | each deployment | every alert of that run, as `"kind": "alert"` records |

The level file sits in the directory the processes share — `outputs/` for the
deployments' default `--state-file`, and the owner's `--out`. Point them at
different directories and they get different settings.

## 10. Several processes, one bot

Telegram lets only **one** process at a time read a bot's incoming messages. A
second reader is answered `409 Conflict`. With four processes up:

- **Sending is unaffected.** Any number of processes can send at once.
- **Reading button presses takes turns.** Each process that is refused backs
  off 5 s, doubling to 60 s, and counts a `telegram_poll_conflict`. Some process
  is nearly always reading, so a press is answered — usually in seconds, at
  worst in about a minute. Because the setting is shared, it does not matter
  which process answers.
- **`--find-chat` during a run** is one more reader, and fails with
  `HTTP Error 409: Conflict`. Run it while nothing is up.

## 11. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `--find-chat` prints `no messages yet` | you have not messaged the bot, or another reader took the messages | send the bot a message, stop any running process, rerun |
| `--find-chat` prints `put bot_token in [telegram] ... first` | the token is still `REPLACE_ME` | step 2 of §2 |
| test send: `HTTP Error 401: Unauthorized` | wrong or revoked token | copy it again from @BotFather (`/mybots` → API Token) |
| test send: `HTTP Error 400: Bad Request` | wrong `chat_id` | rerun `--find-chat`; a group id keeps its minus sign |
| test send: `HTTP Error 403: Forbidden` | you blocked the bot, or never pressed Start | unblock it / press Start, rerun |
| test send: traceback ending in `still holds the example placeholder` | `chat_id` still `REPLACE_ME` | step 3 of §2 |
| a run prints `[WARN] telegram_disabled` | the section is incomplete | §3.2 |
| nothing arrives during a run, `telegram_sent` 0, `telegram_failed` 0 | nothing was raised at your level, or the section is absent | check `/settings`; check the start-up console for `telegram_disabled` |
| nothing arrives, `telegram_failed` > 0 | network down, or token/chat wrong | run the test send; check the machine's connection |
| `/settings` gets no answer | no process is running, or they are all backing off | wait a minute; with nothing running, the press is applied at the next start (within 24 h) |
| a button press is ignored | it came from a different chat than `chat_id` | use the configured chat |
| too many messages | INFO is on | press **WARN + FATAL** or **FATAL only** |
| the `/settings` entry is missing from the bot menu | no process has started since the bot was created | start any process once; or just type `/settings` |

## 12. Privacy

Messages leave the machine and are stored by Telegram. What they carry:

- **Carried:** prices, positions, per-trade PnL, halt reasons, the account
  fingerprint's *digests* and broker fields (in `account_changed`), and in
  `crashed` the exception text and a file path.
- **Not carried:** the Shioaji keys, the bot token, or the account's
  `person_id`. The national ID that Shioaji prints on the **console** never goes
  through the alerter.
- The chat id is the only access control on the buttons; keep the bot's
  username to yourself, or use a private chat.

## 13. For developers

- **Raising a new alert:** call `alerter.fatal / warn / info(kind, message,
  **detail)` at the call site. Telegram picks it up with no other change. Pick
  `kind` as a stable snake_case tag, and add it to §6. A condition that
  repeats every few seconds must alert on a *change* only (see
  `owner_cli._serve`'s `traffic_alerted` / `stale_alerted`).
- **Tests never reach the real bot.** `tests/conftest.py` patches
  `telegram.load_config` to `None` for every test, because the trading box's
  `secrets.toml` carries a real section. Telegram's own tests
  (`tests/test_live_telegram.py`) inject a fake `call(method, payload,
  timeout)`; new tests should do the same.
- **Another transport** (e-mail, a second chat) is one
  `alerter.add_sink(fn, levels=...)`; a new sink defaults to FATAL only. See
  [alerts.md](live/alerts.md).

## Related

[live/telegram.md](live/telegram.md) · [live/alerts.md](live/alerts.md) ·
[simtrade_operations.md §9](simtrade_operations.md#9-alerts-on-your-phone) ·
[live/cli.md](live/cli.md) · [live/owner_cli.md](live/owner_cli.md) ·
[live/credentials.md](live/credentials.md)
