"""Canvas through Falak's browser: the sync asks browser.fetch_json, falls back to the feed, and says so once."""

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from test_school import NOW, assignment, job  # the same fixtures the token sync is tested with

from jarvis.contract.api import AppUnavailable, JobRun, Result
from jarvis.core import actions

LA = ZoneInfo("America/Los_Angeles")
CLI = actions.Origin("cli")
BASE = "https://canvas.seattlecolleges.edu/api/v1"
FEED_URL = "https://canvas.test/feed.ics"


class FakeBrowser:
    """Stands in for the browser app behind ctx.call: answers Canvas API paths from a table."""

    def __init__(self, routes=(), signed_out=False, unavailable=False, error=None):
        self.routes, self.signed_out, self.unavailable, self.error = list(routes), signed_out, unavailable, error
        self.requests = []

    def __call__(self, caller, app, action, args, conn):
        assert (caller, app, action) == ("school", "browser", "fetch_json")
        if self.unavailable:
            raise AppUnavailable("browser needs a device that isn't connected.")
        if self.error:
            raise self.error
        self.requests.append(args["url"])
        if self.signed_out:
            return Result("signed out", data={"signed_out": True, "listed": True, "host": "canvas.seattlecolleges.edu"})
        for part, answer in self.routes:
            if part in args["url"]:
                body, nxt = answer if isinstance(answer, tuple) else (answer, None)
                return Result("ok", data={"json": body, "next": nxt, "signed_out": False, "listed": True})
        raise AssertionError(f"unexpected request {args['url']}")


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def routes(due, modules=(), announcements=()):
    return [
        ("/courses?", [{"id": 7, "course_code": "ECON 100"}, {"id": 8, "name": "Locked", "access_restricted_by_date": True}]),
        ("/courses/7/assignments", [assignment(1, "Module 2 Quiz", due), assignment(2, "Discussion", due, state="submitted")]),
        ("/courses/7/modules", list(modules)),
        ("/announcements", list(announcements)),
    ]


def setup(reg, browser, secrets=None):
    app = reg.apps["school"]
    app.ctx.secrets = secrets or {}
    app.ctx.call_hook = browser
    return app


def rows(reg):
    return {r["key"]: dict(r) for r in reg.conn.execute("SELECT * FROM school_deadlines")}


def notice(id, title, message="", days_ago=1):
    return {"id": id, "title": title, "message": message, "context_code": "course_7",
            "posted_at": iso(NOW - timedelta(days=days_ago))}


def test_browser_rows_are_trackable_and_say_what_is_submitted(real_apps):
    due = NOW + timedelta(days=2)
    browser = FakeBrowser(routes(due))
    app = setup(real_apps, browser)
    assert job(app, "sync").summary == "2 deadlines, 2 new"
    r = rows(real_apps)
    assert (r["canvas:1"]["status"], r["canvas:1"]["trackable"], r["canvas:1"]["source"]) == ("unsubmitted", 1, "canvas")
    assert (r["canvas:2"]["status"], r["canvas:2"]["trackable"]) == ("submitted", 1)
    assert all(u.startswith(BASE) for u in browser.requests) and not any("/courses/8/" in u for u in browser.requests)


def test_browser_rows_replace_the_feeds_copy_of_the_same_assignment(real_apps):
    due = NOW + timedelta(days=2)
    ics = (f"BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:event-assignment-1\nDTSTART:{due.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}\n"
           "SUMMARY:Module 2 Quiz [ECON 100]\nEND:VEVENT\nEND:VCALENDAR\n")
    app = setup(real_apps, FakeBrowser(unavailable=True), {"CANVAS_ICS_URL": FEED_URL})
    app.module.canvas.fetch_feed = lambda url, opener=None: ics.encode()
    job(app, "sync")
    assert rows(real_apps)["canvas:1"]["trackable"] == 0 and rows(real_apps)["canvas:1"]["status"] == "unknown"
    app.ctx.call_hook = FakeBrowser(routes(due))
    job(app, "sync")
    assert (rows(real_apps)["canvas:1"]["status"], rows(real_apps)["canvas:1"]["trackable"]) == ("unsubmitted", 1)
    assert sorted(rows(real_apps)) == ["canvas:1", "canvas:2"]  # one row per assignment, not a feed row beside it


