import os
import sys

import pytest
from conftest import MAC, PROBE_DIR

from jarvis.apps.terminal import actions as terminal
from jarvis.apps.terminal import run as runner
from jarvis.apps.terminal.grade import Verdict
from jarvis.contract.api import ActionError


@MAC
def test_a_safe_command_runs_without_asking_in_the_sandbox_and_is_audited(ctx, project):
    out = terminal.run(ctx, "git status --short --branch", str(project))
    assert out.data["exit"] == 0 and "[exit 0]" in out.text and "master" in out.text or "main" in out.text
    entry = [__import__("json").loads(ln) for ln in (ctx.data_dir / "commands.jsonl").read_text().splitlines()][-1]
    assert entry["by"] == "auto" and entry["risk"] == "low" and "output" not in entry


@MAC
def test_a_project_name_is_enough_for_cwd(ctx, project):
    assert "README.md" in terminal.run(ctx, "ls", "proj").text


@MAC
def test_git_dash_c_runs_from_anywhere(ctx, project):
    out = terminal.run(ctx, f"git -C {project} log --oneline -n 1", "")
    assert out.data["exit"] == 0 and "first" in out.text


def test_a_command_that_needs_a_yes_is_not_run_and_says_what_to_do(ctx, project):
    out = terminal.run(ctx, "git push origin main", str(project))
    assert out.text.startswith("This needs Elijah's OK (") and "use run_with_ok with a short why" in out.text
    assert out.data["needs_ok"] is True


def test_secrets_and_shell_syntax_are_refused_with_the_reason(ctx, project):
    with pytest.raises(ActionError, match="may hold secrets"):
        terminal.run(ctx, "cat .env", str(project))
    with pytest.raises(ActionError, match="shell syntax"):
        terminal.run(ctx, "ls | wc -l", str(project))


@MAC
def test_a_push_with_the_yes_runs_as_elijah_and_fails_plainly_without_a_remote(ctx, project):
    out = terminal.run_with_ok(ctx, "git push", str(project), "push the README fix")
    assert out.data["exit"] != 0 and "[exit" in out.text and "push" in out.text.lower()


def test_run_with_ok_still_refuses_secrets(ctx, project):
    with pytest.raises(ActionError, match="may hold secrets"):
        terminal.run_with_ok(ctx, "cat .env", str(project), "look")


def test_rm_inside_the_root_moves_to_the_trash_and_undo_puts_it_back(ctx, project, trash):
    (project / "notes.txt").write_text("keep me")
    out = terminal.run(ctx, "rm notes.txt", str(project))
    assert not (project / "notes.txt").exists() and [p.name for p in trash.iterdir()] == ["notes.txt"]
    assert "Trash" in out.text and out.undo == {"moves": [[str(trash / "notes.txt"), str(project / "notes.txt")]]}
    assert out.subject.startswith("trash:")
    back = terminal.undo_run(ctx, out.undo)
    assert (project / "notes.txt").read_text() == "keep me" and "Put back notes.txt" in back.text


def test_a_name_already_in_the_trash_gets_a_unique_one(ctx, project, trash):
    trash.mkdir()
    (trash / "notes.txt").write_text("older")
    (project / "notes.txt").write_text("newer")
    out = terminal.run(ctx, "rm notes.txt", str(project))
    assert (trash / "notes.txt").read_text() == "older" and len(list(trash.iterdir())) == 2
    terminal.undo_run(ctx, out.undo)
    assert (project / "notes.txt").read_text() == "newer"


def test_undo_will_not_overwrite_something_new(ctx, project, trash):
    (project / "a.txt").write_text("1")
    out = terminal.run(ctx, "rm a.txt", str(project))
    (project / "a.txt").write_text("2")
    with pytest.raises(ActionError, match="already at"):
        terminal.undo_run(ctx, out.undo)
    assert (project / "a.txt").read_text() == "2"


