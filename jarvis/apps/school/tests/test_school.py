"""Sync over a fake HTTP opener, nudges, mark_done and the migration. No network."""

import json
import re
import urllib.error
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from jarvis.contract.api import JobRun
from jarvis.core import actions

LA = ZoneInfo("America/Los_Angeles")
CLI = actions.Origin("cli")
NOW = datetime(2026, 10, 26, 12, 0, tzinfo=LA)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def assignment(id, name, due, state="unsubmitted", types=("online_upload",)):
    return {"id": id, "name": name, "due_at": iso(due), "html_url": f"https://canvas.test/a/{id}",
            "submission_types": list(types), "submission": {"workflow_state": state, "missing": False}}


class Response:
    def __init__(self, body, headers=None):
        self.body, self.headers = body, headers or {}

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """Answers Canvas API paths from a table; records the requests."""

    def __init__(self, routes):
        self.routes, self.requests = routes, []

    def open(self, request, timeout=None):
        self.requests.append((request.full_url, request.get_header("Authorization"), timeout))
        for part, answer in self.routes:
            if part in request.full_url:
                if isinstance(answer, Exception):
                    raise answer
                body, headers = answer if isinstance(answer, tuple) else (answer, {})
                return Response(body if isinstance(body, bytes) else json.dumps(body).encode(), headers)
        raise AssertionError(f"unexpected request {request.full_url}")


def canvas_routes(due):
    return [
        ("/courses?", [{"id": 7, "course_code": "ENGL&101"}, {"id": 8, "name": "Locked", "access_restricted_by_date": True}]),
        ("/courses/7/assignments", [assignment(1, "Essay", due), assignment(2, "Quiz", due, state="submitted")]),
        ("/courses/7/modules", [{"id": 3, "name": "Module 4 (DUE OCT 30)"}]),
    ]


def school(reg, monkeypatch, routes, secrets=None):
    app = reg.apps["school"]
    opener = FakeOpener(routes)
    monkeypatch.setattr(app.module, "_opener", lambda: opener)
    app.ctx.secrets = {"CANVAS_TOKEN": "tok", **(secrets or {})}
    return app, opener


def job(app, name, now=NOW, reason="every"):
    return getattr(app.module, name)(app.ctx, JobRun(now, None, reason, False))


def keys(reg):
    return [r[0] for r in reg.conn.execute("SELECT key FROM school_deadlines ORDER BY due, key")]


def test_sync_stores_this_terms_deadlines_and_wakes_the_nudge(real_apps, monkeypatch):
    app, opener = school(real_apps, monkeypatch, canvas_routes(NOW + timedelta(days=2)))
    woke = []
    app.ctx.wake_hook = lambda a, j: woke.append((a, j))
    result = job(app, "sync")
    assert result.summary == "3 deadlines, 3 new" and woke == [("school", "nudge")]
    assert sorted(keys(real_apps)) == ["canvas:1", "canvas:2", "module:7:3"]
    assert all(auth == "Bearer tok" and timeout == 20 for _, auth, timeout in opener.requests)
    assert all(url.startswith("https://canvas.seattlecolleges.edu/api/v1/") for url, _, _ in opener.requests)
    assert not any("/courses/8/" in url for url, _, _ in opener.requests)  # the locked course is skipped
    assert job(app, "sync").summary == "3 deadlines, 0 new"


def test_pagination_follows_the_link_header(real_apps, monkeypatch):
    nxt = 'https://canvas.seattlecolleges.edu/api/v1/courses?page=2&per_page=100'
    app, opener = school(real_apps, monkeypatch, [
        ("page=2", [{"id": 9, "course_code": "ECON 100"}]),
        ("/courses?", ([{"id": 7, "course_code": "ENGL&101"}], {"Link": f'<{nxt}>; rel="next"'})),
        ("/assignments", []), ("/modules", []),
    ])
    job(app, "sync")
    courses = [u for u, _, _ in opener.requests if "/assignments" in u]
    assert len(courses) == 2


