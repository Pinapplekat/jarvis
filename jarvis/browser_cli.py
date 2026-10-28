"""`falak browser setup|status|reset`: put Falak's Chromium in the node's folder, and wipe its logins on request."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from jarvis.node.config import node_dir

MIN_FREE = 2 * 1024**3  # Chromium is a few hundred MB; never take the disk below 2 GiB


def folder() -> Path:
    return node_dir() / "browser"


def _free(path: Path) -> int:
    while not path.exists():
        path = path.parent
    return shutil.disk_usage(path).free


def installed(browsers: Path) -> bool:
    return any(browsers.glob("chromium*/*")) if browsers.is_dir() else False


def setup() -> int:
    """Install Playwright's Chromium into FALAK_NODE_DIR/browser/chromium, after checking there is room."""
    try:
        import playwright  # noqa: F401
    except ImportError:
        print("Playwright isn't installed. Run: uv sync --extra browser (bin/falak does this for you).",
              file=sys.stderr)
        return 1
    browsers = folder() / "chromium"
    free = _free(browsers)
    if free < MIN_FREE:
        print(f"Only {free / 1024**3:.1f} GB free; Chromium needs room to spare ({MIN_FREE / 1024**3:.0f} GB). "
              "Free some space and run this again.", file=sys.stderr)
        return 1
    browsers.mkdir(parents=True, exist_ok=True)
    print(f"Installing Chromium into {browsers} ({free / 1024**3:.1f} GB free)...")
    done = subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"],
                          env={**os.environ, "PLAYWRIGHT_BROWSERS_PATH": str(browsers)})
    if done.returncode:
        return done.returncode
    print("Done. Say 'sign in to Canvas' to Falak when you're at the Mac.")
    return 0


def status() -> int:
    from jarvis.apps.browser.sites import Sites

    root = folder()
    sites = Sites(root)
    print(f"folder:   {root}")
    print(f"chromium: {'installed' if installed(root / 'chromium') else 'missing (run `falak browser setup`)'}")
    print(f"profile:  {'present' if sites.profile.is_dir() else 'none yet'}")
    print(f"signed-in sites: {', '.join(sites.listed()) or 'none'}")
    return 0


def reset(read=input) -> int:
    """Delete the profile (every saved login) and the list of sites. Only Elijah, at the Mac: it asks in the terminal."""
    from jarvis.apps.browser.sites import Sites

    sites = Sites(folder())
    if not sites.profile.exists() and not sites.path.exists():
        print("Nothing to reset.")
        return 0
    try:
        said = read(f"This deletes every saved login in Falak's browser ({sites.profile}). Type 'reset' to go on: ")
    except EOFError:
        said = ""
    if said.strip().lower() != "reset":
        print("Left as it was.")
        return 1
    shutil.rmtree(sites.profile, ignore_errors=True)
    sites.path.unlink(missing_ok=True)
    print("Deleted the profile and the list of signed-in sites.")
    return 0


def command(args) -> int:
    return {"setup": setup, "status": status, "reset": reset}[args.browser_command]()
