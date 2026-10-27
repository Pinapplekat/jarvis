from datetime import datetime, timedelta, timezone

from jarvis.contract.api import ActionError, AppContext, JobResult, JobRun, Result

LATE_AFTER = timedelta(minutes=10)
MAX_AHEAD = timedelta(days=730)
CONTEXT_MAX = 5


def _utc(when: datetime) -> str:
    return when.astimezone(timezone.utc).isoformat(timespec="seconds")


def _local(ctx: AppContext, iso: str) -> datetime:
    return datetime.fromisoformat(iso).astimezone(ctx.tz)


def _when(ctx: AppContext, iso: str, now: datetime) -> str:
    due = _local(ctx, iso)
    day = "" if due.date() == now.date() else f"{due:%a} "
    return f"{day}{due:%-I:%M %p}"


def remind_me(ctx: AppContext, text: str, at: str) -> Result:
    try:
        due = datetime.fromisoformat(at)
    except ValueError:
        raise ActionError(f"I couldn't read '{at}' as a time. It needs to look like 2026-10-26T15:00:00-07:00.")
    if due.tzinfo is None:
        raise ActionError("That time had no UTC offset. It needs to look like 2026-10-26T15:00:00-07:00.")
    now = ctx.now()
    if due <= now:
        raise ActionError(f"That time has already passed; it's {now:%a %-I:%M %p} now.")
    if due - now > MAX_AHEAD:
        raise ActionError("That's more than two years away. I only keep reminders up to two years ahead.")
    rid = ctx.db.execute(
        "INSERT INTO reminders_items (due_at, text, created_at) VALUES (?,?,?)", (_utc(due), text, _utc(now))
    ).lastrowid
    ctx.wake("deliver")  # so the delivery job learns the next due time now, not at its next 15-minute pass
    when = due.astimezone(ctx.tz)
    return Result(f"Okay, I'll remind him at {when:%a %b %-d, %-I:%M %p}: {text} (reminder #{rid}).",
                  undo={"id": rid}, subject=f"reminder:{rid}")


def undo_remind_me(ctx: AppContext, undo: dict) -> Result:
    ctx.db.execute("DELETE FROM reminders_items WHERE id=? AND status='pending'", (undo["id"],))
    ctx.wake("deliver")
    return Result(f"Removed reminder #{undo['id']} again.")


def set_timer(ctx: AppContext, minutes: int, label: str = "") -> Result:
    """A timer is a reminder at now + minutes, so it gets the same exact delivery and survives a restart."""
    due = (ctx.now() + timedelta(minutes=minutes)).replace(microsecond=0)
    text = f"Timer done: {label.strip()} ({minutes} min)" if label.strip() else f"Timer done ({minutes} min)"
    out = remind_me(ctx, text, due.isoformat())
    return Result(f"Timer set for {minutes} min, until {due:%-I:%M %p}.", undo=out.undo, subject=out.subject)


def list_reminders(ctx: AppContext, days: int = 7) -> Result:
    now = ctx.now()
    rows = ctx.db.execute(
        "SELECT id, due_at, text FROM reminders_items WHERE status='pending' AND julianday(due_at) < julianday(?) "
        "ORDER BY julianday(due_at)", (_utc(now + timedelta(days=days)),)).fetchall()
    if not rows:
        return Result(f"No reminders waiting in the next {days} days.", data={"reminders": []})
    lines = [f"#{r['id']} {_local(ctx, r['due_at']):%a %b %-d, %-I:%M %p}: {r['text']}" for r in rows]
    return Result("\n".join(lines), data={"reminders": [dict(r) for r in rows]})


def sent_between(ctx: AppContext, start: str, end: str) -> Result:
    try:
        low, high = datetime.fromisoformat(start), datetime.fromisoformat(end)
    except ValueError:
        raise ActionError("start and end need to look like 2026-10-26T00:00:00-07:00.") from None
    rows = ctx.db.execute(
        "SELECT id, text, sent_at FROM reminders_items WHERE status='sent' AND julianday(sent_at) >= julianday(?) "
        "AND julianday(sent_at) < julianday(?) ORDER BY julianday(sent_at)", (_utc(low), _utc(high))).fetchall()
    return Result(f"{len(rows)} sent", data={"reminders": [dict(r) for r in rows]})


def cancel_reminder(ctx: AppContext, id: int) -> Result:
    row = ctx.db.execute("SELECT text, status FROM reminders_items WHERE id=?", (id,)).fetchone()
    if row is None:
        raise ActionError(f"There is no reminder #{id}.")
    if row["status"] != "pending":
        raise ActionError(f"Reminder #{id} can't be cancelled: it is already {row['status']}.")
    ctx.db.execute("UPDATE reminders_items SET status='cancelled' WHERE id=?", (id,))
    ctx.wake("deliver")
    return Result(f"Cancelled reminder #{id}: {row['text']}.", undo={"id": id}, subject=f"reminder:{id}")


def undo_cancel_reminder(ctx: AppContext, undo: dict) -> Result:
    ctx.db.execute("UPDATE reminders_items SET status='pending' WHERE id=? AND status='cancelled'", (undo["id"],))
    ctx.wake("deliver")
    return Result(f"Reminder #{undo['id']} is back.")


def deliver_due(ctx: AppContext, run: JobRun) -> JobResult:
    """Claim what is due (each reminder goes out exactly once), then ask to wake at the next due time."""
    cutoff = _utc(run.now)
    due = ctx.db.execute(
        "SELECT id, due_at, text FROM reminders_items WHERE status='pending' AND julianday(due_at) <= julianday(?) "
        "ORDER BY julianday(due_at)", (cutoff,)).fetchall()
    messages = []
    for r in due:
        claimed = ctx.db.execute(
            "UPDATE reminders_items SET status='sent', sent_at=? WHERE id=? AND status='pending'", (cutoff, r["id"])
        ).rowcount
        if not claimed:
            continue
        text = f"⏰ {r['text']}"
        planned = _local(ctx, r["due_at"])
        if run.now - planned > LATE_AFTER:  # the Mac was asleep or offline at the time
            text += f"\n(late: this was for {planned:%a %-I:%M %p})"
        messages.append(text)
    nxt = ctx.db.execute(
        "SELECT due_at FROM reminders_items WHERE status='pending' ORDER BY julianday(due_at) LIMIT 1").fetchone()
    return JobResult(f"sent {len(messages)}", tuple(messages),
                     wake_at=datetime.fromisoformat(nxt["due_at"]) if nxt else None)


def context(ctx: AppContext) -> str:
    now = ctx.now()
    rows = ctx.db.execute(
        "SELECT due_at, text FROM reminders_items WHERE status='pending' AND julianday(due_at) < julianday(?) "
        "ORDER BY julianday(due_at) LIMIT ?", (_utc(now + timedelta(hours=24)), CONTEXT_MAX)).fetchall()
    if not rows:
        return ""
    return "Reminders due in the next 24 hours:\n" + "\n".join(f"- {_when(ctx, r['due_at'], now)}: {r['text']}"
                                                                  for r in rows)