def test_a_resync_replaces_canvas_rows_but_keeps_what_was_marked_done(real_apps, monkeypatch):
    due = NOW + timedelta(days=2)
    app, _ = school(real_apps, monkeypatch, canvas_routes(due))
    job(app, "sync")
    actions.run(real_apps, "school__mark_done", {"key": "module:7:3"}, CLI)
    school(real_apps, monkeypatch, [("/courses?", [{"id": 7, "course_code": "ENGL&101"}]),
                                    ("/assignments", [assignment(1, "Essay (renamed)", due)]),
                                    ("/modules", [{"id": 3, "name": "Module 4 (DUE OCT 30)"}])])
    assert job(app, "sync").summary == "2 deadlines, 0 new"
    assert sorted(keys(real_apps)) == ["canvas:1", "module:7:3"]  # the quiz is gone from Canvas
    rows = {r["key"]: r for r in real_apps.conn.execute("SELECT * FROM school_deadlines")}
    assert rows["canvas:1"]["title"] == "Essay (renamed)" and rows["module:7:3"]["hand_done"] == 1


def test_the_feed_is_the_fallback_without_a_token(real_apps, monkeypatch):
    from test_canvas import FEED  # the ported feed fixture

    app, opener = school(real_apps, monkeypatch, [("feed.ics", FEED)])
    app.ctx.secrets = {"CANVAS_ICS_URL": "https://canvas.test/feed.ics"}
    assert job(app, "sync").summary.startswith("3 deadlines, 3 new")  # (no browser to try here)
    assert opener.requests[0][1] is None  # no token sent
    rows = real_apps.conn.execute("SELECT trackable, kind FROM school_deadlines").fetchall()
    assert {r["trackable"] for r in rows} == {0} and {r["kind"] for r in rows} == {"due", "event"}


def test_without_any_secret_the_job_is_quiet_and_the_action_explains(real_apps, monkeypatch):
    app, _ = school(real_apps, monkeypatch, [])
    app.ctx.secrets = {}
    assert job(app, "sync").summary.startswith("not set up")
    out = actions.run(real_apps, "school__sync_now", {}, CLI)
    assert isinstance(out, actions.Failed) and "CANVAS_ICS_URL (the Calendar Feed link)" in out.message


@pytest.mark.parametrize("error,fragment", [
    (urllib.error.HTTPError("u", 401, "Unauthorized", {}, None), "401 Unauthorized"),
    (urllib.error.URLError("nodename nor servname provided"), "Canvas can't be reached (nodename"),
    (TimeoutError("slow"), "Canvas can't be reached"),
])
def test_http_errors_become_plain_failures(real_apps, monkeypatch, error, fragment):
    from jarvis.contract.api import ActionError

    app, _ = school(real_apps, monkeypatch, [("/courses?", error)])
    with pytest.raises(ActionError, match=re.escape(fragment)):
        job(app, "sync")
    assert keys(real_apps) == []  # a failed fetch leaves what we had


def test_sync_now_runs_the_same_sync(real_apps, monkeypatch):
    school(real_apps, monkeypatch, canvas_routes(NOW + timedelta(days=2)))
    out = actions.run(real_apps, "school__sync_now", {}, CLI)
    assert isinstance(out, actions.Ran) and out.result.text == "Synced Canvas: 3 deadlines, 3 new."


def seed(reg, key, title, due, status="unsubmitted", trackable=1, kind="due", hand_done=0, source="canvas"):
    reg.conn.execute(
        "INSERT INTO school_deadlines (key, course, title, due, url, status, trackable, kind, hand_done, source, "
        "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (key, "ENGL&101", title, due.astimezone(timezone.utc).isoformat(), None, status, trackable, kind,
         hand_done, source, "2026-10-01T00:00:00+00:00"))


def test_nudges_at_24_hours_and_3_hours_and_each_only_once(real_apps):
    app = real_apps.apps["school"]
    seed(real_apps, "canvas:1", "Essay", NOW + timedelta(hours=20))
    seed(real_apps, "canvas:2", "Reading", NOW + timedelta(hours=20), trackable=0, status="unknown")
    seed(real_apps, "canvas:3", "Far off", NOW + timedelta(days=3))
    seed(real_apps, "canvas:4", "Done", NOW + timedelta(hours=2), status="submitted")
    seed(real_apps, "canvas:5", "Gone", NOW - timedelta(hours=2))
    seed(real_apps, "feed:6", "Review session", NOW + timedelta(hours=2), kind="event", trackable=0)
    first = job(app, "nudge")
    assert sorted(first.messages) == [
        "ENGL&101: Essay due tomorrow at 8:00 AM.",
        "ENGL&101: Reading due tomorrow at 8:00 AM (check it's done).",
    ]
    assert job(app, "nudge").messages == ()  # once
    later = job(app, "nudge", now=NOW + timedelta(hours=18, minutes=30))  # now 1.5 hours before
    assert later.messages == ("ENGL&101: Essay is due at 8:00 AM and isn't submitted.",)  # trackable only
    assert job(app, "nudge", now=NOW + timedelta(hours=18, minutes=45)).messages == ()
    assert job(app, "nudge").summary == "sent 0"


