"""Read new texts for a Messages watch, and nothing else.

Elijah consented to this for the watches he creates himself ("tell me when Arboreal texts"). Each
read is one read-only query of ~/Library/Messages/chat.db (needs Full Disk Access, which he grants
in System Settings), filtered in SQL to the exact handles or group-chat names the watch names and to
rows newer than the last one seen. It never lists, searches or opens other conversations, and an
empty target list never queries at all. Incoming messages only.

There is deliberately no send function here: texts still go out only through siri.queue and his yes.
Message text is returned to the caller for a short local alert and is never logged.
"""

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from jarvis import config

MAX_TARGETS = 5
MAX_ROWS = 20
TIMEOUT_S = 5
UNREADABLE = "Messages isn't readable, grant access in System Settings (Privacy & Security > Full Disk Access)"


class MessagesUnreadable(Exception):
    """No permission, no database, or it stayed locked past the timeout."""


@dataclass
class Text:
    rowid: int
    target: str   # which of the watch's names it matched (as he wrote it)
    text: str


def _phone(target: str) -> str | None:
    """'(206) 555-0100' -> '+12065550100'; None if it isn't a phone number."""
    if not re.fullmatch(r"[+\d\s().-]{7,}", target):
        return None
    digits = re.sub(r"\D", "", target)
    if len(digits) == 10:
        return "+1" + digits
    return "+" + digits if 11 <= len(digits) <= 15 else None


def _handles(target: str) -> set[str]:
    """The exact handle ids (as chat.db stores them) a target can be: a phone in E.164, or an email."""
    t = target.strip()
    if (p := _phone(t)):
        return {p}
    if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", t):
        return {t.lower()}
    return set()


def _body(text: str | None, blob: bytes | None) -> str:
    """Newer macOS keeps the text only in attributedBody (a typedstream); pull the NSString out."""
    if text:
        return text
    if not blob or (i := blob.find(b"NSString")) < 0:
        return ""
    rest = blob[i + len(b"NSString") + 5:]
    if not rest:
        return ""
    if rest[0] == 0x81:
        size, start = int.from_bytes(rest[1:3], "little"), 3
    else:
        size, start = rest[0], 1
    return rest[start:start + size].decode("utf-8", errors="replace")


def _connect(path: str) -> sqlite3.Connection:
    try:
        db = sqlite3.connect(f"{Path(path).as_uri()}?mode=ro", uri=True, timeout=TIMEOUT_S)
        db.execute("SELECT 1 FROM message LIMIT 1")
        return db
    except sqlite3.Error as e:  # "unable to open", "authorization denied", "locked": all the same to him
        raise MessagesUnreadable(UNREADABLE) from e


def _where(targets: list[str]) -> tuple[str, list, dict[str, str]]:
    """SQL filter on exact handles or exact chat names, and a map back to the name he used."""
    back: dict[str, str] = {}
    for t in targets[:MAX_TARGETS]:
        for h in _handles(t):
            back[h] = t
        back.setdefault("chat:" + t.strip().lower(), t)
    handles = [k for k in back if not k.startswith("chat:")]
    names = [k[5:] for k in back if k.startswith("chat:")]
    parts, args = [], []
    if handles:
        parts.append(f"h.id IN ({','.join('?' * len(handles))})")
        args += handles
    parts.append(f"LOWER(c.display_name) IN ({','.join('?' * len(names))})")
    args += names
    return "(" + " OR ".join(parts) + ")", args, back


def read_new(targets: list[str], after: int | None, path: str | None = None) -> tuple[list[Text], int]:
    """Incoming texts in the named threads newer than `after`, oldest first, and the newest id seen.
    With `after` None (first look) nothing is returned, only the id to start from."""
    targets = [t for t in (targets or []) if str(t).strip()]
    if not targets:
        return [], after or 0  # no named target: no query at all
    where, args, back = _where(targets)
    db = _connect(path or config.MESSAGES_DB)
    try:
        base = ("FROM message m JOIN chat_message_join j ON j.message_id = m.ROWID "
                "JOIN chat c ON c.ROWID = j.chat_id LEFT JOIN handle h ON h.ROWID = m.handle_id "
                f"WHERE m.is_from_me = 0 AND {where}")
        if after is None:
            row = db.execute(f"SELECT MAX(m.ROWID) {base}", args).fetchone()
            return [], int(row[0] or 0)
        rows = db.execute(f"SELECT DISTINCT m.ROWID, m.text, m.attributedBody, h.id, c.display_name {base} "
                          "AND m.ROWID > ? ORDER BY m.ROWID LIMIT ?", [*args, after, MAX_ROWS]).fetchall()
    except sqlite3.Error as e:
        raise MessagesUnreadable(UNREADABLE) from e
    finally:
        db.close()
    out, seen = [], set()
    for rowid, text, blob, handle, chat in rows:
        if rowid in seen:
            continue
        seen.add(rowid)
        target = back.get("chat:" + (chat or "").lower()) or back.get(handle or "") or targets[0]
        out.append(Text(rowid, target, _body(text, blob)))
    return out, max([after, *seen])
