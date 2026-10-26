"""JARVIS's own screen: a small floating panel in the top-right corner for "show me...".

'JARVIS Panel.app' is a separate, tiny native app (a WKWebView in a floating window) run by its
own background job. It needs no permissions and has no access to anything: it only reads
panel/state.json, which this module writes, shows what's in it, and writes panel/ack.json so
JARVIS knows it really appeared. It's hidden until something is shown; "close that" hides it.

What can be shown: a plot of a formula ("sin(x) shifted up 2"), an image from JARVIS's own
screenshot folder ("the screenshot from earlier"), a card (a title and a few lines), HTML that
JARVIS wrote (charts; no network: a content policy blocks it), or a web page.
"""

import ast
import base64
import hashlib
import html as htmllib
import json
import math
import plistlib
import re
import subprocess
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from jarvis import config
from jarvis.memory import Memory

APP = Path.home() / "Applications" / "JARVIS Panel.app"
EXECUTABLE = APP / "Contents" / "MacOS" / "jarvis-panel"
PANEL_DIR = config.DATA_DIR / "panel"
STATE = PANEL_DIR / "state.json"
ACK = PANEL_DIR / "ack.json"
HISTORY_KEY = "panel_history"
SIZES = [(420, 300), (560, 400), (760, 540), (980, 700)]
DEFAULT_SIZE = 1
ACK_WAIT = 3.0

