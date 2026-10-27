"""One settings loader: `settings.toml` for choices, `.env` for secrets."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jarvis.core.timeutil import parse_quiet_hours

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
    node_listen: str = "127.0.0.1:8765"  # where devices connect; only this machine until Tailscale
    timezone: str = "America/Los_Angeles"  # Elijah's; "remind me at 3" means 3 PM here
    quiet_hours: str = "23-7"  # "start-end" in whole hours, local; empty turns them off
    catch_up_minutes: int = 120  # an `at` job missed by longer than this (the Mac slept) is skipped, not run late
    notify_channel: str = "discord"  # where Falak speaks first; falls back to the first channel that's running
    thinking_runs_per_day: int = 12  # jobs that ask for a thinking turn; chat turns never count
    apps: dict[str, dict] = field(default_factory=dict)  # per-app sections; used from M2
    channels: dict[str, dict] = field(default_factory=dict)  # per-channel sections; each channel checks its own keys

    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def _listen_ok(value: object) -> bool:
    host, _, port = value.rpartition(":") if isinstance(value, str) else ("", "", "")
    return bool(host) and port.isdigit() and 0 <= int(port) <= 65535


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
    for table in ("apps", "channels"):
        if not isinstance(values.get(table, {}), dict) or not all(isinstance(v, dict) for v in values.get(table, {}).values()):
            raise SettingsError(f"{path}: '{table}' must be a table of [{table}.<name>] sections")
    for key in ("assistant_name", "log_level", "model"):
        if key in values and not (isinstance(values[key], str) and values[key].strip()):
            raise SettingsError(f"{path}: '{key}' must be text")
    for key in ("max_turns", "turn_timeout_seconds", "catch_up_minutes", "thinking_runs_per_day"):
        if key in values and not (isinstance(values[key], int) and not isinstance(values[key], bool) and values[key] > 0):
            raise SettingsError(f"{path}: '{key}' must be a whole number above zero")
    if "node_listen" in values and not _listen_ok(values["node_listen"]):
        raise SettingsError(f"{path}: 'node_listen' must look like host:port")
    for key in ("timezone", "notify_channel"):
        if key in values and not (isinstance(values[key], str) and values[key].strip()):
            raise SettingsError(f"{path}: '{key}' must be text")
    if "timezone" in values:
        try:
            ZoneInfo(values["timezone"])
        except (ZoneInfoNotFoundError, ValueError, OSError):
            raise SettingsError(f"{path}: 'timezone' {values['timezone']!r} is not a timezone name "
                                "(like America/Los_Angeles)") from None
    if "quiet_hours" in values:
        try:
            if not isinstance(values["quiet_hours"], str):
                raise ValueError
            parse_quiet_hours(values["quiet_hours"])
        except ValueError:
            raise SettingsError(f"{path}: 'quiet_hours' must look like \"23-7\", or \"\" for none") from None
    return Settings(data_dir=data_dir, **values)
