"""Small Mac controls that need no extra permission: the system output volume and mute, and
putting the display to sleep. Dark mode goes through System Events, which macOS gates behind an
Automation prompt, so it's off unless MAC_SYSTEM_EVENTS=1 (and Elijah allowed it)."""

import subprocess

from jarvis import config

OPS = ("volume", "volume_up", "volume_down", "mute", "unmute", "display_sleep", "dark_mode")


def _osa(script: str, run=subprocess.run) -> str:
    r = run(["osascript", "-e", script], capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        raise ValueError((r.stderr or "").strip()[:150] or "osascript failed")
    return (r.stdout or "").strip()


def battery(run=subprocess.run) -> tuple[int, bool] | None:
    """(percent, charging) from pmset, or None when there's no battery or it can't tell. No permission needed."""
    import re
    try:
        r = run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"(\d{1,3})%;\s*([\w ]+?);", r.stdout or "")
    if r.returncode != 0 or not m:
        return None
    return int(m[1]), m[2].strip().lower() in ("charging", "charged", "finishing charge")


def control(op: str, value=None, run=subprocess.run) -> str:
    """Do it and return one short line saying what happened."""
    if op not in OPS:
        raise ValueError(f"unknown Mac control {op!r} ({', '.join(OPS)})")
    if op in ("volume", "volume_up", "volume_down"):
        if op == "volume":
            try:
                level = int(value)
            except (TypeError, ValueError):
                raise ValueError("volume needs a number from 0 to 100") from None
        else:
            now = int(_osa("output volume of (get volume settings)", run) or 50)
            step = int(value or 10)
            level = now + step if op == "volume_up" else now - step
        level = max(0, min(100, level))
        _osa(f"set volume output volume {level} without output muted", run)
        return f"Mac volume at {level}%."
    if op in ("mute", "unmute"):
        _osa(f"set volume {'with' if op == 'mute' else 'without'} output muted", run)
        return "Mac muted." if op == "mute" else "Mac unmuted."
    if op == "display_sleep":
        r = run(["pmset", "displaysleepnow"], capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            raise ValueError("couldn't put the display to sleep")
        return "Display asleep."
    if not config.MAC_SYSTEM_EVENTS:
        raise ValueError("dark mode needs MAC_SYSTEM_EVENTS=1 in .env and his OK for JARVIS to control System Events")
    on = str(value).lower() not in ("off", "false", "0", "light")
    _osa("tell application \"System Events\" to tell appearance preferences to set dark mode to "
         + ("true" if on else "false"), run)
    return "Dark mode on." if on else "Dark mode off."
