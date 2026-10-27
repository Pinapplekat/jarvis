import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from jarvis.contract.api import JobRun
from jarvis.core import actions

LA = ZoneInfo("America/Los_Angeles")
CLI = actions.Origin("cli")
TWO_AM = datetime(2026, 10, 26, 2, 0, tzinfo=LA)  # the job runs at 2:00 AM and writes up Sunday the 25th


def do(reg, action, **args):
    return actions.run(reg, f"journal__{action}", args, CLI)


def write(reg, reason="at", now=TWO_AM):
    app = reg.apps["journal"]
    return app.module.write(app.ctx, JobRun(now, None, reason, True))


def utc(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def seed_sunday(reg):
    sunday = datetime(2026, 10, 25, tzinfo=LA)
    rows = [("user", "remind me to call the bank", 9), ("assistant", "Done. 3:00 PM it is.", 9)]
    for role, text, hour in rows:
        reg.conn.execute("INSERT INTO chat_log (at, role, text) VALUES (?,?,?)",
                         (utc(sunday + timedelta(hours=hour)), role, text))
    reg.conn.execute("INSERT INTO chat_log (at, role, text) VALUES (?,?,?)",
                     (utc(sunday + timedelta(days=1, hours=1)), "user", "after midnight, so tomorrow's"))
    reg.conn.execute("INSERT INTO chat_log (at, role, text) VALUES (?,?,?)",
                     (utc(sunday - timedelta(hours=1)), "user", "the day before"))
    reg.conn.execute("INSERT INTO action_log (time, tool, effect, status, args, result) VALUES (?,?,?,?,?,?)",
                     (utc(sunday + timedelta(hours=9)), "reminders__remind_me", "local", "ran", "(not logged)",
                      "Okay, I'll remind him: call the bank"))
    reg.conn.execute("INSERT INTO action_log (time, tool, effect, status, args, result) VALUES (?,?,?,?,?,?)",
                     (utc(sunday + timedelta(hours=10)), "echo__send", "outbound", "ran", '{"to": "SECRET-ARG"}', "sent"))
    reg.conn.execute(
        "INSERT INTO school_deadlines (key, course, title, due, status, trackable, kind, hand_done, source, updated_at) "
        "VALUES ('canvas:1','ENGL&101','Lyric Essay Draft',?,'submitted',1,'due',0,'canvas','x'),"
        "('canvas:2','ECON 100','Quiz Three',?,'unsubmitted',1,'due',0,'canvas','x')",
        (utc(sunday + timedelta(hours=23)), utc(sunday + timedelta(days=3))))
    reg.conn.execute("INSERT INTO reminders_items (due_at, text, created_at, sent_at, status) VALUES (?,?,?,?,?)",
                     (utc(sunday + timedelta(hours=15)), "stretch", "x", utc(sunday + timedelta(hours=15)), "sent"))


def test_the_job_asks_for_a_thinking_turn_that_only_uses_tools(real_apps):
    seed_sunday(real_apps)
    result = write(real_apps)
    think = result.think
    assert think.deliver is False and think.untrusted is True  # the material quotes conversation and titles
    assert result.summary == "writing 2026-10-25"
    prompt = think.prompt
    assert "journal__save_entry" in prompt and "memory__remember" in prompt and "JSON" not in prompt


def test_the_material_is_yesterdays_day_only(real_apps):
    seed_sunday(real_apps)
    prompt = write(real_apps).think.prompt
    assert "Day: Sunday October 25, 2026" in prompt
    assert "9:00 AM Elijah: remind me to call the bank" in prompt and "9:00 AM Falak: Done. 3:00 PM it is." in prompt
    assert "tomorrow's" not in prompt and "the day before" not in prompt
    assert "reminders__remind_me ran: Okay, I'll remind him: call the bank" in prompt
    assert "echo__send ran: sent" in prompt and "SECRET-ARG" not in prompt  # tool, status and result; never the args
    assert "ENGL&101: Lyric Essay Draft (done)" in prompt and "Quiz Three" not in prompt
    assert "- stretch" in prompt


def test_an_empty_day_still_gets_a_prompt(real_apps):
    prompt = write(real_apps).think.prompt
    assert prompt.count("(none)") >= 3


def test_it_is_written_once_but_a_draft_from_before_midnight_is_rewritten(real_apps):
    do(real_apps, "save_entry", day="2026-10-25", entry="A draft.")
    real_apps.conn.execute("UPDATE journal_entries SET created_at=?", (utc(datetime(2026, 10, 25, 22, 0, tzinfo=LA)),))
    assert write(real_apps).think is not None
    real_apps.conn.execute("UPDATE journal_entries SET created_at=?", (utc(TWO_AM - timedelta(minutes=5)),))
    done = write(real_apps)
    assert done.think is None and "already written" in done.summary
    assert write(real_apps, reason="manual").think is not None  # by hand, it goes again


def test_save_entry_and_read_journal_round_trip(real_apps):
    out = do(real_apps, "save_entry", day="2026-10-25", entry="  Elijah did the essay.\nHe was tired. ",
             open_loops=["email the professor", "x" * 500])
    assert isinstance(out, actions.Ran)
    read = do(real_apps, "read_journal", day="2026-10-25")
    assert read.result.text.startswith("2026-10-25:\nElijah did the essay.\nHe was tired.")
    assert "Open loops: email the professor; " in read.result.text
    assert len(read.result.data["open_loops"][1]) == 300
    assert "no journal entry for 2026-11-01" in do(real_apps, "read_journal", day="2026-11-01").result.text


def test_open_loops_are_optional_and_capped(real_apps):
    do(real_apps, "save_entry", day="2026-10-24", entry="Quiet day.")
    assert do(real_apps, "read_journal", day="2026-10-24").result.data["open_loops"] == []
    do(real_apps, "save_entry", day="2026-10-24", entry="Quiet day.", open_loops=[str(i) for i in range(15)])
    assert len(do(real_apps, "read_journal", day="2026-10-24").result.data["open_loops"]) == 10


def test_bad_days_are_refused(real_apps):
    for bad in ("yesterday", "2026-13-01", "10/25"):
        assert "isn't a date" in do(real_apps, "save_entry", day=bad, entry="x").message
        assert isinstance(do(real_apps, "read_journal", day=bad), actions.Failed)


def test_undo_restores_the_previous_entry_or_removes_the_new_one(real_apps):
    do(real_apps, "save_entry", day="2026-10-25", entry="First.", open_loops=["a"])
    before = [tuple(r) for r in real_apps.conn.execute("SELECT * FROM journal_entries")]
    do(real_apps, "save_entry", day="2026-10-25", entry="Second.")
    assert isinstance(actions.undo(real_apps), actions.Ran)
    assert [tuple(r) for r in real_apps.conn.execute("SELECT * FROM journal_entries")] == before
    assert isinstance(actions.undo(real_apps), actions.Ran)
    assert real_apps.conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0] == 0


def test_undo_refuses_out_of_order(real_apps):
    first = do(real_apps, "save_entry", day="2026-10-25", entry="First.")
    do(real_apps, "save_entry", day="2026-10-25", entry="Second.")
    assert "A newer change" in actions.undo(real_apps, first.log_id).message


def test_the_migration_copies_the_old_journal(settings):
    from jarvis.core import db
    from jarvis.core.apps import load_apps

    conn = db.connect(settings)
    conn.execute("INSERT INTO journal (day, entry, open_loops, created_at) VALUES "
                 "('2026-10-20','Old entry.',?, '2026-10-21T08:00:00+00:00')", (json.dumps(["loop"]),))
    conn.close()
    reg = load_apps(settings)
    try:
        row = reg.conn.execute("SELECT * FROM journal_entries").fetchone()
        assert (row["day"], row["entry"], json.loads(row["open_loops"])) == ("2026-10-20", "Old entry.", ["loop"])
        assert reg.conn.execute("SELECT COUNT(*) FROM journal").fetchone()[0] == 1
    finally:
        reg.close()
