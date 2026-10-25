"""Before each chat message, decide (1) which context it needs, so JARVIS doesn't gather or
send everything every time, (2) whether it needs the heavy model (Sonnet) or the quick
one (Haiku), and (3) whether to peek at his screen first. One fast Jev call answers all three;
it leans toward including context and looking, since a missing block hurts more than an extra
one. Without Jev, every block is included, JARVIS looks, and a keyword check picks the model.
The screen peek (3) only happens with PRE_PEEK=1; otherwise looks are loop steps the chat model asks for."""

import re
from dataclasses import dataclass

from jarvis import config
from jarvis.judge import SCREEN_ASK, Engine, Question, screen_look

BLOCKS = {
    "deadlines": "Would answering this need his school deadlines, due dates, classes or schedule?",
    "inbox": "Would answering this need his recent email (inbox, messages from professors, mail)?",
    "activity": "Is this about what he's doing right now on his Mac (this error, this screen, his "
                "terminal, the code or page in front of him, 'this'/'that')?",
    "projects": "Is this about one of his coding/business/personal projects or repos?",
    "journal": "Does this refer to earlier days, his week so far, or things that happened before "
               "this conversation?",
}
HEAVY_ASK = ("Does this need careful, substantial work: drafting or rewriting an email or document, "
             "planning, debugging code or an error, multi-step reasoning, or a long detailed answer? "
             "(Small talk, quick facts, confirmations, reminders and short questions do not.)")
HEAVY_WORDS = re.compile(
    r"\b(email|e-mail|draft|write|rewrite|plan|planning|debug|error|traceback|exception|fix|why|explain|"
    r"essay|outline|compare|research|analy[sz]e|code|bug|stack ?trace)\b|https?://", re.IGNORECASE)
INCLUDE_AT = 0.3   # probability of "yes" above which a block is included
HEAVY_AT = 0.5
ALL = frozenset(BLOCKS)


@dataclass(frozen=True)
class Route:
    blocks: frozenset[str]
    heavy: bool
    how: str  # "jev" | "keywords" (no Jev, or it failed)
    look: bool = True    # peek at his screen privately before answering (Jev's call; unsure = yes)
    look_why: str = ""


def _p_yes(answer) -> float:
    return answer.confidence if answer.value else 1 - answer.confidence


def heavy_by_keywords(message: str) -> bool:
    return len(message) > 280 or bool(HEAVY_WORDS.search(message))


# Playback commands answered without a model round trip (see media.py). The whole message has to be
# the command ("pause", "skip this song", "volume 40", "queue Get Lucky"), so anything longer or
# different ("pause heartbeat", "stop", "play some Daft Punk") goes to chat as usual.
_POLITE = re.compile(r"^(?:(?:hey\s+)?jarvis|ok|okay|please|can you|could you|would you)[\s,]+|"
                     r"[\s,]+(?:please|sir|jarvis|thanks|thank you)$", re.I)
