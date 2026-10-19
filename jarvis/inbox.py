"""JARVIS's inbox on the Mac: the Telegram conversation, mirrored, with its buttons working.

Every message JARVIS sends to Telegram (from any of its processes: chat, alerts, plans, command
approvals, builds) is also recorded in inbox/feed.json, along with what Elijah writes in Telegram
and the HUD. 'JARVIS Inbox.app' shows that feed as a chat in the panel's style. Pressing a button
there (Approve, Run, Yes build it, Deploy...) does exactly what pressing it in Telegram does, through
the same code, and the Telegram message is updated to match. What he types there is answered there.

The app has no access to anything: it reads the feed and drops small request files in
inbox/requests/, which `serve` (run by `jarvis listen`) handles. Only buttons that are really on an
open message can be pressed, so a request file can't invent an action.
"""

import fcntl
import hashlib
import json
import os
import plistlib
import re
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from jarvis import config

INBOX_DIR = config.DATA_DIR / "inbox"
FEED = INBOX_DIR / "feed.json"
REQUESTS = INBOX_DIR / "requests"
KEEP = 300             # messages kept in the feed
STALE_AFTER = 2 * 86400  # buttons left unanswered this long are dropped
TYPING_FOR = 8.0
STREAM_EVERY = 0.08    # seconds between streamed writes of a reply       # seconds "JARVIS is typing" shows after a typing signal
MAX_TEXT = 4000
_lock = threading.Lock()


