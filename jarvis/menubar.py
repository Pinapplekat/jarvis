"""'JARVIS Status.app': a tiny native menu bar indicator showing whether JARVIS is waiting for
"Hey Jarvis", listening, speaking, or off, and while it works, what it's doing (a white icon per
action: a camera for a screenshot, a pin for location...; see status.ACTIONS). It only reads the
status file that jarvis/status.py writes; it has no other access. Its menu can open the typing HUD
(jarvis/hud.py) and put the helpers on the panel. Rebuilt only when its source changes."""

import hashlib
import plistlib
import subprocess
import tempfile
from pathlib import Path

APP = Path.home() / "Applications" / "JARVIS Status.app"
EXECUTABLE = APP / "Contents" / "MacOS" / "jarvis-status"

SWIFT = r"""
import AppKit

let statusPath = CommandLine.arguments.count > 1 ? CommandLine.arguments[1]
    : NSHomeDirectory() + "/Library/Application Support/JARVIS/status.json"
let logsPath = NSHomeDirectory() + "/Library/Logs/JARVIS"

// Every icon is white; only its shape says what's happening. Off is dimmed.
struct Look { let symbol: String; let fallback: String; let label: String; let alpha: CGFloat }
let looks: [String: Look] = [
    "speaking":  Look(symbol: "speaker.wave.2.fill", fallback: "🔊", label: "Speaking", alpha: 1),
    "listening": Look(symbol: "waveform", fallback: "🎙", label: "Listening to you", alpha: 1),
    "acting":    Look(symbol: "bolt.fill", fallback: "⚙️", label: "Working on it", alpha: 1),
    "working":   Look(symbol: "brain.head.profile", fallback: "⚙️", label: "Thinking", alpha: 1),
    "waiting":   Look(symbol: "ear", fallback: "👂", label: "Waiting for \u{201C}Hey Jarvis\u{201D}", alpha: 1),
    "off":       Look(symbol: "ear.trianglebadge.exclamationmark", fallback: "💤", label: "Desk listener is off", alpha: 0.55),
]
// What JARVIS is doing (a screenshot, a page...) shows over plain "thinking", but never over his voice.
let rank = ["speaking": 4, "listening": 3, "acting": 2, "working": 1]
let sourceNames = ["ears": "at the desk", "telegram": "in Telegram", "voice": "out loud", "hud": "typed on the Mac"]

final class Indicator: NSObject {
    let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    let stateLine = NSMenuItem(title: "", action: nil, keyEquivalent: "")
    let detailLine = NSMenuItem(title: "", action: nil, keyEquivalent: "")
    var shown = ""

    override init() {
        super.init()
        let menu = NSMenu()
        let type = NSMenuItem(title: "Type to JARVIS\u{2026}", action: #selector(openHUD), keyEquivalent: " ")
        type.keyEquivalentModifierMask = [.option]
        type.target = self
        menu.addItem(type)
        let helpers = NSMenuItem(title: "Show Helpers", action: #selector(showHelpers), keyEquivalent: "h")
        helpers.target = self
        menu.addItem(helpers)
        menu.addItem(.separator())
        stateLine.isEnabled = false
        detailLine.isEnabled = false
        menu.addItem(stateLine)
        menu.addItem(detailLine)
        menu.addItem(.separator())
        let logs = NSMenuItem(title: "Open JARVIS Logs", action: #selector(openLogs), keyEquivalent: "l")
        logs.target = self
        menu.addItem(logs)
        let quit = NSMenuItem(title: "Hide Indicator", action: #selector(quit), keyEquivalent: "q")
        quit.target = self
        menu.addItem(quit)
        item.menu = menu
        refresh()
        Timer.scheduledTimer(withTimeInterval: 0.3, repeats: true) { _ in self.refresh() }
    }

    func refresh() {
        let now = Date().timeIntervalSince1970
        var entries: [String: [String: Any]] = [:]
        if let data = FileManager.default.contents(atPath: statusPath),
           let obj = try? JSONSerialization.jsonObject(with: data) as? [String: [String: Any]] {
            entries = obj
        }
        func live(_ e: [String: Any]) -> Bool {
            if let until = e["until"] as? Double { return until > now }
            return true
        }
        var best: (state: String, source: String, since: Double, detail: String, symbol: String?)? = nil
        for (source, e) in entries where live(e) {
            guard let state = e["state"] as? String, let r = rank[state] else { continue }
            if best == nil || r > rank[best!.state]! {
                best = (state, source, e["since"] as? Double ?? now, e["detail"] as? String ?? "", e["symbol"] as? String)
            }
        }
        var state = "off", detail = "Start it with: jarvis schedule install", since = now
        var symbol: String? = nil, label: String? = nil
        if let b = best {
            state = b.state
            since = b.since
            if state == "acting" {  // the action itself: its own icon and words
                symbol = b.symbol
                label = b.detail
                detail = "Working on it"
            } else {
                detail = ([b.detail, sourceNames[b.source] ?? b.source].filter { !$0.isEmpty }).joined(separator: " · ")
            }
        } else if let ears = entries["ears"], live(ears) {
            state = "waiting"
            detail = "Telegram and alerts are idle"
        }
        let look = looks[state]!
        let title = label.flatMap { $0.isEmpty ? nil : $0 } ?? look.label
        let key = state + (symbol ?? "") + title + detail
        if key != shown {
            shown = key
            let image = symbol.flatMap { NSImage(systemSymbolName: $0, accessibilityDescription: "JARVIS: " + title) }
                ?? NSImage(systemSymbolName: look.symbol, accessibilityDescription: "JARVIS: " + title)
            if let image = image {
                item.button?.image = white(image, alpha: look.alpha)
                item.button?.title = ""
            } else {
                item.button?.image = nil
                item.button?.title = look.fallback
            }
            item.button?.toolTip = "JARVIS: " + title
            stateLine.title = "JARVIS: " + title
        }
        let f = DateFormatter()
        f.timeStyle = .short
        detailLine.title = detail + " (since " + f.string(from: Date(timeIntervalSince1970: since)) + ")"
    }

    // White, always: the color is drawn into the image (not a template, which macOS would paint black on a
    // light menu bar and ignore any tint).
    func white(_ symbol: NSImage, alpha: CGFloat) -> NSImage {
        let src = symbol.withSymbolConfiguration(.init(pointSize: 14, weight: .medium)) ?? symbol
        let out = NSImage(size: src.size, flipped: false) { rect in
            src.draw(in: rect)
            NSColor.white.withAlphaComponent(alpha).set()
            rect.fill(using: .sourceAtop)
            return true
        }
        out.isTemplate = false
        out.accessibilityDescription = symbol.accessibilityDescription
        return out
    }

    @objc func showHelpers() {
        // The menu bar can't draw the panel itself: it asks `jarvis listen`, which puts the helpers up, kept live.
        let dir = (statusPath as NSString).deletingLastPathComponent + "/panel"
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        let body: [String: Any] = ["kind": "helpers", "at": Date().timeIntervalSince1970]
        if let d = try? JSONSerialization.data(withJSONObject: body) {
            try? d.write(to: URL(fileURLWithPath: dir + "/request.json"), options: .atomic)
        }
    }
    @objc func openLogs() { NSWorkspace.shared.open(URL(fileURLWithPath: logsPath)) }
    @objc func openHUD() {
        // The HUD is its own app (it may be closed): start it if needed, then ask it to show itself.
        let hud = NSHomeDirectory() + "/Applications/JARVIS HUD.app"
        if !NSRunningApplication.runningApplications(withBundleIdentifier: "com.jarvis.hud").isEmpty {
            DistributedNotificationCenter.default().postNotificationName(
                NSNotification.Name("com.jarvis.hud.show"), object: nil, userInfo: nil, deliverImmediately: true)
        } else if FileManager.default.fileExists(atPath: hud) {
            let config = NSWorkspace.OpenConfiguration()
            config.arguments = ["--show"]
            NSWorkspace.shared.openApplication(at: URL(fileURLWithPath: hud), configuration: config)
        }
    }
    @objc func quit() { NSApp.terminate(nil) }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)  // menu bar only, no Dock icon
let indicator = Indicator()
app.run()
"""

INFO = {
    "CFBundleIdentifier": "com.jarvis.status",
    "CFBundleName": "JARVIS Status",
    "CFBundleDisplayName": "JARVIS Status",
    "CFBundleExecutable": "jarvis-status",
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
            raise RuntimeError(f"couldn't compile the menu bar app: {r.stderr[-500:]}")
    subprocess.run(["codesign", "--force", "--sign", "-", "--identifier", INFO["CFBundleIdentifier"], str(APP)],
                   check=True, capture_output=True)
    (APP / "Contents" / "Resources" / "stamp").write_text(STAMP)
    return True
