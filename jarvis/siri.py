"""Siri-style actions on this Mac: texts (Messages), calls (FaceTime / iPhone via a tel: link),
timers and alarms (a Clock Shortcut if one is set, otherwise a JARVIS reminder), and music
(the Spotify app, see spotify.py).

Texts and calls reach other people, so they only ever go out after Elijah's yes in Telegram, and
the yes is bound to the exact recipient and text: any change afterwards voids it, and unanswered
requests lapse like command approvals. Alarms, timers and music run at once. The chat model only
proposes these; names are resolved to numbers here, and an unknown or ambiguous name is refused
(JARVIS asks him) rather than guessed.
"""

import hashlib
import json
import re
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone

from jarvis import config
from jarvis.memory import Memory

KINDS = ("send_text", "make_call")   # the ones that wait for his yes
MAX_TEXT = 1000
MAX_TIMER = 24 * 3600

FIELD, RECORD = "\x1f", "\x1e"

TEXT_SCRIPT = """
on run argv
    set target to item 1 of argv
    set msg to item 2 of argv
    tell application "Messages"
        set svc to 1st account whose service type = iMessage
        send msg to participant target of svc
    end tell
end run
"""

CONTACTS_SCRIPT = """
on run argv
    set q to item 1 of argv
    set FS to character id 31
    set RS to character id 30
    set out to ""
    tell application "Contacts"
        repeat with p in (every person whose name contains q)
            repeat with ph in phones of p
                set out to out & (name of p) & FS & (value of ph) & RS
            end repeat
        end repeat
    end tell
    return out
end run
"""

class SiriError(RuntimeError):
    pass


