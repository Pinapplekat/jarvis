import json
import os
import subprocess
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from jarvis import config, journal, projects, rhythm
from jarvis.memory import Memory

TZ = ZoneInfo("America/Los_Angeles")


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    monkeypatch.setattr(config, "QUIET_HOURS", "23-7")
    monkeypatch.setattr(config, "RHYTHM", True)
    monkeypatch.setattr(config, "MORNING_HOUR", 7)
    monkeypatch.setattr(config, "MORNING_LATEST", 10)
    monkeypatch.setattr(config, "EVENING_HOUR", 21)
    monkeypatch.setattr(config, "JOURNAL_HOUR", 2)


# ---------- projects ----------

def test_project_names_disambiguate_clashes():
    names = projects.project_names(["/a/Code/Squarizer", "/a/old/Squarizer", "/a/Code/rift"])
    assert names == {"/a/Code/Squarizer": "Squarizer (Code)", "/a/old/Squarizer": "Squarizer (old)",
                     "/a/Code/rift": "rift"}


@pytest.fixture
def repo(tmp_path):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    path = tmp_path / "shop-site"
    path.mkdir()
    (path / "README.md").write_text("# Shop site\nAn Etsy-style store for stickers.")
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "add", "."], cwd=path, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "Add cart page"], cwd=path, check=True, env=env)
    return str(path)


def test_sync_and_brief_from_real_repo(repo):
    m = Memory(":memory:")
    m.set_kv("repos", json.dumps([repo]))
    assert projects.sync_projects(m) == 1 and projects.sync_projects(m) == 0
    m.observe("terminal", "$ npm test  [exit 1, 2.0s]", project="shop-site")
    seen = []

    def runner(system, prompt):
        seen.append(prompt)
        return {"summary": "Sticker store site", "brief": "Cart page added; tests failing.",
                "status": "active", "kind": "business"}
    assert projects.write_brief(m, "shop-site", runner) == "Sticker store site"
    assert "Add cart page" in seen[0] and "Etsy-style" in seen[0] and "npm test" in seen[0]
    assert "(not on GitHub)" in seen[0]
    p = m.project("shop-site")
    assert (p["kind"], p["status"], p["brief"]) == ("business", "active", "Cart page added; tests failing.")


def test_refresh_briefs_survives_failures(repo):
    m = Memory(":memory:")
    m.set_kv("repos", json.dumps([repo]))
    projects.sync_projects(m)

    def broken(system, prompt):
        raise RuntimeError("usage limit")
    assert projects.refresh_briefs(m, ["shop-site"], runner=broken) == []
    assert "usage limit" in m.recent_events()[0]["content"]


# ---------- journal ----------

def test_journal_material_and_write():
    m = Memory(":memory:")
    day = date(2026, 10, 15)
    noon = datetime(2026, 10, 15, 12, 0, tzinfo=TZ)
    m.observe("screen", "x", app="Code", title="cart.py — shop-site", project="shop-site", at=noon)
    m.observe("git", "new commit on main: Add cart", project="shop-site", at=noon)
    m.observe("screen", "tomorrow", app="Code", title="next-day-window", at=noon + timedelta(days=1))
    m.db.execute("INSERT INTO chat_log (at, role, text) VALUES (?, 'user', 'work was rough today')",
                 (noon.astimezone(timezone.utc).isoformat(timespec="seconds"),))
    text = journal.material(m, day, TZ)
    assert "work was rough today" in text and "shop-site (2)" in text and "Add cart" in text
    assert "next-day-window" not in text

    out = {"entry": "Elijah worked on shop-site.", "open_loops": ["fix cart tests"],
           "remember": [{"category": "work", "key": "shift_pattern", "value": "Thursdays 2-8pm"}]}
    journal.write(m, day, TZ, runner=lambda s, p: out)
    assert m.journal("2026-10-15")["entry"] == "Elijah worked on shop-site."
    assert m.facts("work")[0]["value"] == "Thursdays 2-8pm"
    assert "fix cart tests" in journal.journal_for_prompt(m)


def test_maybe_write_once_after_hour():
    m = Memory(":memory:")
    calls = []

    def runner(s, p):
        calls.append(p)
        return {"entry": "quiet day", "open_loops": [], "remember": []}
    early = datetime(2026, 10, 16, 1, 0, tzinfo=TZ)
    assert journal.maybe_write(m, early, runner, brief_runner=lambda s, p: {}) is None
    later = datetime(2026, 10, 16, 9, 0, tzinfo=TZ)
    assert journal.maybe_write(m, later, runner, brief_runner=lambda s, p: {}).startswith("journaled 2026-10-15")
    assert journal.maybe_write(m, later, runner, brief_runner=lambda s, p: {}) is None
    assert len(calls) == 1