def test_work_due_within_3_hours_that_never_got_the_24_hour_nudge_gets_the_3_hour_one(real_apps):
    seed(real_apps, "canvas:1", "Essay", NOW + timedelta(hours=2))
    assert job(real_apps.apps["school"], "nudge").messages == ("ENGL&101: Essay is due at 2:00 PM and isn't submitted.",)


def test_a_hand_marked_item_is_not_nudged(real_apps):
    seed(real_apps, "manual:1", "Reading", NOW + timedelta(hours=5), trackable=0, status="unknown", source="manual",
         hand_done=1)
    assert job(real_apps.apps["school"], "nudge").messages == ()


def test_upcoming_is_the_digest_and_untrusted(real_apps):
    seed(real_apps, "canvas:1", "Essay", datetime.now(timezone.utc) + timedelta(days=1))
    out = actions.run(real_apps, "school__upcoming", {}, CLI)
    assert "Essay" in out.result.text and "[canvas:1]" in out.result.text and "still to do" in out.result.text
    assert out.result.data["deadlines"][0]["key"] == "canvas:1"
    spec = [t for t in real_apps.actions() if t.name == "school__upcoming"][0]
    assert spec.untrusted_output and spec.effect == "read"


def test_mark_done_changes_the_digest_and_undo_puts_it_back(real_apps):
    seed(real_apps, "canvas:1", "Reading", datetime.now(timezone.utc) + timedelta(days=1), trackable=0, status="unknown")
    out = actions.run(real_apps, "school__mark_done", {"key": "canvas:1"}, CLI)
    assert isinstance(out, actions.Ran) and "Reading" in out.result.text
    assert "(done)" in actions.run(real_apps, "school__upcoming", {}, CLI).result.text
    assert isinstance(actions.undo(real_apps), actions.Ran)
    assert "(done)" not in actions.run(real_apps, "school__upcoming", {}, CLI).result.text
    assert "don't have a deadline" in actions.run(real_apps, "school__mark_done", {"key": "nope"}, CLI).message


def test_hand_made_twins_of_canvas_items_are_merged_on_read(real_apps):
    due = datetime.now(timezone.utc) + timedelta(days=1)
    seed(real_apps, "canvas:1", "Lyric Essay Draft", due)
    seed(real_apps, "manual:x", "Lyric Essay Draft (rough)", due, source="manual", trackable=0, status="unknown",
         hand_done=1)
    app = real_apps.apps["school"]
    merged = app.module.load_deadlines(app.ctx)
    assert [(d.key, d.done) for d in merged] == [("canvas:1", True)]


def test_context_lists_the_next_48_hours_at_most_six(real_apps):
    app = real_apps.apps["school"]
    assert app.module.context(app.ctx) == ""
    now = datetime.now(timezone.utc)
    for i in range(8):
        seed(real_apps, f"canvas:{i}", f"Item {i}", now + timedelta(hours=1 + i))
    seed(real_apps, "canvas:99", "Next week", now + timedelta(days=5))
    block = app.module.context(app.ctx)
    assert block.count("\n- ") == 6 and "Item 0" in block and "Item 6" not in block and "Next week" not in block


def test_the_migration_copies_the_old_deadlines_and_their_done_flags(settings):
    from jarvis.core import db
    from jarvis.core.apps import load_apps

    conn = db.connect(settings)
    conn.execute("INSERT INTO deadlines (key, course, title, due, url, source, done, updated_at) VALUES "
                 "('manual:a','English','Essay','2026-10-30T23:59:00-07:00',NULL,'weekly',1,'2026-10-01T00:00:00+00:00')")
    conn.close()
    reg = load_apps(settings)
    try:
        row = reg.conn.execute("SELECT * FROM school_deadlines").fetchone()
        assert (row["key"], row["hand_done"], row["source"], row["kind"]) == ("manual:a", 1, "manual", "due")
        assert reg.conn.execute("SELECT COUNT(*) FROM deadlines").fetchone()[0] == 1
    finally:
        reg.close()


