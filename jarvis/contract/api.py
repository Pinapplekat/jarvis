"""The small surface the core offers to apps. Apps import from here, never from other core modules."""

from __future__ import annotations

import logging
import os
import sqlite3
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class Attachment:
    kind: str  # "image" for now
    path: Path  # a private temp file from ctx.temp_path(); the core deletes it on release
    mime: str


@dataclass(frozen=True)
class Result:
    text: str  # short, human-readable outcome
    data: dict | None = None  # structured result for the loop
    undo: dict | None = None  # what the app's undo handler needs, if the action has one
    attachments: tuple[Attachment, ...] = ()
    subject: str | None = None  # what the action changed, e.g. 'fact:personal/color'; undo uses it to refuse out of order


@dataclass(frozen=True)
class SessionStarted:
    """What a session's start handler returns: a line for the model, and the state sample/stop get back."""

    text: str
    state: dict  # JSON-serializable; the host stores it, the node keeps the live resources it points at


@dataclass(frozen=True)
class JobRun:
    now: datetime  # timezone-aware, in Elijah's timezone
    last_ok_at: datetime | None
    reason: str  # "every", "at", "wake", "manual" or "catch-up"
    quiet: bool  # inside quiet hours right now


@dataclass(frozen=True)
class Think:
    prompt: str  # what the thinking turn is about, with the data it needs
    deliver: bool = True  # send the reply to Elijah; False for jobs that only use tools
    untrusted: bool = False  # the prompt quotes someone else's words (assignment titles, mail)


@dataclass(frozen=True)
class JobResult:
    summary: str  # one line for the job log: "sent 2", "17 deadlines, 1 new"
    messages: tuple[str, ...] = ()  # sent to Elijah exactly as written
    think: Think | None = None  # ask the host for a thinking turn (counts against the budget)
    wake_at: datetime | None = None


class ActionError(Exception):
    """A failure to tell Elijah about in plain words ('The camera is in use by another app')."""


class AppUnavailable(ActionError):
    """ctx.call: the other app isn't installed, is turned off, or its device isn't connected."""


# ctx.call goes through this: (calling app, app, action, args, the caller's connection) -> Result. Set by the core.
CallHook = Callable[[str, str, str, dict, "sqlite3.Connection | None"], Result]


class AppContext:
    """Passed as the first argument to every handler."""

    def __init__(
        self,
        name: str,
        settings: Mapping[str, object],
        secrets: Mapping[str, str],
        db: sqlite3.Connection | None,
        tmp_dir: Path,
        tz: tzinfo | None = None,
    ) -> None:
        self.name = name
        self.settings = settings  # manifest defaults overridden by settings.toml [apps.<name>]
        self.secrets = secrets  # only the keys the manifest lists
        self.log = logging.getLogger(f"jarvis.apps.{name}")
        self._db = db  # an app may use only tables named <app>_*
        self._tmp_dir = tmp_dir
        self.tz: tzinfo = tz or timezone.utc  # Elijah's timezone (the core setting)
        self.wake_hook: Callable[[str, str], None] | None = None  # set by the scheduler inside `jarvis serve`
        self.call_hook: CallHook | None = None  # set by the core when the apps are loaded on the host

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def wake(self, job: str) -> None:
        """Run one of this app's jobs now. Does nothing outside `jarvis serve`."""
        if self.wake_hook is not None:
            self.wake_hook(self.name, job)

    def with_db(self, db: sqlite3.Connection) -> "AppContext":
        """The same context on another connection. Each job run gets its own (SQLite connections are per thread)."""
        other = AppContext(self.name, self.settings, self.secrets, db, self._tmp_dir, self.tz)
        other.wake_hook, other.call_hook = self.wake_hook, self.call_hook
        return other

    def call(self, app: str, action: str, **args) -> Result:
        """Run another app's shared read action (listed in this app's `uses`). Raises AppUnavailable when that app
        isn't there; the caller says "not available" instead of failing."""
        if self.call_hook is None:
            raise AppUnavailable(f"{app} can't be reached from here.")
        return self.call_hook(self.name, app, action, args, self._db)

    def chat_between(self, start: datetime, end: datetime) -> list[tuple[datetime, str, str]]:
        """The conversation in [start, end): (when, 'Elijah' or 'Falak', text), oldest first."""
        rows = self.db.execute("SELECT at, role, text FROM chat_log WHERE at >= ? AND at < ? ORDER BY id",
                               (_utc(start), _utc(end))).fetchall()
        return [(datetime.fromisoformat(r["at"]).astimezone(self.tz), "Elijah" if r["role"] == "user" else "Falak",
                 r["text"]) for r in rows]

    def actions_between(self, start: datetime, end: datetime) -> list[tuple[datetime, str, str, str]]:
        """What actions ran in [start, end): (when, tool, status, result text). Arguments are never included."""
        rows = self.db.execute("SELECT time, tool, status, result FROM action_log WHERE time >= ? AND time < ? "
                               "ORDER BY id", (_utc(start), _utc(end))).fetchall()
        return [(datetime.fromisoformat(r["time"]).astimezone(self.tz), r["tool"], r["status"], r["result"])
                for r in rows]

    @property
    def db(self) -> sqlite3.Connection:
        if self._db is None:
            raise RuntimeError(f"{self.name} runs on a device node, which has no database")
        return self._db

    def temp_path(self, suffix: str) -> Path:
        """A private temp file (0600 in a 0700 folder). Return it in an Attachment to hand it to the core."""
        fd, path = tempfile.mkstemp(suffix=suffix, dir=self._tmp_dir)
        os.close(fd)
        return Path(path)


def _utc(when: datetime) -> str:
    return when.astimezone(timezone.utc).isoformat(timespec="seconds")
