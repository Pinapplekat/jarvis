import pytest

from jarvis.node import config as nc


def test_defaults_and_files(tmp_path):
    folder = tmp_path / "Falak-node"
    cfg = nc.load_config({"FALAK_NODE_DIR": str(folder)})
    assert (cfg.name, cfg.host_url, cfg.key) == ("mac", "ws://127.0.0.1:8765", None)
    nc.write_pairing(folder, "mac", "ws://127.0.0.1:9000", "sekret")
    cfg = nc.load_config({"FALAK_NODE_DIR": str(folder)})
    assert (cfg.host_url, cfg.key) == ("ws://127.0.0.1:9000", "sekret")
    assert oct((folder / ".env").stat().st_mode & 0o777) == "0o600"
    assert oct(cfg.tmp_dir.stat().st_mode & 0o777) == "0o700" and cfg.tmp_dir.parent == folder


def test_rewriting_the_key_keeps_other_lines(tmp_path):
    (tmp_path / ".env").write_text("OTHER=1\nFALAK_NODE_KEY=old\n")
    nc.write_pairing(tmp_path, "mac", "ws://h:1", "new")
    assert (tmp_path / ".env").read_text() == "OTHER=1\nFALAK_NODE_KEY=new\n"


def test_node_dir_is_separate_from_the_host_data_dir(monkeypatch):
    monkeypatch.delenv("FALAK_NODE_DIR", raising=False)
    assert nc.node_dir().name == "Falak-node"


def test_unknown_node_setting_is_an_error(tmp_path):
    (tmp_path / "node.toml").write_text('colour = "red"\n')
    with pytest.raises(nc.NodeConfigError, match="unknown setting"):
        nc.load_config({"FALAK_NODE_DIR": str(tmp_path)})
