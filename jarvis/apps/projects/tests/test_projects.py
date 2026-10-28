from datetime import datetime, timedelta, timezone

import pytest

from jarvis.contract.api import ActionError, JobRun, Result
from jarvis.core import actions, db
from jarvis.core.apps import load_apps

CLI = actions.Origin("cli")
NOW = datetime(2026, 10, 27, 9, 0, tzinfo=timezone.utc)


def do(reg, action, **args):
    return actions.run(reg, f"projects__{action}", args, CLI)


def seed_legacy(settings):
    conn = db.connect(settings)  # the old table is in the baseline schema
    rows = [
        (1, "Vaulted", "business", "stalled", "A vault.", "ship v1", "/Users/e/Desktop/Vaulted", "The brief."),
        (2, "JARVIS", "personal", "active", "The assistant.", "", "/Users/e/Code/JARVIS", "Brief two."),
        (3, "Plain", "school", "paused", "", "", None, None),
    ]
    for id_, name, kind, status, summary, goals, repo, brief in rows:
        conn.execute("INSERT INTO projects (id, name, kind, status, summary, goals, updated_at, repo, brief) "
                     "VALUES (?,?,?,?,?,?,?,?,?)", (id_, name, kind, status, summary, goals, "2026-10-01T00:00:00+00:00",
                                                    repo, brief))
    conn.close()


@pytest.fixture
def projects(settings):
    seed_legacy(settings)
    reg = load_apps(settings)
    yield reg
    reg.close()


def rows(reg):
    return [dict(r) for r in reg.conn.execute("SELECT * FROM projects_items ORDER BY id")]


def test_the_old_projects_are_copied_in_once_and_the_legacy_table_is_left_alone(settings):
    seed_legacy(settings)
    reg = load_apps(settings)
    first = rows(reg)
    assert [(r["name"], r["path"], r["kind"], r["status"]) for r in first] == [
        ("Vaulted", "/Users/e/Desktop/Vaulted", "business", "stalled"),
        ("JARVIS", "/Users/e/Code/JARVIS", "personal", "active"), ("Plain", None, "school", "paused")]
    assert first[0]["notes"] == "The brief.\n\nGoals: ship v1" and first[1]["notes"] == "Brief two." and first[2]["notes"] == ""
    assert do(reg, "update_project", name="plain", summary="Changed here").result.text.startswith("Updated Plain")
    reg.conn.execute("INSERT INTO projects (id, name, kind, updated_at) VALUES (9, 'Later', 'personal', 'x')")
    reg.close()
    again = load_apps(settings)  # a restart must not copy anything again, or undo Elijah's changes
    assert [r["name"] for r in rows(again)] == ["Vaulted", "JARVIS", "Plain"] and rows(again)[2]["summary"] == "Changed here"
    assert again.conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 4
    again.close()


def test_list_projects_gives_one_line_each_and_filters_by_status(projects):
    out = do(projects, "list_projects").result
    assert out.text.splitlines()[0] == "- JARVIS [active, personal]: The assistant."
    assert "- Plain [paused, school]" in out.text and out.data["projects"] == ["JARVIS", "Plain", "Vaulted"]
    assert do(projects, "list_projects", status="stalled").result.text == "- Vaulted [stalled, business]: A vault."
    assert do(projects, "list_projects", status="done").result.text == "No projects with status done."


@pytest.mark.parametrize("said,found", [("vaulted", "Vaulted"), ("VAULT", "Vaulted"), ("arvis", "JARVIS"),
                                        ("jar", "JARVIS"), ("lain", "Plain")])
def test_names_match_loosely_by_prefix_then_substring(projects, said, found):
    assert do(projects, "show_project", name=said).result.data["name"] == found


def test_an_ambiguous_or_unknown_name_is_a_plain_failure(projects):
    projects.conn.execute("INSERT INTO projects_items (name, updated_at) VALUES ('Vaulted firmware', 'x')")
    assert "matches 2 projects: Vaulted, Vaulted firmware" in do(projects, "show_project", name="vault").message
    assert do(projects, "show_project", name="Vaulted").result.data["name"] == "Vaulted"  # exact wins
    assert "no project like 'zzz'" in do(projects, "show_project", name="zzz").message


