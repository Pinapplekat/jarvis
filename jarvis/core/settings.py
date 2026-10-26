"""One settings loader: `settings.toml` for choices, `.env` for secrets."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Mapping

DEFAULT_DATA_DIR = "~/Library/Application Support/Falak-dev"


class SettingsError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    assistant_name: str = "Falak"
    log_level: str = "INFO"
    model: str = "sonnet"  # the brain's model
    max_turns: int = 12  # model/tool round trips allowed in one turn
    turn_timeout_seconds: int = 120
    apps: dict[str, dict] = field(default_factory=dict)  # per-app sections; used from M2


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    data_dir = Path(env.get("FALAK_DATA_DIR") or DEFAULT_DATA_DIR).expanduser()
    values: dict = {}
    path = data_dir / "settings.toml"
    if path.exists():
        try:
            values = tomllib.loads(path.read_text())
        except tomllib.TOMLDecodeError as e:
            raise SettingsError(f"{path}: {e}") from e
    allowed = {f.name for f in fields(Settings)} - {"data_dir"}
    unknown = sorted(set(values) - allowed)
    if unknown:
        raise SettingsError(
            f"{path}: unknown setting(s): {', '.join(unknown)} (known: {', '.join(sorted(allowed))})"
        )
    if not isinstance(values.get("apps", {}), dict):
        raise SettingsError(f"{path}: 'apps' must be a table of [apps.<name>] sections")
    for key in ("assistant_name", "log_level", "model"):
        if key in values and not (isinstance(values[key], str) and values[key].strip()):
            raise SettingsError(f"{path}: '{key}' must be text")
    for key in ("max_turns", "turn_timeout_seconds"):
        if key in values and not (isinstance(values[key], int) and not isinstance(values[key], bool) and values[key] > 0):
            raise SettingsError(f"{path}: '{key}' must be a whole number above zero")
    return Settings(data_dir=data_dir, **values)


def load_secrets(data_dir: Path) -> dict[str, str]:
    """Parse `<data_dir>/.env` (KEY=value, # comments, optional quotes). Never log the values."""
    path = Path(data_dir) / ".env"
    secrets: dict[str, str] = {}
    if not path.exists():
        return secrets
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        secrets[key.strip()] = value
    return secrets