SWIFT = r"""
import AppKit
import WebKit

let statePath = CommandLine.arguments.count > 1 ? CommandLine.arguments[1]
    : NSHomeDirectory() + "/Library/Application Support/JARVIS/panel/state.json"
let ackPath = (statePath as NSString).deletingLastPathComponent + "/ack.json"

// Flat glass, hard edges, hairlines. One accent, amber, and only for what needs him soon.
let line = NSColor.white.withAlphaComponent(0.18)
let amber = NSColor(srgbRed: 1.0, green: 0.69, blue: 0.0, alpha: 1)

func ago(_ t: Double) -> String {
    let m = max(0, Int(Date().timeIntervalSince1970 - t)) / 60
    return m < 60 ? "\(m) min ago" : m < 1440 ? "\(m / 60) hr ago" : "\(m / 1440) days ago"
}

// One line: the title. Under it, only once the content is over 15 minutes old, how long ago it was
// synced (amber past an hour). A close mark while the pointer is over the bar; dragging it moves the window.
final class Bar: NSView {
    let title = NSTextField(labelWithString: "")
    let status = NSTextField(labelWithString: "")
    let close = NSButton(title: "", target: nil, action: nil)
    var onClose: () -> Void = {}
    var onHeight: () -> Void = {}
    var syncedAt: Double? = nil
    var stale: Bool { syncedAt.map { Date().timeIntervalSince1970 - $0 > 900 } ?? false }

    override init(frame: NSRect) {
        super.init(frame: frame)
        title.lineBreakMode = .byTruncatingTail
        status.lineBreakMode = .byTruncatingTail
        close.isBordered = false
        close.attributedTitle = NSAttributedString(string: "×", attributes: [
            .font: NSFont.systemFont(ofSize: 17, weight: .light), .foregroundColor: NSColor.white.withAlphaComponent(0.7)])
        close.target = self
        close.action = #selector(closeClicked)
        close.alphaValue = 0
        for v in [title, status, close] as [NSView] { addSubview(v) }
        addTrackingArea(NSTrackingArea(rect: .zero, options: [.mouseEnteredAndExited, .activeAlways, .inVisibleRect],
                                       owner: self, userInfo: nil))
        Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { [weak self] _ in self?.tick() }
    }
    required init?(coder: NSCoder) { fatalError() }

    var height: CGFloat { stale ? 48 : 34 }

    override func layout() {
        super.layout()
        let h = bounds.height
        title.frame = NSRect(x: 14, y: stale ? h - 25 : (h - 17) / 2, width: bounds.width - 52, height: 17)
        status.frame = NSRect(x: 14, y: 8, width: bounds.width - 52, height: 14)
        status.isHidden = !stale
        close.frame = NSRect(x: bounds.width - 34, y: (h - 24) / 2, width: 24, height: 24)
    }

    func set(_ text: String, syncedAt: Double?) {
        title.attributedStringValue = NSAttributedString(string: text, attributes: [
            .font: NSFont.systemFont(ofSize: 13, weight: .semibold), .foregroundColor: NSColor.white])
        self.syncedAt = syncedAt
        tick()
    }

    func tick() {
        let was = status.isHidden
        if let at = syncedAt {
            let old = Date().timeIntervalSince1970 - at > 3600
            status.attributedStringValue = NSAttributedString(string: "Synced \(ago(at))", attributes: [
                .font: NSFont.systemFont(ofSize: 11), .foregroundColor: old ? amber : NSColor.white.withAlphaComponent(0.5)])
        }
        if was == stale { onHeight() }  // crossed the 15-minute mark: the bar grows a line
        needsLayout = true
    }

    override func mouseEntered(with event: NSEvent) { close.animator().alphaValue = 1 }
    override func mouseExited(with event: NSEvent) { close.animator().alphaValue = 0 }
    override func mouseDown(with event: NSEvent) { window?.performDrag(with: event) }
    @objc func closeClicked() { onClose() }

    override func draw(_ dirtyRect: NSRect) {
        NSColor.white.withAlphaComponent(0.14).setFill()
        NSRect(x: 0, y: 0, width: bounds.width, height: 1).fill()
    }
}

final class Panel: NSObject, NSWindowDelegate, WKNavigationDelegate, WKScriptMessageHandler {
    var window: NSPanel!
    var root: NSView!
    var web: WKWebView!
    var bar: Bar!
    var seq = -1
    var lastMod: Date? = nil
    var fit = false

    override init() {
        super.init()
        let style: NSWindow.StyleMask = [.borderless, .resizable, .nonactivatingPanel]
        let frame = NSRect(x: 0, y: 0, width: 560, height: 400)
        window = NSPanel(contentRect: frame, styleMask: style, backing: .buffered, defer: true)
        window.title = "JARVIS"
        window.level = .floating
        window.isReleasedWhenClosed = false
        window.hidesOnDeactivate = false
        window.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        window.isOpaque = false
        window.backgroundColor = .clear
        window.hasShadow = true
        window.appearance = NSAppearance(named: .darkAqua)
        window.delegate = self

        root = NSView(frame: frame)
        root.wantsLayer = true
        root.layer!.cornerRadius = 0  // a screen, not a card
        root.layer!.masksToBounds = true
        root.layer!.borderWidth = 1
        root.layer!.borderColor = line.cgColor
        window.contentView = root
        let glass = NSVisualEffectView(frame: frame)  // translucent: what's behind shows through, blurred
        glass.material = .hudWindow
        glass.blendingMode = .behindWindow
        glass.state = .active
        glass.autoresizingMask = [.width, .height]
        root.addSubview(glass)
        let tint = NSView(frame: frame)  // darkens what's behind, without coloring it
        tint.wantsLayer = true
        tint.layer!.backgroundColor = NSColor.black.withAlphaComponent(0.35).cgColor
        tint.autoresizingMask = [.width, .height]
        root.addSubview(tint)

        bar = Bar(frame: .zero)
        bar.onClose = { [unowned self] in self.window.orderOut(nil); self.ack(self.seq, "closed") }
        bar.onHeight = { [unowned self] in if self.fit { self.fitToContent() } else { self.layout() } }
        root.addSubview(bar)
        let config = WKWebViewConfiguration()
        config.userContentController.add(self, name: "fit")  // a page whose height changed (a helpers tab)
        web = WKWebView(frame: .zero, configuration: config)
        web.setValue(false, forKey: "drawsBackground")  // let the glass show through the page
        web.navigationDelegate = self
        root.addSubview(web)
        layout()
        Timer.scheduledTimer(withTimeInterval: 0.25, repeats: true) { _ in self.poll() }
    }

    func layout() {
        let b = root.bounds, h = bar.height
        bar.frame = NSRect(x: 0, y: b.height - h, width: b.width, height: h)
        web.frame = NSRect(x: 0, y: 0, width: b.width, height: b.height - h)
    }
    func windowDidResize(_ notification: Notification) { layout() }

    func poll() {
        guard let attrs = try? FileManager.default.attributesOfItem(atPath: statePath),
              let mod = attrs[.modificationDate] as? Date, mod != lastMod else { return }
        lastMod = mod
        guard let data = FileManager.default.contents(atPath: statePath),
              let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { return }
        let newSeq = obj["seq"] as? Int ?? 0
        if !(obj["visible"] as? Bool ?? false) {
            if window.isVisible { window.orderOut(nil) }
            web.loadHTMLString("", baseURL: nil)  // let the page's memory go
            seq = newSeq
            ack(newSeq, "hidden")
            return
        }
        if newSeq == seq { return }
        // A live view's update (the helpers): swap the page's contents in place, no reload, no flash.
        if obj["refresh"] as? Bool ?? false, window.isVisible, fit, let body = obj["body"] as? String,
           let enc = try? JSONSerialization.data(withJSONObject: [body]), let arr = String(data: enc, encoding: .utf8) {
            seq = newSeq
            window.title = obj["title"] as? String ?? "JARVIS"
            bar.set(window.title, syncedAt: nil)
            web.evaluateJavaScript("document.body.innerHTML = \(arr)[0]; window.applyFilter && applyFilter()") {
                [weak self] _, _ in self?.fitToContent() }
            ack(newSeq, "shown")
            return
        }
        seq = newSeq
        window.title = obj["title"] as? String ?? "JARVIS"
        bar.set(window.title, syncedAt: (obj["source"] as? [String: Any])?["at"] as? Double)
        place(obj["width"] as? Double ?? 560, obj["height"] as? Double ?? 400)
        if let s = obj["url"] as? String, let u = URL(string: s), u.scheme == "https" || u.scheme == "http" {
            fit = false
            web.load(URLRequest(url: u))
        } else {
            fit = true
            web.loadHTMLString(obj["html"] as? String ?? "", baseURL: nil)
        }
        window.orderFrontRegardless()
        ack(newSeq, "shown")
    }

    func place(_ w: Double, _ h: Double) {
        guard let screen = (NSScreen.main ?? NSScreen.screens.first)?.visibleFrame else { return }
        window.setFrame(NSRect(x: screen.maxX - w - 16, y: screen.maxY - h - 16, width: w, height: h), display: true)
        layout()
    }

    // Our own pages size the window to what they hold (up to most of the screen's height); the top stays put.
    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        if fit { fitToContent() }
    }

    func userContentController(_ c: WKUserContentController, didReceive message: WKScriptMessage) {
        if fit && window.isVisible { fitToContent() }
    }

    func fitToContent() {
        web.evaluateJavaScript("Math.ceil(document.body.getBoundingClientRect().height)") { [weak self] r, _ in
            guard let self = self, let content = r as? Double else { return }
            let f = self.window.frame
            let room = (self.window.screen ?? NSScreen.main)?.visibleFrame.height ?? 800
            let h = min(room * 0.75, max(80, CGFloat(content) + self.bar.height))
            if abs(h - f.height) < 1 { self.layout(); return }
            self.window.setFrame(NSRect(x: f.minX, y: f.maxY - h, width: f.width, height: h), display: true)
            self.layout()
        }
    }

    func ack(_ s: Int, _ what: String) {
        let d = try? JSONSerialization.data(withJSONObject: ["seq": s, "state": what,
                                                             "at": Date().timeIntervalSince1970])
        FileManager.default.createFile(atPath: ackPath, contents: d)
    }

    func windowWillClose(_ notification: Notification) { ack(seq, "closed") }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)  // no Dock icon, never steals focus
let panel = Panel()
app.run()
"""