def test_a_module_titles_date_becomes_a_deadline_when_no_assignment_covers_it(real_apps):
    due = datetime(2026, 10, 30, 23, 59, tzinfo=LA)
    modules = [{"id": 11, "name": "Module 2: Chapters 3 & 4 (DUE OCT 30)"},
               {"id": 12, "name": "Module 5: Money and banking (DUE NOV 20)"}, {"id": 13, "name": "Welcome"}]
    app = setup(real_apps, FakeBrowser(routes(due, modules)))
    job(app, "sync")
    r = rows(real_apps)
    assert "module:7:11" not in r  # "Module 2 Quiz" is due that day: the assignment already says it
    assert r["module:7:12"]["kind"] == "due" and r["module:7:12"]["title"] == "Module 5: Money and banking"
    assert r["module:7:12"]["trackable"] == 0 and "module:7:13" not in r


def test_announcements_that_move_a_date_are_kept_and_others_are_not(real_apps):
    ann = [notice(21, "Quiz 3 extended", "<p>Due date is now <b>Nov 4</b>.</p>"), notice(22, "Welcome to week 3", "Read ch. 5"),
           notice(23, "Reminder", "Office hours are rescheduled to Thursday")]
    app = setup(real_apps, FakeBrowser(routes(NOW + timedelta(days=2), announcements=ann)))
    assert job(app, "sync").summary == "2 deadlines, 2 new, 2 announcements about dates"
    saved = {r["id"]: dict(r) for r in real_apps.conn.execute("SELECT * FROM school_notices")}
    assert sorted(saved) == ["canvas:21", "canvas:23"] and saved["canvas:21"]["body"] == "Due date is now Nov 4 ."
    assert saved["canvas:21"]["course"] == "ECON 100"
    assert job(app, "sync").summary == "2 deadlines, 0 new"  # already known: nothing new to say
    assert len(saved) == 2 and rows(real_apps)["canvas:1"]["due"] == iso(NOW + timedelta(days=2)).replace("Z", "+00:00")


def test_an_announcement_never_changes_a_deadline(real_apps):
    due = NOW + timedelta(days=2)
    app = setup(real_apps, FakeBrowser(routes(due, announcements=[notice(21, "Quiz extended to Nov 9")])))
    job(app, "sync")
    assert datetime.fromisoformat(rows(real_apps)["canvas:1"]["due"]) == due


def test_the_date_notices_are_shared_for_check_ins_by_when_they_were_first_seen(real_apps):
    app = setup(real_apps, FakeBrowser(routes(NOW + timedelta(days=2), announcements=[notice(21, "Quiz 3 extended")])))
    job(app, "sync")
    seen = real_apps.conn.execute("SELECT first_seen_at FROM school_notices").fetchone()[0]
    since = lambda delta: (datetime.fromisoformat(seen) + delta).isoformat()
    ctx = real_apps.apps["checkins"].ctx
    (n,) = ctx.call("school", "date_notices", since=since(timedelta(minutes=-1))).data["notices"]
    assert (n["course"], n["title"]) == ("ECON 100", "Quiz 3 extended")
    assert ctx.call("school", "date_notices", since=since(timedelta(minutes=1))).data["notices"] == []


def test_signed_out_falls_back_to_the_feed_and_says_so_once(real_apps):
    due = NOW + timedelta(days=2)
    app = setup(real_apps, FakeBrowser(signed_out=True), {"CANVAS_ICS_URL": FEED_URL})
    app.module.canvas.fetch_feed = lambda url, opener=None: b"BEGIN:VCALENDAR\nEND:VCALENDAR\n"
    first = job(app, "sync")
    assert first.messages == ("Canvas wants you to sign in again; say 'sign in to Canvas' at your Mac.",)
    assert job(app, "sync").messages == ()  # not again until it works
    app.ctx.call_hook = FakeBrowser(routes(due))
    assert job(app, "sync").messages == ()
    app.ctx.call_hook = FakeBrowser(signed_out=True)
    assert len(job(app, "sync").messages) == 1  # it worked, so the next sign-out is news again


def test_no_node_falls_back_quietly_and_tries_again_soon(real_apps):
    # The host starts before the Mac connects, so this happens on every `falak start`: not a reason to nag him.
    app = setup(real_apps, FakeBrowser(unavailable=True), {"CANVAS_ICS_URL": FEED_URL})
    app.module.canvas.fetch_feed = lambda url, opener=None: b"BEGIN:VCALENDAR\nEND:VCALENDAR\n"
    result = job(app, "sync")
    assert result.messages == () and "isn't connected" in result.summary
    assert result.wake_at is not None and result.wake_at - NOW <= timedelta(minutes=10)


