"""The Mac's camera, on request: one still for JARVIS to look at privately, then deleted.

Only when Elijah himself asks to show JARVIS something (chat.respond checks that); never from a
schedule, tick, email or page. The frame goes to a private temp file (0600, in a 0700 folder),
is looked at once and deleted in a `finally`; leftovers from a crash are swept at startup. Uses
imagesnap (brew install imagesnap) and needs macOS Camera permission for the process running
JARVIS. CAMERA=0 turns it off.
"""

import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

from jarvis import config

PREFIX = "camera-"
NOT_ALLOWED = "camera not allowed"
NOT_FOUND = "camera not found"
DENIED = re.compile(r"not authori[sz]ed|denied|permission|access", re.I)
NO_DEVICE = re.compile(r"no (video )?devices?|not found|no camera", re.I)


def _tmp_dir() -> Path:
    d = config.DATA_DIR / "tmp" / "camera"
    d.mkdir(parents=True, exist_ok=True)
    d.chmod(0o700)
    return d


def _imagesnap() -> str | None:
    return (shutil.which("imagesnap") or next((p for p in ("/opt/homebrew/bin/imagesnap", "/usr/local/bin/imagesnap")
                                               if Path(p).exists()), None))


def capture_frame(run=None, tool: str | None = None) -> Path:
    """One still from the camera in a private temp file; the caller deletes it. RuntimeError
    "camera not allowed" / "camera not found" when the permission or the device is missing."""
    if not config.CAMERA:
        raise RuntimeError("the camera is off (CAMERA=0)")
    tool = _imagesnap() if tool is None else tool
    if not tool:
        raise RuntimeError(f"{NOT_FOUND} (the snapshot tool isn't installed: brew install imagesnap)")
    path = _tmp_dir() / f"{PREFIX}{uuid.uuid4().hex}.jpg"
    os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    run = run or subprocess.run
    try:
        r = run([tool, "-q", "-w", "1", str(path)], capture_output=True, text=True, timeout=20)
        said = f"{r.stdout or ''} {r.stderr or ''}"
        if r.returncode != 0 or not path.exists() or path.stat().st_size == 0:
            if NO_DEVICE.search(said):
                raise RuntimeError(NOT_FOUND)
            if DENIED.search(said) or r.returncode == 0:  # macOS gives an empty frame when it says no
                raise RuntimeError(f"{NOT_ALLOWED} (allow it in System Settings > Privacy & Security > Camera)")
            raise RuntimeError(f"couldn't use the camera ({said.strip()[:120] or r.returncode})")
        path.chmod(0o600)
        from jarvis.screen import VISION_MAX_EDGE
        run(["/usr/bin/sips", "-Z", str(VISION_MAX_EDGE), str(path)], capture_output=True, timeout=20)
        path.chmod(0o600)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def sweep() -> int:
    """Delete camera stills left behind (a crash mid-look). Returns how many."""
    d = config.DATA_DIR / "tmp" / "camera"
    if not d.is_dir():
        return 0
    n = 0
    for p in d.iterdir():
        if p.name.startswith(PREFIX):
            p.unlink(missing_ok=True)
            n += 1
    return n
