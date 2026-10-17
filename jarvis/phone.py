"""Calling JARVIS from your cell phone.

Twilio answers the call and posts to a small webhook server on this Mac, reached through a
tunnel (README, "Calling JARVIS"). Twilio's speech recognition turns what you say into text,
the text goes through the normal chat pipeline, and the reply is rendered in JARVIS's own
voice (macOS `say`, as at the desk), which Twilio fetches and plays down the line.

Only Elijah gets through: every request must carry a valid Twilio signature, calls from any
other number are rejected before they're answered, and an optional PIN guards against
caller-ID spoofing.
"""

import base64
import hashlib
import hmac
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qsl, urlsplit
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

from jarvis import config
from jarvis.voice import URL, speakable

FALLBACK_VOICE = "Polly.Brian"  # Twilio's British voice, used only if our own audio fails
MAX_SPOKEN = 300                # about 20 seconds aloud; the full reply goes to Telegram
FIRST_WAIT = 9.0                # seconds to hold Twilio's request for a reply (it gives up at 15)
WAIT = 10.0                     # ...and on each "still thinking" redirect after that
TURN_LIMIT = 120.0              # then stop waiting and send the answer to Telegram instead
MAX_MISSES = 3                  # silent turns in a row before hanging up
PIN_TRIES = 2
CALL_IDLE = 600                 # forget a call (and close its Claude session) after this
AUDIO_TTL = 600
MAX_BODY = 64 * 1024
ROUTES = {"/voice/incoming": "_incoming", "/voice/pin": "_pin", "/voice/heard": "_heard",
          "/voice/wait": "_wait", "/voice/status": "_status"}
ENDED = {"completed", "busy", "failed", "no-answer", "canceled"}
GOODBYE = re.compile(r"(ok(ay)? )?(thanks |thank you )?(jarvis )?(bye|goodbye|bye bye|that's all|"
                     r"that's it|that will be all|that'll be all|hang up)( jarvis| sir| thanks| thank you)?")

NOT_FOUND = (404, "text/plain", b"not found")


def signature(url: str, params: dict[str, str], token: str) -> str:
    """Twilio's X-Twilio-Signature: HMAC-SHA1 of the full URL plus the POST fields sorted by name."""
    data = url + "".join(k + v for k, v in sorted(params.items()))
    return base64.b64encode(hmac.new(token.encode(), data.encode(), hashlib.sha1).digest()).decode()


def valid_signature(url: str, params: dict[str, str], given: str | None, token: str) -> bool:
    return bool(given and token) and hmac.compare_digest(signature(url, params, token), given)


def _digits(number: str) -> str:
    return re.sub(r"[^\d+]", "", number or "")


def allowed_caller(params: dict[str, str], owner: str) -> bool:
    """Elijah's own number only. A carrier that checked the caller ID and found it forged
    (STIR/SHAKEN "TN-Validation-Failed") is refused even if the number matches."""
    if not owner or _digits(params.get("From", "")) != _digits(owner):
        return False
    return not params.get("StirVerstat", "").startswith("TN-Validation-Failed")


def masked(number: str) -> str:
    d = _digits(number)
    return f"…{d[-2:]}" if len(d) > 2 else "unknown"


def is_goodbye(text: str) -> bool:
    words = " ".join(re.sub(r"[^\w' ]", " ", text.lower()).split())
    return bool(GOODBYE.fullmatch(words))


def spoken_reply(reply: str) -> tuple[str, bool]:
    """What to say on the phone, and whether something was left out (long, a list, links)."""
    said = speakable(reply, limit=MAX_SPOKEN)
    whole = speakable(reply, limit=len(reply) * 2 + 100)
    return said, len(said) < len(whole) or bool(URL.search(reply))


def greeting(now: datetime | None = None) -> str:
    hour = (now or datetime.now(ZoneInfo(config.TIMEZONE))).hour
    return f"Good {'morning' if hour < 12 else 'afternoon' if hour < 18 else 'evening'}, sir."