def test_early_draft_is_rewritten_by_nightly_run():
    m = Memory(":memory:")
    m.save_journal("2026-10-15", "draft written at 10pm", [])
    m.db.execute("UPDATE journal SET created_at = '2026-10-16T05:00:00+00:00'")  # 10pm Pacific, before day end
    runner = lambda s, p: {"entry": "full day", "open_loops": [], "remember": []}  # noqa: E731
    out = journal.maybe_write(m, datetime(2026, 10, 16, 3, 0, tzinfo=TZ), runner, brief_runner=lambda s, p: {})
    assert out.startswith("journaled") and m.journal("2026-10-15")["entry"] == "full day"


# ---------- rhythm ----------

class FakeAnnouncer:
    def __init__(self):
        self.sent = []

    def send(self, text, spoken=None):
        self.sent.append((text, spoken))


def compose(system, ctx):
    kind = "morning" if "MORNING" in system else "evening"
    return {"spoken": f"Good {kind}, sir.", "text": f"{kind} text"}


def test_morning_waits_for_presence_until_latest():
    m, a = Memory(":memory:"), FakeAnnouncer()
    at8 = datetime(2026, 10, 16, 8, 0, tzinfo=TZ)
    assert rhythm.maybe_check_in(m, at8, a, present=False, deadlines_text="", runner=compose) is None
    assert rhythm.maybe_check_in(m, at8, a, present=True, deadlines_text="", runner=compose) == "morning"
    assert a.sent == [("morning text", "Good morning, sir.")]
    assert rhythm.maybe_check_in(m, at8, a, present=True, deadlines_text="", runner=compose) is None

    m2, a2 = Memory(":memory:"), FakeAnnouncer()
    at1030 = datetime(2026, 10, 16, 10, 30, tzinfo=TZ)
    assert rhythm.maybe_check_in(m2, at1030, a2, present=False, deadlines_text="", runner=compose) == "morning"


def test_evening_once_and_nothing_in_quiet_hours_or_too_early():
    m, a = Memory(":memory:"), FakeAnnouncer()
    assert rhythm.maybe_check_in(m, datetime(2026, 10, 16, 6, 0, tzinfo=TZ), a, True, "", compose) is None
    m.set_kv("rhythm_morning", "2026-10-16")
    at9pm = datetime(2026, 10, 16, 21, 10, tzinfo=TZ)
    assert rhythm.maybe_check_in(m, at9pm, a, True, "", compose) == "evening"
    assert rhythm.maybe_check_in(m, at9pm, a, True, "", compose) is None
    assert m.chat_history(1)[0]["text"] == "evening text"
    m3 = Memory(":memory:")
    assert rhythm.maybe_check_in(m3, datetime(2026, 10, 16, 23, 30, tzinfo=TZ), a, True, "", compose) is None


def test_failed_check_in_is_retried_later_not_lost():
    m, a = Memory(":memory:"), FakeAnnouncer()
    m.set_kv("rhythm_morning", "2026-10-16")
    at9pm = datetime(2026, 10, 16, 21, 10, tzinfo=TZ)

    def broken(system, ctx):
        raise RuntimeError("killed mid-compose")
    with pytest.raises(RuntimeError):
        rhythm.maybe_check_in(m, at9pm, a, True, "", broken)
    assert m.get_kv("rhythm_evening") is None  # not marked done
    assert rhythm.maybe_check_in(m, at9pm + timedelta(minutes=5), a, True, "", compose) is None  # waits
    assert rhythm.maybe_check_in(m, at9pm + timedelta(minutes=31), a, True, "", compose) == "evening"


def test_rhythm_context_includes_journal_and_reminders():
    m = Memory(":memory:")
    now = datetime(2026, 10, 16, 8, 0, tzinfo=TZ)
    m.save_journal("2026-10-15", "Elijah fixed the cart.", ["email Prof K about the quiz"])
    m.add_reminder(now + timedelta(hours=3), "Econ module")
    ctx = rhythm.context(m, now, "Sun Oct 18: ECON module 3")
    assert "email Prof K" in ctx and "Econ module" in ctx and "ECON module 3" in ctx