def test_a_browser_that_cant_fetch_uses_the_feed_quietly_and_says_why_on_demand(real_apps):
    from jarvis.contract.api import ActionError
    app = setup(real_apps, FakeBrowser(error=ActionError("Falak's browser (Chromium) isn't installed yet.")),
                {"CANVAS_ICS_URL": FEED_URL})
    app.module.canvas.fetch_feed = lambda url, opener=None: b"BEGIN:VCALENDAR\nEND:VCALENDAR\n"
    assert job(app, "sync").messages == ()  # not a sign-in problem: nothing to tell him to do at the Mac
    out = actions.run(real_apps, "school__sync_now", {}, CLI)
    assert "isn't installed yet" in out.result.text and "calendar feed" in out.result.text


def test_nothing_set_up_is_quiet_for_the_job_and_an_error_for_a_request(real_apps):
    app = setup(real_apps, FakeBrowser(unavailable=True))
    result = job(app, "sync")
    assert result.summary.startswith("not set up") and result.messages == ()
    out = actions.run(real_apps, "school__sync_now", {}, CLI)
    assert "isn't set up" in out.message and "isn't connected" in out.message


def test_the_three_hour_nudge_works_for_browser_rows_with_submission_status(real_apps):
    due = NOW + timedelta(hours=2)
    app = setup(real_apps, FakeBrowser(routes(due)))
    job(app, "sync")
    out = job(app, "nudge")
    assert out.messages == ("ECON 100: Module 2 Quiz is due at 2:00 PM and isn't submitted.",)  # not the submitted one


def test_a_token_still_wins_when_there_is_one(real_apps):
    browser = FakeBrowser(routes(NOW))
    app = setup(real_apps, browser, {"CANVAS_TOKEN": "tok"})
    app.module._opener = lambda: (_ for _ in ()).throw(RuntimeError("the token path was taken"))
    with pytest.raises(RuntimeError, match="token path"):
        job(app, "sync")
    assert browser.requests == []


def test_a_module_date_he_already_imported_isnt_listed_twice(real_apps):
    # ECON Module 5 showed up twice: "ECON 100" from his import, "ECON100 1F2F 35268" from the module title.
    due = datetime(2026, 10, 30, 23, 59, tzinfo=LA)
    real_apps.conn.execute(
        "INSERT INTO school_deadlines (key, course, title, due, status, trackable, kind, source, updated_at) "
        "VALUES ('manual:econ-module-5', 'ECON 100', 'Module 5 (Ch. 9 & 10): Smartwork', "
        "'2026-11-20T23:59:00-08:00', 'unknown', 0, 'due', 'manual', '2026-10-28T00:00:00+00:00')")
    modules = [{"id": 12, "name": "Module 5: Money and banking (DUE NOV 20)"}]
    app = setup(real_apps, FakeBrowser(routes(due, modules)))
    job(app, "sync")
    r = rows(real_apps)
    assert "module:7:12" not in r and "manual:econ-module-5" in r


def test_a_browser_sync_keeps_the_school_calendars_events_from_the_feed(real_apps, monkeypatch):
    # Holidays, no-class days and tuition deadlines are only in the feed: 43 of them vanished on the first browser sync.
    due = NOW + timedelta(days=2)
    ics = ("BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:event-calendar-event-205590\nDTSTART;VALUE=DATE:20261222\n"
           "SUMMARY:Tuition deadline - winter quarter\nEND:VEVENT\nEND:VCALENDAR\n")
    app = setup(real_apps, FakeBrowser(routes(due)), {"CANVAS_ICS_URL": FEED_URL})
    monkeypatch.setattr(app.module.canvas, "fetch_feed", lambda url, opener=None: ics.encode())
    monkeypatch.setattr(app.module, "_opener", lambda: None)  # (another test leaves its own in place)
    job(app, "sync")
    r = rows(real_apps)
    events = [v for v in r.values() if v["kind"] == "event"]
    assert [e["title"] for e in events] == ["Tuition deadline - winter quarter"] and "canvas:1" in r
