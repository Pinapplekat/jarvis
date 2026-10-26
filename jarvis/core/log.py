"""One logging setup: JSON lines to a rotating file, plain text to stderr."""

from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler

from jarvis.core.settings import Settings

_STANDARD = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}
_MARK = "_falak"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        data.update({k: v for k, v in vars(record).items() if k not in _STANDARD})
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        return json.dumps(data, default=str)


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
