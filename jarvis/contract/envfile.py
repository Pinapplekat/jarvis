"""Reading a `.env` file; the host and the node each have one."""

from __future__ import annotations

from pathlib import Path


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
