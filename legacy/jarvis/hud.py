"""'JARVIS HUD.app': type to JARVIS on the Mac. ⌥Space (or "Type to JARVIS…" in the menu bar
indicator) brings up a floating glass bar; Return sends, Esc hides, ↑ recalls the last question.
While JARVIS works, the HUD shows what it's doing (the same white icons as the menu bar), then
the reply is typed out under the question as it streams in.

Like the panel, it's a tiny native app with no access to anything: it writes hud/request.json
(what he typed) and reads hud/reply.json, which `serve` (run by `jarvis listen`) writes, plus
the status file for the icons. Nothing goes over the network from the app itself.
"""

import hashlib
import json
import os
import plistlib
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

from jarvis import config

APP = Path.home() / "Applications" / "JARVIS HUD.app"
EXECUTABLE = APP / "Contents" / "MacOS" / "jarvis-hud"
HUD_DIR = config.DATA_DIR / "hud"
REQUEST = HUD_DIR / "request.json"
REPLY = HUD_DIR / "reply.json"
MAX_AGE = 120        # a question older than this (JARVIS was down) isn't answered out of the blue
STREAM_EVERY = 0.06  # seconds between streamed reply writes

SWIFT = r"""
import AppKit
import Carbon

let hudDir = CommandLine.arguments.dropFirst().first { !$0.hasPrefix("--") }
    ?? NSHomeDirectory() + "/Library/Application Support/JARVIS/hud"
let requestPath = hudDir + "/request.json"
let replyPath = hudDir + "/reply.json"
let statusPath = (hudDir as NSString).deletingLastPathComponent + "/status.json"

// The panel's look: flat glass, hard edges, hairlines, white.
let line = NSColor.white.withAlphaComponent(0.18)
let dim = NSColor.white.withAlphaComponent(0.55)
let width: CGFloat = 680, inputHeight: CGFloat = 58, statusHeight: CGFloat = 30, maxReply: CGFloat = 380

func readJSON(_ path: String) -> [String: Any] {
    guard let d = FileManager.default.contents(atPath: path),
          let o = (try? JSONSerialization.jsonObject(with: d)) as? [String: Any] else { return [:] }
    return o
}

func symbol(_ name: String, _ size: CGFloat) -> NSImage? {
    let img = NSImage(systemSymbolName: name, accessibilityDescription: nil)
    return img?.withSymbolConfiguration(.init(pointSize: size, weight: .regular))
}

final class KeyPanel: NSPanel {
    override var canBecomeKey: Bool { true }  // borderless panels can't take typing otherwise
}

final class HUD: NSObject, NSTextFieldDelegate, NSWindowDelegate {
    var window: KeyPanel!
    let field = NSTextField()
    let icon = NSImageView()
    let statusIcon = NSImageView()
    let statusText = NSTextField(labelWithString: "")
    let divider = NSView()
    let scroll = NSScrollView()
    let replyView = NSTextView()
    var seq = 0
    var waiting = false
    var sentAt = 0.0
    var lastQuestion = ""
    var replyMod: Date? = nil
    var target: [Character] = []   // the reply as it has arrived; it's typed out on screen at an even pace
    var shown = 0.0
    var typeTimer: Timer? = nil

    override init() {
        super.init()
        // Carry on past anything already asked or answered, so JARVIS never skips a new question as old.
        seq = max((readJSON(requestPath)["seq"] as? Int) ?? 0, (readJSON(replyPath)["seq"] as? Int) ?? 0)
        window = KeyPanel(contentRect: NSRect(x: 0, y: 0, width: width, height: inputHeight),
                          styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: true)
        window.level = .floating
        window.isReleasedWhenClosed = false
        window.hidesOnDeactivate = false
        window.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        window.isOpaque = false
        window.backgroundColor = .clear
        window.hasShadow = true
        window.appearance = NSAppearance(named: .darkAqua)
        window.delegate = self

        let root = NSView()
        root.wantsLayer = true
        root.layer!.borderWidth = 1
        root.layer!.borderColor = line.cgColor
        window.contentView = root
        let glass = NSVisualEffectView()
        glass.material = .hudWindow
        glass.blendingMode = .behindWindow
        glass.state = .active
        glass.autoresizingMask = [.width, .height]
        root.addSubview(glass)
        let tint = NSView()
        tint.wantsLayer = true
        tint.layer!.backgroundColor = NSColor.black.withAlphaComponent(0.35).cgColor
        tint.autoresizingMask = [.width, .height]
        root.addSubview(tint)
        glass.frame = root.bounds
        tint.frame = root.bounds

        icon.image = symbol("chevron.right", 17)
        icon.contentTintColor = .white
        field.isBordered = false
        field.drawsBackground = false
        field.focusRingType = .none
        field.font = .systemFont(ofSize: 21, weight: .light)
        field.textColor = .white
        field.placeholderAttributedString = NSAttributedString(string: "Ask JARVIS\u{2026}", attributes: [
            .font: NSFont.systemFont(ofSize: 21, weight: .light), .foregroundColor: NSColor.white.withAlphaComponent(0.4)])
        field.delegate = self
        field.cell?.isScrollable = true
        field.cell?.wraps = false
        divider.wantsLayer = true
        divider.layer!.backgroundColor = NSColor.white.withAlphaComponent(0.14).cgColor
        statusIcon.contentTintColor = .white
        statusText.font = .systemFont(ofSize: 12, weight: .medium)
        statusText.textColor = dim
        statusText.lineBreakMode = .byTruncatingTail

        replyView.isEditable = false
        replyView.isSelectable = true
        replyView.drawsBackground = false
        replyView.textContainerInset = NSSize(width: 14, height: 4)
        replyView.font = .systemFont(ofSize: 14)
        replyView.textColor = .white
        scroll.documentView = replyView
        scroll.drawsBackground = false
        scroll.hasVerticalScroller = true
        scroll.autohidesScrollers = true
        replyView.autoresizingMask = [.width]
        for v in [icon, field, divider, statusIcon, statusText, scroll] as [NSView] { root.addSubview(v) }

        Timer.scheduledTimer(withTimeInterval: 0.08, repeats: true) { _ in self.poll() }  // replies stream in
        DistributedNotificationCenter.default().addObserver(
            self, selector: #selector(showFromMenu), name: NSNotification.Name("com.jarvis.hud.show"), object: nil)
        layout(replyHeight: 0)
    }

    // ----- showing and hiding -----
    @objc func showFromMenu() { show() }

    func toggle() { window.isVisible && window.isKeyWindow ? hide() : show() }

    func show() {
        if !window.isVisible {
            let screen = (NSScreen.main ?? NSScreen.screens.first)!.visibleFrame
            let h = window.frame.height
            window.setFrameOrigin(NSPoint(x: screen.midX - width / 2, y: screen.maxY - screen.height * 0.22 - h))
        }
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
        window.makeFirstResponder(field)
        field.currentEditor()?.selectAll(nil)
    }

    func hide() { window.orderOut(nil) }

    // Clicking elsewhere puts it away, unless an answer is still coming (it stays to show it).
    func windowDidResignKey(_ notification: Notification) { if !waiting { hide() } }

    // ----- typing -----
    func control(_ control: NSControl, textView: NSTextView, doCommandBy sel: Selector) -> Bool {
        switch sel {
        case #selector(NSResponder.insertNewline(_:)): send(); return true
        case #selector(NSResponder.cancelOperation(_:)): hide(); return true
        case #selector(NSResponder.moveUp(_:)):
            if field.stringValue.isEmpty && !lastQuestion.isEmpty {
                field.stringValue = lastQuestion
                field.currentEditor()?.moveToEndOfDocument(nil)
                return true
            }
            return false
        default: return false
        }
    }

    func send() {
        let text = field.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, !waiting else { return }
        seq += 1
        let now = Date().timeIntervalSince1970
        let body: [String: Any] = ["seq": seq, "text": text, "at": now]
        try? FileManager.default.createDirectory(atPath: hudDir, withIntermediateDirectories: true)
        guard let data = try? JSONSerialization.data(withJSONObject: body),
              (try? data.write(to: URL(fileURLWithPath: requestPath), options: .atomic)) != nil else {
            setStatus("exclamationmark.triangle", "Couldn't hand that to JARVIS")
            return
        }
        lastQuestion = text
        field.stringValue = ""
        field.placeholderAttributedString = NSAttributedString(string: text, attributes: [
            .font: NSFont.systemFont(ofSize: 21, weight: .light), .foregroundColor: NSColor.white.withAlphaComponent(0.4)])
        waiting = true
        sentAt = now
        typeTimer?.invalidate()
        typeTimer = nil
        target = []
        shown = 0
        setReply("")
        setStatus("brain.head.profile", "Thinking")
    }

    // ----- what comes back -----
    func poll() {
        guard waiting || window.isVisible else { return }
        if waiting { showActivity() }
        guard let attrs = try? FileManager.default.attributesOfItem(atPath: replyPath),
              let mod = attrs[.modificationDate] as? Date, mod != replyMod else { return }
        replyMod = mod
        let r = readJSON(replyPath)
        guard (r["seq"] as? Int) == seq else { return }
        let text = r["text"] as? String ?? ""
        if !text.isEmpty { aim(text) }
        if (r["state"] as? String) == "done" {
            waiting = false
            setStatus(nil, "")
            field.placeholderAttributedString = NSAttributedString(string: "Ask JARVIS\u{2026}", attributes: [
                .font: NSFont.systemFont(ofSize: 21, weight: .light), .foregroundColor: NSColor.white.withAlphaComponent(0.4)])
            if !window.isVisible { window.orderFrontRegardless() }
        }
    }

    // The action JARVIS is on right now (status.json, the menu bar's source), else "Thinking".
    func showActivity() {
        let now = Date().timeIntervalSince1970
        let reply = readJSON(replyPath)
        if (reply["seq"] as? Int) != seq && now - sentAt > 6 {
            setStatus("exclamationmark.triangle", "No answer yet: is JARVIS's listener running? (jarvis schedule status)")
            return
        }
        if let a = readJSON(statusPath)["action"] as? [String: Any], a["state"] as? String == "acting",
           (a["until"] as? Double ?? 0) > now, let sym = a["symbol"] as? String, let label = a["detail"] as? String {
            setStatus(sym, label)
        } else {
            setStatus("brain.head.profile", "Thinking")
        }
    }

    func setStatus(_ sym: String?, _ text: String) {
        statusIcon.image = sym.flatMap { symbol($0, 12) }
        statusText.stringValue = text
        relayout()
    }

    // Like the Claude app: what has arrived is revealed a few characters a frame, faster when it falls
    // behind, so streamed chunks never land in lumps. A revised reply is retyped from where it differs.
    func aim(_ text: String) {
        let chars = Array(text)
        var same = 0
        while same < min(chars.count, target.count) && chars[same] == target[same] { same += 1 }
        shown = min(shown, Double(same))
        target = chars
        if typeTimer == nil {
            typeTimer = Timer.scheduledTimer(withTimeInterval: 1.0 / 60, repeats: true) { [weak self] _ in self?.typeStep() }
        }
    }

    func typeStep() {
        let behind = Double(target.count) - shown
        if behind <= 0 {  // shown is clamped to the length, so the last character always lands
            typeTimer?.invalidate()
            typeTimer = nil
            return
        }
        shown = min(Double(target.count), shown + max(1, max(70, behind / 0.35) / 60))
        setReply(String(target.prefix(Int(shown))))
    }

    func setReply(_ text: String) {
        replyView.string = text
        replyView.font = .systemFont(ofSize: 14)
        replyView.textColor = .white
        relayout()
    }

    func relayout() {
        var h: CGFloat = 0
        if !replyView.string.isEmpty, let lm = replyView.layoutManager, let tc = replyView.textContainer {
            tc.containerSize = NSSize(width: width - 28, height: .greatestFiniteMagnitude)
            lm.ensureLayout(for: tc)
            h = min(maxReply, ceil(lm.usedRect(for: tc).height) + 14)
        }
        layout(replyHeight: h)
        replyView.scrollToEndOfDocument(nil)
    }

    func layout(replyHeight: CGFloat) {
        let showStatus = !statusText.stringValue.isEmpty
        let total = inputHeight + (showStatus ? statusHeight : 0) + replyHeight + (replyHeight > 0 || showStatus ? 8 : 0)
        let f = window.frame
        window.setFrame(NSRect(x: f.minX, y: f.maxY - total, width: width, height: total), display: true)
        let top = total
        icon.frame = NSRect(x: 18, y: top - inputHeight + 19, width: 20, height: 20)
        field.frame = NSRect(x: 46, y: top - inputHeight + 15, width: width - 64, height: 28)
        divider.frame = NSRect(x: 0, y: top - inputHeight, width: width, height: 1)
        divider.isHidden = !(showStatus || replyHeight > 0)
        statusIcon.isHidden = !showStatus
        statusText.isHidden = !showStatus
        let sy = top - inputHeight - statusHeight
        statusIcon.frame = NSRect(x: 18, y: sy + 8, width: 16, height: 16)
        statusText.frame = NSRect(x: 42, y: sy + 8, width: width - 60, height: 16)
        let ry = 8.0
        scroll.isHidden = replyHeight == 0
        scroll.frame = NSRect(x: 4, y: ry, width: width - 8, height: replyHeight)
        replyView.frame = NSRect(x: 0, y: 0, width: width - 8, height: max(replyHeight, replyView.frame.height))
    }
}

// Only one HUD, ever: two would both answer ⌥Space and stack two bars. A second copy hands over and exits.
let lockPath = NSHomeDirectory() + "/Library/Application Support/JARVIS/.hud.lock"
try? FileManager.default.createDirectory(atPath: (lockPath as NSString).deletingLastPathComponent,
                                         withIntermediateDirectories: true)
let lockFD = open(lockPath, O_CREAT | O_RDWR, 0o644)
if lockFD < 0 || flock(lockFD, LOCK_EX | LOCK_NB) != 0 {
    if CommandLine.arguments.contains("--show") {
        DistributedNotificationCenter.default().postNotificationName(
            NSNotification.Name("com.jarvis.hud.show"), object: nil, userInfo: nil, deliverImmediately: true)
    }
    exit(0)
}

// ⌥Space from anywhere (a Carbon hot key: needs no Accessibility permission).
enum Hot { static var action: () -> Void = {} }
var hotKeyRef: EventHotKeyRef?
var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
InstallEventHandler(GetApplicationEventTarget(), { _, _, _ in
    DispatchQueue.main.async { Hot.action() }
    return noErr
}, 1, &spec, nil, nil)
RegisterEventHotKey(UInt32(kVK_Space), UInt32(optionKey), EventHotKeyID(signature: OSType(0x4A52_5653), id: 1),
                    GetApplicationEventTarget(), 0, &hotKeyRef)

let app = NSApplication.shared
app.setActivationPolicy(.accessory)  // no Dock icon
let hud = HUD()
Hot.action = { hud.toggle() }
if CommandLine.arguments.contains("--show") { hud.show() }
app.run()
"""

