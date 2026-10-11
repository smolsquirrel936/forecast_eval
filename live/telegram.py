"""Telegram — the transport `live/alerts.py` was built to take.

One bot, one chat, every deployment. Each process registers a
`TelegramNotifier` as an `Alerter` sink; messages carry the deployment as a
prefix (`[night] [INFO] position_opened: ...`) so one chat can serve all three.

**The level is a setting, not a constant.** Send `/settings` to the bot and it
answers with three buttons — *All (INFO)*, *WARN + FATAL*, *FATAL only*. The
choice is written to one `telegram_level.json` beside the restart state, which
every deployment reads on every alert, so a press in the chat changes what all
of them send, and it survives restarts. `level` in `[telegram]` is only the
value used until the first press. **FATAL is always sent**: no button mutes it.

**Nothing here may block the run.** Sends go through a queue to a background
thread with a short timeout; button presses are read by a second thread that
long-polls `getUpdates`. Telegram allows one poller per bot, so concurrent
deployments take turns and answer 409 to each other — that is counted, not
fatal, and sending is unaffected.

    python -m forecast_eval.live.telegram --find-chat   # print chat ids that wrote to the bot
    python -m forecast_eval.live.telegram               # send a test message
"""
from __future__ import annotations

import argparse
import atexit
import json
import os
import queue
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from forecast_eval.live import credentials
from forecast_eval.live.alerts import ALL, Alert, Alerter, Severity

API = "https://api.telegram.org/bot{token}/{method}"

# Tech: per-request timeouts — a send, and the long poll's server-side wait.
# Why:  a send that hangs holds a queue slot, never the trading loop; ten
#       seconds is long for one HTTPS round trip and short enough that the
#       shutdown drain finishes. The poll's HTTP timeout must outlast its own
#       server-side wait, or every quiet poll would look like a network error.
SEND_TIMEOUT_S = 10.0
POLL_WAIT_S = 25
TEXT_LIMIT = 4096                     # Telegram's own sendMessage cap

# Tech: setting value -> the lowest severity sent, and the button label.
# Why:  stored as the lowercase severity name so the file reads as what it
#       means; FATAL-only is the floor because FATAL is never filtered.
LEVELS: Dict[str, Severity] = {"info": Severity.INFO, "warn": Severity.WARN,
                               "fatal": Severity.FATAL}
LABELS = {"info": "All (INFO)", "warn": "WARN + FATAL", "fatal": "FATAL only"}
_RANK = {Severity.INFO: 0, Severity.WARN: 1, Severity.FATAL: 2}


@dataclass(frozen=True)
class TelegramConfig:
    # Tech: the `[telegram]` section — bot token, chat id, starting level.
    #       `__repr__` hides the token.
    # Why:  the token is a credential (anyone holding it can read and write the
    #       chat), and this object travels through tracebacks like `Credential`.
    token: str
    chat_id: str
    level: str = "info"

    def __repr__(self) -> str:
        return (f"TelegramConfig(token=<redacted>, chat_id={self.chat_id!r}, "
                f"level={self.level!r})")


def _section() -> Optional[dict]:
    table = credentials._read_secrets().get("telegram")
    return table if isinstance(table, dict) else None


def load_config() -> Optional[TelegramConfig]:
    """The `[telegram]` section of `config/secrets.toml`, or None when absent."""
    # Tech: read through `credentials._read_secrets`, reject placeholders and
    #       unknown levels with a message naming the line to edit.
    # Why:  the same file, so the same guard — a git-tracked `secrets.toml` is
    #       refused here exactly as it is for the API keys. Placeholders raise
    #       rather than return None because a half-filled section is a mistake
    #       the operator should hear about, not a silent opt-out.
    table = _section()
    if table is None:
        return None
    token = str(table.get("bot_token", "")).strip()
    chat_id = str(table.get("chat_id", "")).strip()
    level = str(table.get("level", "info")).strip().lower()
    if token in credentials._PLACEHOLDERS or chat_id in credentials._PLACEHOLDERS:
        raise RuntimeError(
            f"[telegram] in {credentials.SECRETS_FILE} still holds the example "
            f"placeholder. Fill in bot_token and chat_id (python -m "
            f"forecast_eval.live.telegram --find-chat), or delete the section.")
    if level not in LEVELS:
        raise RuntimeError(f"[telegram] level={level!r} is not one of "
                           f"{sorted(LEVELS)}")
    return TelegramConfig(token, chat_id, level)


