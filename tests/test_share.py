"""ctx.call: apps run each other's shared read actions instead of reading each other's tables."""

import re
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from jarvis.contract.api import AppUnavailable
from jarvis.core import share
from jarvis.core.dispatch import NodeReply, Target

LA = ZoneInfo("America/Los_Angeles")
APPS = Path(__file__).resolve().parent.parent / "jarvis" / "apps"


def test_a_shared_read_runs_across_apps(real_apps):
    real_apps.conn.execute(
        "INSERT INTO school_deadlines (key, course, title, due, status, trackable, kind, hand_done, source, updated_at) "
        "VALUES ('k1','ECON 100','Quiz','2026-10-26T20:00:00+00:00','unsubmitted',1,'due',0,'canvas','x')")
    ctx = real_apps.apps["checkins"].ctx
    out = ctx.call("school", "deadlines_between", start="2026-10-26T00:00:00-07:00", end="2026-10-27T00:00:00-07:00")
    (d,) = out.data["deadlines"]
    assert (d["course"], d["title"], d["done"]) == ("ECON 100", "Quiz", False)
    assert datetime.fromisoformat(d["due"]).utcoffset() == datetime(2026, 10, 26, tzinfo=LA).utcoffset()
    assert real_apps.conn.execute("SELECT COUNT(*) FROM action_log").fetchone()[0] == 0  # not an action anyone took


def test_only_shared_actions_of_apps_in_uses(real_apps):
    ctx = real_apps.apps["checkins"].ctx
    with pytest.raises(PermissionError, match="isn't a shared action"):
        ctx.call("school", "mark_done", key="k1")
    with pytest.raises(PermissionError, match="doesn't list it in uses"):
        real_apps.apps["journal"].ctx.call("checkins", "anything")
    with pytest.raises(ValueError, match="bad input"):
        ctx.call("school", "deadlines_between", start="2026-10-26T00:00:00-07:00")


def test_a_missing_app_is_unavailable(real_apps):
    del real_apps.apps["school"]
    with pytest.raises(AppUnavailable):
        real_apps.apps["checkins"].ctx.call("school", "deadlines_between", start="x", end="y")


def test_hidden_actions_arent_model_tools(real_apps):
    names = {t.name for t in real_apps.actions()}
    assert "school__upcoming" in names and "school__deadlines_between" not in names
    assert "reminders__list_reminders" in names and "reminders__sent_between" not in names


def test_a_shared_call_on_a_job_connection_uses_that_connection(real_apps, settings):
    from jarvis.core import db

    conn = db.connect_extra(settings)
    try:
        ctx = real_apps.apps["checkins"].ctx.with_db(conn)
        assert ctx.call("journal", "latest_entry").text == "There's no journal yet."
    finally:
        conn.close()


class FakeLink:
    def __init__(self, reply=None, error=None):
        self.reply, self.error, self.calls = reply, error, []

    def call(self, cap, app, handler, args, timeout_s, *, as_arg=False, target=None):
        self.calls.append((cap, app, handler, args))
        return self.reply

    def is_live(self, target):
        return True

    def connected(self):
        return []


def _wire(echo):
    eye = echo.apps["eye"]
    snap = next(a for a in eye.manifest.actions if a.name == "snap")
    actions = tuple(replace(a, share=True) if a is snap else a for a in eye.manifest.actions)
    echo.apps["eye"] = replace(eye, manifest=replace(eye.manifest, actions=actions))
    app = echo.apps["echo"]
    echo.apps["echo"] = replace(app, manifest=replace(app.manifest, uses=("eye",)))


def test_a_shared_call_reaches_a_node_app_and_leaves_no_files(echo):
    _wire(echo)
    echo.nodes = FakeLink(NodeReply(Target(1, 1), True, "snap front", {"n": 1},
                                    attachments=(("image", "image/jpeg", b"pic"),)))
    out = share.call(echo, "echo", "eye", "snap", {})
    assert (out.text, out.data) == ("snap front", {"n": 1})
    assert echo.nodes.calls == [("eye", "eye", "snap", {})]
    assert list((echo.settings.data_dir / "tmp").iterdir()) == []


def test_a_node_app_with_no_device_is_unavailable(echo):
    _wire(echo)
    echo.nodes = None
    with pytest.raises(AppUnavailable, match="no device"):
        share.call(echo, "echo", "eye", "snap", {})


TABLE_WORDS = r"\b(?:FROM|JOIN|INTO|UPDATE|TABLE)\s+([a-z][a-z0-9_]*)"
CORE_TABLES = {"chat_log", "action_log", "asks", "turns", "outbox", "jobs", "job_runs", "sessions"}


def test_no_app_names_another_apps_tables_or_the_core_tables():
    apps = {p.name for p in APPS.iterdir() if (p / "manifest.toml").exists()}
    problems = []
    for app in sorted(apps):
        for f in sorted((APPS / app).rglob("*")):
            if f.suffix not in (".py", ".sql") or "tests" in f.parts:
                continue
            for table in re.findall(TABLE_WORDS, f.read_text(), flags=re.IGNORECASE):
                owner = table.split("_", 1)[0]
                if table in CORE_TABLES or (owner in apps and owner != app):
                    problems.append(f"{f.relative_to(APPS)} uses {table}")
    assert problems == []
