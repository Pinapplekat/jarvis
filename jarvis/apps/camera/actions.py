import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path

from jarvis.contract.api import ActionError, AppContext, Attachment, Result, SessionStarted

TIMEOUT = 10
DENIED = re.compile(r"not authori[sz]ed|denied|permission|access", re.I)
NO_DEVICE = re.compile(r"no (video )?devices?|not found|no camera", re.I)
FALLBACKS = ("/opt/homebrew/bin/imagesnap", "/usr/local/bin/imagesnap")
FFMPEG_FALLBACKS = ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg")
FFPLAY_FALLBACKS = ("/opt/homebrew/bin/ffplay", "/usr/local/bin/ffplay")
BUSY = re.compile(r"busy|in use|resource temporarily|could not lock", re.I)
FIRST_FRAME_WAIT = 10  # seconds for ffmpeg to produce the first frame
STALE_AFTER = 10  # a frame file untouched this long means the feed froze
PREVIEW_TITLE = "Falak: live view"
_procs: dict[int, subprocess.Popen] = {}  # the capture and preview processes this node started, by pid


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


# ---- live view ---------------------------------------------------------------------------------------


def _tool(name: str, fallbacks: tuple[str, ...]) -> str | None:
    return shutil.which(name) or next((p for p in fallbacks if Path(p).exists()), None)


def list_cameras(ffmpeg: str) -> list[tuple[str, str]]:
    """(avfoundation index, name) of every camera. Screens that avfoundation lists as 'devices' are left out."""
    r = subprocess.run([ffmpeg, "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
                       capture_output=True, text=True, timeout=TIMEOUT)
    cameras, in_video = [], False
    for line in (r.stderr or "").splitlines():
        if "AVFoundation video devices" in line:
            in_video = True
        elif "AVFoundation audio devices" in line:
            break
        elif in_video and (m := re.search(r"\[(\d+)\]\s+(.+)$", line)):
            if not m.group(2).lower().startswith("capture screen"):
                cameras.append((m.group(1), m.group(2).strip()))
    return cameras


def choose_camera(cameras: list[tuple[str, str]], wanted: str) -> tuple[str, str]:
    """The camera whose name contains `wanted` (case-insensitive); the first one when nothing is asked for."""
    if not cameras:
        raise ActionError("I couldn't find a camera.")
    if not wanted.strip():
        return cameras[0]
    for index, name in cameras:
        if wanted.strip().lower() in name.lower():
            return index, name
    raise ActionError(f"No camera is called '{wanted}'. The cameras I can see: {', '.join(n for _, n in cameras)}.")


_GUARD = ('parent=$1; shift; "$@" & child=$!; '
          'while kill -0 "$parent" 2>/dev/null && kill -0 "$child" 2>/dev/null; do sleep 1; done; '
          'kill "$child" 2>/dev/null')


def _spawn(argv: list[str], log: Path | None = None) -> subprocess.Popen:
    """Start a process in its own group. The shell around it ends it if this node dies without cleaning up."""
    err = open(log, "wb") if log else subprocess.DEVNULL
    try:
        proc = subprocess.Popen(["/bin/sh", "-c", _GUARD, "sh", str(os.getpid()), *argv],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err,
                                start_new_session=True)
    finally:
        if log:
            err.close()
    _procs[proc.pid] = proc
    return proc


def _kill(pid: int | None) -> None:
    if not pid:
        return
    proc = _procs.pop(pid, None)
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    if proc is not None:
        try:
            proc.wait(3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            proc.wait(3)


def _alive(pid: int | None) -> bool:
    proc = _procs.get(pid) if pid else None
    return proc is not None and proc.poll() is None


def _capture_error(said: str) -> ActionError:
    if DENIED.search(said):
        return ActionError("Camera access is blocked. Allow it for the terminal app running me in "
                           "System Settings > Privacy & Security > Camera.")
    if BUSY.search(said):
        return ActionError("The camera is in use by another app.")
    return ActionError(f"The camera didn't start ({said.strip().splitlines()[-1][:120] if said.strip() else 'no reason given'}).")


def start_live(ctx: AppContext, device: str = "") -> SessionStarted:
    ffmpeg = _tool("ffmpeg", FFMPEG_FALLBACKS)
    if not ffmpeg:
        raise ActionError("The video tool isn't installed. Run: brew install ffmpeg")
    try:
        cameras = list_cameras(ffmpeg)
    except subprocess.TimeoutExpired:
        raise ActionError("The camera list didn't come back. Is another app holding the camera?") from None
    index, name = choose_camera(cameras, device or str(ctx.settings["device"]))
    frame = ctx.temp_path(".jpg")
    log = ctx.temp_path(".log")
    capture = _spawn([ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "avfoundation", "-framerate", "30",
                      "-i", index, "-vf", "fps=2,scale='min(1280,iw)':-2", "-q:v", "5", "-update", "1",
                      "-atomic_writing", "1", "-y", str(frame)], log)
    deadline = time.monotonic() + FIRST_FRAME_WAIT
    try:
        while time.monotonic() < deadline:
            if capture.poll() is not None:
                capture.wait()
                raise _capture_error(log.read_text(errors="replace"))
            if frame.stat().st_size > 0:
                break
            time.sleep(0.2)
        else:
            raise ActionError(f"{name} didn't produce a picture in {FIRST_FRAME_WAIT} seconds. "
                              "Is another app using it, or is camera access blocked for this terminal?")
    except BaseException:
        _kill(capture.pid)
        frame.unlink(missing_ok=True)
        log.unlink(missing_ok=True)
        raise
    state = {"pid": capture.pid, "frame": str(frame), "log": str(log), "device": name,
             "preview_pid": _start_preview(ctx, index)}
    return SessionStarted(f"Watching {name}.", state)


def _start_preview(ctx: AppContext, index: str) -> int | None:
    """A small window so Elijah can see what the camera sees. Best effort: a session without it is fine."""
    ffplay = _tool("ffplay", FFPLAY_FALLBACKS)
    if not ctx.settings["preview"] or not ffplay:
        return None
    try:
        proc = _spawn([ffplay, "-loglevel", "quiet", "-window_title", PREVIEW_TITLE, "-f", "avfoundation",
                       "-framerate", "30", "-i", index, "-x", "480", "-an", "-alwaysontop"])
        return proc.pid
    except OSError:
        ctx.log.warning("the preview window did not start", exc_info=True)
        return None


def sample_live(ctx: AppContext, state: dict) -> Result:
    frame = Path(state["frame"])
    if not _alive(state["pid"]):
        raise ActionError(f"The camera feed from {state['device']} has stopped. Close the view and open it again.")
    try:
        if time.time() - frame.stat().st_mtime > STALE_AFTER or frame.stat().st_size == 0:
            raise ActionError(f"The picture from {state['device']} has frozen. Close the view and open it again.")
        path = ctx.temp_path(".jpg")
        shutil.copyfile(frame, path)
    except FileNotFoundError:
        raise ActionError(f"The picture from {state['device']} is gone. Close the view and open it again.") from None
    return Result(f"Current view from {state['device']}.", attachments=(Attachment("image", path, "image/jpeg"),))


def stop_live(ctx: AppContext, state: dict) -> Result:
    _kill(state.get("preview_pid"))
    _kill(state.get("pid"))
    for leftover in (state["frame"], state["frame"] + ".tmp", state["log"]):
        Path(leftover).unlink(missing_ok=True)
    return Result(f"Closed the view of {state['device']}.")