INFO = {
    "CFBundleIdentifier": "com.jarvis.panel",
    "CFBundleName": "JARVIS Panel",
    "CFBundleDisplayName": "JARVIS Panel",
    "CFBundleExecutable": "jarvis-panel",
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
    """Compile and ad-hoc sign the panel app if missing or outdated. Its own app: building it never
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
            raise RuntimeError(f"couldn't compile the panel app: {r.stderr[-500:]}")
    subprocess.run(["codesign", "--force", "--sign", "-", "--identifier", INFO["CFBundleIdentifier"], str(APP)],
                   check=True, capture_output=True)
    (APP / "Contents" / "Resources" / "stamp").write_text(STAMP)
    return True


# ----- what's on it -----
def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def current() -> dict:
    """The panel's state as last written (empty if never used)."""
    return _read(STATE)


def visible() -> bool:
    st, ack = current(), _read(ACK)
    if not st.get("visible"):
        return False
    return not (ack.get("seq") == st.get("seq") and ack.get("state") == "closed")  # he clicked its close box


def describe() -> str:
    """One line for the right-now block."""
    st = current()
    if not visible():
        return ""
    return f"On your panel (top-right of his screen): {st.get('title') or 'something'}"


def _write(state: dict) -> int:
    PANEL_DIR.mkdir(parents=True, exist_ok=True)
    state = {**state, "seq": int(current().get("seq", 0)) + 1}
    fd, tmp = tempfile.mkstemp(dir=PANEL_DIR, prefix=".state-")
    with open(fd, "w") as f:
        json.dump(state, f)
    Path(tmp).replace(STATE)
    return state["seq"]


def _wait_ack(seq: int, want: str, timeout: float = ACK_WAIT, sleep=time.sleep) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        ack = _read(ACK)
        if ack.get("seq") == seq and ack.get("state") == want:
            return True
        sleep(0.05)
    return False


def _history(mem: Memory) -> list[dict]:
    try:
        return json.loads(mem.get_kv(HISTORY_KEY) or "[]")
    except ValueError:
        return []


def _present(mem: Memory, content: dict, wait=_wait_ack, source: dict | None = None) -> str:
    """Put content on the panel. `source` (the show action that made it) goes into the history, so
    "go back" can rebuild it without keeping big pages in the database."""
    size = int(content.get("size", 2 if content.get("live") == "helpers" else DEFAULT_SIZE))
    w, h = SIZES[max(0, min(size, len(SIZES) - 1))]
    seq = _write({**content, "visible": True, "width": w, "height": h, "size": size})
    if source is not None:
        mem.set_kv(HISTORY_KEY, json.dumps((_history(mem) + [source])[-5:]))
    if not wait(seq, "shown"):
        raise ValueError("the panel app didn't respond (is the panel job running? `jarvis schedule status`)")
    mem.log("panel", f"showing: {content.get('title', '')[:100]}")
    return f"On your screen, sir: {content.get('title') or 'there it is'}."


def close(mem: Memory, wait=_wait_ack) -> str:
    if not visible():
        return "Nothing's showing, sir."
    seq = _write({**current(), "visible": False})
    wait(seq, "hidden", 1.5)
    mem.log("panel", "closed")
    return "Closed, sir."


def resize(mem: Memory, step: int, wait=_wait_ack) -> str:
    st = current()
    if not visible():
        raise ValueError("nothing is showing on the panel")
    size = max(0, min(int(st.get("size", DEFAULT_SIZE)) + step, len(SIZES) - 1))
    if size == st.get("size", DEFAULT_SIZE):
        return "That's as " + ("big" if step > 0 else "small") + " as it goes, sir."
    _present(mem, {k: st[k] for k in ("title", "html", "url", "source", "body", "live", "filter") if k in st} | {"size": size}, wait)
    return "Bigger, sir." if step > 0 else "Smaller, sir."


def back(mem: Memory, wait=_wait_ack) -> str:
    hist = _history(mem)
    if len(hist) < 2:
        raise ValueError("there's nothing earlier to go back to")
    hist.pop()
    mem.set_kv(HISTORY_KEY, json.dumps(hist))
    return _present(mem, render(mem, hist[-1]), wait)


# ----- rendering -----
CSP = ("<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; img-src data:; "
       "style-src 'unsafe-inline'; script-src 'unsafe-inline'; font-src data:\">")
STYLE = ("<style>:root{color-scheme:dark;--amber:#ffb000;--line:rgba(255,255,255,.1)}"
         "html,body{margin:0;background:transparent;color:#fff;font:13px -apple-system,sans-serif}"
         "body{display:flow-root}main{padding:0 0 8px}"
         "ul{list-style:none;padding:0;margin:0}li{padding:5px 14px;border-top:1px solid var(--line)}"
         "li:first-child{border-top:0}"
         "h2{margin:0;padding:10px 14px 3px;font-size:12px;font-weight:600;color:rgba(255,255,255,.55)}"
         "h2.today{color:var(--amber)}"
         "li.row{display:flex;align-items:baseline;gap:8px;border:0;padding:4px 14px}"
         ".title{flex:1;min-width:0}"
         ".at{font-size:11px;font-variant-numeric:tabular-nums;color:rgba(255,255,255,.6);white-space:nowrap}"
         ".course{font-size:11px;color:rgba(255,255,255,.5);white-space:nowrap}"
         ".dot{display:inline-block;width:6px;height:6px;border-radius:50%;margin-right:5px;vertical-align:1px}"
         ".done .title{text-decoration:line-through;color:rgba(255,255,255,.35)}.done .at{visibility:hidden}"
         ".note{margin:8px 14px 0;color:rgba(255,255,255,.6);font-style:italic}"
         ".empty{padding:10px 14px;color:rgba(255,255,255,.6)}"
         "img{max-width:100%;max-height:100vh;display:block;margin:auto}svg{width:100%;height:auto;display:block}"
         "</style>")


def page(body: str, title: str = "") -> str:
    """A self-contained page: no network (the content policy blocks it), JARVIS's dark style."""
    return (f"<!doctype html><html><head><meta charset=\"utf-8\">{CSP}{STYLE}<title>{htmllib.escape(title)}</title>"
            f"</head><body>{body}</body></html>")


def card(title: str, lines: list[str]) -> dict:
    items = "".join(f"<li>{htmllib.escape(str(x))}</li>" for x in lines[:20])
    return {"title": title[:80], "html": page(f"<main><ul>{items}</ul></main>", title)}  # the bar shows the title


COURSE_COLORS = ["#7fb2ff", "#86d6a0", "#f0a0c8", "#c3a8ff", "#8fd8d8"]


def _clock(due: datetime) -> str:
    """Due at the end of the day is the usual case and goes unsaid; anything else gets its time."""
    return "" if (due.hour, due.minute) == (23, 59) else f"{due:%-I:%M %p}"


def deadlines(mem: Memory, now: datetime | None = None) -> dict:
    """What's due this week (through Sunday; from Saturday on, through next Sunday), grouped by day, with
    what's already done struck through, plus work Canvas marks missing. Read from what the background sync
    already saved (Canvas's feed and /weekly's list), so it's instant."""
    from jarvis.canvas import COURSE_CODE, course_name, from_feed, from_memory, merge, spoken_course
    tz = ZoneInfo(config.TIMEZONE)
    now = now or datetime.now(tz)
    feed = config.DATA_DIR / "canvas_feed.ics"
    canvas_items, typed_items = from_feed(feed.read_bytes(), tz) if feed.exists() else [], from_memory(mem)
    items = merge(canvas_items, typed_items)
    if feed.exists():
        source = {"label": "Canvas", "at": feed.stat().st_mtime}
    else:
        stamps = [datetime.fromisoformat(r["updated_at"]).timestamp() for r in mem.deadlines()]
        source = {"label": "Weekly plan", "at": max(stamps, default=now.timestamp())}
    # Say classes the way he does: what he typed in /weekly for that class, else its subject.
    codes = {d.course for d in items if COURSE_CODE.fullmatch(d.course)}
    typed = {course_name(d.course, codes): d.course for d in typed_items}
    names = sorted({spoken_course(d.course, typed) for d in items if d.kind == "due"})
    color = {n: COURSE_COLORS[i % len(COURSE_COLORS)] for i, n in enumerate(names)}

    today = datetime.combine(now.date(), datetime.min.time(), tz)
    span = 6 - now.weekday() + (7 if now.weekday() >= 5 else 0)  # days after today the list runs to
    end = today + timedelta(days=span + 1)
    monday = today - timedelta(days=now.weekday())
    # Past due: only what Canvas says is missing, or what's marked done. The feed can't see submissions,
    # so anything else that has passed is most likely handed in, and calling it late would be a guess.
    items = [d for d in items if d.kind == "due" and d.due < end
             and (d.due >= now or d.status == "missing" or (d.done and d.due >= monday))]
    groups: dict[str, list] = {}
    for d in items:
        n = (d.due.astimezone(tz).date() - now.date()).days
        day = d.due.astimezone(tz).date()
        name = ("Missing" if not d.done else "Done earlier this week") if n < 0 else (
            "Today" if n == 0 else "Tomorrow" if n == 1 else f"Next {day:%A}" if n >= 7 else f"{day:%A}")
        groups.setdefault(name, []).append(d)
    order = sorted(groups, key=lambda k: (k == "Done earlier this week", k != "Missing", min(d.due for d in groups[k])))
    parts = []
    for name in order:
        rows = "".join(
            f'<li class="row{" done" if d.done else ""}"><span class="title">{htmllib.escape(d.title)}</span>'
            f'<span class="at">{_clock(d.due.astimezone(tz))}</span><span class="course">'
            f'<i class="dot" style="background:{color[spoken_course(d.course, typed)]}"></i>'
            f'{htmllib.escape(spoken_course(d.course, typed))}</span></li>'
            for d in sorted(groups[name], key=lambda d: (d.done, d.due)))
        today_class = ' class="today"' if name == "Today" and any(not d.done for d in groups[name]) else ""
        parts.append(f"<h2{today_class}>{name}</h2><ul>{rows}</ul>")
    open_by_day = {k: sum(1 for d in v if not d.done) for k, v in groups.items() if k not in ("Missing", "Done earlier this week")}
    heavy = sorted(open_by_day.items(), key=lambda kv: -kv[1])
    if heavy and heavy[0][1] >= 3 and (len(heavy) == 1 or heavy[1][1] < heavy[0][1]):
        parts.append(f'<p class="note">{heavy[0][0]}\'s the heavy one, sir.</p>')
    left = sum(1 for d in items if not d.done)
    when = "by next Sunday" if span > 6 else "this week"
    title = f"{left} due {when}" if left else f"Nothing left {when}"
    body = f"<main>{''.join(parts)}</main>" if parts else '<main><p class="empty">Nothing due, sir.</p></main>'
    return {"title": title, "html": page(body, title), "source": source}


HELPER_STYLE = ("<style>.helper{padding:9px 14px 10px;border-top:1px solid var(--line)}.helper:first-child{border-top:0}"
                ".helper .head{display:flex;align-items:baseline;gap:8px}"
                ".helper .name{flex:1;min-width:0;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}"
                ".helper .state{font-size:11px;color:rgba(255,255,255,.55);white-space:nowrap}"
                ".helper .where{font-size:11px;color:rgba(255,255,255,.5);margin:1px 0 4px 14px}"
                ".helper .line{margin:2px 0 0 14px;color:rgba(255,255,255,.85);overflow-wrap:anywhere}"
                ".helper .k{color:rgba(255,255,255,.45);margin-right:5px}"
                ".helper ol{margin:3px 0 0 14px;padding:0;list-style:none;font-size:12px}"
                ".helper ol li{padding:1px 0;border:0;color:rgba(255,255,255,.6)}"
                ".helper ol li.completed{color:rgba(255,255,255,.35);text-decoration:line-through}"
                ".helper ol li.in_progress{color:#fff}.helper ol li.in_progress::before{content:'\\25B8  '}"
                ".helper ol li.pending::before,.helper ol li.completed::before{content:'\\00B7  '}"
                ".helper .steps{font-size:12px;color:rgba(255,255,255,.55);display:-webkit-box;-webkit-line-clamp:2;"
                "-webkit-box-orient:vertical;overflow:hidden}"
                ".helper.quiet .name{color:rgba(255,255,255,.6);font-weight:500}"
                ".helper.waiting .state{color:var(--amber)}"
                "@keyframes pulse{50%{opacity:.35}}.dot.busy{animation:pulse 1.6s ease-in-out infinite}"
                "</style>")


# The helpers view's tabs: which kinds each shows. Picking one is done in the page; it sticks across refreshes.
HELPER_GROUPS = {"all": ("All", None), "code": ("Claude Code", ("session",)),
                 "commands": ("Commands", ("job",)), "tasks": ("Tasks", ("task", "build"))}
HELPER_TABS_STYLE = ("<style>nav.tabs{display:flex;gap:2px;padding:8px 10px 6px;border-bottom:1px solid var(--line);"
                     "position:sticky;top:0;background:rgba(20,20,20,.55);backdrop-filter:blur(12px);z-index:1}"
                     "nav.tabs button{all:unset;cursor:default;font-size:12px;padding:3px 9px;color:rgba(255,255,255,.55);"
                     "border:1px solid transparent}nav.tabs button:hover{color:#fff}"
                     "nav.tabs button.on{color:#fff;border-color:rgba(255,255,255,.28)}"
                     "nav.tabs .n{margin-left:5px;font-variant-numeric:tabular-nums;color:rgba(255,255,255,.4)}"
                     ".helper.hidden,.empty.hidden{display:none}</style>")
HELPER_SCRIPT = """<script>
window.jarvisFilter = window.jarvisFilter || document.body.dataset.filter || 'all';
window.applyFilter = function () {
  var f = window.jarvisFilter, shown = 0;
  document.querySelectorAll('nav.tabs button').forEach(function (b) { b.classList.toggle('on', b.dataset.f === f); });
  document.querySelectorAll('.helper').forEach(function (h) {
    var on = f === 'all' || h.dataset.group === f; h.classList.toggle('hidden', !on); if (on) shown++; });
  var none = document.querySelector('.empty.filtered'); if (none) none.classList.toggle('hidden', shown > 0);
  if (window.webkit && webkit.messageHandlers && webkit.messageHandlers.fit) webkit.messageHandlers.fit.postMessage(1);
};
document.addEventListener('click', function (ev) {
  var b = ev.target.closest('nav.tabs button'); if (!b) return;
  window.jarvisFilter = b.dataset.f; window.applyFilter();
});
window.applyFilter();
</script>"""


def helpers_view(mem: Memory | None, now: float | None = None, show: str = "all") -> dict:
    """Every helper (JARVIS's tasks, jobs and build, and Claude Code sessions on this Mac): where it
    runs and exactly what it's doing, with tabs for All, Claude Code, Commands and Tasks (`show` picks
    the first one). Kept live while it's up (serve_live)."""
    from jarvis import helpers
    board = helpers.board(mem, now)
    show = show if show in HELPER_GROUPS else "all"
    group_of = {k: g for g, (_, kinds) in HELPER_GROUPS.items() if kinds for k in kinds}
    e = htmllib.escape
    parts = []
    for h in board:
        dot = "var(--amber)" if h["waiting"] else "#fff" if h["busy"] else "rgba(255,255,255,.3)"
        state = "waiting for you" if h["waiting"] else h["state"]
        lines = [f'<div class="where">{e(h["where"])}</div>']
        if h["asked"] and not h["asked"].startswith(h["name"].rstrip(".…")):  # named after it already
            lines.append(f'<div class="line"><span class="k">Asked</span>{e(h["asked"][:220])}</div>')
        if h["plan"]:
            done = sum(1 for x in h["plan"] if x["status"] == "completed")
            items = "".join(f'<li class="{e(x["status"])}">{e(x["content"])}</li>' for x in h["plan"])
            lines.append(f'<div class="line"><span class="k">Plan</span>{done} of {len(h["plan"])} done</div><ol>{items}</ol>')
        elif h["now"]:
            lines.append(f'<div class="line"><span class="k">Now</span>{e(h["now"][:200])}</div>')
        if h["steps"]:
            lines.append(f'<div class="line steps"><span class="k">Last steps</span>{e(" · ".join(h["steps"][-3:]))}</div>')
        if h["said"]:
            lines.append(f'<div class="line steps"><span class="k">{"Output" if h["kind"] == "job" else "Said"}</span>'
                         f'{e(h["said"][-220:])}</div>')
        cls = "helper" + (" waiting" if h["waiting"] else "" if h["busy"] else " quiet")
        parts.append(f'<section class="{cls}" data-group="{group_of.get(h["kind"], "tasks")}"><div class="head"><span class="name"><i class="dot{" busy" if h["busy"] else ""}" '
                     f'style="background:{dot}"></i>{e(h["name"])}</span><span class="state">{e(state)}</span></div>'
                     f'{"".join(lines)}</section>')
    busy = sum(1 for h in board if h["busy"])
    title = (f"{len(board)} helper{'s' if len(board) != 1 else ''}, {busy} working" if board else "No helpers running")
    counts = {g: sum(1 for h in board if not kinds or h["kind"] in kinds) for g, (_, kinds) in HELPER_GROUPS.items()}
    tabs = "".join(f'<button data-f="{g}" class="{"on" if g == show else ""}">{label}<span class="n">{counts[g]}</span></button>'
                   for g, (label, _) in HELPER_GROUPS.items())
    body = (HELPER_STYLE + HELPER_TABS_STYLE + f'<nav class="tabs">{tabs}</nav><main>{"".join(parts)}'
            '<p class="empty filtered hidden">Nothing of that kind running, sir.</p></main>')
    html = page(body + HELPER_SCRIPT, title).replace("<body>", f'<body data-filter="{show}">', 1)
    return {"title": title, "html": html, "body": body, "live": "helpers", "filter": show}


def image(path: str, mem: Memory | None = None) -> dict:
    from jarvis.attachments import IMAGE_TYPES, SHOTS_DIR, safe_image_path
    if str(path).strip().lower() in ("", "latest", "last", "earlier"):
        shots = sorted((p for p in SHOTS_DIR.glob("*") if p.suffix.lower() in IMAGE_TYPES),
                       key=lambda p: p.stat().st_mtime) if SHOTS_DIR.exists() else []
        if not shots:
            raise ValueError("there's no screenshot saved yet")
        path = str(shots[-1])
    full = safe_image_path(path, mem)
    data = base64.b64encode(full.read_bytes()).decode()
    mime = IMAGE_TYPES[full.suffix.lower()]
    title = full.name
    if (m := re.match(r"screen-(\d{8})-(\d{6})", full.stem)):  # "your screenshot from Sun 3:55 PM"
        title = f"your screenshot from {datetime.strptime(m[1] + m[2], '%Y%m%d%H%M%S'):%a %-I:%M %p}"
    return {"title": title, "html": page(f"<img src=\"data:{mime};base64,{data}\" alt=\"{htmllib.escape(title)}\">", title)}


def raw_html(markup: str, title: str = "JARVIS") -> dict:
    """HTML JARVIS wrote (a chart, a table). Scripts may run, but nothing can reach the network."""
    if len(markup) > 300_000:
        raise ValueError("that page is too big for the panel")
    body = re.sub(r"(?is)<\s*meta[^>]*http-equiv[^>]*>", "", markup)  # its own policy can't loosen ours
    return {"title": title[:80], "html": page(body, title)}


def web(url: str) -> dict:
    url = str(url).strip()
    if not re.match(r"^https?://", url):
        raise ValueError("only http(s) pages can be shown")
    from jarvis.web import blocked_site
    if blocked_site(url):
        raise ValueError("that site is on the blocked list")
    return {"title": url[:80], "url": url}


# A formula, safely: numbers, x, pi, e, + - * / ^, and a few functions. Nothing else is evaluated.
_FUNCS = {"sin": math.sin, "cos": math.cos, "tan": math.tan, "exp": math.exp, "log": math.log, "ln": math.log,
          "sqrt": math.sqrt, "abs": abs, "asin": math.asin, "acos": math.acos, "atan": math.atan,
          "sinh": math.sinh, "cosh": math.cosh, "tanh": math.tanh, "floor": math.floor, "ceil": math.ceil}
_OPS = {ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b, ast.Mult: lambda a, b: a * b,
        ast.Div: lambda a, b: a / b, ast.Pow: lambda a, b: a ** b, ast.Mod: lambda a, b: a % b}


def compile_formula(expr: str):
    """'sin(x) + 2', '2x^2 - 3' -> f(x). ValueError for anything that isn't a plain formula in x."""
    text = str(expr).strip().lower().replace("^", "**").replace("π", "pi").replace("−", "-")
    text = re.sub(r"^(?:y|f\(x\))\s*=\s*", "", text)
    text = re.sub(r"(\d)\s*(x|pi|\(|[a-z]{2,}\()", r"\1*\2", text)  # 2x, 3(x+1), 2sin(x)
    text = re.sub(r"\)\s*(\(|x)", r")*\1", text)
    if len(text) > 200:
        raise ValueError("that formula is too long")
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError:
        raise ValueError(f"can't read the formula {expr!r}") from None

    def ev(node, x):
        if isinstance(node, ast.Expression):
            return ev(node.body, x)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)  # floats overflow cleanly; huge integer powers would hang
        if isinstance(node, ast.Name):
            if node.id == "x":
                return x
            if node.id in ("pi", "e"):
                return math.pi if node.id == "pi" else math.e
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](ev(node.left, x), ev(node.right, x))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            v = ev(node.operand, x)
            return -v if isinstance(node.op, ast.USub) else v
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS
                and len(node.args) == 1 and not node.keywords):
            return _FUNCS[node.func.id](ev(node.args[0], x))
        raise ValueError(f"can't plot {expr!r} (only x, numbers, + - * / ^ and functions like sin, log)")
    for node in ast.walk(tree):  # check the whole formula up front, not just the parts one x reaches
        ok = isinstance(node, (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Load, ast.USub, ast.UAdd, *_OPS)) or (
            isinstance(node, ast.Constant) and isinstance(node.value, (int, float))) or (
            isinstance(node, ast.Name) and node.id in ("x", "pi", "e", *_FUNCS)) or (
            isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS
            and len(node.args) == 1 and not node.keywords)
        if not ok:
            raise ValueError(f"can't plot {expr!r} (only x, numbers, + - * / ^ and functions like sin, log)")
    return lambda x: ev(tree, x)


