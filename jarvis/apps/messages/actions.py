"""Texts, calls and Contacts on the Mac, ported from legacy/jarvis/siri.py.

Texts and calls reach other people, so they're outbound: they ask every time, and the ask is bound to the exact
number and text. A name is only ever resolved through Contacts (find_contact), never guessed, and a name passed
with a number has to own that number in Contacts. Everything handed to AppleScript is an argument, never script text.
"""

from __future__ import annotations

import re
import subprocess
from difflib import SequenceMatcher

from jarvis.contract.api import ActionError, AppContext, Result

TIMEOUT = 25
FIELD, RECORD = "\x1f", "\x1e"
MAX_FOUND = 8

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

PERMISSION = ("macOS blocked it: allow the terminal app running my node to control {app} in System Settings > "
              "Privacy & Security > Automation.")


def _osascript(script: str, *args: str, app: str) -> str:
    try:
        r = subprocess.run(["osascript", "-", *args], input=script, capture_output=True, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        raise ActionError(f"{app} didn't answer in time.") from None
    if r.returncode != 0:
        err = r.stderr.strip()
        if "-1743" in err or "not allowed" in err.lower():
            raise ActionError(PERMISSION.format(app=app))
        raise ActionError(f"{app} said: {err[:200] or 'it failed'}")
    return r.stdout.rstrip("\n")


# -- numbers and names ---------------------------------------------------------------------------------------------

_ODD_CHARS = str.maketrans({"‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-",
                            "−": "-", " ": " ", " ": " ", " ": " ", "​": "",
                            "‎": "", "‏": "", "‪": "", "‬": "", "⁦": "", "⁩": ""})
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.I)


def normalize_number(text: str) -> str | None:
    """'(206) 555-0100' -> '+12065550100'; None if it isn't a phone number. Copes with what Contacts synced from an
    iPhone stores: non-breaking spaces and hyphens, direction marks, 'tel:'."""
    text = str(text).translate(_ODD_CHARS).strip()
    text = re.sub(r"^tel:", "", text, flags=re.I).strip()
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


NICKNAMES = {"mike": "michael", "matt": "matthew", "chris": "christopher", "nick": "nicholas", "jon": "jonathan",
             "dan": "daniel", "dave": "david", "jim": "james", "jimmy": "james", "bill": "william", "will": "william",
             "bob": "robert", "rob": "robert", "tom": "thomas", "tony": "anthony", "joe": "joseph", "sam": "samuel",
             "ben": "benjamin", "alex": "alexander", "liz": "elizabeth", "beth": "elizabeth", "katie": "katherine",
             "kate": "katherine", "jen": "jennifer", "jenny": "jennifer", "em": "emily", "abby": "abigail"}


def _words(text: str) -> list[str]:
    return [w for w in re.split(r"[\s_.-]+", text.lower()) if w]


def _search_words(words: list[str]) -> list[str]:
    out = []
    for w in words:
        for v in (w, NICKNAMES.get(w)):
            if v and len(v) >= 3 and v not in out:
                out.append(v)
    return out


def close_name(name: str, wanted: str) -> bool:
    """Each word he said closely matches a word of the Contacts name (nicknames count), so 'Julian Lanton' finds
    'Julian Linton' but not 'Julian Moore'."""
    have = _words(name)
    said = _words(wanted)

    def near(w: str) -> bool:
        w = NICKNAMES.get(w, w)
        return any(SequenceMatcher(None, w, NICKNAMES.get(h, h)).ratio() >= 0.8 for h in have)

    return bool(said) and all(near(w) for w in said)


def lookup(name: str) -> list[tuple[str, str]]:
    """(name, raw number) for every phone of every contact whose name contains `name`."""
    raw = _osascript(CONTACTS_SCRIPT, name, app="Contacts")
    return [tuple(rec.split(FIELD)[:2]) for rec in filter(None, raw.split(RECORD)) if FIELD in rec]


def matches(name: str) -> list[tuple[str, str]]:
    """(contact name, normalized number) for a name; a misheard or partial name searches word by word."""
    hits = lookup(name)
    words = _words(name)
    if not any(normalize_number(raw) for _, raw in hits) and len(words) >= 2:
        wider = [h for w in _search_words(words) for h in lookup(w)]
        hits = [h for h in wider if close_name(h[0], name)]
    found = {}
    for who, raw in hits:
        if (number := normalize_number(raw)):
            found.setdefault((who, number), None)
    return list(found)


# -- actions -----------------------------------------------------------------------------------------------------

def find_contact(ctx: AppContext, name: str) -> Result:
    found = matches(name.strip())
    if not found:
        return Result(f"No one called '{name}' with a phone number is in Contacts. Ask Elijah for the number.",
                      data={"contacts": []})
    shown = found[:MAX_FOUND]
    lines = [f"- {who}: {number}" for who, number in shown]
    more = f"\n(and {len(found) - MAX_FOUND} more)" if len(found) > MAX_FOUND else ""
    note = "" if len(found) == 1 else "\nMore than one: ask Elijah which, don't guess."
    return Result("\n".join(lines) + more + note, data={"contacts": [{"name": w, "number": n} for w, n in shown]})


def _recipient(number: str, name: str) -> str:
    """The checked address to send to. A name has to own this number in Contacts."""
    target = normalize_number(number) or (number.strip() if _EMAIL.match(number.strip()) else None)
    if target is None:
        raise ActionError(f"'{number}' isn't a phone number or an email address I can send to.")
    name = name.strip()
    if name and not _EMAIL.match(target) and (name, target) not in matches(name):
        raise ActionError(f"Contacts doesn't have {target} for {name}, so I didn't send anything. "
                          "Look them up again with find_contact.")
    return target


def send_text(ctx: AppContext, number: str, body: str, name: str = "") -> Result:
    target = _recipient(number, name)
    _osascript(TEXT_SCRIPT, target, body, app="Messages")
    return Result(f"Sent to {name or target}: {body}")


def call(ctx: AppContext, number: str, name: str = "") -> Result:
    target = _recipient(number, name)
    if _EMAIL.match(target):
        raise ActionError("I can only call a phone number.")
    try:
        r = subprocess.run(["open", f"tel:{target}"], capture_output=True, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        raise ActionError("The call didn't start in time.") from None
    if r.returncode != 0:
        raise ActionError(f"The call didn't start: {r.stderr.strip()[:200] or 'macOS refused'}")
    return Result(f"Calling {name or target} through the iPhone. The Mac shows the call; Elijah takes it from there.")