def test_rm_outside_the_root_asks_and_deletes_nothing(ctx, project, trash, tmp_path):
    other = tmp_path / "other.txt"
    other.write_text("x")
    out = terminal.run(ctx, f"rm {other}", str(project))
    assert other.exists() and "needs Elijah's OK" in out.text and not trash.exists()


def test_deleting_something_that_isnt_there_says_so(ctx, project, trash):
    with pytest.raises(ActionError, match="doesn't exist"):
        terminal.run(ctx, "rm ghost.txt", str(project))


@MAC
def test_a_low_command_in_the_read_sandbox_cannot_write_a_file(project):
    probe = PROBE_DIR / f".probe-read-{os.getpid()}"
    try:
        v = Verdict("auto", "t", [sys.executable, "-c", f"open({str(probe)!r}, 'w').write('x')"], risk="low")
        result = runner.run_command(v, project, project / "log.jsonl", approved=False)
        assert result.code != 0 and not probe.exists()
    finally:
        probe.unlink(missing_ok=True)


@MAC
def test_a_medium_command_writes_in_its_root_but_not_beside_it(project):
    probe = PROBE_DIR / f".probe-write-{os.getpid()}"
    try:
        code = f"open('made.txt','w').write('x'); open({str(probe)!r}, 'w').write('x')"
        v = Verdict("auto", "t", [sys.executable, "-c", code], risk="medium", sandbox="write", root=str(project))
        result = runner.run_command(v, project, project / "log.jsonl", approved=False)
        assert result.code != 0 and not probe.exists()
        ok = Verdict("auto", "t", [sys.executable, "-c", "open('made.txt','w').write('x')"], risk="medium",
                     sandbox="write", root=str(project))
        assert runner.run_command(ok, project, project / "log.jsonl", approved=False).code == 0
        assert (project / "made.txt").read_text() == "x"
        (project / ".git" / "hooks").mkdir(exist_ok=True)
        hook = Verdict("auto", "t", ["touch", ".git/hooks/pre-commit"], risk="medium", sandbox="write", root=str(project))
        assert runner.run_command(hook, project, project / "log.jsonl", approved=False).code != 0
    finally:
        probe.unlink(missing_ok=True)


@MAC
def test_the_read_sandbox_cannot_open_env_files_even_by_another_route(project):
    (project / ".env").write_text("SECRET_TOKEN=abc123456789\n")
    v = Verdict("auto", "t", [sys.executable, "-c", "print(open('.env').read())"], risk="low")
    result = runner.run_command(v, project, project / "log.jsonl", approved=False)
    assert result.code != 0 and "abc123456789" not in result.output


@MAC
def test_output_is_redacted_and_capped(project):
    (project / "notes.txt").write_text("key sk-ant-" + "z" * 30 + "\n" + "line\n" * 3000)
    v = Verdict("auto", "t", ["cat", "notes.txt"], risk="low")
    result = runner.run_command(v, project, project / "log.jsonl", approved=False)
    assert "zzzz" not in result.output and len(result.output) < 6200 and "(cut;" in result.output


def test_an_approval_command_cannot_run_without_the_yes(project):
    with pytest.raises(ValueError):
        runner.run_command(Verdict("approval", "t", ["ls"]), project, project / "log.jsonl", approved=False)


@MAC
def test_ps_works_even_though_the_sandbox_cant_launch_it(ctx, project):
    out = terminal.run(ctx, "ps -o pid,etime -p 1", str(project))
    assert out.data["exit"] == 0 and "PID" in out.text


def test_repo_state_and_find_repos(ctx, project):
    state = terminal.repo_state(ctx, str(project))
    assert state.data["branch"] in ("main", "master") and state.data["subject"] == "first" and state.data["changed"] == 0
    (project / "new.txt").write_text("x")
    assert terminal.repo_state(ctx, str(project)).data["changed"] == 1
    assert terminal.find_repos(ctx).data == {"repos": [str(project)]}
    with pytest.raises(ActionError, match="isn't a git repo"):
        terminal.repo_state(ctx, str(project.parent))
