"""'JARVIS Senses.app': a tiny signed app that launches the watcher.

macOS grants Screen Recording to apps, and won't reliably list a bare command-line Python.
This app's only job is to start a command and stay its parent, so macOS attributes the
watcher's screen reading to "JARVIS Senses", which Elijah can approve once in System
Settings. It is only rebuilt when its source changes, since a rebuild changes its signature
and macOS would forget the permission.
"""

import hashlib
import plistlib
import subprocess
import tempfile
from pathlib import Path

APP = Path.home() / "Applications" / "JARVIS Senses.app"
EXECUTABLE = APP / "Contents" / "MacOS" / "jarvis-senses"
BUNDLE_ID = "com.jarvis.senses"

SOURCE = r"""
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <sys/wait.h>

extern char **environ;
static pid_t child = 0;
static void forward(int sig) { if (child > 0) kill(child, sig); }

/* Run argv[1..] as a child and wait, so macOS treats the child as this app's work. */
int main(int argc, char *argv[]) {
    if (argc < 2) { fprintf(stderr, "usage: jarvis-senses command [args...]\n"); return 2; }
    signal(SIGTERM, forward);
    signal(SIGINT, forward);
    if (posix_spawnp(&child, argv[1], NULL, NULL, &argv[1], environ) != 0) { perror("spawn"); return 1; }
    int status;
    while (waitpid(child, &status, 0) < 0) {}
    return WIFEXITED(status) ? WEXITSTATUS(status) : 1;
}
"""

INFO = {
    "CFBundleIdentifier": BUNDLE_ID,
    "CFBundleName": "JARVIS Senses",
    "CFBundleDisplayName": "JARVIS Senses",
    "CFBundleExecutable": "jarvis-senses",
    "CFBundlePackageType": "APPL",
    "CFBundleShortVersionString": "1.0",
    "CFBundleVersion": "1",
    "LSUIElement": True,  # no Dock icon
    "NSScreenCaptureUsageDescription": "JARVIS reads the text of your front window to help you.",
}

STAMP = hashlib.sha256((SOURCE + repr(sorted(INFO.items()))).encode()).hexdigest()[:16]


# A second app for the desk microphone ("Hey Jarvis"). Separate so that granting or changing
# it never touches JARVIS Senses' Screen Recording permission.
EARS_APP = Path.home() / "Applications" / "JARVIS Ears.app"
EARS_EXECUTABLE = EARS_APP / "Contents" / "MacOS" / "jarvis-senses"
EARS_INFO = {
    **{k: v for k, v in INFO.items() if k != "NSScreenCaptureUsageDescription"},
    "CFBundleIdentifier": "com.jarvis.ears",
    "CFBundleName": "JARVIS Ears",
    "CFBundleDisplayName": "JARVIS Ears",
    "NSMicrophoneUsageDescription": "JARVIS listens for \"Hey Jarvis\" and to what you say after it.",
}
EARS_STAMP = hashlib.sha256((SOURCE + repr(sorted(EARS_INFO.items()))).encode()).hexdigest()[:16]


def _built(app: Path, executable: Path, stamp: str) -> bool:
    marker = app / "Contents" / "Resources" / "stamp"
    return executable.exists() and marker.exists() and marker.read_text() == stamp


def _build(app: Path, executable: Path, info: dict, stamp: str) -> bool:
    if _built(app, executable, stamp):
        return False
    (app / "Contents" / "MacOS").mkdir(parents=True, exist_ok=True)
    (app / "Contents" / "Resources").mkdir(parents=True, exist_ok=True)
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps(info))
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "launcher.c"
        src.write_text(SOURCE)
        subprocess.run(["clang", "-O2", "-o", str(executable), str(src)], check=True, capture_output=True)
    subprocess.run(["codesign", "--force", "--sign", "-", "--identifier", info["CFBundleIdentifier"], str(app)],
                   check=True, capture_output=True)
    (app / "Contents" / "Resources" / "stamp").write_text(stamp)
    return True


def built() -> bool:
    return _built(APP, EXECUTABLE, STAMP)


def build() -> bool:
    """Build and ad-hoc sign JARVIS Senses if missing or outdated. Returns True if it (re)built."""
    return _build(APP, EXECUTABLE, INFO, STAMP)


def build_ears() -> bool:
    return _build(EARS_APP, EARS_EXECUTABLE, EARS_INFO, EARS_STAMP)
