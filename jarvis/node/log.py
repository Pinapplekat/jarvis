"""The node's own log: JSON lines in FALAK_NODE_DIR/logs/node.log, plain text on stderr."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from jarvis.contract.logfmt import JsonFormatter
from jarvis.node.config import NodeConfig

_MARK = "_falak_node"


def setup_logging(config: NodeConfig, level: str = "INFO") -> None:
    root = logging.getLogger()
    for h in [h for h in root.handlers if getattr(h, _MARK, False)]:
        root.removeHandler(h)
        h.close()
    config.log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(config.log_dir / "node.log", maxBytes=1_000_000, backupCount=4)
    file_handler.setFormatter(JsonFormatter())
    stderr = logging.StreamHandler()
    stderr.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    for h in (file_handler, stderr):
        setattr(h, _MARK, True)
        root.addHandler(h)
    root.setLevel(level)
