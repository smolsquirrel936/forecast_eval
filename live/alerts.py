"""Alerts — the one door everything that needs a human goes through.

A five-day unattended soak is the case this exists for: when something goes
wrong at 03:00 it has to leave the machine, and today it only reaches a console
nobody is watching and a JSONL nobody reads until morning.

**One method, four severities.** `fatal` / `warn` / `trade` / `info` all funnel
into `emit`, and every alert carries a `severity` and a `kind` tag. Sinks subscribe to
the levels they want, so one Telegram chat can take fatals while another takes
everything — without the call sites knowing anything about channels. Adding a
transport later is `alerter.add_sink(fn, levels=...)` and nothing else changes.

    FATAL  the run is over or a position is exposed — act now
    WARN   handled, but you would want to know (a feed repaired, an anomaly)
    TRADE  a fill opened or closed a position
    INFO   milestones (started, warm, resumed, ended cleanly)

**A recovery is raised at the level of the problem it ends.** `recovery` sends
WARN when one of the kinds it `resolves` was raised (WARN or FATAL) and not yet
resolved, and INFO otherwise — so a channel that shows a warning also shows the
all-clear, while a routine re-login stays INFO.

**Delivery is best-effort and can never take down the run.** A sink that raises
is swallowed and noted; the alert path must not become a new way to lose a
session. Everything is written to the trail regardless of whether any transport
worked, and fatals additionally append to a **standalone journal** that outlives
the run directory — so "what has ever gone badly wrong" is one file, not a search
across every `session.jsonl`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple


class Severity(str, Enum):
    # Tech: the four levels, ordered fatal > warn > trade > info.
    # Why:  a tag rather than a boolean because the whole point is that different
    #       classes reach the user by different routes. `str`-valued so it
    #       serialises into the trail without conversion. TRADE sits above INFO
    #       because a fill is rare and worth reading, while INFO also carries the
    #       twice-daily re-logins of four processes; below WARN because nothing
    #       has gone wrong.
    FATAL = "fatal"
    WARN = "warn"
    TRADE = "trade"
    INFO = "info"


ALL: Tuple[Severity, ...] = (Severity.FATAL, Severity.WARN, Severity.TRADE,
                             Severity.INFO)


@dataclass(frozen=True)
class Alert:
    # Tech: one notification — level, machine-readable tag, human sentence,
    #       timestamp, and arbitrary structured detail.
    # Why:  `kind` is what a transport filters and groups on ("halt",
    #       "feed_repaired"); `message` is what a human reads on a phone. Keeping
    #       both means a Telegram sink never has to parse prose to route.
    severity: Severity
    kind: str
    message: str
    when: datetime
    detail: Dict[str, Any] = field(default_factory=dict)

    def format(self) -> str:
        # Tech: one line, severity-prefixed, with detail appended when present.
        # Why:  the default rendering for any transport that just wants text —
        #       a phone notification is one line before it is anything else.
        head = f"[{self.severity.value.upper()}] {self.kind}: {self.message}"
        if not self.detail:
            return head
        extra = " ".join(f"{k}={v}" for k, v in self.detail.items())
        return f"{head} | {extra}"

    def as_dict(self) -> Dict[str, Any]:
        return {"severity": self.severity.value, "kind": self.kind,
                "message": self.message, "when": self.when.isoformat(),
                **self.detail}


class Alerter:
    def __init__(self, *, recorder: Any = None,
                 fatal_journal: Optional[Path] = None,
                 console: bool = True):
        # Tech: hold the built-in destinations; user transports arrive via
        #       `add_sink`.
        # Why:  the built-ins are the ones that must never be optional in
        #       practice — the trail is the audit record and the console is what
        #       an attended run shows. A Telegram bot is an *addition* to those,
        #       never a replacement, because it is the one that can silently fail.
        self.recorder = recorder
        self.fatal_journal = fatal_journal
        self.console = console
        self._sinks: List[Tuple[Callable[[Alert], None], Tuple[Severity, ...]]] = []
        self._open: Set[str] = set()
        self.counts: Dict[str, int] = {s.value: 0 for s in ALL}
        self.sink_failures = 0

    def add_sink(self, sink: Callable[[Alert], None], *,
                 levels: Iterable[Severity] = (Severity.FATAL,)) -> None:
        """Register a transport. Defaults to fatals only."""
        # Tech: store the callable with the levels it wants.
        # Why:  per-sink filtering is what lets "different tunnels" exist without
        #       the call sites knowing. Defaulting to FATAL-only is the safe
        #       default for a new transport: over-notifying trains you to ignore
        #       the channel, which is worse than not having it.
        self._sinks.append((sink, tuple(levels)))

    # ---- the one method, three ways in -------------------------------------

    def fatal(self, kind: str, message: str, **detail: Any) -> Alert:
        return self.emit(Severity.FATAL, kind, message, **detail)

    def warn(self, kind: str, message: str, **detail: Any) -> Alert:
        return self.emit(Severity.WARN, kind, message, **detail)

    def trade(self, kind: str, message: str, **detail: Any) -> Alert:
        return self.emit(Severity.TRADE, kind, message, **detail)

    def info(self, kind: str, message: str, **detail: Any) -> Alert:
        return self.emit(Severity.INFO, kind, message, **detail)

    def recovery(self, kind: str, message: str, *, resolves: Iterable[str],
                 **detail: Any) -> Alert:
        # Tech: emit at WARN when any kind in `resolves` is still open (raised at
        #       WARN or FATAL since its last recovery), else at INFO; close them.
        #       The kinds it closed go in `detail` as `resolves`.
        # Why:  a channel filtered to WARN + FATAL showed `feed_missed` and never
        #       the all-clear, so every incident looked open on the phone. Capped
        #       at WARN: good news is never "act now", and FATAL would also write
        #       it to the fatal journal.
        cleared = [k for k in resolves if k in self._open]
        self._open.difference_update(cleared)
        if cleared:
            detail = {**detail, "resolves": ",".join(cleared)}
        return self.emit(Severity.WARN if cleared else Severity.INFO,
                         kind, message, **detail)

    def emit(self, severity: Severity, kind: str, message: str,
             **detail: Any) -> Alert:
        # Tech: build the Alert, then fan it out to every destination, isolating
        #       each one so a failure cannot reach the caller.
        # Why:  call sites are inside halt paths and exception handlers. An alert
        #       that raises there would replace a real diagnosis with a traceback
        #       from the notifier — the failure mode where adding monitoring
        #       makes the incident harder to read.
        alert = Alert(severity=severity, kind=kind, message=message,
                      when=datetime.now(), detail=dict(detail))
        self.counts[severity.value] += 1
        if severity in (Severity.WARN, Severity.FATAL):
            self._open.add(kind)

        if self.console:
            self._safe(self._to_console, alert)
        if self.recorder is not None:
            self._safe(self._to_recorder, alert)
        if self.fatal_journal is not None and severity is Severity.FATAL:
            self._safe(self._to_journal, alert)
        for sink, levels in self._sinks:
            if severity in levels:
                self._safe(sink, alert)
        return alert

    # ---- destinations -------------------------------------------------------

    def _to_console(self, alert: Alert) -> None:
        # Tech: fatals and warnings to stderr, trades and info to stdout.
        # Why:  stderr is what survives a redirect of normal output and what a
        #       terminal highlights; an attended run should not have to hunt for
        #       the bad line among a night of bar prints.
        import sys
        stream = (sys.stdout if alert.severity in (Severity.TRADE, Severity.INFO)
                  else sys.stderr)
        print(alert.format(), file=stream, flush=True)

    def _to_recorder(self, alert: Alert) -> None:
        self.recorder.record("alert", **alert.as_dict())

    def _to_journal(self, alert: Alert) -> None:
        # Tech: append one JSON line to a file outside the run directory, opened
        #       and closed per write.
        # Why:  the journal answers "what has ever gone badly wrong" without
        #       searching every run's `session.jsonl`, so it must outlive the run
        #       directory. Open-per-write rather than a held handle because this
        #       is called at most a few times per run, usually while something is
        #       already failing — a handle opened at start-up is one more thing
        #       that can be stale by the time it matters.
        self.fatal_journal.parent.mkdir(parents=True, exist_ok=True)
        with open(self.fatal_journal, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(alert.as_dict(), default=str) + "\n")

    def _safe(self, fn: Callable[[Alert], None], alert: Alert) -> None:
        # Tech: call `fn`, swallow anything it raises, count it.
        # Why:  best-effort delivery, decided deliberately: the alert most worth
        #       sending is often about a network fault, which is exactly when the
        #       transport fails. Losing that message is acceptable; losing the run
        #       to the notifier is not. The count is what makes a silently broken
        #       transport visible in the summary.
        try:
            fn(alert)
        except Exception:                       # noqa: BLE001 — see Why
            self.sink_failures += 1

    def summary(self) -> Dict[str, int]:
        return {"alerts_fatal": self.counts[Severity.FATAL.value],
                "alerts_warn": self.counts[Severity.WARN.value],
                "alerts_trade": self.counts[Severity.TRADE.value],
                "alerts_info": self.counts[Severity.INFO.value],
                "alert_sink_failures": self.sink_failures}
