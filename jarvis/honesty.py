"""Keeping JARVIS's words in line with what it actually did.

The chat model writes its reply in the same breath as the actions it proposes, so on its own
it can say "Playing study lo-fi now" for music that never started, or "I'm drafting it" with
no draft. Two backstops, used by chat.respond:

- Promises need an action. A sentence that says JARVIS is doing or will do something ("I'm
  checking", "I'll send it", "the draft is above", "it's waiting in Telegram", "the build is
  running") is dropped unless an action in the same reply (or real state) backs it.
- Failures are told plainly. When an action fails, sentences claiming it worked are dropped and
  a templated line says what didn't happen and why.

Streaming replies (spoken at the desk, first sentence sent early in Telegram) go through `Gate`,
which passes on only complete sentences that survive the same checks, so nothing false is
said before the actions are known.
"""

import re
from typing import Callable

from jarvis.speech import _BOUNDARY, _abbreviation

# "I'm checking", "I'll send it", "let me look", "now drafting", "going to text her"
_DOING = (r"draft|send|check|look|redo|revis|rebuild|build|run|start|play|put|pull|set|queu|writ|work|"
          r"fetch|open|text|call|remind|search|find|grab|get|deploy|fix|install|kick|launch|message|email")
PROMISE = re.compile(
    r"\b(?:i'?m|i am|i'?ll|i will|i'?ve|let me|i'?m going to|going to|now|currently)\s+"
    r"(?:just\s+|now\s+|going\s+to\s+|also\s+|quickly\s+|already\s+)*(?:" + _DOING + r")\w*"
    r"|\bon it\b|\bright away\b|\bstand by\b|\bone moment\b|\blet me see\b|\b(?:having|taking) a (?:quick\s+)?look\b", re.I)
# "Playing study lo-fi now." / "Redoing it now." / "Sending it over." (short ones only: "Looking at the
# error, it's a missing import." is an answer, not a promise)
GERUND = re.compile(
    r"^\s*(?:playing|redoing|sending|drafting|checking|looking|starting|putting|pulling|texting|calling|"
    r"opening|fetching|building|deploying|searching|setting|queu(?:e)?ing|pausing|skipping|revising|"
    r"having a look|taking a look)\b", re.I)
# Claims about things that exist somewhere else, which need a matching action or real state.
DRAFT_CLAIM = re.compile(r"\bdraft(?:'s|\s+is)\s+(?:above|below|ready|up|in\s+telegram|there)\b|"
                         r"\b(?:drafted|wrote)\s+(?:it|the\s+(?:email|text|message|reply))\b", re.I)
WAITING_CLAIM = re.compile(r"\b(?:waiting|wait|queued|tap|approve)\b[^.!?]*\btelegram\b|"
                           r"\btelegram\b[^.!?]*\b(?:waiting|queued|to approve)\b|"
                           r"\bwaiting (?:for|on) your (?:ok|okay|yes|approval)\b", re.I)
RUNNING_CLAIM = re.compile(r"\b(?:build|task|helper|job|session|deploy)\b[^.!?]*\b(?:is|are|'s)\s+"
                           r"(?:running|underway|going|in progress|started|building)\b", re.I)
# Success words that are false once the matching action failed.
SUCCESS = re.compile(r"\b(?:playing|played|paused|resumed|skipp(?:ed|ing)|sent|set|started|queued|drafted|"
                     r"done|redoing|revising|putting|put it on|scheduled|added|saved|texted|calling|"
                     r"on it|here it is|here you go|here you are)\b", re.I)

QUEUED_KINDS = {"draft_email", "send_text", "make_call", "run_command", "plan_improvement", "revise_plan",
                "rule_change", "rule_revert"}
WORK_KINDS = {"start_task", "plan_improvement", "run_command", "revise_plan"}
FAILED_AS = {
    "play_music": "start the music", "media_control": "do that with the music",
    "send_text": "set up that text", "make_call": "set up that call", "draft_email": "draft that email",
    "revise_plan": "revise the plan", "plan_improvement": "start that plan", "start_task": "start that task",
    "set_timer": "set the timer", "set_alarm": "set the alarm", "remind": "set that reminder",
    "cancel_reminder": "cancel that reminder", "run_command": "run that command", "attach_image": "send that picture",
    "add_deadline": "add that deadline", "deadline_done": "mark that done", "show": "show that",
    "switch_model": "switch models", "dont_remind": "note that", "deploy_build": "deploy that", "mac": "do that on the Mac", "click": "click that", "panel": "do that with the panel",
    "rule_change": "set up that rule change", "rule_revert": "undo that rule change",
    "watch_create": "set up that watch", "watch_cancel": "stop that watch",
    "look_through_camera": "look through the camera",
}