INFO = {
    "CFBundleIdentifier": "com.jarvis.hud",
    "CFBundleName": "JARVIS HUD",
    "CFBundleDisplayName": "JARVIS HUD",
    "CFBundleExecutable": "jarvis-hud",
    "CFBundlePackageType": "APPL",
    "CFBundleShortVersionString": "1.0",
    "CFBundleVersion": "1",
    "LSUIElement": True,
}
STAMP = hashlib.sha256((SWIFT + repr(sorted(INFO.items()))).encode()).hexdigest()[:16]


def built() -> bool:
    marker = APP / "Contents" / "Resources" / "stamp"
    return EXECUTABLE.exists() and marker.exists() and marker.read_text() == STAMP


def build() -> bool:
    """Compile and ad-hoc sign the HUD app if missing or outdated. Its own app: building it never
    touches JARVIS Senses or JARVIS Ears (their permissions)."""
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
            raise RuntimeError(f"couldn't compile the HUD app: {r.stderr[-500:]}")
    subprocess.run(["codesign", "--force", "--sign", "-", "--identifier", INFO["CFBundleIdentifier"], str(APP)],
                   check=True, capture_output=True)
    (APP / "Contents" / "Resources" / "stamp").write_text(STAMP)
    return True


# ----- the JARVIS side -----
def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_reply(seq: int, text: str, state: str) -> None:
    HUD_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=HUD_DIR, prefix=".reply-")
    with os.fdopen(fd, "w") as f:
        json.dump({"seq": seq, "text": text, "state": state, "at": time.time()}, f)
    os.replace(tmp, REPLY)  # atomic: the HUD never reads half a reply