def plot(expr: str, x_min: float = -10, x_max: float = 10, title: str = "") -> dict:
    """An SVG line plot of y = expr over [x_min, x_max], with axes and a few tick labels."""
    f = compile_formula(expr)
    x_min, x_max = float(x_min), float(x_max)
    if not x_min < x_max or x_max - x_min > 1e6:
        raise ValueError("bad x range")
    n = 400
    pts = []
    for i in range(n + 1):
        x = x_min + (x_max - x_min) * i / n
        try:
            y = float(f(x))
        except (ValueError, ZeroDivisionError, OverflowError):
            y = math.nan
        pts.append((x, y if math.isfinite(y) and abs(y) < 1e6 else math.nan))
    ys = [y for _, y in pts if not math.isnan(y)]
    if not ys:
        raise ValueError(f"{expr} has no values to plot between {x_min:g} and {x_max:g}")
    lo, hi = min(ys + [0.0]), max(ys + [0.0])
    pad = (hi - lo) * 0.1 or 1.0
    lo, hi = lo - pad, hi + pad
    W, H = 640, 400

    def sx(x):
        return (x - x_min) / (x_max - x_min) * W

    def sy(y):
        return H - (y - lo) / (hi - lo) * H
    path, pen = [], "M"
    for x, y in pts:
        if math.isnan(y):
            pen = "M"
            continue
        path.append(f"{pen}{sx(x):.1f},{sy(y):.1f}")
        pen = "L"
    grid = []
    if x_min < 0 < x_max:
        grid.append(f'<line x1="{sx(0):.1f}" y1="0" x2="{sx(0):.1f}" y2="{H}" stroke="rgba(95,215,255,.45)"/>')
    if lo < 0 < hi:
        grid.append(f'<line x1="0" y1="{sy(0):.1f}" x2="{W}" y2="{sy(0):.1f}" stroke="rgba(95,215,255,.45)"/>')
    labels = [f'<text x="4" y="14" fill="#5fd7ff" fill-opacity=".7" font-size="11" font-family="ui-monospace,Menlo">y max {hi - pad:.3g}</text>',
              f'<text x="4" y="{H - 6}" fill="#5fd7ff" fill-opacity=".7" font-size="11" font-family="ui-monospace,Menlo">y min {lo + pad:.3g}</text>',
              f'<text x="{W - 4}" y="{H - 6}" fill="#5fd7ff" fill-opacity=".7" font-size="11" font-family="ui-monospace,Menlo" text-anchor="end">x {x_min:g} to {x_max:g}</text>']
    label = title or f"y = {expr}"
    svg = (f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg">{"".join(grid)}'
           f'<path d="{" ".join(path)}" fill="none" stroke="#5fd7ff" stroke-width="2"/>'
           f'{"".join(labels)}</svg>')
    return {"title": label[:80], "html": page(f"<main>{svg}</main>", label)}


