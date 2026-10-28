"""Projects: Elijah's projects and repos, carried over from the old database, with live git state from the Mac."""

from __future__ import annotations

from datetime import timedelta, timezone
from pathlib import Path

from jarvis.contract.api import ActionError, AppContext, AppUnavailable, JobResult, JobRun, Result

SUMMARY_MAX = 140


def _utc(ctx: AppContext) -> str:
    return ctx.now().astimezone(timezone.utc).isoformat(timespec="seconds")


def _find(ctx: AppContext, name: str):
    """The row whose name is exactly `name`, else the one whose name has it as a (case-insensitive) prefix, else as a
    substring. More than one at the same level is a question for Elijah."""
    rows = ctx.db.execute("SELECT * FROM projects_items ORDER BY name COLLATE NOCASE").fetchall()
    low = name.strip().lower()
    for match in (lambda n: n == low, lambda n: n.startswith(low), lambda n: low in n):
        found = [r for r in rows if match(r["name"].lower())]
        if len(found) == 1:
            return found[0]
        if found:
            raise ActionError(f"'{name}' matches {len(found)} projects: " + ", ".join(r["name"] for r in found)
                              + ". Ask Elijah which.")
    raise ActionError(f"There is no project like '{name}'.")


def _line(r) -> str:
    summary = r["summary"].strip().replace("\n", " ")
    return f"- {r['name']} [{r['status']}, {r['kind']}]" + (f": {summary[:SUMMARY_MAX]}" if summary else "")


def list_projects(ctx: AppContext, status: str = "") -> Result:
    rows = ctx.db.execute("SELECT * FROM projects_items WHERE (?='' OR status=?) ORDER BY name COLLATE NOCASE",
                          (status, status)).fetchall()
    return Result("\n".join(_line(r) for r in rows) or "No projects" + (f" with status {status}." if status else " yet."),
                  {"projects": [r["name"] for r in rows]})


def _git_state(ctx: AppContext, path: str | None) -> str:
    if not path:
        return "(no folder, so no git state)"
    try:
        return ctx.call("terminal", "repo_state", path=path).text
    except AppUnavailable:
        return "(the Mac isn't connected, so no live git state)"
    except ActionError as e:
        return f"(no live git state: {e})"


def show_project(ctx: AppContext, name: str) -> Result:
    r = _find(ctx, name)
    parts = [f"{r['name']} [{r['status']}, {r['kind']}]", f"Folder: {r['path'] or '(none)'}"]
    if r["summary"].strip():
        parts.append(f"Summary: {r['summary'].strip()}")
    parts.append(f"Git: {_git_state(ctx, r['path'])}")
    if r["notes"].strip():
        parts.append(f"Notes:\n{r['notes'].strip()}")
    return Result("\n".join(parts), {"name": r["name"], "path": r["path"], "status": r["status"]})


def update_project(ctx: AppContext, name: str, status: str | None = None, summary: str | None = None,
                   notes: str | None = None) -> Result:
    r = _find(ctx, name)
    changes = {k: v for k, v in (("status", status), ("summary", summary), ("notes", notes)) if v is not None}
    if not changes:
        raise ActionError("Nothing to change: give a status, summary or notes.")
    before = {k: r[k] for k in changes}
    ctx.db.execute(f"UPDATE projects_items SET {', '.join(f'{k}=?' for k in changes)}, updated_at=? WHERE id=?",
                   (*changes.values(), _utc(ctx), r["id"]))
    return Result(f"Updated {r['name']}: " + ", ".join(changes) + ".", undo={"id": r["id"], "before": before},
                  subject=f"project:{r['id']}")


def undo_update_project(ctx: AppContext, undo: dict) -> Result:
    before = undo["before"]
    ctx.db.execute(f"UPDATE projects_items SET {', '.join(f'{k}=?' for k in before)}, updated_at=? WHERE id=?",
                   (*before.values(), _utc(ctx), undo["id"]))
    return Result("Put the project back the way it was.")


def add_project(ctx: AppContext, name: str, path: str, kind: str) -> Result:
    name = name.strip()
    if ctx.db.execute("SELECT 1 FROM projects_items WHERE name=? COLLATE NOCASE", (name,)).fetchone():
        raise ActionError(f"There is already a project called {name}.")
    folder = str(Path(path).expanduser()) if path.strip() else None
    pid = ctx.db.execute("INSERT INTO projects_items (name, path, kind, updated_at) VALUES (?,?,?,?)",
                         (name, folder, kind, _utc(ctx))).lastrowid
    return Result(f"Added the project {name}.", undo={"id": pid}, subject=f"project:{pid}")


def undo_add_project(ctx: AppContext, undo: dict) -> Result:
    ctx.db.execute("DELETE FROM projects_items WHERE id=?", (undo["id"],))
    return Result("Removed the project again.")


RETRY = timedelta(minutes=10)


def sync(ctx: AppContext, run: JobRun) -> JobResult:
    """Add a project for every repo the Mac finds that isn't one yet. Says nothing to Elijah."""
    try:
        repos = ctx.call("terminal", "find_repos").data["repos"]
    except AppUnavailable:
        # The host starts before the Mac connects, so a sync at startup always lands here; try again soon instead of
        # in six hours.
        return JobResult("the Mac isn't connected; trying again in 10 minutes", wake_at=run.now + RETRY)
    known = {p for (p,) in ctx.db.execute("SELECT path FROM projects_items WHERE path IS NOT NULL")}
    taken = {n.lower() for (n,) in ctx.db.execute("SELECT name FROM projects_items")}
    new = 0
    for path in repos:
        if path.rstrip("/") in {k.rstrip("/") for k in known}:
            continue
        folder = Path(path)
        name = folder.name if folder.name.lower() not in taken else f"{folder.name} ({folder.parent.name})"
        ctx.db.execute("INSERT INTO projects_items (name, path, updated_at) VALUES (?,?,?)", (name, path, _utc(ctx)))
        taken.add(name.lower())
        new += 1
    return JobResult(f"{len(repos)} repos, {new} new")