def _http_call(token: str, method: str, payload: dict, timeout: float) -> Any:
    # Tech: one Bot API call as a JSON POST; returns `result`, raises on `ok: false`.
    # Why:  stdlib only — the venv carries no `requests`, and adding a
    #       dependency for four endpoints is not worth a `uv pip install` on the
    #       trading box. Error messages never include the URL, which holds the token.
    req = urllib.request.Request(
        API.format(token=token, method=method),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    if not body.get("ok"):
        raise RuntimeError(f"telegram {method}: {body.get('description')}")
    return body.get("result")


class LevelStore:
    def __init__(self, path: Path, default: str = "info"):
        # Tech: the shared setting, re-read from disk on every `get`.
        # Why:  several deployments read one file and whichever process caught
        #       the button press wrote it; re-reading is what makes a press reach
        #       all of them. Alerts are rare, so the read costs nothing. `_last`
        #       answers when the file is missing, torn mid-replace, or locked.
        self.path = Path(path)
        self._last = default

    def get(self) -> str:
        try:
            level = json.loads(self.path.read_text(encoding="utf-8")).get("level")
            if level in LEVELS:
                self._last = level
        except Exception:                       # noqa: BLE001 — see __init__
            pass
        return self._last

    def set(self, level: str, *, by: str = "") -> None:
        # Tech: write to a per-process temp file, then `os.replace` over the target.
        # Why:  a reader in another process must see the old file or the new one,
        #       never half of either.
        if level not in LEVELS:
            raise ValueError(f"unknown level {level!r}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"level": level, "by": by,
                                   "set_at": time.strftime("%Y-%m-%dT%H:%M:%S")}),
                       encoding="utf-8")
        os.replace(tmp, self.path)
        self._last = level

    def allows(self, severity: Severity) -> bool:
        # Tech: FATAL always passes; otherwise compare against the setting's floor.
        # Why:  a halt or an exposed position must reach the phone whatever the
        #       chat was last set to — muting it is not a choice this offers.
        if severity is Severity.FATAL:
            return True
        return _RANK[severity] >= _RANK[LEVELS[self.get()]]