def show(mem: Memory, action: dict, wait=_wait_ack) -> str:
    """The chat `show` action: build the content, put it on the panel, confirm only once it's up."""
    if not config.PANEL:
        raise ValueError("the panel is turned off (PANEL=0)")
    source = {k: v for k, v in action.items() if k != "type"}
    if len(json.dumps(source)) > 20_000:
        source = {"kind": "card", "title": str(action.get("title") or "JARVIS"), "lines": ["(too big to keep)"]}
    return _present(mem, render(mem, action), wait, source)


def render(mem: Memory, action: dict) -> dict:
    kind = str(action.get("kind") or "").lower()
    title = str(action.get("title") or "")
    if kind == "plot":
        content = plot(str(action.get("expr") or ""), action.get("x_min", -10), action.get("x_max", 10), title)
    elif kind == "image":
        content = image(str(action.get("path") or "latest"), mem)
        if title:
            content["title"] = title[:80]
    elif kind == "deadlines":
        content = deadlines(mem)
    elif kind == "helpers":
        content = helpers_view(mem, show=str(action.get("show") or "all").lower())
    elif kind == "card":
        lines = action.get("lines") or []
        content = card(title or "JARVIS", [str(x) for x in lines] if isinstance(lines, list) else [str(lines)])
    elif kind == "html":
        content = raw_html(str(action.get("html") or ""), title or "JARVIS")
    elif kind == "url":
        content = web(str(action.get("url") or ""))
    else:
        raise ValueError(f"can't show kind {kind!r} (plot, image, deadlines, helpers, card, html or url)")
    return content