def test_show_project_adds_live_git_state_when_the_mac_is_there(projects):
    seen = []

    def terminal(caller, app, action, args, conn):
        seen.append((caller, app, action, args))
        return Result("Vaulted: on main, nothing uncommitted", {})

    projects.apps["projects"].ctx.call_hook = terminal
    text = do(projects, "show_project", name="vaulted").result.text
    assert seen == [("projects", "terminal", "repo_state", {"path": "/Users/e/Desktop/Vaulted"})]
    assert "Git: Vaulted: on main, nothing uncommitted" in text and "Folder: /Users/e/Desktop/Vaulted" in text
    assert "Notes:\nThe brief." in text


def test_show_project_says_so_when_the_terminal_app_is_missing(projects):
    text = do(projects, "show_project", name="vaulted").result.text  # no node is connected in the tests
    assert "Git: (the Mac isn't connected, so no live git state)" in text and "The brief." in text


def test_show_project_survives_a_repo_the_mac_cant_read_and_a_project_with_no_folder(projects):
    def broken(*a):
        raise ActionError("that isn't a git repo")

    projects.apps["projects"].ctx.call_hook = broken
    assert "(no live git state: that isn't a git repo)" in do(projects, "show_project", name="vaulted").result.text
    assert "Git: (no folder, so no git state)" in do(projects, "show_project", name="plain").result.text


def test_update_project_changes_only_what_is_given_and_can_be_undone(projects):
    out = do(projects, "update_project", name="vaulted", status="active", notes="New notes.")
    assert out.result.text == "Updated Vaulted: status, notes."
    row = rows(projects)[0]
    assert (row["status"], row["notes"], row["summary"]) == ("active", "New notes.", "A vault.")
    assert isinstance(actions.undo(projects), actions.Ran)
    row = rows(projects)[0]
    assert (row["status"], row["notes"], row["summary"]) == ("stalled", "The brief.\n\nGoals: ship v1", "A vault.")
    assert "Nothing to change" in do(projects, "update_project", name="vaulted").message


def test_add_project_and_its_undo(projects):
    out = do(projects, "add_project", name="Falak", path="~/Developer/falak", kind="personal")
    assert isinstance(out, actions.Ran) and rows(projects)[-1]["name"] == "Falak"
    assert rows(projects)[-1]["path"].endswith("/Developer/falak") and not rows(projects)[-1]["path"].startswith("~")
    assert "already a project called falak" in do(projects, "add_project", name="falak", path="", kind="other").message
    actions.undo(projects)
    assert [r["name"] for r in rows(projects)] == ["Vaulted", "JARVIS", "Plain"]


def sync(reg, repos=None, unavailable=False):
    app = reg.apps["projects"]

    def hook(caller, name, action, args, conn):
        if unavailable:
            from jarvis.contract.api import AppUnavailable
            raise AppUnavailable("no mac")
        return Result("repos", {"repos": repos})

    app.ctx.call_hook = hook
    return app.module.sync(app.ctx, JobRun(NOW, None, "every", False))


def test_sync_adds_a_project_for_each_new_repo_and_says_nothing(projects):
    out = sync(projects, ["/Users/e/Desktop/Vaulted", "/Users/e/Developer/falak", "/Users/e/local_dev/falak"])
    assert out.summary == "3 repos, 2 new" and out.messages == () and out.think is None
    added = [(r["name"], r["status"], r["kind"]) for r in rows(projects)[3:]]
    assert added == [("falak", "unknown", "other"), ("falak (local_dev)", "unknown", "other")]
    assert sync(projects, ["/Users/e/Developer/falak/"]).summary == "1 repos, 0 new"  # already known, trailing slash or not


def test_sync_with_no_mac_connected_tries_again_soon(projects):
    out = sync(projects, unavailable=True)
    assert out.summary.startswith("the Mac isn't connected") and out.messages == ()
    assert out.wake_at == NOW + timedelta(minutes=10)  # not six hours later: the host starts before the Mac connects
    assert len(rows(projects)) == 3


def test_the_sync_job_is_scheduled_every_six_hours_and_uses_terminal(projects):
    manifest = projects.apps["projects"].manifest
    (job,) = manifest.jobs
    assert (job.name, job.every, job.kind, job.quiet) == ("sync", 6 * 3600, "sync", "ignore")
    assert manifest.uses == ("terminal",)
