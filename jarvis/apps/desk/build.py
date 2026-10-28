"""Compile and sign 'Falak Ears.app' under the node's own folder. Rebuilt only when its source or this recipe changes.

Port of the old JARVIS camera app's build: swiftc with an explicit deployment target (left to the SDK default, the
binary can be newer than the running macOS and LaunchServices refuses to open it), a stamp written *before* signing
because codesign seals the bundle, and a stable signing identity (the `signing_identity` setting) so macOS keeps the
Microphone permission across rebuilds. Ad-hoc signing works but macOS asks for the permission again after a rebuild."""

from __future__ import annotations

import hashlib
import platform
import plistlib
import subprocess
from pathlib import Path

SOURCE = Path(__file__).with_name("swift") / "Ears.swift"
BUILD_VERSION = "1"
TARGET = f"{'x86_64' if platform.machine() == 'x86_64' else 'arm64'}-apple-macos13"
INFO = {
    "CFBundleIdentifier": "com.falak.ears",
    "CFBundleName": "Falak Ears",
    "CFBundleDisplayName": "Falak Ears",
    "CFBundleExecutable": "falak-ears",
    "CFBundlePackageType": "APPL",
    "CFBundleShortVersionString": "1.0",
    "CFBundleVersion": "1",
    "LSUIElement": True,
    "NSMicrophoneUsageDescription": "Falak listens at your desk after you say its name or press its key; "
                                    "the audio is transcribed and dropped, never saved.",
}


def app_path(desk_dir: Path) -> Path:
    return desk_dir / "Falak Ears.app"


def stamp(identity: str) -> str:
    return hashlib.sha256((BUILD_VERSION + SOURCE.read_text() + repr(sorted(INFO.items())) + identity).encode()).hexdigest()[:16]


def built(desk_dir: Path, identity: str = "") -> bool:
    app = app_path(desk_dir)
    marker = app / "Contents" / "Resources" / "stamp"
    return (app / "Contents" / "MacOS" / "falak-ears").exists() and marker.exists() and marker.read_text() == stamp(identity)


def build(desk_dir: Path, identity: str = "") -> bool:
    """Returns whether it built (False: already up to date). Raises RuntimeError with the compiler's complaint."""
    if built(desk_dir, identity):
        return False
    app = app_path(desk_dir)
    (app / "Contents" / "MacOS").mkdir(parents=True, exist_ok=True)
    (app / "Contents" / "Resources").mkdir(parents=True, exist_ok=True)
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps(INFO))
    result = subprocess.run(["swiftc", "-O", "-target", TARGET, "-o", str(app / "Contents" / "MacOS" / "falak-ears"),
                             str(SOURCE)], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"couldn't compile Falak Ears: {result.stderr[-600:]}")
    stamp_file = app / "Contents" / "Resources" / "stamp"
    stamp_file.write_text(stamp(identity))  # inside the bundle, so it is written before signing seals it
    signed = subprocess.run(["codesign", "--force", "--sign", identity or "-", "--identifier", INFO["CFBundleIdentifier"],
                             str(app)], capture_output=True, text=True)
    if signed.returncode != 0:
        stamp_file.unlink(missing_ok=True)  # unsigned counts as not built: try again next time
        raise RuntimeError(f"couldn't sign Falak Ears: {signed.stderr[-400:]}")
    return True