PANEL_OPS = {"close": close, "bigger": lambda mem, wait=_wait_ack: resize(mem, 1, wait),
             "smaller": lambda mem, wait=_wait_ack: resize(mem, -1, wait), "back": back}


# ----- live views -----
REQUEST = PANEL_DIR / "request.json"   # "Show Helpers" in the menu bar writes this
LIVE_EVERY = 3.0                        # seconds between refreshes of a live view


def refresh(mem: Memory | None) -> bool:
    """Re-render a live view that's up (helpers) in place: the panel swaps its contents without a reload."""
    st = current()
    if st.get("live") != "helpers" or not visible():
        return False
    content = helpers_view(mem, show=st.get("filter", "all"))
    if content["body"] == st.get("body"):
        return False
    _write({**st, **content, "refresh": True})
    return True


def take_request(now: float | None = None) -> str | None:
    """A view asked for from the menu bar ("helpers"), once, if it's fresh."""
    req = _read(REQUEST)
    if not req:
        return None
    REQUEST.unlink(missing_ok=True)
    kind = str(req.get("kind") or "")
    return kind if kind in ("helpers", "deadlines") and (now or time.time()) - float(req.get("at") or 0) < 30 else None


def serve_live(open_mem, log=print, stop=None, every: float = LIVE_EVERY) -> None:
    """Run by `jarvis listen` on its own thread: opens what the menu bar asks for and keeps live views current."""
    import threading
    stop = stop or threading.Event()
    mem = open_mem()  # SQLite connections stay on the thread that opened them
    while not stop.is_set():
        try:
            if (kind := take_request()):
                show(mem, {"type": "show", "kind": kind})
            else:
                refresh(mem)
        except Exception as e:  # a bad file or a slow panel mustn't kill the listener
            log(f"panel: {e}")
        stop.wait(every)