class TelegramNotifier:
    def __init__(self, cfg: TelegramConfig, *, deployment: str, store: LevelStore,
                 call: Optional[Callable[[str, dict, float], Any]] = None,
                 send_attempts: int = 3, retry_backoff_s: float = 2.0,
                 queue_size: int = 200):
        # Tech: a queue, a sender thread, a poller thread, and counters.
        # Why:  `__call__` runs inside halt paths, so it only enqueues. `call` is
        #       the Bot API seam — tests pass a fake, production binds the token.
        #       The queue is bounded so a dead network cannot grow memory all
        #       night; overflow is counted as `dropped`.
        self.cfg = cfg
        self.deployment = deployment
        self.store = store
        self._call = call or (lambda m, p, t: _http_call(cfg.token, m, p, t))
        self.send_attempts = send_attempts
        self.retry_backoff_s = retry_backoff_s
        self._queue: "queue.Queue[Optional[dict]]" = queue.Queue(maxsize=queue_size)
        self._stop = threading.Event()
        self._closed = False
        self._sender: Optional[threading.Thread] = None
        self._poller: Optional[threading.Thread] = None
        self._poll_delay = 0.0
        self._offset: Optional[int] = None
        self.sent = self.failed = self.filtered = self.dropped = 0
        self.poll_conflicts = self.poll_errors = self.ignored = 0

    def start(self, *, poll: bool = True) -> None:
        self._sender = threading.Thread(target=self._send_loop, daemon=True,
                                        name="telegram-send")
        self._sender.start()
        if poll:
            self._poller = threading.Thread(target=self._poll_loop, daemon=True,
                                            name="telegram-poll")
            self._poller.start()

    # ---- the sink -----------------------------------------------------------

    def __call__(self, alert: Alert) -> None:
        # Tech: filter on the shared level, prefix the deployment, enqueue.
        # Why:  registered with `levels=ALL` so the filter is the live setting,
        #       not the level fixed at `add_sink` time.
        if not self.store.allows(alert.severity):
            self.filtered += 1
            return
        self._enqueue(f"[{self.deployment}] {alert.format()}")

    def _enqueue(self, text: str, **extra: Any) -> None:
        if self._closed:
            self.dropped += 1
            return
        try:
            self._queue.put_nowait({"chat_id": self.cfg.chat_id,
                                    "text": text[:TEXT_LIMIT], **extra})
        except queue.Full:
            self.dropped += 1

    def _send_loop(self) -> None:
        while True:
            payload = self._queue.get()
            if payload is None:
                return
            self._deliver(payload)

    def _deliver(self, payload: dict) -> None:
        # Tech: up to `send_attempts` tries with a growing pause; count the outcome.
        # Why:  the alert worth sending is often about a network blip, which a
        #       few seconds may outlast. Past that it is lost, by decision
        #       (alerts.py) — the trail and the fatal journal still hold it.
        for attempt in range(self.send_attempts):
            try:
                self._call("sendMessage", payload, SEND_TIMEOUT_S)
                self.sent += 1
                return
            except Exception:                   # noqa: BLE001 — best-effort
                if attempt + 1 < self.send_attempts:
                    time.sleep(self.retry_backoff_s * (attempt + 1))
        self.failed += 1

    # ---- the settings button -------------------------------------------------

    def _poll_loop(self) -> None:
        # Tech: register `/settings` in the bot's menu, then poll until stopped,
        #       backing off 5 s doubling to 60 s after a failed poll.
        # Why:  the menu entry is what makes the setting one tap away. Backoff
        #       because the usual failure is another deployment holding the poll
        #       (409) — hammering it would just kick that one off in turn.
        self._best_effort("setMyCommands", {"commands": [
            {"command": "settings", "description": "choose which alerts are sent"}]})
        while not self._stop.is_set():
            if not self._poll_once():
                self._stop.wait(self._poll_delay)

    def _poll_once(self) -> bool:
        # Tech: one long poll; advance the offset past every update seen and hand
        #       each to `handle_update`. Returns False when the poll itself failed.
        # Why:  the offset is what confirms an update to Telegram, so it moves
        #       even when handling one raises — a malformed update must not be
        #       redelivered on every poll for the rest of the night.
        payload: Dict[str, Any] = {"timeout": POLL_WAIT_S,
                                   "allowed_updates": ["message", "callback_query"]}
        if self._offset is not None:
            payload["offset"] = self._offset
        try:
            updates = self._call("getUpdates", payload, POLL_WAIT_S + 10) or []
        except Exception as exc:                # noqa: BLE001 — counted
            if getattr(exc, "code", None) == 409:
                self.poll_conflicts += 1
            else:
                self.poll_errors += 1
            self._poll_delay = min(60.0, max(5.0, self._poll_delay * 2))
            return False
        self._poll_delay = 0.0
        for update in updates:
            self._offset = update["update_id"] + 1
            try:
                self.handle_update(update)
            except Exception:                   # noqa: BLE001 — counted
                self.poll_errors += 1
        return True

    def handle_update(self, update: dict) -> None:
        # Tech: answer `/settings` (or `/start`) with the keyboard; apply a
        #       `level:<name>` button press. Anything from another chat is ignored.
        # Why:  a bot is reachable by anyone who finds its name, so the chat id
        #       is the access check — only the configured chat can change what
        #       the trading box sends.
        msg = update.get("message")
        if msg is not None:
            if str(msg.get("chat", {}).get("id")) != self.cfg.chat_id:
                self.ignored += 1
                return
            words = (msg.get("text") or "").split()
            command = words[0].split("@")[0].lower() if words else ""
            if command in ("/settings", "/start"):
                self._enqueue(self._settings_text(),
                              reply_markup=self._keyboard())
            return

        cq = update.get("callback_query")
        if cq is None:
            return
        message = cq.get("message") or {}
        chat_id = message.get("chat", {}).get("id")
        data = cq.get("data") or ""
        level = data.split(":", 1)[1] if data.startswith("level:") else None
        if str(chat_id) != self.cfg.chat_id or level not in LEVELS:
            self.ignored += 1
            return
        self.store.set(level, by=self.deployment)
        self._best_effort("answerCallbackQuery",
                          {"callback_query_id": cq.get("id"),
                           "text": f"Now sending: {LABELS[level]}"})
        self._best_effort("editMessageText",
                          {"chat_id": chat_id, "message_id": message.get("message_id"),
                           "text": self._settings_text(),
                           "reply_markup": self._keyboard()})

    def _settings_text(self) -> str:
        return (f"Alerts sent to this chat: {LABELS[self.store.get()]}.\n"
                f"FATAL is always sent. The setting is shared by every "
                f"deployment and survives restarts.")

    def _keyboard(self) -> dict:
        current = self.store.get()
        return {"inline_keyboard": [[
            {"text": ("✓ " if name == current else "") + LABELS[name],
             "callback_data": f"level:{name}"}] for name in LEVELS]}

    def _best_effort(self, method: str, payload: dict) -> None:
        try:
            self._call(method, payload, SEND_TIMEOUT_S)
        except Exception:                       # noqa: BLE001 — cosmetic calls
            pass

    # ---- shutdown -----------------------------------------------------------

    def close(self, timeout: float = 15.0) -> None:
        """Stop polling and give queued messages `timeout` seconds to go out."""
        # Tech: idempotent; stop the poller, queue the sender's sentinel, join it.
        # Why:  the last alerts of a run — `crashed`, `flatten_incomplete` — are
        #       raised moments before exit, and both threads are daemons that die
        #       with the interpreter. Without this drain the most important
        #       message of the night is the one most likely to be lost.
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        if self._sender is None:
            return
        try:
            self._queue.put(None, timeout=1.0)
        except queue.Full:
            pass
        self._sender.join(timeout)

    def summary(self) -> Dict[str, int]:
        return {"telegram_sent": self.sent, "telegram_failed": self.failed,
                "telegram_filtered": self.filtered, "telegram_dropped": self.dropped,
                "telegram_poll_conflicts": self.poll_conflicts,
                "telegram_poll_errors": self.poll_errors}


