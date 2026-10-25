"""The Mac's camera, on request: one still for JARVIS to look at privately, then deleted.

Only when Elijah himself asks to show JARVIS something (chat.respond checks that); never from a
schedule, tick, email or page. The frame goes to a private temp file (0600, in a 0700 folder),
is looked at once and deleted in a `finally`; leftovers from a crash are swept at startup.

Normally 'JARVIS Camera.app' takes it: a small native app, opened on demand, that holds the Camera
permission (Panel and Inbox stay permission-free). It shows a floating live preview marked LIVE,
waits CAMERA_PREVIEW_SECONDS, takes the picture with a white flash, writes it into the private file
JARVIS made, shows the frozen frame for CAMERA_HOLD_SECONDS and goes back to the live feed. The
preview stays up until Elijah closes it (its ✕, Esc) or asks JARVIS to (close_preview), or after
CAMERA_PREVIEW_MAX_SECONDS without a look; then the camera stops and the app quits. A second look
while it's up reuses it. It never saves anything itself. It's asked the way the panel is: a request
file and an ack file (plus a state file while the preview is up, and a close file). If it can't
open or doesn't answer in time (CAMERA_APP_TIMEOUT), or with CAMERA_PREVIEW=0, imagesnap takes the
still with no preview (brew install imagesnap; needs Camera permission for the process running
JARVIS), and `last_note` says so plainly for his reply. CAMERA=0 turns it off.
"""

import hashlib
import json
import os
import platform
import plistlib
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from jarvis import config

PREFIX = "camera-"
NOT_ALLOWED = "camera not allowed"
NOT_FOUND = "camera not found"
NO_SCREEN = "no screen for the preview"
CLOSED_EARLY = "the preview was closed before the picture"
DENIED = re.compile(r"not authori[sz]ed|denied|permission|access", re.I)
NO_DEVICE = re.compile(r"no (video )?devices?|not found|no camera", re.I)
last_how = ""  # how the last still was taken (the step trace says when the preview was skipped)
last_note = ""  # a plain sentence for his reply about the preview (skipped and why, or still up)
STILL_UP = "The camera preview is still up; say \"close the camera\" or press Esc on it when you're done."
CLOSED = "Closed the camera preview, sir; the camera's off."
NOT_OPEN = "The camera preview isn't open, sir."


class AppUnavailable(RuntimeError):
    """The camera app couldn't open or didn't answer: imagesnap takes the still instead."""


class Closed(RuntimeError):
    """He closed the preview before the picture: none is taken, not even by imagesnap."""


def _skipped(why: str) -> str:
    return f"The camera preview didn't open ({why}), so I took the picture without it."


def _tmp_dir() -> Path:
    d = config.DATA_DIR / "tmp" / "camera"
    d.mkdir(parents=True, exist_ok=True)
    d.chmod(0o700)
    return d


def _app_dir() -> Path:
    d = config.DATA_DIR / "camera-app"
    d.mkdir(parents=True, exist_ok=True)
    d.chmod(0o700)
    return d


def _imagesnap() -> str | None:
    return (shutil.which("imagesnap") or next((p for p in ("/opt/homebrew/bin/imagesnap", "/usr/local/bin/imagesnap")
                                               if Path(p).exists()), None))


def _private_file() -> Path:
    path = _tmp_dir() / f"{PREFIX}{uuid.uuid4().hex}.jpg"
    os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    return path


def _shrink(path: Path, run) -> None:
    path.chmod(0o600)
    from jarvis.screen import VISION_MAX_EDGE
    run(["/usr/bin/sips", "-Z", str(VISION_MAX_EDGE), str(path)], capture_output=True, timeout=20)
    path.chmod(0o600)


