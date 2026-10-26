"""Messages that couldn't reach Telegram (the Mac's network was down) wait here and go out, oldest
first, as soon as it's back, marked late when they are. Messages with buttons aren't held here:
their senders already retry until the question has gone out."""

import json
from datetime import datetime, timezone
from typing import Callable

from jarvis.memory import Memory

KEY = "telegram_outbox"
MAX_HELD = 50
LATE_SECONDS = 120


def _load(mem: Memory) -> list[dict]:
    try:
        return json.loads(mem.get_kv(KEY) or "[]")
    except ValueError:
        return []


def hold(mem: Memory, text: str, now: datetime | None = None) -> None:
    items = _load(mem)
    items.append({"text": text, "at": (now or datetime.now(timezone.utc)).timestamp()})
    mem.set_kv(KEY, json.dumps(items[-MAX_HELD:]))
    mem.log("telegram_held", f"held a message until Telegram is reachable ({len(items)} waiting)")


def waiting(mem: Memory) -> int:
    return len(_load(mem))


def flush(mem: Memory, send: Callable[[str], object], now: datetime | None = None, tz=None) -> int:
    """Send what's waiting, oldest first; stop at the first failure (still offline). Returns how many went."""
    items, sent = _load(mem), 0
    now = now or datetime.now(timezone.utc)
    while items:
        item = items[0]
        text = item["text"]
        if now.timestamp() - item["at"] > LATE_SECONDS:
            at = datetime.fromtimestamp(item["at"], timezone.utc).astimezone(tz)
            text += f"\n(late: Telegram was unreachable at {at:%-I:%M %p})"
        try:
            send(text)
        except Exception:
            break
        items.pop(0)
        sent += 1
        mem.set_kv(KEY, json.dumps(items))
    return sent