def attach(alerter: Alerter, *, deployment: str,
           level_file: Path) -> Optional[TelegramNotifier]:
    """Register Telegram on `alerter` if `[telegram]` is configured."""
    # Tech: load the section; on a config error warn and carry on without it;
    #       otherwise start the notifier, subscribe it to every level, and
    #       register its drain at interpreter exit.
    # Why:  Telegram is an addition to the console and the trail, never a
    #       precondition — a typo in its section must not refuse a run. The
    #       `atexit` drain covers the early `return`s in `cli.main` that happen
    #       before its `finally` exists.
    try:
        cfg = load_config()
    except RuntimeError as exc:
        alerter.warn("telegram_disabled", str(exc))
        return None
    if cfg is None:
        return None
    notifier = TelegramNotifier(cfg, deployment=deployment,
                                store=LevelStore(level_file, cfg.level))
    notifier.start()
    alerter.add_sink(notifier, levels=ALL)
    atexit.register(notifier.close)
    return notifier


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Check the [telegram] setup.")
    ap.add_argument("--find-chat", action="store_true",
                    help="print the chats that recently messaged the bot "
                         "(send it any message first). Needs only bot_token.")
    args = ap.parse_args(argv)

    # Tech: --find-chat reads bot_token straight from the section, before the
    #       full config is valid.
    # Why:  finding the chat id is the step *before* chat_id can be filled in,
    #       so it cannot go through `load_config`, which rejects the placeholder.
    if args.find_chat:
        table = _section() or {}
        token = str(table.get("bot_token", "")).strip()
        if token in credentials._PLACEHOLDERS:
            print(f"put bot_token in [telegram] in {credentials.SECRETS_FILE} first")
            return 2
        updates = _http_call(token, "getUpdates", {"timeout": 0}, SEND_TIMEOUT_S)
        chats = {}
        for u in updates or []:
            chat = (u.get("message") or {}).get("chat")
            if chat:
                chats[chat["id"]] = chat.get("title") or chat.get("username") or ""
        if not chats:
            print("no messages yet — send the bot any message, then rerun")
            return 1
        for chat_id, name in chats.items():
            print(f"chat_id = {chat_id}    {name}")
        return 0

    cfg = load_config()
    if cfg is None:
        print(f"no [telegram] section in {credentials.SECRETS_FILE}")
        return 2
    _http_call(cfg.token, "sendMessage",
               {"chat_id": cfg.chat_id,
                "text": "[test] [INFO] telegram_test: forecast_eval can reach "
                        "this chat. Send /settings to choose the alert level."},
               SEND_TIMEOUT_S)
    print("sent — check the chat")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
