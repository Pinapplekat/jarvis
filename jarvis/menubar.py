"""'JARVIS Status.app': a tiny native menu bar indicator showing whether JARVIS is waiting for
"Hey Jarvis", listening, working, speaking, or off. It only reads the status file that
jarvis/status.py writes; it has no other access. Rebuilt only when its source changes."""

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

struct Look { let symbol: String; let fallback: String; let label: String; let tint: NSColor? }
let looks: [String: Look] = [
    "speaking":  Look(symbol: "speaker.wave.2.fill", fallback: "🔊", label: "Speaking", tint: .systemBlue),
    "listening": Look(symbol: "waveform", fallback: "🎙", label: "Listening to you", tint: .systemRed),
    "working":   Look(symbol: "ellipsis.circle.fill", fallback: "⚙️", label: "Working on it", tint: .systemOrange),
    "waiting":   Look(symbol: "ear", fallback: "👂", label: "Waiting for \u{201C}Hey Jarvis\u{201D}", tint: nil),
    "off":       Look(symbol: "ear.trianglebadge.exclamationmark", fallback: "💤", label: "Desk listener is off", tint: .secondaryLabelColor),
]
let rank = ["speaking": 4, "listening": 3, "working": 2]
let sourceNames = ["ears": "at the desk", "telegram": "in Telegram", "voice": "out loud"]

final class Indicator: NSObject {
    let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    let stateLine = NSMenuItem(title: "", action: nil, keyEquivalent: "")
    let detailLine = NSMenuItem(title: "", action: nil, keyEquivalent: "")
    var shown = ""

    override init() {
        super.init()
        let menu = NSMenu()
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
        var best: (state: String, source: String, since: Double, detail: String)? = nil
        for (source, e) in entries where live(e) {
            guard let state = e["state"] as? String, let r = rank[state] else { continue }
            if best == nil || r > rank[best!.state]! {
                best = (state, source, e["since"] as? Double ?? now, e["detail"] as? String ?? "")
            }
        }
        var state = "off", detail = "Start it with: jarvis schedule install", since = now
        if let b = best {
            state = b.state
            since = b.since
            detail = ([b.detail, sourceNames[b.source] ?? b.source].filter { !$0.isEmpty }).joined(separator: " · ")
        } else if let ears = entries["ears"], live(ears) {
            state = "waiting"
            detail = "Telegram and alerts are idle"
        }
        let look = looks[state]!
        let key = state + detail
        if key != shown {
            shown = key
            if let image = NSImage(systemSymbolName: look.symbol, accessibilityDescription: "JARVIS: " + look.label) {
                image.isTemplate = true
                item.button?.image = image
                item.button?.title = ""
            } else {
                item.button?.image = nil
                item.button?.title = look.fallback
            }
            item.button?.contentTintColor = look.tint
            item.button?.toolTip = "JARVIS: " + look.label
            stateLine.title = "JARVIS: " + look.label
        }
        let f = DateFormatter()
        f.timeStyle = .short
        detailLine.title = detail + " (since " + f.string(from: Date(timeIntervalSince1970: since)) + ")"
    }

    @objc func openLogs() { NSWorkspace.shared.open(URL(fileURLWithPath: logsPath)) }
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