def twiml(*verbs: str) -> str:
    return '<?xml version="1.0" encoding="UTF-8"?><Response>' + "".join(verbs) + "</Response>"


@dataclass
class Call:
    sid: str
    seen: float
    authorized: bool = False
    pin_tries: int = 0
    misses: int = 0
    session: Any = None  # whatever start_call made (a warm Claude session)


@dataclass
class Turn:
    id: str
    call_sid: str
    started: float
    done: threading.Event = field(default_factory=threading.Event)
    play: str = ""      # the reply, as a TwiML verb
    late: bool = False  # the caller stopped waiting: the answer goes to Telegram


class PhoneLine:
    """The call logic, separate from HTTP. `answer(text, session) -> reply` runs one chat turn;
    `render(text) -> WAV bytes` makes JARVIS's voice; `send_text` (optional) puts full replies
    in Telegram; `start_call()` / `end_call(session)` bracket each call."""

    def __init__(self, answer: Callable[[str, Any], str], render: Callable[[str], bytes],
                 public_url: str, auth_token: str, owner: str, pin: str = "",
                 send_text: Callable[[str], None] | None = None,
                 start_call: Callable[[], Any] = lambda: None, end_call: Callable[[Any], None] = lambda s: None,
                 log: Callable[[str], None] = lambda s: None, clock: Callable[[], float] = time.time,
                 first_wait: float = FIRST_WAIT, wait: float = WAIT):
        if not (public_url and auth_token and owner):
            raise ValueError("needs the public URL, the Twilio auth token and your phone number")
        if pin and not pin.isdigit():
            raise ValueError("PHONE_PIN must be digits only")
        self.base, self.token, self.owner, self.pin = public_url.rstrip("/"), auth_token, owner, pin
        self.answer, self.render, self.send_text = answer, render, send_text
        self.start_call, self.end_call, self.log, self.clock = start_call, end_call, log, clock
        self.first_wait, self.wait = first_wait, wait
        self.calls: dict[str, Call] = {}
        self.turns: dict[str, Turn] = {}
        self.audio: dict[str, tuple[bytes, float]] = {}
        self.phrases: dict[str, bytes] = {}  # fixed lines, rendered once
        self._lock = threading.Lock()
        self._answering = threading.Lock()  # one answer at a time: one Claude session, one DB writer

    # --- HTTP entry point -------------------------------------------------------------------

    def handle(self, method: str, target: str, params: dict[str, str], sig: str | None) -> tuple[int, str, bytes]:
        """One request: `target` is the path with its query string, as received."""
        self.sweep()
        parts = urlsplit(target)
        if method == "GET":
            m = re.fullmatch(r"/voice/audio/([\w-]{20,})\.wav", parts.path)
            clip = self.audio.get(m[1]) if m else None
            # Twilio doesn't sign media fetches; the address is a random, short-lived token.
            return (200, "audio/wav", clip[0]) if clip else NOT_FOUND
        if method != "POST" or parts.path not in ROUTES:
            return NOT_FOUND
        if not valid_signature(self.base + target, params, sig, self.token):
            self.log(f"refused {parts.path}: no valid Twilio signature")
            return 403, "text/plain", b"forbidden"
        if not allowed_caller(params, self.owner):
            self.log(f"rejected a call from {masked(params.get('From', ''))}")
            self._drop(params.get("CallSid", ""))
            return self._xml(twiml('<Reject reason="rejected"/>'))
        try:
            return self._xml(getattr(self, ROUTES[parts.path])(params, dict(parse_qsl(parts.query))))
        except Exception as e:  # say something rather than Twilio's "application error"
            self.log(f"error on {parts.path}: {e}")
            return self._xml(twiml(f'<Say voice="{FALLBACK_VOICE}">Sorry, sir, something went wrong on my end.</Say>',
                                   "<Hangup/>"))

    @staticmethod
    def _xml(body: str) -> tuple[int, str, bytes]:
        return 200, "text/xml", body.encode()

    # --- routes -----------------------------------------------------------------------------

    def _incoming(self, p: dict, q: dict) -> str:
        sid = p.get("CallSid", "")
        with self._lock:
            call = self.calls.setdefault(sid, Call(sid, self.clock()))
        self.log("answered a call from Elijah")
        if self.pin and not call.authorized:
            return twiml(self._ask_pin("Passcode, please."))
        return self._welcome(call)

    def _pin(self, p: dict, q: dict) -> str:
        call = self.calls.get(p.get("CallSid", ""))
        if call is None:
            return twiml("<Hangup/>")
        if call.authorized or (self.pin and hmac.compare_digest(p.get("Digits", "").encode(), self.pin.encode())):
            return self._welcome(call)
        call.pin_tries += 1
        self.log("wrong passcode")
        if call.pin_tries >= PIN_TRIES:
            self._drop(call.sid)
            return twiml(self._say("Goodbye.", fixed=True), "<Hangup/>")
        return twiml(self._ask_pin("That's not it. Once more."))

    def _heard(self, p: dict, q: dict) -> str:
        call = self._authorized(p)
        if call is None:
            return twiml("<Hangup/>")
        text = " ".join(p.get("SpeechResult", "").split())
        if not text:
            call.misses += 1
            if call.misses >= MAX_MISSES:
                return self._goodbye(call, "I'll let you go, sir.")
            return twiml(self._listen(self._say("Sorry, sir, I didn't catch that.", fixed=True)))
        call.misses = 0
        if is_goodbye(text):
            return self._goodbye(call, "Goodbye, sir.")
        turn = Turn(secrets.token_urlsafe(12), call.sid, self.clock())
        self.turns[turn.id] = turn
        threading.Thread(target=self._work, args=(call, turn, text), daemon=True).start()
        return self._await(turn, self.first_wait, first=True)

    def _wait(self, p: dict, q: dict) -> str:
        call = self._authorized(p)
        if call is None:
            return twiml("<Hangup/>")
        turn = self.turns.get(q.get("turn", ""))
        if turn is None or turn.call_sid != call.sid:
            return twiml(self._listen(self._say("Sorry, sir, I lost that one. Go on.", fixed=True)))
        return self._await(turn, self.wait, first=False)

    def _status(self, p: dict, q: dict) -> str:
        if p.get("CallStatus") in ENDED:
            self._drop(p.get("CallSid", ""))
        return twiml()

    # --- pieces -----------------------------------------------------------------------------

    def _welcome(self, call: Call) -> str:
        if not call.authorized:
            call.authorized = True
            try:
                call.session = self.start_call()  # warms up while the greeting plays
            except Exception as e:
                self.log(f"couldn't start a chat session ({e}); answering without one")
        return twiml(self._listen(self._say(greeting(), fixed=True)))

    def _authorized(self, p: dict) -> Call | None:
        call = self.calls.get(p.get("CallSid", ""))
        if call is None or not call.authorized:
            return None
        call.seen = self.clock()
        return call

    def _listen(self, prompt: str) -> str:
        """Play `prompt` (he can talk over it), then hear what he says."""
        return (f'<Gather input="speech" action="{escape(self.base)}/voice/heard" method="POST" '
                'speechTimeout="auto" timeout="8" language="en-US" speechModel="phone_call" enhanced="true" '
                f'actionOnEmptyResult="true">{prompt}</Gather>')

    def _ask_pin(self, prompt: str) -> str:
        return (f'<Gather input="dtmf" numDigits="{len(self.pin)}" timeout="10" action="{escape(self.base)}/voice/pin" '
                f'method="POST" actionOnEmptyResult="true">{self._say(prompt, fixed=True)}</Gather>')

    def _say(self, text: str, fixed: bool = False) -> str:
        """A <Play> of `text` in JARVIS's voice, or Twilio's own voice if rendering fails."""
        try:
            clip = self.phrases.get(text) if fixed else None
            if clip is None:
                clip = self.render(text)
                if fixed:
                    self.phrases[text] = clip
        except Exception as e:
            self.log(f"couldn't render audio ({e}); using Twilio's voice")
            return f'<Say voice="{FALLBACK_VOICE}">{escape(text)}</Say>'
        token = secrets.token_urlsafe(24)
        self.audio[token] = (clip, self.clock())
        return f"<Play>{escape(self.base)}/voice/audio/{token}.wav</Play>"

    def _await(self, turn: Turn, timeout: float, first: bool) -> str:
        if turn.done.wait(timeout):
            self.turns.pop(turn.id, None)
            return twiml(self._listen(turn.play))
        with self._lock:
            if turn.done.is_set():
                self.turns.pop(turn.id, None)
                return twiml(self._listen(turn.play))
            if self.clock() - turn.started > TURN_LIMIT:
                turn.late = True
                self.turns.pop(turn.id, None)
                return twiml(self._listen(self._say(
                    "That's taking too long, sir. I'll send the answer to Telegram.", fixed=True)))
        hold = self._say("One moment, sir.", fixed=True) if first else '<Pause length="1"/>'
        return twiml(hold, f'<Redirect method="POST">{escape(self.base)}/voice/wait?turn={turn.id}</Redirect>')

    def _work(self, call: Call, turn: Turn, text: str) -> None:
        with self._answering:
            try:
                reply = self.answer(text, call.session)
            except Exception as e:
                self.log(f"answer failed: {e}")
                reply = "Sorry, sir, I couldn't think just now."
        said, cut = spoken_reply(reply)
        sent = cut and self._to_telegram(reply)
        if sent:
            said += " The rest is in Telegram, sir."
        play = self._say(said)
        with self._lock:
            late = turn.late  # the caller stopped waiting, so this answer is only text now
            if not late:
                turn.play = play
                turn.done.set()
        if late and not sent:
            self._to_telegram(reply)

    def _to_telegram(self, reply: str) -> bool:
        if not self.send_text:
            return False
        try:
            self.send_text(f"From our call:\n{reply}")
            return True
        except Exception as e:
            self.log(f"couldn't send the full reply to Telegram: {e}")
            return False

    def _goodbye(self, call: Call, text: str) -> str:
        self._drop(call.sid)
        return twiml(self._say(text, fixed=True), "<Hangup/>")

    def _drop(self, sid: str) -> None:
        with self._lock:
            call = self.calls.pop(sid, None)
        if call and call.session is not None:
            threading.Thread(target=self.end_call, args=(call.session,), daemon=True).start()
            self.log("call ended")

    def sweep(self) -> None:
        """Forget idle calls (closing their sessions), stale turns and old audio."""
        now = self.clock()
        for sid in [c.sid for c in list(self.calls.values()) if now - c.seen > CALL_IDLE]:
            self._drop(sid)
        for key in [k for k, (_, at) in list(self.audio.items()) if now - at > AUDIO_TTL]:
            self.audio.pop(key, None)
        for key in [k for k, t in list(self.turns.items()) if now - t.started > TURN_LIMIT * 2]:
            self.turns.pop(key, None)


def serve(line: PhoneLine, port: int, host: str = "127.0.0.1") -> None:
    """Run the webhook on localhost only; the tunnel is the one way in from outside."""

    class Handler(BaseHTTPRequestHandler):
        server_version, sys_version = "JARVIS", ""

        def do_GET(self):
            self._send(line.handle("GET", self.path, {}, None))

        def do_POST(self):
            try:
                size = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._send((400, "text/plain", b"bad request"))
            if not 0 <= size <= MAX_BODY:
                return self._send((413, "text/plain", b"too large"))
            body = self.rfile.read(size).decode("utf-8", "replace")
            params = dict(parse_qsl(body, keep_blank_values=True))
            self._send(line.handle("POST", self.path, params, self.headers.get("X-Twilio-Signature")))

        def _send(self, response):
            status, ctype, body = response
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):  # PhoneLine logs what matters; skip per-request lines
            pass

    def sweeper():
        while True:
            time.sleep(60)
            line.sweep()

    threading.Thread(target=sweeper, daemon=True).start()
    ThreadingHTTPServer((host, port), Handler).serve_forever()
