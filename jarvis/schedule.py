"""Run `jarvis tick` every few minutes with launchd (macOS's per-user scheduler)."""

import os
import plistlib
import shutil
import subprocess
from pathlib import Path

LABEL = "com.jarvis.tick"
PLIST = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
LOG = Path.home() / "Library/Logs/JARVIS/tick.log"
PROJECT = Path(__file__).resolve().parent.parent


def build_plist(uv: str, interval: int) -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": [uv, "run", "--project", str(PROJECT), "-q", "jarvis", "tick"],
        "WorkingDirectory": str(PROJECT),
        "EnvironmentVariables": {"PATH": f"{Path(uv).parent}:/usr/bin:/bin:/usr/sbin:/sbin"},
        "StartInterval": interval,
        "RunAtLoad": True,
        "StandardOutPath": str(LOG),
        "StandardErrorPath": str(LOG),
    }


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _launchctl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, check=check)


def install(interval: int = 300) -> None:
    uv = shutil.which("uv")
    if not uv:
        raise SystemExit("uv not found on PATH")
    LOG.parent.mkdir(parents=True, exist_ok=True)
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    if is_loaded():
        _launchctl("bootout", f"{_domain()}/{LABEL}", check=False)
    PLIST.write_bytes(plistlib.dumps(build_plist(uv, interval)))
    _launchctl("bootstrap", _domain(), str(PLIST))


def uninstall() -> bool:
    was_loaded = is_loaded()
    if was_loaded:
        _launchctl("bootout", f"{_domain()}/{LABEL}", check=False)
    PLIST.unlink(missing_ok=True)
    return was_loaded


def is_loaded() -> bool:
    return _launchctl("print", f"{_domain()}/{LABEL}", check=False).returncode == 0


def run_now() -> None:
    _launchctl("kickstart", f"{_domain()}/{LABEL}")


def status(log_lines: int = 5) -> str:
    if not is_loaded():
        return "Not scheduled. Run: jarvis schedule install"
    info = _launchctl("print", f"{_domain()}/{LABEL}").stdout
    keep = ("state =", "run interval", "last exit code", "runs =")
    lines = []
    for line in (raw.strip() for raw in info.splitlines()):
        if line.startswith(keep) and line not in lines:
            lines.append(line)
    if LOG.exists():
        lines += ["", f"Last runs ({LOG}):", *LOG.read_text().splitlines()[-log_lines:]]
    return "\n".join(lines)