def capture_frame(run=None, tool: str | None = None, launch=None, sleep=time.sleep) -> Path:
    """One still from the camera in a private temp file; the caller deletes it. RuntimeError
    "camera not allowed" / "camera not found" when the permission or the device is missing."""
    global last_how, last_note
    if not config.CAMERA:
        raise RuntimeError("the camera is off (CAMERA=0)")
    run = run or subprocess.run
    last_note = ""
    if config.CAMERA_PREVIEW:
        reused = preview_open()
        try:
            path = _via_app(run, (lambda: None) if reused else (launch or _launch), sleep)
            last_how = "with the preview (already open)" if reused else "with the preview"
            last_note = STILL_UP
            return path
        except AppUnavailable as e:
            last_how = f"no preview: {e}; taken with imagesnap"
            last_note = _skipped(str(e))
        except Closed:
            raise
        except RuntimeError as e:  # the app said no (permission, no picture): imagesnap may still manage
            if not (_imagesnap() if tool is None else tool):
                raise
            try:
                path = _via_imagesnap(run, tool)
            except RuntimeError:
                raise e from None
            last_how = f"no preview: {e}; taken with imagesnap"
            last_note = _skipped(str(e))
            return path
    else:
        last_how = "no preview (CAMERA_PREVIEW=0); taken with imagesnap"
    return _via_imagesnap(run, tool)


def _via_imagesnap(run, tool: str | None) -> Path:
    tool = _imagesnap() if tool is None else tool
    if not tool:
        raise RuntimeError(f"{NOT_FOUND} (the snapshot tool isn't installed: brew install imagesnap)")
    path = _private_file()
    try:
        r = run([tool, "-q", "-w", "1", str(path)], capture_output=True, text=True, timeout=20)
        said = f"{r.stdout or ''} {r.stderr or ''}"
        if r.returncode != 0 or not path.exists() or path.stat().st_size == 0:
            if NO_DEVICE.search(said):
                raise RuntimeError(NOT_FOUND)
            if DENIED.search(said) or r.returncode == 0:  # macOS gives an empty frame when it says no
                raise RuntimeError(f"{NOT_ALLOWED} (allow it in System Settings > Privacy & Security > Camera)")
            raise RuntimeError(f"couldn't use the camera ({said.strip()[:120] or r.returncode})")
        _shrink(path, run)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


# ----- JARVIS Camera.app -----
def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _launch() -> None:
    if not built():
        raise AppUnavailable("the camera app isn't built (jarvis schedule install builds it)")
    r = subprocess.run(["/usr/bin/open", "-g", "-a", str(APP), "--args", str(_app_dir())],
                       capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        raise AppUnavailable(f"the camera app didn't open ({(r.stderr or '').strip()[:100] or r.returncode})")


def _via_app(run, launch, sleep) -> Path:
    """Ask the app for one picture: a request file, then wait for its ack with a timeout."""
    d = _app_dir()
    path, rid = _private_file(), uuid.uuid4().hex
    request, ack = d / "request.json", d / "ack.json"
    try:
        ack.unlink(missing_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".request-")
        with os.fdopen(fd, "w") as f:
            json.dump({"id": rid, "out": str(path), "preview": config.CAMERA_PREVIEW_SECONDS,
                       "hold": config.CAMERA_HOLD_SECONDS, "max": config.CAMERA_PREVIEW_MAX_SECONDS,
                       "at": time.time()}, f)
        os.replace(tmp, request)
        launch()
        deadline = time.time() + config.CAMERA_APP_TIMEOUT
        got = {}
        while time.time() < deadline:
            got = _read(ack)
            if got.get("id") == rid:
                break
            sleep(0.05)
        if got.get("id") != rid:
            raise AppUnavailable("the camera app didn't answer in time")
        if got.get("error"):
            why = str(got["error"])[:120]
            if why.startswith(NO_SCREEN):  # headless, or no display awake: the preview can't show
                raise AppUnavailable(why)
            if why.startswith(CLOSED_EARLY):  # he closed it: no picture any other way either
                raise Closed(f"{CLOSED_EARLY}, so none was taken")
            if why.startswith(NOT_ALLOWED):
                raise RuntimeError(f"{NOT_ALLOWED} (allow JARVIS Camera in System Settings > Privacy & Security > Camera)")
            raise RuntimeError(NOT_FOUND if why.startswith(NOT_FOUND) else f"couldn't use the camera ({why})")
        if not path.exists() or path.stat().st_size == 0:
            raise RuntimeError("couldn't use the camera (the app wrote no picture)")
        _shrink(path, run)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    finally:
        request.unlink(missing_ok=True)  # an app that answers late finds nothing to do
    return path


def _alive(pid) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def preview_open() -> bool:
    """The app's live preview is up (its state file names a running process)."""
    state = config.DATA_DIR / "camera-app" / "state.json"
    if not state.exists():
        return False
    if not _alive(_read(state).get("pid")):
        state.unlink(missing_ok=True)  # left by a crash: nothing is showing
        return False
    return True


def close_preview(timeout: float = 3.0, sleep=time.sleep) -> bool:
    """Close the live preview (the camera stops and the app quits). False if none was open;
    RuntimeError if the app didn't close in time."""
    if not preview_open():
        return False
    d = _app_dir()
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".close-")
    with os.fdopen(fd, "w") as f:
        json.dump({"at": time.time()}, f)
    os.replace(tmp, d / "close.json")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not preview_open():
            return True
        sleep(0.05)
    raise RuntimeError("the camera preview didn't close; close it with its ✕ or Esc")


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


