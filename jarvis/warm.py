"""A warm, persistent Claude session for chat replies.

Starting `claude -p` per message costs ~2-3.5 s before Claude even thinks. A persistent
stream-json session answers in ~1-2.5 s. Sessions remember earlier turns, so each one is
retired after MAX_TURNS messages or MAX_IDLE of silence, with the next one already started
in the background. Any failure falls back to a normal one-off call, so a message is never
lost to the warm path.
"""

import json
import queue
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

    def ask(self, text: str, timeout: float = REPLY_TIMEOUT) -> dict:
        if not self.alive():
            raise SessionDied("session process has exited")
        try:
            self.proc.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError):
            raise SessionDied("session stdin closed") from None
        deadline = time.time() + timeout
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
        return [find_claude(), "-p", "--model", self.model, "--tools", "", "--strict-mcp-config",
                "--setting-sources", "", "--no-session-persistence", "--system-prompt", self.system,
                "--json-schema", json.dumps(self.schema), "--input-format", "stream-json",
                "--output-format", "stream-json", "--verbose"]

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

    def run(self, system: str, prompt: str) -> dict:
        with self._lock:
            try:
                session = self._session()
                out = session.ask(prompt)
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