WEEKLY = [
    {"course": "ECON 100", "title": "Module 5: Smartwork", "due": "2026-11-01 23:59"},
    {"course": "CMST& 101", "title": "Weekly Homework 4 (real date)", "due": "2026-10-19", "key": "canvas:901",
     "url": "https://canvas.test/a/901"},
    {"course": "English", "title": "Freewrite journal", "due": "2026-10-28T23:59:00-07:00", "done": True},
]


def test_import_saves_weekly_rows_and_undo_puts_everything_back(real_apps):
    seed(real_apps, "canvas:901", "Weekly Homework Assignments", NOW + timedelta(days=50), trackable=0)
    before = [tuple(r) for r in real_apps.conn.execute("SELECT * FROM school_deadlines ORDER BY key")]
    out = actions.run(real_apps, "school__import_deadlines", {"items": WEEKLY}, CLI)
    assert isinstance(out, actions.Ran) and out.result.text == "Saved 3 deadlines."
    rows = {r["key"]: r for r in real_apps.conn.execute("SELECT * FROM school_deadlines")}
    assert rows["canvas:901"]["due"] == "2026-10-19T23:59:00-07:00" and rows["canvas:901"]["source"] == "weekly"
    assert rows["manual:econ-100-module-5-smartwork:20261101"]["due"] == "2026-11-01T23:59:00-08:00"  # after DST ends
    assert rows["manual:english-freewrite-journal:20261028"]["hand_done"] == 1
    assert isinstance(actions.undo(real_apps), actions.Ran)
    assert [tuple(r) for r in real_apps.conn.execute("SELECT * FROM school_deadlines ORDER BY key")] == before


def test_a_sync_keeps_the_weekly_date_and_rows(real_apps, monkeypatch):
    from test_canvas import FEED

    actions.run(real_apps, "school__import_deadlines", {"items": WEEKLY}, CLI)
    app, _ = school(real_apps, monkeypatch, [("feed.ics", FEED)])
    app.ctx.secrets = {"CANVAS_ICS_URL": "https://canvas.test/feed.ics"}
    job(app, "sync")
    rows = {r["key"]: r for r in real_apps.conn.execute("SELECT * FROM school_deadlines")}
    assert rows["canvas:901"]["due"] == "2026-10-19T23:59:00-07:00"  # the feed's placeholder date didn't win
    assert rows["canvas:901"]["title"] == "Weekly Homework 4 (real date)"
    assert "manual:econ-100-module-5-smartwork:20261101" in rows  # the sync only drops Canvas's own rows
    assert sum(k == "canvas:901" for k in rows) == 1


def test_a_reimport_without_done_keeps_what_was_marked_done(real_apps):
    actions.run(real_apps, "school__import_deadlines", {"items": WEEKLY[2:]}, CLI)
    again = [{k: v for k, v in WEEKLY[2].items() if k != "done"}]
    actions.run(real_apps, "school__import_deadlines", {"items": again}, CLI)
    assert real_apps.conn.execute("SELECT hand_done FROM school_deadlines").fetchone()[0] == 1


def test_bad_due_dates_are_refused_in_plain_words(real_apps):
    out = actions.run(real_apps, "school__import_deadlines",
                      {"items": [{"course": "X", "title": "Y", "due": "next Tuesday"}]}, CLI)
    assert isinstance(out, actions.Failed) and "couldn't read 'next Tuesday'" in out.message


def test_feed_deadlines_never_get_the_unsubmitted_nudge(real_apps):
    seed(real_apps, "canvas:5", "Essay", NOW + timedelta(hours=2), trackable=0)
    app = real_apps.apps["school"]
    first = job(app, "nudge")
    assert first.messages == ("ENGL&101: Essay due today at 2:00 PM (check it's done).",)
    assert job(app, "nudge", NOW + timedelta(minutes=30)).messages == ()