_UNIT = r"(?:the\s+)?(?:music|song|track|spotify|playback|it|this(?:\s+song)?)"
MEDIA_PHRASES: list[tuple[re.Pattern, str, object]] = [(re.compile(p, re.I), op, v) for p, op, v in [
    (rf"pause(?:\s+{_UNIT})?|hold\s+(?:the\s+)?music", "pause", None),
    (rf"(?:resume|unpause|continue)(?:\s+{_UNIT})?|play|keep\s+playing|play\s+(?:the\s+)?music", "play", None),
    (rf"(?:skip|next)(?:\s+{_UNIT})?|next\s+one|skip\s+ahead", "next", None),
    (r"previous(?:\s+(?:song|track))?|go\s+back(?:\s+a\s+(?:song|track))?|(?:play\s+)?(?:the\s+)?(?:last|previous)\s+(?:song|track)(?:\s+again)?", "previous", None),
    (r"(?:turn\s+(?:it|the\s+(?:music|volume))\s+up|volume\s+up|louder)", "volume_up", None),
    (r"(?:turn\s+(?:it|the\s+(?:music|volume))\s+down|volume\s+down|quieter|softer)", "volume_down", None),
    (r"mute(?:\s+(?:it|the\s+music))?", "mute", None),
    (r"unmute(?:\s+(?:it|the\s+music))?", "unmute", None),
    (r"(?:turn\s+on\s+)?shuffle(?:\s+on)?|shuffle\s+(?:it|the\s+music)", "shuffle", "on"),
    (r"(?:turn\s+off\s+shuffle|shuffle\s+off|stop\s+shuffling|no\s+shuffle)", "shuffle", "off"),
    (r"repeat\s+(?:this(?:\s+song)?|the\s+song|(?:this\s+)?track|one)|put\s+(?:this|it)\s+on\s+repeat", "repeat", "track"),
    (r"(?:turn\s+on\s+)?repeat(?:\s+on)?|repeat\s+(?:the\s+)?(?:playlist|album|all)", "repeat", "context"),
    (r"(?:turn\s+off\s+repeat|repeat\s+off|stop\s+repeating|no\s+repeat)", "repeat", "off"),
    (rf"(?:i\s+)?(?:like|love|save)\s+{_UNIT}|add\s+this(?:\s+song)?\s+to\s+(?:my\s+)?liked\s+songs", "like", None),
    (r"what(?:'s|\s+is)\s+(?:playing|this\s+song|that\s+song)|what\s+song\s+is\s+(?:this|that|playing)"
     r"|who(?:'s|\s+is)\s+(?:this|singing(?:\s+this)?)|what\s+am\s+i\s+listening\s+to|now\s+playing", "now_playing", None),
]]
_VOLUME = re.compile(r"(?:set\s+)?(?:the\s+)?volume\s+(?:to\s+|at\s+)?(\d{1,3})(?:\s*(?:%|percent))?", re.I)
_SEEK = re.compile(r"(?:skip|jump|go|seek|fast\s+forward)\s+to\s+(?:(\d{1,2}):(\d{2})|(\d{1,3})\s*(seconds?|minutes?))", re.I)
_QUEUE = re.compile(r"(?:queue(?:\s+up)?\s+(?P<a>.+?)|add\s+(?P<b>.+?)\s+to\s+(?:the\s+|my\s+)?queue|play\s+(?P<c>.+?)\s+next)", re.I)


def media_command(text: str) -> tuple[str, object] | None:
    """(op, value) for media.media_control when the whole message is a playback command, else None."""
    t = re.sub(r"[.!?]+$", "", " ".join(str(text).replace("’", "'").split())).strip()
    for _ in range(3):
        t = _POLITE.sub("", t).strip(" ,")
    if not t or len(t) > 120:
        return None
    if m := _VOLUME.fullmatch(t):
        n = int(m[1])
        return ("volume", n) if n <= 100 else None
    if m := _SEEK.fullmatch(t):
        if m[1]:
            return "seek", int(m[1]) * 60 + int(m[2])
        return "seek", int(m[3]) * (60 if m[4].lower().startswith("minute") else 1)
    for pattern, op, value in MEDIA_PHRASES:
        if pattern.fullmatch(t):
            return op, value
    if m := _QUEUE.fullmatch(t):
        song = (m["a"] or m["b"] or m["c"]).strip()
        if song.lower() not in ("it", "this", "that", "something", "a song", "music") and len(song) <= 200:
            return "queue", song
    return None


PRE_PEEK_OFF = (False, "pre-peek off (PRE_PEEK=0); looks only when the chat model asks")


def route(message: str, recent: list[str], fast: Engine | None, on_error=None) -> Route:
    forced = config.JARVIS_MODEL_ROUTING
    pre_peek = config.PRE_PEEK
    def finish(blocks, heavy, how, look=(True, "no judgment from Jev; looking to be safe")):
        if forced in ("fast", "heavy"):
            heavy = forced == "heavy"
        return Route(frozenset(blocks), heavy, how, *(look if pre_peek else PRE_PEEK_OFF))

    if fast is None:
        return finish(ALL, heavy_by_keywords(message), "keywords")
    state = "Recent conversation:\n" + ("\n".join(recent[-4:]) or "(none)") + f"\n\nNew message from Elijah:\n{message}"
    questions = ([Question(k, ask) for k, ask in BLOCKS.items()] + [Question("heavy", HEAVY_ASK)]
                 + ([Question("look_at_screen", SCREEN_ASK)] if pre_peek else []))
    try:
        answers = fast(state, questions)
    except Exception as e:
        if on_error:
            on_error(e)
        return finish(ALL, heavy_by_keywords(message), "keywords")
    blocks = {k for k in BLOCKS if _p_yes(answers[k]) >= INCLUDE_AT}
    return finish(blocks, _p_yes(answers["heavy"]) >= HEAVY_AT, "jev", screen_look(answers))


