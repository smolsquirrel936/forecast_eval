"""Telegram — the alert transport, and the `/settings` buttons that steer it.

What must hold:

  * the level is one shared file: a press caught by one deployment changes what
    every deployment sends, and survives a restart;
  * FATAL is sent whatever the setting;
  * the sink never raises and never blocks — sends are queued, failures counted;
  * only the configured chat can change the level;
  * the shutdown drain delivers what was queued last.

The Bot API is a fake `call(method, payload, timeout)`; nothing leaves the box.
"""
import json

import pytest

from forecast_eval.live import credentials, telegram
from forecast_eval.live.alerts import ALL, Alert, Alerter, Severity
from forecast_eval.live.telegram import (LevelStore, TelegramConfig,
                                         TelegramNotifier)

CHAT = "4242"


class _FakeApi:
    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail or {}            # method -> exception to raise

    def __call__(self, method, payload, timeout):
        self.calls.append((method, payload))
        if method in self.fail:
            raise self.fail[method]
        return []

    def sent(self):
        return [p for m, p in self.calls if m == "sendMessage"]


def _notifier(tmp_path, api=None, level="info", **kw):
    api = api or _FakeApi()
    store = LevelStore(tmp_path / "telegram_level.json", level)
    n = TelegramNotifier(TelegramConfig("tok", CHAT, level), deployment="night",
                         store=store, call=api, retry_backoff_s=0, **kw)
    return n, api, store


def _alert(sev, kind="k", message="m"):
    from datetime import datetime
    return Alert(severity=sev, kind=kind, message=message, when=datetime.now())


# ---- the level ------------------------------------------------------------

def test_level_defaults_until_the_first_press_then_persists(tmp_path):
    path = tmp_path / "telegram_level.json"
    assert LevelStore(path, "info").get() == "info"
    LevelStore(path, "info").set("warn", by="night")
    assert LevelStore(path, "info").get() == "warn"      # a restart keeps it


def test_one_press_reaches_every_deployment(tmp_path):
    """Two processes, two stores on one file: the second sees the first's write
    on its next alert, with nothing restarted."""
    path = tmp_path / "telegram_level.json"
    night, day = LevelStore(path, "info"), LevelStore(path, "info")
    assert day.allows(Severity.INFO)
    night.set("fatal")
    assert not day.allows(Severity.INFO)
    assert not day.allows(Severity.WARN)


@pytest.mark.parametrize("level", ["info", "trade", "warn", "fatal"])
def test_fatal_is_sent_whatever_the_setting(tmp_path, level):
    store = LevelStore(tmp_path / "l.json", level)
    assert store.allows(Severity.FATAL)


@pytest.mark.parametrize("level,sent", [
    ("info", {Severity.INFO, Severity.TRADE, Severity.WARN, Severity.FATAL}),
    ("trade", {Severity.TRADE, Severity.WARN, Severity.FATAL}),
    ("warn", {Severity.WARN, Severity.FATAL}),
    ("fatal", {Severity.FATAL}),
])
def test_each_level_sends_itself_and_everything_louder(tmp_path, level, sent):
    """"Trades + WARN + FATAL" is the setting that drops the twice-daily
    re-logins of four processes and keeps the fills."""
    store = LevelStore(tmp_path / "l.json", level)
    assert {s for s in ALL if store.allows(s)} == sent


def test_a_torn_level_file_keeps_the_last_good_value(tmp_path):
    path = tmp_path / "l.json"
    store = LevelStore(path, "info")
    store.set("warn")
    path.write_text("{not json", encoding="utf-8")
    assert store.get() == "warn"


# ---- the sink ---------------------------------------------------------------

def test_sink_filters_on_the_live_level_and_prefixes_the_deployment(tmp_path):
    n, api, store = _notifier(tmp_path)
    n.start(poll=False)
    n(_alert(Severity.INFO, "position_opened", "LONG 1 @ 46200"))
    store.set("warn")
    n(_alert(Severity.INFO, "position_closed", "+150"))
    n(_alert(Severity.WARN, "feed_missed", "resubscribing"))
    n.close()
    texts = [p["text"] for p in api.sent()]
    assert texts == ["[night] [INFO] position_opened: LONG 1 @ 46200",
                     "[night] [WARN] feed_missed: resubscribing"]
    assert all(p["chat_id"] == CHAT for p in api.sent())
    assert n.summary()["telegram_filtered"] == 1


def test_a_dead_network_is_counted_not_raised(tmp_path):
    api = _FakeApi(fail={"sendMessage": OSError("network down")})
    n, _, _ = _notifier(tmp_path, api=api)
    n.start(poll=False)
    n(_alert(Severity.FATAL, "halt"))                  # must not raise
    n.close()
    assert len(api.sent()) == 3                        # retried
    assert n.summary()["telegram_failed"] == 1
    assert n.summary()["telegram_sent"] == 0


def test_close_delivers_what_was_queued_last(tmp_path):
    """`crashed` and `flatten_incomplete` are raised moments before exit."""
    n, api, _ = _notifier(tmp_path)
    n.start(poll=False)
    for i in range(5):
        n(_alert(Severity.FATAL, f"k{i}"))
    n.close()
    assert len(api.sent()) == 5
    n(_alert(Severity.FATAL, "after"))                 # closed: counted, not sent
    assert n.summary()["telegram_dropped"] == 1
    n.close()                                          # idempotent


def test_a_full_queue_drops_instead_of_blocking(tmp_path):
    n, _, _ = _notifier(tmp_path, queue_size=2)        # sender never started
    for _ in range(3):
        n(_alert(Severity.FATAL, "x"))
    assert n.summary()["telegram_dropped"] == 1