def _osascript(script: str, *args: str, timeout: int = 30) -> str:
    r = subprocess.run(["osascript", "-", *args], input=script, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise SiriError(r.stderr.strip()[:200] or "AppleScript failed")
    return r.stdout.rstrip("\n")


# ----- the Mac actions themselves (only called after the checks below) -----
def send_text(number: str, body: str) -> None:
    _osascript(TEXT_SCRIPT, number, body)


def place_call(number: str) -> None:
    r = subprocess.run(["open", f"tel:{number}"], capture_output=True, text=True, timeout=20)
    if r.returncode != 0:
        raise SiriError(r.stderr.strip()[:200] or "couldn't start the call")


def play_music(query: str, kind: str = "any") -> str:
    """Plays the best Spotify match. Returns what's actually playing."""
    from jarvis import spotify
    return spotify.play(query, kind)


def run_shortcut(name: str, text: str) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".txt") as f:
        f.write(text)
        f.flush()
        r = subprocess.run(["shortcuts", "run", name, "--input-path", f.name],
                           capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise SiriError(r.stderr.strip()[:200] or f"Shortcut {name!r} failed")


def contacts_lookup(name: str) -> list[tuple[str, str]]:
    """(name, number) for every phone of every contact whose name contains `name`."""
    raw = _osascript(CONTACTS_SCRIPT, name)
    return [tuple(rec.split(FIELD)[:2]) for rec in filter(None, raw.split(RECORD)) if FIELD in rec]


# ----- checking what the model proposed -----
PHONE = re.compile(r"\+?\d[\d\s().-]{6,}\d")


def normalize_number(text: str) -> str | None:
    """'(206) 555-0100' -> '+12065550100'; None if it isn't a phone number."""
    text = str(text).strip()
    if not re.fullmatch(r"\+?[\d\s().-]+", text):
        return None
    digits = re.sub(r"\D", "", text)
    if text.startswith("+") and 8 <= len(digits) <= 15:
        return "+" + digits
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return None


def resolve_contact(mem: Memory, who: str, lookup=None) -> tuple[str, str]:
    """(display name, number) for a number or a name from saved facts or Contacts. Raises
    ValueError when there's no match or more than one, so JARVIS asks instead of guessing."""
    who = str(who or "").strip()
    if not who or len(who) > 80:
        raise ValueError("needs a recipient (a name or a number)")
    if (number := normalize_number(who)):
        return number, number
    words = [w for w in re.split(r"[\s_]+", who.lower()) if w]
    found: dict[str, str] = {}
    for f in mem.facts():
        hay = f"{f['key']} {f['value']}".lower().replace("_", " ")
        m = PHONE.search(str(f["value"]))
        if m and (number := normalize_number(m[0])) and all(w in hay for w in words):
            found.setdefault(number, who)
    if not found:
        try:
            hits = (lookup or contacts_lookup)(who)
        except Exception as e:
            raise ValueError(f"couldn't search Contacts for {who!r} ({str(e)[:80]}); ask him for the number")
        for name, raw in hits:
            if (number := normalize_number(raw)):
                found.setdefault(number, name)
    if not found:
        raise ValueError(f"no number for {who!r} in saved facts or Contacts; ask him for it")
    if len(found) > 1:
        options = "; ".join(f"{name} {number}" for number, name in list(found.items())[:5])
        raise ValueError(f"{who!r} matches more than one number ({options}); ask him which")
    number, name = next(iter(found.items()))
    return name, number


def parse_duration(text) -> int:
    """'5m', '90s', '1h30m', '10 minutes' -> seconds (1s to 24h)."""
    s = str(text).strip().lower()
    unit_re = r"(\d+)\s*(hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)(?![a-z])"
    parts = re.findall(unit_re, s)
    if not parts or re.sub(unit_re + r"|and|[\s,]", "", s):
        raise ValueError(f"can't read the duration {text!r} (use e.g. 5m, 90s, 1h30m)")
    unit = {"h": 3600, "m": 60, "s": 1}
    seconds = sum(int(n) * unit[u[0]] for n, u in parts)
    if not 1 <= seconds <= MAX_TIMER:
        raise ValueError("a timer runs from one second to 24 hours")
    return seconds


def parse_alarm(text, now: datetime) -> datetime:
    """'07:30', '7:30am' (the next one) or 'YYYY-MM-DD HH:MM', within the next week."""
    s = str(text).strip().lower()
    if (m := re.fullmatch(r"(\d{1,2}):(\d{2})\s*(am|pm)?", s)):
        hour, minute = int(m[1]), int(m[2])
        if m[3]:
            if not 1 <= hour <= 12:
                raise ValueError(f"bad alarm time {text!r}")
            hour = hour % 12 + (12 if m[3] == "pm" else 0)
        if hour > 23 or minute > 59:
            raise ValueError(f"bad alarm time {text!r}")
        at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return at if at > now else at + timedelta(days=1)
    from jarvis.canvas import parse_due
    if len(s) <= 10:
        raise ValueError("an alarm needs a time")
    at = parse_due(s, now.tzinfo)
    if not now - timedelta(minutes=1) <= at <= now + timedelta(days=7):
        raise ValueError(f"alarm time {text} is in the past or more than a week out")
    return at


def _say_duration(seconds: int) -> str:
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    out = [f"{n} {word}{'s' if n != 1 else ''}" for n, word in ((h, "hour"), (m, "minute"), (s, "second")) if n]
    return " ".join(out)


# ----- alarms, timers and music: run at once -----
def set_timer(mem: Memory, seconds: int, label: str = "", now: datetime | None = None, shortcut=None) -> str:
    """Starts a Clock timer through SIRI_TIMER_SHORTCUT if set, otherwise a JARVIS reminder (said
    out loud and sent to Telegram). Returns the one-line confirmation."""
    now = now or datetime.now(timezone.utc)
    label = str(label or "").strip()[:100]
    how = "reminder"
    if config.SIRI_TIMER_SHORTCUT:
        try:
            (shortcut or run_shortcut)(config.SIRI_TIMER_SHORTCUT, str(seconds))
            how = "Clock"
        except Exception as e:
            mem.log("siri", f"timer Shortcut failed, using a reminder: {str(e)[:120]}")
    if how == "reminder":
        mem.add_reminder(now + timedelta(seconds=seconds), "Time's up, sir" + (f": {label}." if label else "."))
    mem.log("siri", f"timer {_say_duration(seconds)} ({how})" + (f": {label}" if label else ""))
    return f"Timer set for {_say_duration(seconds)}" + (f" ({label})" if label else "") + "."


def set_alarm(mem: Memory, at: datetime, label: str = "", shortcut=None) -> str:
    label = str(label or "").strip()[:100]
    how = "reminder"
    if config.SIRI_ALARM_SHORTCUT:
        try:
            (shortcut or run_shortcut)(config.SIRI_ALARM_SHORTCUT, f"{at:%H:%M}")
            how = "Clock"
        except Exception as e:
            mem.log("siri", f"alarm Shortcut failed, using a reminder: {str(e)[:120]}")
    if how == "reminder":
        mem.add_reminder(at, f"It's {at:%-I:%M %p}, sir" + (f": {label}." if label else "; your alarm."))
    mem.log("siri", f"alarm {at:%a %-I:%M %p} ({how})" + (f": {label}" if label else ""))
    return f"Alarm set for {at:%a %-I:%M %p}" + (f" ({label})" if label else "") + "."


def music(mem: Memory, query: str, kind: str = "any", player=None) -> str:
    query = str(query or "").strip()
    if not query or len(query) > 200:
        raise ValueError("needs something to play (a song, artist or playlist)")
    if kind not in ("song", "artist", "playlist", "any"):
        raise ValueError(f"bad music kind {kind!r}")
    from jarvis.spotify import SpotifyError
    try:
        playing = (player or play_music)(query, kind)
    except (SiriError, SpotifyError, OSError, subprocess.TimeoutExpired) as e:
        mem.log("siri", f"music {kind} {query!r} failed: {str(e)[:120]}")
        raise ValueError(f"couldn't play {query!r} ({str(e)[:120]})")
    mem.log("siri", f"playing {playing[:150]}")
    return f"Playing {playing}."


# ----- texts and calls: queued for his yes, bound to exactly what he saw -----
def queue(mem: Memory, kind: str, who: str, body: str = "", lookup=None) -> int:
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}")
    body = str(body or "").strip()
    if kind == "send_text" and not 0 < len(body) <= MAX_TEXT:
        raise ValueError(f"a text needs a message of 1 to {MAX_TEXT} characters")
    name, number = resolve_contact(mem, who, lookup)
    payload = {"to": number, "name": name} | ({"body": body} if kind == "send_text" else {})
    aid = mem.queue_action(kind, payload)
    mem.log("siri", f"{'text' if kind == 'send_text' else 'call'} to {name} ({number}) waiting for his OK")
    return aid