def sentence_spans(text: str, final: bool = False) -> list[tuple[int, int]]:
    """(start, end) of each complete sentence, its trailing whitespace included. With `final`, the
    unfinished tail counts as a sentence too (the reply is over)."""
    spans, start = [], 0
    for m in _BOUNDARY.finditer(text):
        if m[0] != "\n" and _abbreviation(text[start:m.start() + 1]):
            continue
        end = m.end()
        while end < len(text) and text[end].isspace():
            end += 1
        if text[start:end].strip():
            spans.append((start, end))
        start = end
    if final and text[start:].strip():
        spans.append((start, len(text)))
    return spans


def is_unbacked(sentence: str, kinds: set[str], pending: bool = False, busy: bool = False) -> bool:
    """Does this sentence promise or claim something nothing in this reply (or real state) backs?
    `kinds` are the action types in the same reply; `pending` = something really is waiting for
    his OK; `busy` = a task, job or build really is running."""
    if DRAFT_CLAIM.search(sentence) and not (kinds & {"draft_email", "send_text"}):
        return True
    if WAITING_CLAIM.search(sentence) and not (kinds & QUEUED_KINDS or pending):
        return True
    if RUNNING_CLAIM.search(sentence) and not (kinds & WORK_KINDS or busy):
        return True
    promise = PROMISE.search(sentence) or (GERUND.search(sentence) and len(sentence.split()) <= 8)
    return bool(promise) and not kinds


def failure_line(problems: list[str]) -> str:
    """'Sorry, sir, I couldn't start the music: no active device.' for each failed action."""
    lines = []
    for p in problems:
        kind, _, why = p.partition(": ")
        what = FAILED_AS.get(kind, "do that")
        why = " ".join(why.split())[:160].rstrip(".")
        line = f"I couldn't {what}" + (f": {why}." if why else ".")
        if line not in lines:
            lines.append(line)
    return ("Sorry, sir. " + " ".join(lines)) if lines else ""


def clean(reply: str, kinds: set[str], problems: list[str] = (), pending: bool = False,
          busy: bool = False) -> tuple[str, list[str]]:
    """The reply with unbacked promises dropped and, when actions failed, false success claims
    dropped and the failure said plainly. Returns (text, dropped sentences)."""
    failed = {p.partition(": ")[0] for p in problems}
    keep_kinds = kinds - failed
    kept, dropped = [], []
    for start, end in sentence_spans(reply, final=True):
        sentence = reply[start:end]
        bad = is_unbacked(sentence, keep_kinds, pending, busy) or (
            failed and (SUCCESS.search(sentence) or PROMISE.search(sentence) or GERUND.search(sentence)))
        (dropped if bad else kept).append(sentence)
    text = "".join(kept).strip()
    if problems:
        text = (text + "\n\n" if text else "") + failure_line(problems)
    return text, [s.strip() for s in dropped]


class Gate:
    """Sits between the streaming reply and whoever speaks or sends it early. Passes on complete
    sentences only, and once the reply's actions are known, only sentences they back. If an
    action has already failed, nothing more goes out early (the final reply says what happened)."""

    def __init__(self, forward: Callable[[str], None] | None, pending: bool = False, busy: bool = False,
                 display: Callable[[str], None] | None = None):
        self.forward, self.pending, self.busy = forward, pending, busy
        self.display = display               # on-screen text (HUD, inbox): word by word, can be revised
        self.shown = ""
        self.kinds: set[str] | None = None   # None until the actions are known
        self.failed = False
        self.sent = ""                       # everything passed on so far

    def actions_known(self, actions: list[dict]) -> None:
        self.kinds = {str(a.get("type")) for a in actions if isinstance(a, dict)}

    def feed(self, so_far: str) -> None:
        if self.failed:
            return
        out, done = [], 0
        for start, end in sentence_spans(so_far):
            done = end
            sentence = so_far[start:end]
            if self.kinds is not None and is_unbacked(sentence, self.kinds, self.pending, self.busy):
                continue
            out.append(sentence)
        text = "".join(out)
        if self.forward and text != self.sent and text.startswith(self.sent):
            self.sent = text
            self.forward(text)
        if self.display:
            # The sentence still being written shows too, once the actions are known and it doesn't (yet)
            # promise anything they don't back. Text on a screen can be taken back; spoken words can't.
            tail = so_far[done:]
            if self.kinds is None or is_unbacked(tail, self.kinds, self.pending, self.busy):
                tail = ""
            live = (text + tail).strip()
            if live and live != self.shown:
                self.shown = live
                self.display(live)
