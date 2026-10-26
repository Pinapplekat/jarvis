"""The small surface the core offers to apps. Apps import from here, never from other core modules."""

from __future__ import annotations

import logging
import os
import sqlite3
import tempfile
from dataclasses import dataclass
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


class ActionError(Exception):
    """A failure to tell Elijah about in plain words ('The camera is in use by another app')."""


class AppContext:
    """Passed as the first argument to every handler."""

    def __init__(
        self,
        name: str,
        settings: Mapping[str, object],
        secrets: Mapping[str, str],
        db: sqlite3.Connection,
        tmp_dir: Path,
    ) -> None:
        self.name = name
        self.settings = settings  # manifest defaults overridden by settings.toml [apps.<name>]
        self.secrets = secrets  # only the keys the manifest lists
        self.log = logging.getLogger(f"jarvis.apps.{name}")
        self.db = db  # an app may use only tables named <app>_*
        self._tmp_dir = tmp_dir

    def temp_path(self, suffix: str) -> Path:
        """A private temp file (0600 in a 0700 folder). Return it in an Attachment to hand it to the core."""
        fd, path = tempfile.mkstemp(suffix=suffix, dir=self._tmp_dir)
        os.close(fd)
        return Path(path)
