"""One logging setup: JSON lines to a rotating file, plain text to stderr."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from jarvis.contract.logfmt import JsonFormatter
from jarvis.core.settings import Settings

_MARK = "_falak"


def setup_logging(settings: Settings) -> None:
    root = logging.getLogger()
    for h in [h for h in root.handlers if getattr(h, _MARK, False)]:
        root.removeHandler(h)
        h.close()
    log_dir = settings.data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(log_dir / "falak.log", maxBytes=1_000_000, backupCount=4)
    file_handler.setFormatter(JsonFormatter())
    stderr_handler = logging.StreamHandler()
    stderr_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    for h in (file_handler, stderr_handler):
        setattr(h, _MARK, True)
        root.addHandler(h)
    root.setLevel(settings.log_level.upper())