# Other things that need no model at all: "play Daft Punk", "set a timer for 5 minutes", "wake me at 7:30",
# "what time is it". Whole-message only, like media_command; anything vaguer goes to chat.
_NUM_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
              "eight": 8, "nine": 9, "ten": 10, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40,
              "forty-five": 45, "sixty": 60, "ninety": 90}
_AMOUNT = r"(\d{1,3}|" + "|".join(sorted(map(re.escape, _NUM_WORDS), key=len, reverse=True)) + r")"
_UNITS = r"(seconds?|secs?|minutes?|mins?|hours?|hrs?)"
_TIMER = re.compile(rf"(?:set\s+|start\s+)?(?:a\s+|an\s+)?(?:timer\s+(?:for\s+)?{_AMOUNT}[\s-]*{_UNITS}"
                    rf"|{_AMOUNT}[\s-]*{_UNITS}\s+timer)", re.I)
_ALARM = re.compile(r"(?:set\s+(?:an?\s+)?alarm\s+(?:for|at)|wake\s+me(?:\s+up)?\s+at|alarm\s+(?:for|at))\s+"
                    r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?", re.I)
_PLAY = re.compile(r"(?:play|put\s+on|throw\s+on)\s+(?:some\s+|me\s+some\s+)?(?P<q>.+?)(?:\s+on\s+spotify)?", re.I)
_TIME = re.compile(r"what(?:'s|\s+is)\s+the\s+time|what\s+time\s+is\s+it(?:\s+now)?|time\s+check", re.I)
_DATE = re.compile(r"what(?:'s|\s+is)\s+(?:the\s+|today'?s\s+)?date(?:\s+today)?|what\s+day\s+is\s+(?:it|today)"
                   r"|what(?:'s|\s+is)\s+today", re.I)
_NOT_A_QUERY = {"it", "this", "that", "something", "music", "a song", "some music", "the music", "again", "",
                "something else", "anything"}


MODELS = {"haiku": "haiku", "sonnet": "sonnet", "opus": "opus", "fast": "haiku", "quick": "haiku",
          "smart": "sonnet", "heavy": "sonnet", "smarter": "sonnet", "auto": "auto", "automatic": "auto"}
_MODEL = re.compile(r"(?:(?:switch|change|go)\s+(?:back\s+)?to|use|go\s+back\s+to|back\s+to)\s+(?:the\s+)?"
                    r"(?P<m>haiku|sonnet|opus|fast|quick|smart|smarter|heavy|auto|automatic)(?:\s+model)?"
                    r"(?:\s+(?:for\s+(?:now|chat|replies|this))|\s+again)?"
                    r"|(?:pick|choose)\s+the\s+model\s+(?:yourself|automatically)", re.I)


def chosen_model(name: str) -> str | None:
    return MODELS.get(str(name or "").strip().lower())


def _amount(word: str) -> int:
    return int(word) if word.isdigit() else _NUM_WORDS[word.lower()]


def instant_command(text: str) -> tuple | None:
    """("play", query) | ("timer", seconds) | ("alarm", "HH:MM") | ("time",) | ("date",), or None."""
    t = re.sub(r"[.!?]+$", "", " ".join(str(text).replace("’", "'").split())).strip()
    for _ in range(3):
        t = _POLITE.sub("", t).strip(" ,")
    if not t or len(t) > 120:
        return None
    if m := _MODEL.fullmatch(t):
        return ("model", MODELS[m["m"].lower()] if m["m"] else "auto")
    if _TIME.fullmatch(t):
        return ("time",)
    if _DATE.fullmatch(t):
        return ("date",)
    if m := _TIMER.fullmatch(t):
        groups = [g for g in m.groups() if g]
        n, unit = _amount(groups[0]), groups[1].lower()
        seconds = n * (3600 if unit.startswith("h") else 60 if unit.startswith("m") else 1)
        return ("timer", seconds) if 1 <= seconds <= 24 * 3600 else None
    if m := _ALARM.fullmatch(t):
        hour, minute, half = int(m[1]), int(m[2] or 0), (m[3] or "").replace(".", "").lower()
        if not half and hour <= 12:
            return None  # "alarm for 6": morning or evening is the model's call (or his)
        if half:
            if not 1 <= hour <= 12:
                return None
            hour = hour % 12 + (12 if half == "pm" else 0)
        return ("alarm", f"{hour:02d}:{minute:02d}") if hour <= 23 and minute <= 59 else None
    if m := _PLAY.fullmatch(t):
        q = m["q"].strip(" ,")
        if q.lower() in _NOT_A_QUERY or re.search(r"\b(?:video|youtube|movie|game|episode)\b", q, re.I):
            return None
        return ("play", q) if len(q) <= 100 else None
    return None


# The panel (JARVIS's own screen). "go back" / "bigger" only mean the panel while it's showing.
_PANEL_SHOT = re.compile(r"(?:show|bring\s+up|put\s+up|open)\s+(?:me\s+)?(?:the\s+|that\s+|my\s+)?(?:last\s+|latest\s+)?"
                         r"screen\s?shot(?:\s+(?:from\s+)?(?:earlier|before|again))?(?:\s+on\s+(?:the|your)\s+(?:screen|panel))?", re.I)
_PANEL_OPS = [
    (re.compile(r"(?:close|hide|dismiss|clear)\s+(?:that|it|this|the\s+panel|the\s+window|your\s+screen)|close\s+panel", re.I), "close"),
    (re.compile(r"(?:make\s+(?:it|that)\s+)?(?:bigger|larger)|zoom\s+in|enlarge\s+(?:it|that)", re.I), "bigger"),
    (re.compile(r"(?:make\s+(?:it|that)\s+)?smaller|zoom\s+out|shrink\s+(?:it|that)", re.I), "smaller"),
    (re.compile(r"go\s+back|(?:show\s+(?:me\s+)?)?(?:the\s+)?previous\s+one|back", re.I), "back"),
]


_CAMERA_OFF = re.compile(r"(?:close|hide|dismiss|shut|stop|end|turn\s+off|switch\s+off)\s+(?:the\s+|your\s+)?"
                         r"(?:camera|webcam|preview)(?:\s+(?:preview|window|feed))?(?:\s+off)?"
                         r"|(?:turn|switch|shut)\s+(?:the\s+)?(?:camera|webcam)\s+off|(?:camera|webcam|preview)\s+off", re.I)
_CLOSE_IT = re.compile(r"(?:close|hide|dismiss)\s+(?:that|it|this|the\s+window)", re.I)


def camera_command(text: str, open_: bool) -> str | None:
    """'close' when the whole message asks to close the camera preview ("close the camera"); "close that"
    only counts while the preview is up (the panel, when showing, gets it first)."""
    t = re.sub(r"[.!?]+$", "", " ".join(str(text).replace("’", "'").split())).strip()
    for _ in range(3):
        t = _POLITE.sub("", t).strip(" ,")
    if not t or len(t) > 80:
        return None
    if _CAMERA_OFF.fullmatch(t) or (open_ and _CLOSE_IT.fullmatch(t)):
        return "close"
    return None


def panel_command(text: str, showing: bool) -> str | None:
    """'close' | 'bigger' | 'smaller' | 'back' (only while the panel shows something) | 'screenshot'."""
    t = re.sub(r"[.!?]+$", "", " ".join(str(text).replace("’", "'").split())).strip()
    for _ in range(3):
        t = _POLITE.sub("", t).strip(" ,")
    if not t or len(t) > 80:
        return None
    if _PANEL_SHOT.fullmatch(t):
        return "screenshot"
    if showing:
        for pattern, op in _PANEL_OPS:
            if pattern.fullmatch(t):
                return op
    return None