def test_through_the_alerter(tmp_path):
    n, api, _ = _notifier(tmp_path)
    n.start(poll=False)
    a = Alerter(console=False)
    a.add_sink(n, levels=ALL)
    a.info("resumed", "adopted +1")
    n.close()
    assert api.sent()[0]["text"] == "[night] [INFO] resumed: adopted +1"


# ---- the settings button ------------------------------------------------------

def _msg(text, chat=CHAT):
    return {"update_id": 1, "message": {"chat": {"id": int(chat)}, "text": text}}


def _press(data, chat=CHAT):
    return {"update_id": 2, "callback_query": {
        "id": "cq1", "data": data,
        "message": {"message_id": 7, "chat": {"id": int(chat)}}}}


def test_settings_replies_with_four_buttons_marking_the_current_one(tmp_path):
    n, api, _ = _notifier(tmp_path)
    n.start(poll=False)
    n.handle_update(_msg("/settings"))
    n.close()
    (reply,) = api.sent()
    buttons = [row[0] for row in reply["reply_markup"]["inline_keyboard"]]
    assert [b["callback_data"] for b in buttons] == [
        "level:info", "level:trade", "level:warn", "level:fatal"]
    assert buttons[1]["text"] == "Trades + WARN + FATAL"
    assert buttons[0]["text"].startswith("✓")
    assert not any(b["text"].startswith("✓") for b in buttons[1:])


def test_a_press_sets_the_shared_level_and_redraws(tmp_path):
    n, api, store = _notifier(tmp_path)
    n.handle_update(_press("level:fatal"))
    assert LevelStore(store.path, "info").get() == "fatal"
    methods = [m for m, _ in api.calls]
    assert methods == ["answerCallbackQuery", "editMessageText"]
    edited = api.calls[1][1]["reply_markup"]["inline_keyboard"]
    assert edited[3][0]["text"].startswith("✓")


def test_the_trade_button_keeps_fills_and_drops_relogins(tmp_path):
    n, api, store = _notifier(tmp_path)
    n.handle_update(_press("level:trade"))
    assert store.get() == "trade"
    api.calls.clear()
    n.start(poll=False)
    n(_alert(Severity.INFO, "relogin", "token refreshed"))
    n(_alert(Severity.TRADE, "position_opened", "LONG 1 @ 46200"))
    n.close()
    assert [p["text"] for p in api.sent()] == [
        "[night] [TRADE] position_opened: LONG 1 @ 46200"]


def test_a_failed_redraw_still_keeps_the_new_level(tmp_path):
    """Telegram refuses to answer a callback older than a few minutes, which is
    what a press made while no deployment was polling becomes."""
    api = _FakeApi(fail={"answerCallbackQuery": RuntimeError("query is too old"),
                         "editMessageText": RuntimeError("not modified")})
    n, _, store = _notifier(tmp_path, api=api)
    n.handle_update(_press("level:warn"))
    assert store.get() == "warn"


def test_another_chat_cannot_change_the_level(tmp_path):
    n, api, store = _notifier(tmp_path)
    n.handle_update(_press("level:fatal", chat="999"))
    n.handle_update(_msg("/settings", chat="999"))
    n.handle_update(_press("level:bogus"))
    assert store.get() == "info"
    assert api.calls == []
    assert n.ignored == 3


def test_a_poll_conflict_is_counted_and_backs_off(tmp_path):
    """Two deployments, one bot: Telegram answers the second poller 409."""
    import urllib.error
    conflict = urllib.error.HTTPError("u", 409, "Conflict", {}, None)
    n, _, _ = _notifier(tmp_path, api=_FakeApi(fail={"getUpdates": conflict}))
    assert n._poll_once() is False
    assert n._poll_once() is False
    assert n.summary()["telegram_poll_conflicts"] == 2
    assert n._poll_delay == 10.0


def test_poll_advances_the_offset_past_a_bad_update(tmp_path):
    class _Api(_FakeApi):
        def __call__(self, method, payload, timeout):
            self.calls.append((method, payload))
            if method == "getUpdates":
                return [{"update_id": 10, "message": None},
                        {"update_id": 11, "message": {"chat": "garbage"}}]
            return []

    n, api, _ = _notifier(tmp_path, api=_Api())
    assert n._poll_once() is True
    assert n._offset == 12
    assert n.summary()["telegram_poll_errors"] == 1


# ---- config ----------------------------------------------------------------------

def _secrets(monkeypatch, table):
    # conftest patches `load_config` away for every test on this same
    # `monkeypatch`; undo that first so the real loader runs.
    monkeypatch.undo()
    monkeypatch.setattr(credentials, "_read_secrets", lambda: table)


def test_config_absent_is_none(monkeypatch):
    _secrets(monkeypatch, {"night": {}})
    assert telegram.load_config() is None


def test_config_placeholder_is_refused(monkeypatch):
    _secrets(monkeypatch, {"telegram": {"bot_token": "REPLACE_ME",
                                        "chat_id": "REPLACE_ME"}})
    with pytest.raises(RuntimeError, match="placeholder"):
        telegram.load_config()


def test_config_reads_an_integer_chat_id_and_hides_the_token(monkeypatch):
    _secrets(monkeypatch, {"telegram": {"bot_token": "123:secret",
                                        "chat_id": 4242, "level": "WARN"}})
    cfg = telegram.load_config()
    assert (cfg.chat_id, cfg.level) == ("4242", "warn")
    assert "secret" not in repr(cfg)


def test_a_broken_section_warns_and_the_run_carries_on(tmp_path, monkeypatch):
    def _bad():
        raise RuntimeError("[telegram] ... placeholder")
    monkeypatch.setattr(telegram, "load_config", _bad)
    a = Alerter(console=False)
    assert telegram.attach(a, deployment="night",
                           level_file=tmp_path / "l.json") is None
    assert a.counts["warn"] == 1