def digest(kind: str, payload: dict) -> str:
    """Exactly what was approved: the kind, the number and the text."""
    return hashlib.sha256(json.dumps([kind, payload["to"], payload.get("body", "")]).encode()).hexdigest()


def describe(action) -> str:
    p = json.loads(action["payload"])
    who = p["to"] if p.get("name") in (None, "", p["to"]) else f"{p['name']} ({p['to']})"
    return f"the {'text' if action['kind'] == 'send_text' else 'call'} to {who}"


def format_request(action) -> str:
    p = json.loads(action["payload"])
    who = p["to"] if p.get("name") in (None, "", p["to"]) else f"{p['name']} ({p['to']})"
    if action["kind"] == "send_text":
        return (f"Text to {who}:\n\n{p['body']}\n\n"
                f"Is that what you meant, sir? Reply yes to send it to {who}, no to cancel.")
    return (f"Call {who} from this Mac (FaceTime or your iPhone).\n\n"
            f"Is that what you meant, sir? Reply yes to call {who}, no to cancel.")


def _expired(action, now: datetime, field: str = "created_at") -> bool:
    at = datetime.fromisoformat(action[field])
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return now - at > timedelta(minutes=config.COMMAND_APPROVAL_MINUTES)


def expire(mem: Memory, now: datetime | None = None) -> int:
    """Unanswered texts and calls lapse, so a late tap can never send an old one."""
    now = now or datetime.now(timezone.utc)
    stale = [a for kind in KINDS for a in mem.pending_actions(kind=kind) if _expired(a, now)]
    for a in stale:
        mem.resolve_action(a["id"], "rejected")
        mem.log("siri", f"{describe(a)} lapsed unanswered")
    return len(stale)


def approve(mem: Memory, action_id: int) -> None:
    """Approve it as it is now. Any later change to the number or text voids the approval."""
    action = mem.get_action(action_id)
    mem.set_kv(f"siri_ok:{action_id}", digest(action["kind"], json.loads(action["payload"])))
    mem.resolve_action(action_id, "approved")


def execute(mem: Memory, action_id: int, now: datetime | None = None) -> str:
    """Send or place one approved text or call, if it's still exactly what he approved and the yes
    is fresh. Returns a short note for him."""
    action = mem.get_action(action_id)
    if not action or action["kind"] not in KINDS or action["status"] != "approved":
        return "Nothing to do; it was already handled."
    p = json.loads(action["payload"])
    what = describe(action)
    if mem.get_kv(f"siri_ok:{action_id}") != digest(action["kind"], p):
        mem.resolve_action(action_id, "failed")
        mem.log("siri", f"{what}: changed after approval (or never confirmed); not sent")
        return "It changed after you approved it, so I didn't send it."
    if _expired(action, now or datetime.now(timezone.utc), "resolved_at"):
        mem.resolve_action(action_id, "failed")
        mem.log("siri", f"{what}: approved too long ago; not sent")
        return "That yes is too old now; ask again."
    try:
        if action["kind"] == "send_text":
            send_text(p["to"], p["body"])
        else:
            place_call(p["to"])
    except Exception as e:
        mem.resolve_action(action_id, "failed")
        mem.log("siri", f"{what} failed: {str(e)[:200]}")
        return f"It didn't go through ({str(e)[:120]})."
    mem.resolve_action(action_id, "done")
    if action["kind"] == "send_text":
        mem.log("siri", f"texted {p.get('name') or p['to']} ({p['to']}): {p['body'][:80]}")
        return "Sent."
    mem.log("siri", f"called {p.get('name') or p['to']} ({p['to']})")
    return "Calling now; pick up on the Mac or your iPhone."


def process_approved(mem: Memory, now: datetime | None = None) -> int:
    """Backstop for the tick: any approved text or call not yet carried out."""
    done = 0
    for kind in KINDS:
        for a in mem.pending_actions(status="approved", kind=kind):
            execute(mem, a["id"], now)
            done += mem.get_action(a["id"])["status"] == "done"
    return done
