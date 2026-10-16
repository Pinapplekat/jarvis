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


def built() -> bool:
    stamp = APP / "Contents" / "Resources" / "stamp"
    return EXECUTABLE.exists() and stamp.exists() and stamp.read_text() == STAMP


def build() -> bool:
    """Build and ad-hoc sign the app if missing or outdated. Returns True if it (re)built."""
    if built():
        return False
    (APP / "Contents" / "MacOS").mkdir(parents=True, exist_ok=True)
    (APP / "Contents" / "Resources").mkdir(parents=True, exist_ok=True)
    (APP / "Contents" / "Info.plist").write_bytes(plistlib.dumps(INFO))
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "launcher.c"
        src.write_text(SOURCE)
        subprocess.run(["clang", "-O2", "-o", str(EXECUTABLE), str(src)], check=True, capture_output=True)
    subprocess.run(["codesign", "--force", "--sign", "-", "--identifier", BUNDLE_ID, str(APP)],
                   check=True, capture_output=True)
    (APP / "Contents" / "Resources" / "stamp").write_text(STAMP)
    return True
