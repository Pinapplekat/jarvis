"""The Mac's camera, on request: one still for JARVIS to look at privately, then deleted.

Only when Elijah himself asks to show JARVIS something (chat.respond checks that); never from a
schedule, tick, email or page. The frame goes to a private temp file (0600, in a 0700 folder),
is looked at once and deleted in a `finally`; leftovers from a crash are swept at startup.

Normally 'JARVIS Camera.app' takes it: a small native app, opened on demand, that holds the Camera
permission (Panel and Inbox stay permission-free). It shows a floating live preview for
CAMERA_PREVIEW_SECONDS, takes the picture with a white flash, writes it into the private file JARVIS
made, holds the frozen frame for CAMERA_HOLD_SECONDS and closes. It never saves anything itself.
It's asked the way the panel is: a request file and an ack file. If it can't open or doesn't
answer in time (CAMERA_APP_TIMEOUT), or with CAMERA_PREVIEW=0, imagesnap takes the still with no
preview (brew install imagesnap; needs Camera permission for the process running JARVIS).
CAMERA=0 turns it off.
"""

import hashlib
import json
import os
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
DENIED = re.compile(r"not authori[sz]ed|denied|permission|access", re.I)
NO_DEVICE = re.compile(r"no (video )?devices?|not found|no camera", re.I)
last_how = ""  # how the last still was taken (the step trace says when the preview was skipped)


class AppUnavailable(RuntimeError):
    """The camera app couldn't open or didn't answer: imagesnap takes the still instead."""


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
    global last_how
    if not config.CAMERA:
        raise RuntimeError("the camera is off (CAMERA=0)")
    run = run or subprocess.run
    if config.CAMERA_PREVIEW:
        try:
            path = _via_app(run, launch or _launch, sleep)
            last_how = "with the preview"
            return path
        except AppUnavailable as e:
            last_how = f"no preview: {e}; taken with imagesnap"
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
                       "hold": config.CAMERA_HOLD_SECONDS, "at": time.time()}, f)
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
    let id: String, out: String, preview: Double, hold: Double
}

func pending(after last: String) -> Request? {
    guard let data = FileManager.default.contents(atPath: requestPath),
          let o = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
          let id = o["id"] as? String, id != last, id.range(of: "^[a-f0-9]{32}$", options: .regularExpression) != nil,
          let out = o["out"] as? String, out.hasPrefix(stillPrefix), !out.contains("/.."),
          !out.dropFirst(stillPrefix.count).contains("/"),
          let at = o["at"] as? Double, abs(Date().timeIntervalSince1970 - at) < 30 else { return nil }
    let clamp = { (v: Any?, d: Double) in min(5, max(0, (v as? Double) ?? d)) }
    return Request(id: id, out: out, preview: clamp(o["preview"], 1.5), hold: clamp(o["hold"], 2))
}

let line = NSColor.white.withAlphaComponent(0.18)

final class Camera: NSObject, AVCapturePhotoCaptureDelegate {
    var req: Request!
    var last = ""
    var busy = false
    var session: AVCaptureSession?
    var photo = AVCapturePhotoOutput()
    var window: NSPanel!
    var frozen: NSImageView!
    var flash: NSView!

    // One request at a time; when none is left, the app quits (it never sits holding the camera).
    func next() {
        guard !busy else { return }
        guard let r = pending(after: last) else { exit(0) }
        req = r
        last = r.id
        busy = true
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
        done(after: 0)
    }

    func open() {
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
        let screen = (NSScreen.main ?? NSScreen.screens.first)!.visibleFrame
        let frame = NSRect(x: screen.maxX - size.width - 16, y: screen.maxY - size.height - 16,
                           width: size.width, height: size.height)
        window = NSPanel(contentRect: frame, styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
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
        window.contentView = root
        window.orderFrontRegardless()
        DispatchQueue.global(qos: .userInitiated).async { s.startRunning() }
        DispatchQueue.main.asyncAfter(deadline: .now() + req.preview) { self.snap() }
    }

    func snap() {
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
            guard let data = data else { return self.fail("couldn't take the picture") }
            self.frozen.image = NSImage(data: data)  // the frozen frame, on screen only
            self.frozen.isHidden = false
            self.session?.stopRunning()
            if writeStill(data, to: self.req.out) {
                ack(self.req.id, ["state": "captured"])
            } else {
                ack(self.req.id, ["error": "JARVIS stopped waiting for the picture"])
            }
            self.done(after: self.req.hold)
        }
    }

    func done(after: Double) {
        DispatchQueue.main.asyncAfter(deadline: .now() + after) {
            self.session?.stopRunning()
            self.session = nil
            self.window?.orderOut(nil)
            self.window = nil
            self.busy = false
            self.next()
        }
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)  // no Dock icon
let camera = Camera()
DispatchQueue.main.async { camera.next() }
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


def _stamp() -> str:
    return hashlib.sha256((SWIFT + repr(sorted(INFO.items())) + config.CAMERA_SIGN_IDENTITY).encode()).hexdigest()[:16]


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
        r = subprocess.run(["swiftc", "-O", "-o", str(EXECUTABLE), str(src)], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"couldn't compile the camera app: {r.stderr[-500:]}")
    subprocess.run(["codesign", "--force", "--sign", config.CAMERA_SIGN_IDENTITY or "-",
                    "--identifier", INFO["CFBundleIdentifier"], str(APP)], check=True, capture_output=True)
    (APP / "Contents" / "Resources" / "stamp").write_text(_stamp())
    return True
