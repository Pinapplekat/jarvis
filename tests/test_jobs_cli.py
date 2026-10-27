from datetime import datetime

from scripted_brain import ScriptedBrain

from jarvis import chat_cli, cli
from jarvis.core import db, outbox
from jarvis.core.settings import load_settings


def rows(settings, sql):
    conn = db.connect(settings)
    try:
        return [dict(r) for r in conn.execute(sql)]
    finally:
        conn.close()


def run(settings, target, brain=None, force=False):
    return chat_cli.jobs_run(settings, target, force, brain or ScriptedBrain(text="Good morning, sir."))


def test_jobs_lists_every_job_with_its_trigger(data_dir, capsys):
    assert cli.main(["jobs"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 7
    by_name = {line.split()[0]: line for line in out}
    assert "every 15m" in by_name["reminders.deliver"] and "every 2h" in by_name["school.sync"]
    assert "at 02:00" in by_name["journal.write"] and "at 07:00" in by_name["checkins.morning"]
    assert all("last: never run" in line and "failures: 0" in line for line in out)


def test_jobs_shows_the_last_run_after_one_ran(data_dir, capsys):
    assert run(load_settings(), "reminders.deliver") == 0
    capsys.readouterr()
    cli.main(["jobs"])
    line = next(l for l in capsys.readouterr().out.splitlines() if l.startswith("reminders.deliver"))
    assert "last: ok" in line and "ms)" in line


def test_jobs_run_prints_the_outcome_and_the_queued_message(data_dir, capsys):
    brain = ScriptedBrain(text="Good morning, sir. Nothing due today.")
    assert run(load_settings(), "checkins.morning", brain, force=True) == 0
    out = capsys.readouterr().out
    assert "checkins.morning: ok" in out and "Good morning, sir. Nothing due today." in out
    assert "Your task now" in brain.prompts[0]  # a thinking job runs its turn here, with the real brain
    sent = rows(load_settings(), "SELECT * FROM outbox")
    assert [(r["text"], r["source"], r["sent_at"]) for r in sent] == [
        ("Good morning, sir. Nothing due today.", "job:checkins.morning", None)]  # waits for `serve`; never dropped


def test_jobs_run_an_unknown_job_names_the_known_ones(data_dir, capsys):
    assert run(load_settings(), "nope.nothing") == 2
    err = capsys.readouterr().err
    assert "no job called nope.nothing" in err and "school.sync" in err


def test_jobs_run_respects_quiet_hours_unless_forced(data_dir, capsys):
    hour = datetime.now(load_settings().tz()).hour
    data_dir.mkdir()
    (data_dir / "settings.toml").write_text(f'quiet_hours = "{hour}-{(hour + 1) % 24}"\n')
    brain = ScriptedBrain(text="Evening, sir.")
    assert run(load_settings(), "checkins.evening", brain) == 0
    assert "skipped, quiet hours" in capsys.readouterr().out and brain.prompts == []
    assert rows(load_settings(), "SELECT * FROM outbox") == []
    assert run(load_settings(), "checkins.evening", brain, force=True) == 0
    assert len(rows(load_settings(), "SELECT * FROM outbox")) == 1


def test_the_budget_applies_to_a_manual_thinking_run(data_dir, capsys):
    data_dir.mkdir()
    (data_dir / "settings.toml").write_text("thinking_runs_per_day = 1\n")
    assert run(load_settings(), "checkins.morning", force=True) == 0
    assert run(load_settings(), "checkins.evening", force=True) == 0
    out = capsys.readouterr().out
    assert "checkins.morning: ok" in out and "checkins.evening: skipped, budget" in out
    assert [r["status"] for r in rows(load_settings(), "SELECT status FROM job_runs ORDER BY id")] == ["ok", "skipped"]
    assert len(rows(load_settings(), "SELECT * FROM outbox")) == 1


def test_a_failing_job_exits_nonzero(data_dir, capsys):
    assert run(load_settings(), "journal.write", ScriptedBrain(error="the model fell over")) == 1
    out = capsys.readouterr().out
    assert "journal.write: failed" in out and "the model fell over" in out


def test_outbox_lists_sent_and_waiting_messages(data_dir, capsys):
    conn = db.connect(load_settings())
    first = outbox.add(conn, "sent one", "reminders.deliver")
    conn.execute("UPDATE outbox SET sent_at='2026-10-26T20:00:00+00:00' WHERE id=?", (first,))
    outbox.add(conn, "waiting one", "school.nudge", not_before=datetime(2026, 10, 27, 14, 0).astimezone())
    conn.close()
    assert cli.main(["outbox"]) == 0
    out = capsys.readouterr().out
    assert "sent one" in out and "[sent 2026-10-26T20:00:00+00:00]" in out
    assert "waiting one" in out and "waiting until 2026-10-27" in out


def test_dev_info_shows_the_job_count_and_unsent_messages(data_dir, capsys):
    conn = db.connect(load_settings())
    outbox.add(conn, "hello", "t")
    conn.close()
    cli.main(["dev", "info"])
    assert "jobs:        7 (unsent outbox messages: 1)" in capsys.readouterr().out
