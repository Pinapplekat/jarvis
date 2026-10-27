"""The node's own configuration folder (FALAK_NODE_DIR). It never shares files with the host, even on one Mac."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from jarvis.contract.envfile import load_secrets

DEFAULT_NODE_DIR = "~/Library/Application Support/Falak-node"
DEFAULT_HOST_URL = "ws://127.0.0.1:8765"


class NodeConfigError(Exception):
    pass


@dataclass(frozen=True)
class NodeConfig:
    node_dir: Path
    name: str
    host_url: str
    key: str | None  # FALAK_NODE_KEY from the node's .env

    @property
    def tmp_dir(self) -> Path:
        path = self.node_dir / "tmp"
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.chmod(0o700)
        return path

    @property
    def log_dir(self) -> Path:
        return self.node_dir / "logs"


def node_dir(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    return Path(env.get("FALAK_NODE_DIR") or DEFAULT_NODE_DIR).expanduser()


def load_config(env: Mapping[str, str] | None = None) -> NodeConfig:
    folder = node_dir(env)
    values: dict = {}
    path = folder / "node.toml"
    if path.exists():
        try:
            values = tomllib.loads(path.read_text())
        except tomllib.TOMLDecodeError as e:
            raise NodeConfigError(f"{path}: {e}") from e
    unknown = sorted(set(values) - {"name", "host_url"})
    if unknown:
        raise NodeConfigError(f"{path}: unknown setting(s): {', '.join(unknown)} (known: host_url, name)")
    for key in ("name", "host_url"):
        if key in values and not (isinstance(values[key], str) and values[key].strip()):
            raise NodeConfigError(f"{path}: '{key}' must be text")
    return NodeConfig(folder, values.get("name", "mac"), values.get("host_url", DEFAULT_HOST_URL),
                      load_secrets(folder).get("FALAK_NODE_KEY"))


def write_pairing(folder: Path, name: str, host_url: str, key: str) -> None:
    """Save a freshly paired key and where to find the host (what `jarvis serve --with-node` does for you)."""
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    env = folder / ".env"
    env.touch(mode=0o600)
    env.chmod(0o600)
    lines = [ln for ln in env.read_text().splitlines() if not ln.startswith("FALAK_NODE_KEY=")]
    env.write_text("\n".join([*lines, f"FALAK_NODE_KEY={key}"]) + "\n")
    (folder / "node.toml").write_text(f'name = "{name}"\nhost_url = "{host_url}"\n')