def pending(handled: int, now: float | None = None) -> dict | None:
    """The newest question not answered yet, if it's recent and has words in it."""
    req = _read(REQUEST)
    seq, text = req.get("seq"), str(req.get("text") or "").strip()
    if not isinstance(seq, int) or seq <= handled or not text:
        return None
    if (now or time.time()) - float(req.get("at") or 0) > MAX_AGE:
        return None
    return {"seq": seq, "text": text[:4000]}


def answer_one(req: dict, answer: Callable[[str, Callable[[str], None]], str], clock=time.time) -> str:
    """Answer one question, streaming the reply into reply.json as it's written."""
    seq = req["seq"]
    write_reply(seq, "", "thinking")
    last = [0.0]

    def on_text(so_far: str) -> None:
        if so_far and clock() - last[0] >= STREAM_EVERY:
            last[0] = clock()
            write_reply(seq, so_far, "thinking")
    try:
        reply = answer(req["text"], on_text)
    except Exception as e:  # never leave the HUD waiting
        reply = f"Sorry, sir, I couldn't answer that just now ({str(e)[:150]})."
    write_reply(seq, reply, "done")
    return reply


def serve(answer: Callable[[str, Callable[[str], None]], str], log=print, stop: threading.Event | None = None,
          every: float = 0.25) -> None:
    """Answer questions typed into the HUD until `stop` is set. `answer(text, on_text)` returns the reply."""
    handled = _read(REPLY).get("seq") or 0
    if not isinstance(handled, int):
        handled = 0
    stop = stop or threading.Event()
    while not stop.is_set():
        try:
            req = pending(handled)
            if req:
                handled = req["seq"]
                answer_one(req, answer)
        except Exception as e:  # a bad file mustn't kill the listener
            log(f"hud: {e}")
        stop.wait(every)