APP = Path.home() / "Applications" / "JARVIS Camera.app"
EXECUTABLE = APP / "Contents" / "MacOS" / "jarvis-camera"

SWIFT = r"""
import AppKit
import AVFoundation

let appDir = CommandLine.arguments.dropFirst().first { !$0.hasPrefix("-") }
    ?? NSHomeDirectory() + "/Library/Application Support/JARVIS/camera-app"
let requestPath = appDir + "/request.json"
let ackPath = appDir + "/ack.json"
let statePath = appDir + "/state.json"  // there while the preview is up, so JARVIS reuses it
let closePath = appDir + "/close.json"  // JARVIS asking to close it
// The only place a picture may go: the private files JARVIS makes in its camera temp folder.
let stillPrefix = (appDir as NSString).deletingLastPathComponent + "/tmp/camera/camera-"

func ack(_ id: String, _ fields: [String: Any]) {
    var body = fields
    body["id"] = id
    body["at"] = Date().timeIntervalSince1970
    if let d = try? JSONSerialization.data(withJSONObject: body) {
        try? d.write(to: URL(fileURLWithPath: ackPath), options: .atomic)
    }
}

// Into the 0600 file JARVIS made, never a new one: a file it gave up on stays gone. Nothing else is saved.
func writeStill(_ data: Data, to path: String) -> Bool {
    guard let h = FileHandle(forWritingAtPath: path) else { return false }
    defer { h.closeFile() }
    h.truncateFile(atOffset: 0)
    h.write(data)
    return true
}

struct Request {
    let id: String, out: String, preview: Double, hold: Double, max: Double
}

func pending(after last: String) -> Request? {
    guard let data = FileManager.default.contents(atPath: requestPath),
          let o = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
          let id = o["id"] as? String, id != last, id.range(of: "^[a-f0-9]{32}$", options: .regularExpression) != nil,
          let out = o["out"] as? String, out.hasPrefix(stillPrefix), !out.contains("/.."),
          !out.dropFirst(stillPrefix.count).contains("/"),
          let at = o["at"] as? Double, abs(Date().timeIntervalSince1970 - at) < 30 else { return nil }
    let clamp = { (v: Any?, d: Double) in min(5, max(0, (v as? Double) ?? d)) }
    let mx = min(3600, max(0, (o["max"] as? Double) ?? 900))
    return Request(id: id, out: out, preview: clamp(o["preview"], 1), hold: clamp(o["hold"], 0.6), max: mx)
}

let line = NSColor.white.withAlphaComponent(0.18)

// Borderless, but it can take Esc once clicked; it never takes focus on its own.
final class Preview: NSPanel {
    var onClose: (() -> Void)?
    override var canBecomeKey: Bool { true }
    override func cancelOperation(_ sender: Any?) { onClose?() }
    override func keyDown(with event: NSEvent) {
        if event.keyCode == 53 { onClose?() } else { super.keyDown(with: event) }
    }
}

final class Camera: NSObject, AVCapturePhotoCaptureDelegate {
    var req: Request!
    var last = ""
    var busy = false
    var session: AVCaptureSession?
    var photo = AVCapturePhotoOutput()
    var window: Preview?
    var frozen: NSImageView!
    var flash: NSView!
    var lastUse = Date()
    var maxOpen = 900.0

    // One request at a time. The preview stays up between them; with no preview and nothing to do,
    // the app quits (it never sits holding the camera unseen).
    func next() {
        guard !busy else { return }
        if FileManager.default.fileExists(atPath: closePath) { return close() }
        if window != nil, maxOpen > 0, Date().timeIntervalSince(lastUse) > maxOpen { return close() }
        guard let r = pending(after: last) else {
            if window == nil { exit(0) }
            return
        }
        req = r
        last = r.id
        busy = true
        lastUse = Date()
        maxOpen = r.max
        if window != nil {  // already live: just wait a moment and take it
            DispatchQueue.main.asyncAfter(deadline: .now() + r.preview) { self.snap() }
            return
        }
        switch AVCaptureDevice.authorizationStatus(for: .video) {
        case .authorized: open()
        case .notDetermined:
            AVCaptureDevice.requestAccess(for: .video) { ok in
                DispatchQueue.main.async { ok ? self.open() : self.fail("camera not allowed") }
            }
        default: fail("camera not allowed")
        }
    }

    func fail(_ why: String) {
        ack(req.id, ["error": why])
        busy = false
        next()
    }

    @objc func close() {
        session?.stopRunning()
        session = nil
        window?.orderOut(nil)
        window = nil
        try? FileManager.default.removeItem(atPath: statePath)
        try? FileManager.default.removeItem(atPath: closePath)
        if busy {
            ack(req.id, ["error": "the preview was closed before the picture"])
            busy = false
        }
        next()
    }

    func open() {
        guard let screen = (NSScreen.main ?? NSScreen.screens.first)?.visibleFrame else {
            return fail("no screen for the preview")
        }
        let s = AVCaptureSession()
        photo = AVCapturePhotoOutput()
        guard let cam = AVCaptureDevice.default(for: .video), let input = try? AVCaptureDeviceInput(device: cam),
              s.canAddInput(input), s.canAddOutput(photo) else { return fail("camera not found") }
        s.beginConfiguration()
        s.sessionPreset = .photo
        s.addInput(input)
        s.addOutput(photo)
        s.commitConfiguration()
        session = s

        // A small floating preview, top right, in the panel's look: hard edges, a hairline.
        let size = NSSize(width: 320, height: 240)
        let frame = NSRect(x: screen.maxX - size.width - 16, y: screen.maxY - size.height - 16,
                           width: size.width, height: size.height)
        let window = Preview(contentRect: frame, styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        window.onClose = { [weak self] in self?.close() }
        self.window = window
        window.level = .floating
        window.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        window.isReleasedWhenClosed = false
        window.hasShadow = true
        window.backgroundColor = .black
        let root = NSView(frame: NSRect(origin: .zero, size: size))
        root.wantsLayer = true
        root.layer!.backgroundColor = NSColor.black.cgColor
        root.layer!.borderWidth = 1
        root.layer!.borderColor = line.cgColor
        let live = AVCaptureVideoPreviewLayer(session: s)
        live.frame = root.bounds
        live.videoGravity = .resizeAspectFill
        root.layer!.addSublayer(live)
        frozen = NSImageView(frame: root.bounds)
        frozen.imageScaling = .scaleProportionallyUpOrDown
        frozen.isHidden = true
        root.addSubview(frozen)
        flash = NSView(frame: root.bounds)
        flash.wantsLayer = true
        flash.layer!.backgroundColor = NSColor.white.cgColor
        flash.alphaValue = 0
        root.addSubview(flash)
        // It's plainly live while it's up, and closes with its ✕ (or Esc once clicked).
        let badge = NSTextField(labelWithString: "● LIVE")
        badge.font = .boldSystemFont(ofSize: 11)
        badge.textColor = .systemRed
        badge.frame = NSRect(x: 8, y: size.height - 22, width: 80, height: 16)
        root.addSubview(badge)
        let x = NSButton(title: "✕", target: self, action: #selector(close))
        x.isBordered = false
        x.contentTintColor = .white
        x.toolTip = "Close the camera"
        x.frame = NSRect(x: size.width - 28, y: size.height - 26, width: 22, height: 22)
        root.addSubview(x)
        window.contentView = root
        window.orderFrontRegardless()
        let state = ["pid": Int(ProcessInfo.processInfo.processIdentifier), "at": Date().timeIntervalSince1970] as [String: Any]
        if let d = try? JSONSerialization.data(withJSONObject: state) {
            try? d.write(to: URL(fileURLWithPath: statePath), options: .atomic)
        }
        DispatchQueue.global(qos: .userInitiated).async { s.startRunning() }
        DispatchQueue.main.asyncAfter(deadline: .now() + req.preview) { self.snap() }
    }

    func snap() {
        guard window != nil, busy else { return }  // closed while waiting
        photo.capturePhoto(with: AVCapturePhotoSettings(format: [AVVideoCodecKey: AVVideoCodecType.jpeg]), delegate: self)
        flash.alphaValue = 1
        NSAnimationContext.runAnimationGroup { c in
            c.duration = 0.35
            self.flash.animator().alphaValue = 0
        }
    }

    func photoOutput(_ output: AVCapturePhotoOutput, didFinishProcessingPhoto photo: AVCapturePhoto, error: Error?) {
        let data = error == nil ? photo.fileDataRepresentation() : nil
        DispatchQueue.main.async {
            guard self.window != nil, self.busy else { return }  // closed meanwhile (close() acked it)
            guard let data = data else { return self.fail("couldn't take the picture") }
            self.frozen.image = NSImage(data: data)  // the frozen frame, on screen only
            self.frozen.isHidden = false
            if writeStill(data, to: self.req.out) {
                ack(self.req.id, ["state": "captured"])
            } else {
                ack(self.req.id, ["error": "JARVIS stopped waiting for the picture"])
            }
            self.lastUse = Date()
            DispatchQueue.main.asyncAfter(deadline: .now() + self.req.hold) {
                self.frozen?.isHidden = true  // back to the live feed; it stays up until closed
                self.frozen?.image = nil
                self.busy = false
                self.next()
            }
        }
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)  // no Dock icon
let camera = Camera()
try? FileManager.default.removeItem(atPath: closePath)  // an old close request isn't for this preview
DispatchQueue.main.async { camera.next() }
// Watch for the next look, a close request or the idle limit while the preview is up.
Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { _ in camera.next() }
app.run()
"""

