import re
import shutil
import subprocess
from pathlib import Path

from jarvis.core.api import ActionError, AppContext, Attachment, Result

TIMEOUT = 10
DENIED = re.compile(r"not authori[sz]ed|denied|permission|access", re.I)
NO_DEVICE = re.compile(r"no (video )?devices?|not found|no camera", re.I)
FALLBACKS = ("/opt/homebrew/bin/imagesnap", "/usr/local/bin/imagesnap")


def _imagesnap() -> str | None:
    return shutil.which("imagesnap") or next((p for p in FALLBACKS if Path(p).exists()), None)


def take_photo(ctx: AppContext) -> Result:
    tool = _imagesnap()
    if not tool:
        raise ActionError("The camera tool isn't installed. Run: brew install imagesnap")
    path = ctx.temp_path(".jpg")
    cmd = [tool, "-q", "-w", "1"]
    if ctx.settings["device"]:
        cmd += ["-d", str(ctx.settings["device"])]
    try:
        r = subprocess.run([*cmd, str(path)], capture_output=True, text=True, timeout=TIMEOUT)
        said = f"{r.stdout or ''} {r.stderr or ''}"
        if r.returncode != 0 or path.stat().st_size == 0:
            if NO_DEVICE.search(said):
                raise ActionError("I couldn't find a camera.")
            if DENIED.search(said) or r.returncode == 0:  # macOS hands back an empty frame when it says no
                raise ActionError(
                    "Camera access is blocked. Allow it for the terminal app running me in "
                    "System Settings > Privacy & Security > Camera."
                )
            raise ActionError(f"The camera didn't work ({said.strip()[:120] or r.returncode}).")
    except subprocess.TimeoutExpired:
        path.unlink(missing_ok=True)
        raise ActionError(f"The camera didn't answer within {TIMEOUT} seconds. Is another app using it?") from None
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return Result("Took a photo.", attachments=(Attachment("image", path, "image/jpeg"),))