# ----- the feed -----
def read() -> dict:
    try:
        data = json.loads(FEED.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


@contextmanager
def _editing():
    """Read-modify-write the feed under a lock shared by every JARVIS process."""
    with _lock:
        INBOX_DIR.mkdir(parents=True, exist_ok=True)
        with open(INBOX_DIR / ".feed.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            data = read()
            data.setdefault("messages", [])
            yield data
            data["messages"] = data["messages"][-KEEP:]
            now = time.time()
            for m in data["messages"]:  # questions nobody answered lapse (commands expire much sooner anyway)
                if m.get("buttons") and now - m["at"] > STALE_AFTER:
                    m["buttons"], m["note"] = [], m.get("note") or "No longer waiting."
            data["waiting"] = sum(1 for m in data["messages"] if m.get("buttons"))
            data["updated"] = time.time()
            fd, tmp = tempfile.mkstemp(dir=INBOX_DIR, prefix=".feed-")
            with os.fdopen(fd, "w") as f:
                json.dump(data, f)
            os.replace(tmp, FEED)  # atomic: the app never reads half a feed


def record(who: str, text: str, buttons: list[tuple[str, str]] | None = None, tg_id: int | None = None,
           via: str = "", streaming: bool = False) -> int:
    """Add a message ('jarvis' or 'you'). Returns its local id. Never raises: the feed is a mirror."""
    try:
        with _editing() as data:
            mid = int(data.get("next_id", 1))
            data["next_id"] = mid + 1
            data["messages"].append({"id": mid, "who": who, "text": str(text)[:MAX_TEXT], "via": via,
                                     "buttons": [[str(l), str(d)] for l, d in (buttons or [])],
                                     "tg_id": tg_id, "at": time.time(), "note": "", "streaming": streaming})
            if who == "jarvis":
                data["typing_until"] = 0
        return mid
    except OSError:
        return 0


def resolve(local_id: int | None = None, tg_id: int | None = None, note: str = "", text: str | None = None) -> None:
    """A message's buttons were answered (here or in Telegram): drop them and show what happened."""
    if not local_id and not tg_id:
        return
    try:
        with _editing() as data:
            for m in reversed(data["messages"]):
                if (local_id and m["id"] == local_id) or (tg_id and m.get("tg_id") == tg_id):
                    m["buttons"] = []
                    if text is not None:
                        m["text"] = str(text)[:MAX_TEXT]
                    if note:
                        m["note"] = str(note)[:300]
                    break
    except OSError:
        pass


def update(local_id: int, text: str, streaming: bool = True) -> None:
    """New text for a message that's still being written (or, with streaming=False, its final text)."""
    try:
        with _editing() as data:
            for m in reversed(data["messages"]):
                if m["id"] == local_id:
                    m["text"], m["streaming"] = str(text)[:MAX_TEXT], streaming
                    break
            data["typing_until"] = 0
    except OSError:
        pass


def read_text(local_id: int) -> str:
    return next((m["text"] for m in read().get("messages", []) if m["id"] == local_id), "")


def typing() -> None:
    try:
        with _editing() as data:
            data["typing_until"] = time.time() + TYPING_FOR
    except OSError:
        pass


def open_button(local_id: int, data_str: str) -> dict | None:
    """The message, if `data_str` really is one of its open buttons."""
    for m in read().get("messages", []):
        if m["id"] == local_id and any(d == data_str for _, d in m.get("buttons", [])):
            return m
    return None


_PREFIX = re.compile(r"^\((?:typed in the HUD on his Mac|said out loud[^)]*|on the phone[^)]*)\)\s*", re.I)


def seed(mem) -> int:
    """First run: start the feed with the recent conversation, so it isn't empty."""
    if FEED.exists():
        return 0
    rows = list(reversed(mem.db.execute("SELECT * FROM chat_log ORDER BY id DESC LIMIT 40").fetchall()))
    try:
        with _editing() as data:
            for r in rows:
                text = r["text"]
                via = "HUD" if text.startswith("(typed in the HUD") else ""
                mid = int(data.get("next_id", 1))
                data["next_id"] = mid + 1
                when = datetime.fromisoformat(r["at"])
                at = (when if when.tzinfo else when.replace(tzinfo=timezone.utc)).timestamp()
                data["messages"].append({"id": mid, "who": "you" if r["role"] == "user" else "jarvis",
                                         "text": _PREFIX.sub("", text)[:MAX_TEXT], "via": via, "buttons": [],
                                         "tg_id": None, "at": at, "note": ""})
    except (OSError, ValueError):
        return 0
    return len(rows)


# ----- Telegram, mirrored -----
class Mirror:
    """A Telegram client that also records what it sends in the feed. Anything else passes through."""

    def __init__(self, tg):
        self._tg = tg

    def __getattr__(self, name):
        return getattr(self._tg, name)

    def send(self, text: str, buttons: list[tuple[str, str]] | None = None) -> int:
        mid = self._tg.send(text, buttons=buttons)
        record("jarvis", text, buttons, tg_id=mid)
        return mid

    def edit(self, message_id: int, text: str) -> None:
        self._tg.edit(message_id, text)
        resolve(tg_id=message_id, text=text)

    def send_photo(self, png: bytes, caption: str = "") -> None:
        self._tg.send_photo(png, caption)
        record("jarvis", ("📷 " + caption) if caption else "📷 (a picture, in Telegram)")

    def send_voice(self, ogg: bytes, caption: str = "") -> None:
        self._tg.send_voice(ogg, caption)
        record("jarvis", caption or "🔊 (a voice note, in Telegram)")

    def send_document(self, filename: str, data: bytes, caption: str = "", mime: str = "text/plain") -> None:
        self._tg.send_document(filename, data, caption, mime)
        record("jarvis", f"📄 {filename}" + (f": {caption}" if caption else ""))


class MacChat:
    """Stands in for Telegram while handling what was pressed or typed in the inbox app. Replies to
    what he typed there stay on the Mac; a button's result also updates its Telegram message."""

    def __init__(self, tg, local_id: int | None = None):
        self._tg = tg            # the real (mirrored) client, for editing Telegram messages and photos
        self.local_id = local_id
        self.draft = 0           # the reply being written, shown as it streams
        self.wrote = 0.0

    def stream(self, so_far: str) -> None:
        """The reply so far: one message that fills in as it's written (at most every STREAM_EVERY)."""
        if not self.draft:
            self.draft = record("jarvis", so_far, via="Mac", streaming=True)
        elif time.time() - self.wrote >= STREAM_EVERY:
            update(self.draft, so_far)
        else:
            return
        self.wrote = time.time()

    def finish(self, reply: str) -> None:
        """The whole reply (checked, actions done): it replaces what streamed."""
        if self.draft:
            update(self.draft, reply, streaming=False)
            self.draft = 0
        else:
            record("jarvis", reply, via="Mac")

    def send(self, text: str, buttons: list[tuple[str, str]] | None = None) -> int:
        if buttons:  # a question for his OK goes to Telegram too, in case he walks away; both copies work
            try:
                return self._tg.send(text, buttons=buttons)
            except Exception:
                pass
        record("jarvis", text, buttons, via="Mac")
        return 0

    def edit(self, message_id: int, text: str) -> None:
        if message_id:
            try:
                self._tg.edit(message_id, text)  # Telegram's copy too (drops its buttons there)
            except Exception:
                pass
            resolve(tg_id=message_id, text=text)

    def answer(self, callback_id: str, text: str) -> None:
        resolve(self.local_id, note=text)

    def typing(self) -> None:
        typing()

    def action(self, kind: str) -> None:
        typing()

    def send_photo(self, png: bytes, caption: str = "") -> None:
        try:
            self._tg.send_photo(png, caption)  # pictures can only be seen in Telegram
        except Exception:
            record("jarvis", "📷 " + (caption or "a picture (couldn't reach Telegram)"), via="Mac")

    def send_voice(self, ogg: bytes, caption: str = "") -> None:
        record("jarvis", caption or "🔊 (a voice note)", via="Mac")

    def send_document(self, filename: str, data: bytes, caption: str = "", mime: str = "text/plain") -> None:
        try:
            self._tg.send_document(filename, data, caption, mime)
        except Exception:
            record("jarvis", f"📄 {filename} (couldn't reach Telegram)", via="Mac")


# ----- what the app asks for -----
def take_requests() -> list[dict]:
    """Requests the app dropped, oldest first; each is removed as it's taken."""
    if not REQUESTS.is_dir():
        return []
    out = []
    for path in sorted(REQUESTS.glob("*.json")):
        try:
            req = json.loads(path.read_text())
        except (OSError, ValueError):
            req = None
        path.unlink(missing_ok=True)
        if isinstance(req, dict) and time.time() - float(req.get("at") or 0) < 300:
            out.append(req)
    return out


def serve(handle: Callable[[dict], None], log=print, stop: threading.Event | None = None, every: float = 0.25) -> None:
    """Handle what's pressed or typed in the inbox app until `stop` is set (run by `jarvis listen`)."""
    stop = stop or threading.Event()
    while not stop.is_set():
        for req in take_requests():
            try:
                handle(req)
            except Exception as e:  # one bad request mustn't stop the rest
                log(f"inbox: {e}")
                record("jarvis", f"Sorry, sir, that didn't go through ({str(e)[:150]}).", via="Mac")
        stop.wait(every)


# ----- the app -----
APP = Path.home() / "Applications" / "JARVIS Inbox.app"
EXECUTABLE = APP / "Contents" / "MacOS" / "jarvis-inbox"

PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'">
<style>
:root{color-scheme:dark;--amber:#ffb000;--line:rgba(255,255,255,.1);--dim:rgba(255,255,255,.5)}
*{box-sizing:border-box}
html,body{margin:0;height:100%;background:transparent;color:#fff;font:13px -apple-system,sans-serif}
body{display:flex;flex-direction:column}
#waiting{display:none;padding:7px 14px;border-bottom:1px solid var(--line);color:var(--amber);font-size:12px;font-weight:600;cursor:default}
#waiting.on{display:block}
#feed{flex:1;overflow-y:auto;padding:6px 0 10px}
.day{margin:14px 14px 4px;font-size:11px;font-weight:600;color:var(--dim)}
.msg{margin:6px 14px;max-width:86%;display:flex;flex-direction:column}
.msg.you{margin-left:auto;align-items:flex-end}
.meta{font-size:10.5px;color:rgba(255,255,255,.4);margin:0 0 2px;font-variant-numeric:tabular-nums}
.text{white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.4;padding:7px 10px;border:1px solid var(--line);background:rgba(255,255,255,.04)}
.you .text{background:rgba(255,255,255,.12);border-color:rgba(255,255,255,.16)}
.msg.ask .text{border-color:rgba(255,176,0,.55)}
.buttons{display:flex;gap:6px;margin-top:6px;flex-wrap:wrap}
.buttons button{all:unset;cursor:default;font-size:12px;padding:5px 12px;border:1px solid rgba(255,255,255,.28);color:#fff}
.buttons button:first-child{border-color:var(--amber);color:var(--amber)}
.buttons button:hover{background:rgba(255,255,255,.08)}
.buttons button:disabled{opacity:.4}
.note{margin-top:4px;font-size:12px;font-style:italic;color:var(--dim)}
#typing{display:none;margin:6px 14px;font-size:12px;color:var(--dim)}
#typing.on{display:block}
form{display:flex;align-items:flex-end;gap:8px;border-top:1px solid rgba(255,255,255,.14);padding:9px 12px}
textarea{flex:1;resize:none;border:0;outline:0;background:transparent;color:#fff;font:14px -apple-system,sans-serif;
  line-height:1.35;max-height:120px;padding:2px 0}
textarea::placeholder{color:rgba(255,255,255,.4)}
form button{all:unset;cursor:default;font-size:12px;color:var(--dim);padding:2px 2px 3px}
form button:hover{color:#fff}
.empty{margin:30px 14px;color:var(--dim)}
</style></head><body>
<div id="waiting"></div>
<div id="feed"><p class="empty">Nothing yet, sir.</p></div>
<div id="typing">JARVIS is typing…</div>
<form id="compose"><textarea id="box" rows="1" placeholder="Message JARVIS…"></textarea><button type="submit">Send</button></form>
<script>
var post = function (m) { webkit.messageHandlers.jarvis.postMessage(m); };
var feed = document.getElementById('feed'), box = document.getElementById('box');
var last = null, pressed = {};
function clock(t) { return new Date(t * 1000).toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'}); }
function dayName(t) {
  var d = new Date(t * 1000), today = new Date(); today.setHours(0, 0, 0, 0);
  var n = Math.round((today - new Date(d.getFullYear(), d.getMonth(), d.getDate())) / 86400000);
  return n === 0 ? 'Today' : n === 1 ? 'Yesterday' : d.toLocaleDateString([], {weekday: 'long', month: 'short', day: 'numeric'});
}
function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
// While a reply streams only its text changes: update that one bubble instead of redrawing the chat.
function shape(data) {
  return JSON.stringify((data.messages || []).map(function (m, i, all) {
    return [m.id, i === all.length - 1 ? '' : m.text, m.buttons, m.note, m.streaming]; }));
}
function render(data) {
  var msgs0 = data.messages || [], tail = msgs0[msgs0.length - 1];
  if (last && tail && tail.streaming && shape(data) === shape(last)) {
    var bubbles = feed.querySelectorAll('.msg .text'), atEnd = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 60;
    if (bubbles.length) { bubbles[bubbles.length - 1].textContent = tail.text; }
    last = data; tick();
    if (atEnd) feed.scrollTop = feed.scrollHeight;
    return;
  }
  last = data;
  var msgs = data.messages || [], atBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 40;
  feed.textContent = '';
  if (!msgs.length) feed.appendChild(el('p', 'empty', 'Nothing yet, sir.'));
  var day = '', firstAsk = null;
  msgs.forEach(function (m) {
    var d = dayName(m.at);
    if (d !== day) { day = d; feed.appendChild(el('div', 'day', d)); }
    var open = m.buttons && m.buttons.length;
    var box = el('div', 'msg ' + (m.who === 'you' ? 'you' : 'jarvis') + (open ? ' ask' : ''));
    box.appendChild(el('div', 'meta', (m.who === 'you' ? 'You' : 'JARVIS') + (m.via ? ' · ' + m.via : '') + ' · ' + clock(m.at)));
    box.appendChild(el('div', 'text', m.text));
    if (open) {
      if (!firstAsk) firstAsk = box;
      var row = el('div', 'buttons');
      m.buttons.forEach(function (b) {
        var btn = el('button', '', b[0]);
        btn.disabled = !!pressed[m.id];
        btn.onclick = function () {
          pressed[m.id] = true;
          row.querySelectorAll('button').forEach(function (x) { x.disabled = true; });
          post({type: 'button', id: m.id, data: b[1]});
        };
        row.appendChild(btn);
      });
      box.appendChild(row);
    } else { delete pressed[m.id]; }
    if (m.note) box.appendChild(el('div', 'note', '— ' + m.note));
    feed.appendChild(box);
  });
  var w = document.getElementById('waiting'), n = data.waiting || 0;
  w.textContent = n ? n + (n === 1 ? ' thing waits' : ' things wait') + ' for your OK' : '';
  w.className = n ? 'on' : '';
  w.onclick = function () { if (firstAsk) firstAsk.scrollIntoView({block: 'center'}); };
  tick();
  if (atBottom) feed.scrollTop = feed.scrollHeight;
}
function tick() {
  var msgs = (last && last.messages) || [], streaming = msgs.length && msgs[msgs.length - 1].streaming;
  document.getElementById('typing').className = !streaming && last && (last.typing_until || 0) * 1000 > Date.now() ? 'on' : '';
}
setInterval(tick, 1000);
function grow() { box.style.height = 'auto'; box.style.height = Math.min(box.scrollHeight, 120) + 'px'; }
box.addEventListener('input', grow);
box.addEventListener('keydown', function (e) {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
});
document.getElementById('compose').onsubmit = function (e) { e.preventDefault(); send(); };
function send() {
  var t = box.value.trim(); if (!t) return;
  post({type: 'text', text: t}); box.value = ''; grow();
  feed.scrollTop = feed.scrollHeight;
}
function focusBox() { box.focus(); feed.scrollTop = feed.scrollHeight; }
</script></body></html>"""

SWIFT = r"""
import AppKit
import WebKit

let inboxDir = CommandLine.arguments.dropFirst().first { !$0.hasPrefix("--") }
    ?? NSHomeDirectory() + "/Library/Application Support/JARVIS/inbox"
let feedPath = inboxDir + "/feed.json"
let requestsDir = inboxDir + "/requests"
let page = PAGE_LITERAL

// The panel's look: flat glass, hard edges, hairlines, white; amber only for what waits on him.
let line = NSColor.white.withAlphaComponent(0.18)
let amber = NSColor(srgbRed: 1.0, green: 0.69, blue: 0.0, alpha: 1)

final class KeyPanel: NSPanel {
    override var canBecomeKey: Bool { true }  // borderless panels can't take typing otherwise
}

// One line: "JARVIS", how many things wait (amber), and a close mark. Dragging it moves the window.
final class Bar: NSView {
    let title = NSTextField(labelWithString: "")
    let close = NSButton(title: "", target: nil, action: nil)
    var onClose: () -> Void = {}

    override init(frame: NSRect) {
        super.init(frame: frame)
        close.isBordered = false
        close.attributedTitle = NSAttributedString(string: "×", attributes: [
            .font: NSFont.systemFont(ofSize: 17, weight: .light), .foregroundColor: NSColor.white.withAlphaComponent(0.7)])
        close.target = self
        close.action = #selector(closeClicked)
        addSubview(title)
        addSubview(close)
        set(waiting: 0)
    }
    required init?(coder: NSCoder) { fatalError() }

    func set(waiting: Int) {
        let s = NSMutableAttributedString(string: "JARVIS", attributes: [
            .font: NSFont.systemFont(ofSize: 13, weight: .semibold), .foregroundColor: NSColor.white])
        if waiting > 0 {
            s.append(NSAttributedString(string: "   \(waiting) waiting", attributes: [
                .font: NSFont.systemFont(ofSize: 12, weight: .medium), .foregroundColor: amber]))
        }
        title.attributedStringValue = s
    }

    override func layout() {
        super.layout()
        title.frame = NSRect(x: 14, y: (bounds.height - 17) / 2, width: bounds.width - 52, height: 17)
        close.frame = NSRect(x: bounds.width - 34, y: (bounds.height - 24) / 2, width: 24, height: 24)
    }
    override func mouseDown(with event: NSEvent) { window?.performDrag(with: event) }
    @objc func closeClicked() { onClose() }
    override func draw(_ dirtyRect: NSRect) {
        NSColor.white.withAlphaComponent(0.14).setFill()
        NSRect(x: 0, y: 0, width: bounds.width, height: 1).fill()
    }
}

final class Inbox: NSObject, NSWindowDelegate, WKScriptMessageHandler, WKNavigationDelegate {
    var window: KeyPanel!
    var root: NSView!
    var bar: Bar!
    var web: WKWebView!
    var lastMod: Date? = nil
    var loaded = false

    override init() {
        super.init()
        let frame = NSRect(x: 0, y: 0, width: 420, height: 600)
        window = KeyPanel(contentRect: frame, styleMask: [.borderless, .resizable], backing: .buffered, defer: true)
        window.title = "JARVIS Inbox"
        window.level = .floating  // like the panel: opening it brings it above the panel, not under it
        window.isReleasedWhenClosed = false
        window.hidesOnDeactivate = false
        window.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        window.isOpaque = false
        window.backgroundColor = .clear
        window.hasShadow = true
        window.appearance = NSAppearance(named: .darkAqua)
        window.minSize = NSSize(width: 320, height: 300)
        window.delegate = self

        root = NSView(frame: frame)
        root.wantsLayer = true
        root.layer!.masksToBounds = true
        root.layer!.borderWidth = 1
        root.layer!.borderColor = line.cgColor
        window.contentView = root
        let glass = NSVisualEffectView(frame: frame)
        glass.material = .hudWindow
        glass.blendingMode = .behindWindow
        glass.state = .active
        glass.autoresizingMask = [.width, .height]
        root.addSubview(glass)
        let tint = NSView(frame: frame)
        tint.wantsLayer = true
        tint.layer!.backgroundColor = NSColor.black.withAlphaComponent(0.35).cgColor
        tint.autoresizingMask = [.width, .height]
        root.addSubview(tint)

        bar = Bar(frame: .zero)
        bar.onClose = { [unowned self] in self.window.orderOut(nil) }
        root.addSubview(bar)
        let config = WKWebViewConfiguration()
        config.userContentController.add(self, name: "jarvis")
        web = WKWebView(frame: .zero, configuration: config)
        web.setValue(false, forKey: "drawsBackground")
        web.navigationDelegate = self
        root.addSubview(web)
        web.loadHTMLString(page, baseURL: nil)
        layout()

        if !window.setFrameUsingName("JARVISInbox") {  // where he left it; else the right edge, under the panel
            let s = (NSScreen.main ?? NSScreen.screens.first)!.visibleFrame
            let h = min(640, s.height * 0.62)
            window.setFrame(NSRect(x: s.maxX - 420 - 16, y: s.minY + 16, width: 420, height: h), display: false)
        }
        window.setFrameAutosaveName("JARVISInbox")
        Timer.scheduledTimer(withTimeInterval: 0.08, repeats: true) { _ in self.poll() }  // replies stream in
        DistributedNotificationCenter.default().addObserver(
            self, selector: #selector(showFromMenu), name: NSNotification.Name("com.jarvis.inbox.show"), object: nil)
    }

    func layout() {
        let b = root.bounds, h: CGFloat = 34
        bar.frame = NSRect(x: 0, y: b.height - h, width: b.width, height: h)
        web.frame = NSRect(x: 0, y: 0, width: b.width, height: b.height - h)
    }
    func windowDidResize(_ notification: Notification) { layout() }

    @objc func showFromMenu() { show() }
    func show() {
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
        window.makeFirstResponder(web)
        lastMod = nil  // render fresh
        poll()
        web.evaluateJavaScript("focusBox()", completionHandler: nil)
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) { loaded = true; lastMod = nil; poll() }

    func poll() {
        guard loaded, let attrs = try? FileManager.default.attributesOfItem(atPath: feedPath),
              let mod = attrs[.modificationDate] as? Date, mod != lastMod,
              let data = FileManager.default.contents(atPath: feedPath),
              let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let json = String(data: data, encoding: .utf8) else { return }
        lastMod = mod
        bar.set(waiting: obj["waiting"] as? Int ?? 0)
        if window.isVisible { web.evaluateJavaScript("render(\(json))", completionHandler: nil) } else { lastMod = nil }
    }

    // A button pressed or a message sent: a small request file for `jarvis listen`. Nothing else leaves the app.
    func userContentController(_ c: WKUserContentController, didReceive message: WKScriptMessage) {
        guard var body = message.body as? [String: Any] else { return }
        body["at"] = Date().timeIntervalSince1970
        try? FileManager.default.createDirectory(atPath: requestsDir, withIntermediateDirectories: true)
        let name = String(format: "%.6f-%d.json", Date().timeIntervalSince1970, Int.random(in: 0..<1_000_000))
        if let d = try? JSONSerialization.data(withJSONObject: body) {
            try? d.write(to: URL(fileURLWithPath: requestsDir + "/" + name), options: .atomic)
        }
    }
}

// Only one inbox, ever. A second copy hands over and exits.
let lockPath = NSHomeDirectory() + "/Library/Application Support/JARVIS/.inbox.lock"
try? FileManager.default.createDirectory(atPath: (lockPath as NSString).deletingLastPathComponent,
                                         withIntermediateDirectories: true)
let lockFD = open(lockPath, O_CREAT | O_RDWR, 0o644)
if lockFD < 0 || flock(lockFD, LOCK_EX | LOCK_NB) != 0 {
    if CommandLine.arguments.contains("--show") {
        DistributedNotificationCenter.default().postNotificationName(
            NSNotification.Name("com.jarvis.inbox.show"), object: nil, userInfo: nil, deliverImmediately: true)
    }
    exit(0)
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)  // no Dock icon
let inbox = Inbox()
if CommandLine.arguments.contains("--show") { inbox.show() }
app.run()
""".replace("PAGE_LITERAL", "#\"\"\"\n" + PAGE + "\n\"\"\"#")

INFO = {
    "CFBundleIdentifier": "com.jarvis.inbox",
    "CFBundleName": "JARVIS Inbox",
    "CFBundleDisplayName": "JARVIS Inbox",
    "CFBundleExecutable": "jarvis-inbox",
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
    """Compile and ad-hoc sign the inbox app if missing or outdated. Its own app, with no permissions."""
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
            raise RuntimeError(f"couldn't compile the inbox app: {r.stderr[-500:]}")
    subprocess.run(["codesign", "--force", "--sign", "-", "--identifier", INFO["CFBundleIdentifier"], str(APP)],
                   check=True, capture_output=True)
    (APP / "Contents" / "Resources" / "stamp").write_text(STAMP)
    return True
