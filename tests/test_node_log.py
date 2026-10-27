"""The node process logs once, to its own file, even after the CLI's common startup set up the host's logging."""

import logging

import pytest

from jarvis.core.log import setup_logging as host_logging
from jarvis.core.settings import load_settings
from jarvis.node.config import NodeConfig
from jarvis.node.log import setup_logging as node_logging
from jarvis.node.runtime import NodeRuntime


@pytest.fixture
def clean_root():
    root = logging.getLogger()
    saved, level = list(root.handlers), root.level
    yield root
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    for h in saved:
        root.addHandler(h)
    root.setLevel(level)


def test_node_logging_replaces_the_host_handlers(tmp_path, clean_root):
    host_logging(load_settings({"FALAK_DATA_DIR": str(tmp_path / "host")}))  # what `jarvis node run` gets first
    node_logging(NodeConfig(tmp_path / "node", "mac", "ws://127.0.0.1:1", "k"))
    streams = [h for h in clean_root.handlers if type(h) is logging.StreamHandler]
    files = [h.baseFilename for h in clean_root.handlers if isinstance(h, logging.FileHandler)]
    assert len(streams) == 1
    assert files == [str(tmp_path / "node" / "logs" / "node.log")]


def test_a_node_line_is_written_once(tmp_path, clean_root):
    host_logging(load_settings({"FALAK_DATA_DIR": str(tmp_path / "host")}))
    node_logging(NodeConfig(tmp_path / "node", "mac", "ws://127.0.0.1:1", "k"))
    logging.getLogger("jarvis.node.client").info("call 1 camera.sample_live ok=True")
    for h in clean_root.handlers:
        h.flush()
    assert (tmp_path / "node" / "logs" / "node.log").read_text().count("call 1 camera.sample_live") == 1
    host_log = tmp_path / "host" / "logs" / "falak.log"
    assert not host_log.exists() or "call 1" not in host_log.read_text()


def test_the_runtime_clears_leftover_frames_on_start(tmp_path):
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    (tmp / "frame.jpg").write_bytes(b"desk")
    (tmp / "photo.jpg.tmp").write_bytes(b"half")
    NodeRuntime(tmp_path, tmp)
    assert list(tmp.iterdir()) == []
