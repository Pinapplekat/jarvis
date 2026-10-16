"""Background jobs via launchd (macOS's per-user scheduler).

- com.jarvis.tick:   `jarvis tick` every few minutes (Canvas, alerts, drafts, sending mail)
- com.jarvis.listen: `jarvis listen`, always running (Telegram chat and approval buttons)
"""

import os
import plistlib
import shutil
import subprocess
import time
from pathlib import Path

SOURCE = Path(__file__).resolve().parent.parent
# Jobs run from a copy outside iCloud Drive: iCloud offloads files ("dataless") and
# background reads of them intermittently fail with EDEADLK.
PROJECT = Path.home() / "Library/Application Support/JARVIS/app"
COPY_EXCLUDES = [".git", ".venv", ".venv.nosync", "__pycache__", ".pytest_cache", "data"]
LOG_DIR = Path.home() / "Library/Logs/JARVIS"
AGENTS = Path.home() / "Library/LaunchAgents"
TICK, LISTEN, WATCH, EARS = "com.jarvis.tick", "com.jarvis.listen", "com.jarvis.watch", "com.jarvis.ears"
MENUBAR = "com.jarvis.menubar"
LOG = LOG_DIR / "tick.log"


def _plist(label: str, uv: str, command: str, interval: int | None, launcher: str | None = None) -> dict:
    path_dirs = [str(Path(uv).parent), str(Path.home() / ".local/bin"), "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    args = [uv, "run", "--project", str(PROJECT), "-q", "jarvis", command]
    plist = {
        "Label": label,
        "ProgramArguments": ([launcher] if launcher else []) + args,
        "WorkingDirectory": str(PROJECT),
        "EnvironmentVariables": {"PATH": ":".join(dict.fromkeys(path_dirs)), "PYTHONUNBUFFERED": "1"},
        "RunAtLoad": True,
        "StandardOutPath": str(LOG_DIR / f"{command}.log"),
        "StandardErrorPath": str(LOG_DIR / f"{command}.log"),
    }
    if interval:
        plist["StartInterval"] = interval
    else:
        plist["KeepAlive"] = True
        plist["ThrottleInterval"] = 10  # if it crashes, wait 10s before restarting
    return plist


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _launchctl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, check=check)


def is_loaded(label: str = TICK) -> bool:
    return _launchctl("print", f"{_domain()}/{label}", check=False).returncode == 0


def _unload(label: str) -> bool:
    """Stop a job and wait until launchd has actually removed it (bootout is asynchronous)."""
    if not is_loaded(label):
        return False
    _launchctl("bootout", f"{_domain()}/{label}", check=False)
    for _ in range(50):
        if not is_loaded(label):
            break
        time.sleep(0.2)
    return True


def _load(label: str, plist: dict) -> None:
    path = AGENTS / f"{label}.plist"
    _unload(label)
    path.write_bytes(plistlib.dumps(plist))
    r = _launchctl("bootstrap", _domain(), str(path), check=False)
    if r.returncode != 0:
        raise SystemExit(f"launchctl couldn't start {label}: {r.stderr.strip() or r.returncode}")


def deploy(uv: str) -> None:
    """Copy the project (including .env) out of iCloud and give the copy its own venv."""
    PROJECT.mkdir(parents=True, exist_ok=True)
    excludes = [arg for name in COPY_EXCLUDES for arg in ("--exclude", name)]
    subprocess.run(["rsync", "-a", "--delete", *excludes, f"{SOURCE}/", f"{PROJECT}/"], check=True)
    if (PROJECT / ".env").exists():
        (PROJECT / ".env").chmod(0o600)
    subprocess.run([uv, "sync", "-q", "--project", str(PROJECT)], check=True)


def install(interval: int = 300, listen: bool = True, watch: bool = True, ears: bool = True,
            menubar: bool = True) -> None:
    uv = shutil.which("uv")
    if not uv:
        raise SystemExit("uv not found on PATH")
    if SOURCE != PROJECT:
        deploy(uv)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    AGENTS.mkdir(parents=True, exist_ok=True)
    _load(TICK, _plist(TICK, uv, "tick", interval))
    if listen:
        _load(LISTEN, _plist(LISTEN, uv, "listen", None))
    if watch:
        from jarvis import launcher
        launcher.build()  # the watcher runs inside "JARVIS Senses.app" for Screen Recording
        _load(WATCH, _plist(WATCH, uv, "watch", None, launcher=str(launcher.EXECUTABLE)))
    else:
        _unload(WATCH)
    if ears:
        from jarvis import launcher
        launcher.build_ears()  # "Hey Jarvis" runs inside "JARVIS Ears.app" for the Microphone
        _load(EARS, _plist(EARS, uv, "ears", None, launcher=str(launcher.EARS_EXECUTABLE)))
    else:
        _unload(EARS)
    if menubar:
        from jarvis import config, menubar as indicator
        indicator.build()
        _load(MENUBAR, {
            "Label": MENUBAR,
            "ProgramArguments": [str(indicator.EXECUTABLE), str(config.DATA_DIR / "status.json")],
            "RunAtLoad": True,
            # Restart after a crash, but stay closed if Elijah chooses "Hide Indicator".
            "KeepAlive": {"SuccessfulExit": False},
            "StandardOutPath": str(LOG_DIR / "menubar.log"),
            "StandardErrorPath": str(LOG_DIR / "menubar.log"),
        })
    else:
        _unload(MENUBAR)


def uninstall() -> bool:
    removed = False
    for label in (TICK, LISTEN, WATCH, EARS, MENUBAR):
        removed |= _unload(label)
        (AGENTS / f"{label}.plist").unlink(missing_ok=True)
    return removed


def run_now() -> None:
    _launchctl("kickstart", f"{_domain()}/{TICK}")


def status(log_lines: int = 5) -> str:
    keep = ("state =", "run interval", "last exit code", "runs =", "pid =")
    out = []
    for label, command in ((TICK, "tick"), (LISTEN, "listen"), (WATCH, "watch"), (EARS, "ears")):
        if not is_loaded(label):
            out.append(f"{command}: not installed (jarvis schedule install)")
            continue
        info = _launchctl("print", f"{_domain()}/{label}").stdout
        lines = []
        for line in (raw.strip() for raw in info.splitlines()):
            if line.startswith(keep) and line not in lines:
                lines.append(line)
        out.append(f"{command}: " + ", ".join(lines))
        log = LOG_DIR / f"{command}.log"
        if log.exists():
            out += [f"  {line}" for line in log.read_text().splitlines()[-log_lines:]]
    return "\n".join(out)
