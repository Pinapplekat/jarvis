"""The outbox: everything Falak says first is a row here, and one sender task delivers it."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta, tzinfo

from jarvis.core import conversation
from jarvis.core.timeutil import stamp

log = logging.getLogger("jarvis.core.outbox")

RETRY_S = (5, 15, 60, 120)  # after the 1st, 2nd, 3rd failure; then every 2 minutes
LATE_AFTER = timedelta(minutes=2)
POLL_S = 2.0  # other processes (jarvis jobs run) add rows without waking us


def add(conn: sqlite3.Connection, text: str, source: str, now: datetime | None = None,
        not_before: datetime | None = None, dedupe_key: str | None = None) -> int | None:
    """Queue a message. With a `dedupe_key` that is already there (a node resent an event), nothing is added and
    the result is None, so the same event is never sent to Elijah twice."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO outbox (text, source, created_at, not_before, dedupe_key) VALUES (?,?,?,?,?)",
        (text, source, stamp(now), stamp(not_before) if not_before else None, dedupe_key),
    )
    return cur.lastrowid if cur.rowcount else None


def unsent(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM outbox WHERE sent_at IS NULL ORDER BY id").fetchall()


def recent(conn: sqlite3.Connection, n: int = 20) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM outbox ORDER BY id DESC LIMIT ?", (n,)).fetchall()[::-1]


def unsent_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM outbox WHERE sent_at IS NULL").fetchone()[0]


def prune(conn: sqlite3.Connection, now: datetime, days: int = 14) -> None:
    conn.execute("DELETE FROM outbox WHERE sent_at IS NOT NULL AND sent_at < ?", (stamp(now - timedelta(days=days)),))


def _late_note(row: sqlite3.Row, channel: str, now: datetime, tz: tzinfo) -> str:
    """'(held: Discord was unreachable at 3:05 PM)' for a message that only went out after a failed try."""
    meant = datetime.fromisoformat(max(row["created_at"], row["not_before"] or ""))
    if not row["attempts"] or now - meant <= LATE_AFTER:
        return ""
    return f"\n(held: {channel.capitalize()} was unreachable at {meant.astimezone(tz):%-I:%M %p})"


class Sender:
    """Sends due rows oldest first through the notify channel; a failure keeps the row and backs off."""

    def __init__(self, conn: sqlite3.Connection, pick_channel: Callable[[], object | None],
                 clock: Callable[[], datetime], tz) -> None:
        self.conn, self.pick_channel, self.clock, self.tz = conn, pick_channel, clock, tz
        self._retry_at: dict[int, datetime] = {}  # in memory: a restart tries again at once
        self._wake = asyncio.Event()
        self._stop = False

    def nudge(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop = True
        self._wake.set()

    async def flush(self) -> int:
        """One pass: send everything that is due. Returns how many went out."""
        channel = self.pick_channel()
        if channel is None:
            return 0
        now, sent = self.clock(), 0
        for row in unsent(self.conn):
            if row["not_before"] and datetime.fromisoformat(row["not_before"]) > now:
                continue
            if self._retry_at.get(row["id"], now) > now:
                break  # keep the order: nothing overtakes a message that is waiting to be retried
            text = row["text"] + _late_note(row, channel.name, now, self.tz)
            try:
                await channel.send(text)
            except Exception as e:
                attempts = row["attempts"] + 1
                self._retry_at[row["id"]] = now + timedelta(seconds=RETRY_S[min(attempts, len(RETRY_S)) - 1])
                self.conn.execute("UPDATE outbox SET attempts=?, last_error=? WHERE id=?",
                                  (attempts, f"{type(e).__name__}: {e}"[:200], row["id"]))
                log.warning("outbox: sending #%d through %s failed (%s: %s)", row["id"], channel.name,
                            type(e).__name__, e)
                break
            self._retry_at.pop(row["id"], None)
            self.conn.execute("UPDATE outbox SET sent_at=?, attempts=attempts+1 WHERE id=?", (stamp(now), row["id"]))
            conversation.add(self.conn, "assistant", text, now)  # so "done" or "how was it?" has context
            sent += 1
        return sent

    async def run(self) -> None:
        while not self._stop:
            try:
                await self.flush()
            except Exception:
                log.exception("the outbox sender failed")
            try:
                await asyncio.wait_for(self._wake.wait(), POLL_S)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
