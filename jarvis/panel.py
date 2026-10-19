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
from pathlib import Path

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

final class Panel: NSObject, NSWindowDelegate {
    var window: NSPanel!
    var web: WKWebView!
    var seq = -1
    var lastMod: Date? = nil

    override init() {
        super.init()
        let style: NSWindow.StyleMask = [.titled, .closable, .resizable, .utilityWindow, .nonactivatingPanel]
        window = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 560, height: 400), styleMask: style,
                         backing: .buffered, defer: true)
        window.title = "JARVIS"
        window.level = .floating
        window.isReleasedWhenClosed = false
        window.hidesOnDeactivate = false
        window.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        window.delegate = self
        web = WKWebView(frame: window.contentView!.bounds, configuration: WKWebViewConfiguration())
        web.autoresizingMask = [.width, .height]
        window.contentView!.addSubview(web)
        Timer.scheduledTimer(withTimeInterval: 0.25, repeats: true) { _ in self.poll() }
    }

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
        seq = newSeq
        place(obj["width"] as? Double ?? 560, obj["height"] as? Double ?? 400)
        window.title = obj["title"] as? String ?? "JARVIS"
        if let s = obj["url"] as? String, let u = URL(string: s), u.scheme == "https" || u.scheme == "http" {
            web.load(URLRequest(url: u))
        } else {
            web.loadHTMLString(obj["html"] as? String ?? "", baseURL: nil)
        }
        window.orderFrontRegardless()
        ack(newSeq, "shown")
    }

    func place(_ w: Double, _ h: Double) {
        guard let screen = (NSScreen.main ?? NSScreen.screens.first)?.visibleFrame else { return }
        window.setFrame(NSRect(x: screen.maxX - w - 16, y: screen.maxY - h - 16, width: w, height: h), display: true)
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
    size = int(content.get("size", DEFAULT_SIZE))
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
    _present(mem, {k: st[k] for k in ("title", "html", "url") if k in st} | {"size": size}, wait)
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
STYLE = ("<style>html,body{margin:0;background:#101418;color:#e8eef2;font:15px -apple-system,Helvetica,sans-serif}"
         "main{padding:18px 20px}h1{font-size:18px;margin:0 0 10px;color:#8fd3ff}ul{padding-left:18px;margin:0}"
         "li{margin:4px 0}img{max-width:100%;max-height:100vh;display:block;margin:auto}svg{width:100%;height:auto}"
         "</style>")


def page(body: str, title: str = "") -> str:
    """A self-contained page: no network (the content policy blocks it), JARVIS's dark style."""
    return (f"<!doctype html><html><head><meta charset=\"utf-8\">{CSP}{STYLE}<title>{htmllib.escape(title)}</title>"
            f"</head><body>{body}</body></html>")


def card(title: str, lines: list[str]) -> dict:
    items = "".join(f"<li>{htmllib.escape(str(x))}</li>" for x in lines[:20])
    return {"title": title[:80], "html": page(f"<main><h1>{htmllib.escape(title)}</h1><ul>{items}</ul></main>", title)}


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
        from datetime import datetime
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
        grid.append(f'<line x1="{sx(0):.1f}" y1="0" x2="{sx(0):.1f}" y2="{H}" stroke="#4a5560"/>')
    if lo < 0 < hi:
        grid.append(f'<line x1="0" y1="{sy(0):.1f}" x2="{W}" y2="{sy(0):.1f}" stroke="#4a5560"/>')
    labels = [f'<text x="4" y="14" fill="#8a96a0" font-size="12">y max {hi - pad:.3g}</text>',
              f'<text x="4" y="{H - 6}" fill="#8a96a0" font-size="12">y min {lo + pad:.3g}</text>',
              f'<text x="{W - 4}" y="{H - 6}" fill="#8a96a0" font-size="12" text-anchor="end">x {x_min:g} to {x_max:g}</text>']
    label = title or f"y = {expr}"
    svg = (f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg">{"".join(grid)}'
           f'<path d="{" ".join(path)}" fill="none" stroke="#8fd3ff" stroke-width="2.5"/>{"".join(labels)}</svg>')
    return {"title": label[:80], "html": page(f"<main><h1>{htmllib.escape(label)}</h1>{svg}</main>", label)}


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
    elif kind == "card":
        lines = action.get("lines") or []
        content = card(title or "JARVIS", [str(x) for x in lines] if isinstance(lines, list) else [str(lines)])
    elif kind == "html":
        content = raw_html(str(action.get("html") or ""), title or "JARVIS")
    elif kind == "url":
        content = web(str(action.get("url") or ""))
    else:
        raise ValueError(f"can't show kind {kind!r} (plot, image, card, html or url)")
    return content


PANEL_OPS = {"close": close, "bigger": lambda mem, wait=_wait_ack: resize(mem, 1, wait),
             "smaller": lambda mem, wait=_wait_ack: resize(mem, -1, wait), "back": back}
