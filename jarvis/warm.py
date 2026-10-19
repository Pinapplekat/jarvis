"""A warm, persistent Claude session for chat replies.

Starting `claude -p` per message costs ~2-3.5 s before Claude even thinks. A persistent
stream-json session answers in ~1-2.5 s. Sessions remember earlier turns, so each one is
retired after MAX_TURNS messages or MAX_IDLE of silence, with the next one already started
in the background. Any failure falls back to a normal one-off call, so a message is never
lost to the warm path.
"""

import json
import queue
import re
import subprocess
import threading
import time
from typing import Callable

from jarvis import config

MAX_TURNS = 12
MAX_IDLE = 2 * 60 * 60  # each message carries fresh context, so an idle session isn't harmful
REPLY_TIMEOUT = 180


class SessionDied(RuntimeError):
    pass


_REPLY_START = re.compile(r'"reply"\s*:\s*"')
_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}


def partial_reply(partial_json: str) -> str:
    """The (decoded) "reply" string so far from a structured output that's still streaming."""
    m = _REPLY_START.search(partial_json)
    if not m:
        return ""
    out, i, s = [], m.end(), partial_json
    while i < len(s):
        c = s[i]
        if c == '"':
            break
        if c == "\\":
            if i + 1 >= len(s):
                break  # escape cut off mid-stream; wait for more
            nxt = s[i + 1]
            if nxt == "u":
                if i + 6 > len(s):
                    break
                try:
                    out.append(chr(int(s[i + 2:i + 6], 16)))
                except ValueError:
                    pass
                i += 6
                continue
            out.append(_ESCAPES.get(nxt, nxt))
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def early_actions(partial_json: str) -> list | None:
    """The "actions" list, once it's complete and the "reply" has begun (the schema puts actions
    first), so JARVIS can act before it says anything. None if not known yet."""
    m = _REPLY_START.search(partial_json)
    if not m:
        return None
    head = partial_json[:m.start()].rstrip().rstrip(",")
    try:
        out = json.loads(head + "}")
    except ValueError:
        return None
    actions = out.get("actions") if isinstance(out, dict) else None
    return actions if isinstance(actions, list) else None


def one_off(model: str, system: str, schema: dict, prompt: str,
            on_text: Callable[[str], None] | None = None, on_actions: Callable[[list], None] | None = None) -> dict:
    """A single streamed call on a fresh process (used for the heavy model, which isn't kept
    warm to save RAM)."""
    from jarvis.chat import find_claude
    session = Session(_argv(find_claude(), model, system, schema), cwd=config.DATA_DIR)
    try:
        return session.ask(prompt, on_text=on_text, on_actions=on_actions)
    finally:
        session.close()


def _argv(claude: str, model: str, system: str, schema: dict) -> list[str]:
    return [claude, "-p", "--model", model, "--tools", "", "--strict-mcp-config",
            "--setting-sources", "", "--no-session-persistence", "--system-prompt", system,
            "--json-schema", json.dumps(schema), "--input-format", "stream-json",
            "--output-format", "stream-json", "--verbose", "--include-partial-messages"]


class Session:
    """One running `claude -p --input-format stream-json` process."""

    def __init__(self, argv: list[str], cwd=None):
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True, bufsize=1, cwd=cwd)
        self.lines: queue.Queue = queue.Queue()
        self.turns = 0
        self.last_used = time.time()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        for line in self.proc.stdout:
            self.lines.put(line)
        self.lines.put(None)  # process ended

    def alive(self) -> bool:
        return self.proc.poll() is None

    def ask(self, text: str | list, timeout: float = REPLY_TIMEOUT, on_text: Callable[[str], None] | None = None,
            on_actions: Callable[[list], None] | None = None) -> dict:
        """Send one message; return the structured reply. `text` may also be a list of content
        blocks (an image plus text). `on_text` gets the "reply" text so far as it streams (needs
        --include-partial-messages); `on_actions` gets the actions once, as soon as they're complete
        and before any reply text (only when the model wrote them first)."""
        if not self.alive():
            raise SessionDied("session process has exited")
        try:
            self.proc.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError):
            raise SessionDied("session stdin closed") from None
        deadline = time.time() + timeout
        partial, shown, acted = "", "", on_actions is None
        while True:
            try:
                line = self.lines.get(timeout=max(0.1, deadline - time.time()))
            except queue.Empty:
                self.close()
                raise SessionDied("no reply in time") from None
            if line is None:
                raise SessionDied("session process ended mid-reply")
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (on_text or on_actions) and event.get("type") == "stream_event":
                delta = (event.get("event") or {}).get("delta") or {}
                if delta.get("type") == "input_json_delta":
                    partial += delta.get("partial_json", "")
                    if not acted and (found := early_actions(partial)) is not None:
                        acted = True
                        on_actions(found)
                    if not on_text:
                        continue
                    so_far = partial_reply(partial)
                    if so_far != shown:
                        shown = so_far
                        on_text(so_far)
                continue
            if event.get("type") != "result":
                continue
            self.turns += 1
            self.last_used = time.time()
            if event.get("is_error") or not event.get("structured_output"):
                raise RuntimeError(f"Claude error: {str(event.get('result'))[:300]}")
            return event["structured_output"]

    def close(self) -> None:
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.terminate()
        except OSError:
            pass


class WarmClaude:
    """Drop-in runner for chat.respond: run(system, prompt) -> structured dict."""

    def __init__(self, system: str, schema: dict, model: str | None = None,
                 fallback: Callable[[str, str], dict] | None = None, argv_for=None, log=None):
        self.system, self.schema = system, schema
        self.model = model or config.JARVIS_MODEL
        self.fallback = fallback
        self.log = log or (lambda s: None)
        self._argv_for = argv_for or self._default_argv
        self._lock = threading.Lock()
        self._current: Session | None = None
        self._next: Session | None = None
        self._spawn_next()  # warm one up right away

    def _default_argv(self) -> list[str]:
        from jarvis.chat import find_claude
        return _argv(find_claude(), self.model, self.system, self.schema)

    def _spawn_next(self) -> None:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._next = Session(self._argv_for(), cwd=config.DATA_DIR)

    def _session(self) -> Session:
        cur = self._current
        stale = cur is None or not cur.alive() or cur.turns >= MAX_TURNS or time.time() - cur.last_used > MAX_IDLE
        if stale:
            if cur:
                cur.close()
            if self._next is None or not self._next.alive():
                self._spawn_next()
            self._current, self._next = self._next, None
        return self._current

    def run(self, system: str, prompt: str, on_text: Callable[[str], None] | None = None,
            on_actions: Callable[[list], None] | None = None) -> dict:
        with self._lock:
            try:
                session = self._session()
                out = session.ask(prompt, on_text=on_text, on_actions=on_actions)
                # RAM is tight (8 GB), so a spare only warms up when this one is about to retire.
                if session.turns >= MAX_TURNS - 1 and self._next is None:
                    threading.Thread(target=self._spawn_next, daemon=True).start()
                return out
            except SessionDied as e:
                self.log(f"warm session failed ({e}); using a one-off call")
                if self._current:
                    self._current.close()
                self._current = None
                if self.fallback is None:
                    raise
                return self.fallback(system, prompt)

    def close(self) -> None:
        for s in (self._current, self._next):
            if s:
                s.close()