INFO = {
    "CFBundleIdentifier": "com.jarvis.camera",
    "CFBundleName": "JARVIS Camera",
    "CFBundleDisplayName": "JARVIS Camera",
    "CFBundleExecutable": "jarvis-camera",
    "CFBundlePackageType": "APPL",
    "CFBundleShortVersionString": "1.0",
    "CFBundleVersion": "1",
    "LSUIElement": True,
    "NSCameraUsageDescription": "JARVIS takes one picture when you ask to show it something; it's looked at, then deleted.",
}


# Bumped when build() itself changes, so apps built the old way are rebuilt (2: signed after the stamp,
# 3: explicit deployment target).
BUILD_VERSION = "3"

# An explicit deployment target: left to the SDK default, the binary's minos can be newer than the
# running macOS and LaunchServices refuses to open it (error -10825).
TARGET = f"{'x86_64' if platform.machine() == 'x86_64' else 'arm64'}-apple-macos13"


def _stamp() -> str:
    return hashlib.sha256((BUILD_VERSION + SWIFT + repr(sorted(INFO.items()))
                           + config.CAMERA_SIGN_IDENTITY).encode()).hexdigest()[:16]


def built() -> bool:
    marker = APP / "Contents" / "Resources" / "stamp"
    return EXECUTABLE.exists() and marker.exists() and marker.read_text() == _stamp()


def build() -> bool:
    """Compile and sign the camera app if missing or outdated. Only rebuilt when its source changes,
    and signed with CAMERA_SIGN_IDENTITY when set, so macOS keeps its Camera permission."""
    if built():
        return False
    (APP / "Contents" / "MacOS").mkdir(parents=True, exist_ok=True)
    (APP / "Contents" / "Resources").mkdir(parents=True, exist_ok=True)
    (APP / "Contents" / "Info.plist").write_bytes(plistlib.dumps(INFO))
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "main.swift"
        src.write_text(SWIFT)
        r = subprocess.run(["swiftc", "-O", "-target", TARGET, "-o", str(EXECUTABLE), str(src)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"couldn't compile the camera app: {r.stderr[-500:]}")
    # The stamp lives inside the bundle, so it's written before signing: codesign seals it, and
    # nothing touches the bundle afterwards (a later write breaks the signature and the app won't open).
    stamp = APP / "Contents" / "Resources" / "stamp"
    stamp.write_text(_stamp())
    try:
        r = subprocess.run(["codesign", "--force", "--sign", config.CAMERA_SIGN_IDENTITY or "-",
                            "--identifier", INFO["CFBundleIdentifier"], str(APP)], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"couldn't sign the camera app: {r.stderr[-500:]}")
    except BaseException:
        stamp.unlink(missing_ok=True)  # unsigned isn't built: try again next time
        raise
    return True
