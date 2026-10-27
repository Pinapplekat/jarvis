import pytest

from jarvis.apps.terminal.grade import classify
from jarvis.apps.terminal.sandbox import is_secret_path


def grade(command, cwd, folders):
    return classify(command, cwd, folders)


@pytest.mark.parametrize("command", [
    "ls", "ls -la", "cat README.md", "grep -rn hi .", "head -n 3 README.md", "wc -l README.md", "find . -name x",
    "git status", "git log -n 3 --oneline", "git diff", "git branch", "git remote -v", "git ls-files",
    "git -C {p} status", "git -C {p} log --oneline -n 5", "git -C {p} diff --stat", "git -C {p} ls-files",
    "pgrep -fl claude", "ps -o pid,etime -p 1", "which git", "screen -ls", "tail -n 60 README.md",
    "sed -n 1,30p README.md", "lsof -nP -iTCP:8787 -sTCP:LISTEN", "lsof -p 1", "lsof -i :8999",
    "open -a Mail", "open README.md", "open {p}", "open -a Mail README.md", "mdfind -name kokoro",
])
def test_these_run_without_asking(command, project, folders):
    v = grade(command.format(p=project), project, folders)
    assert v.kind == "auto" and v.risk == "low", (command, v.reason)


@pytest.mark.parametrize("command", [
    "git add README.md", "git commit -m x", "git -C {p} add README.md", "git -C {p} commit -m x", "touch made.txt",
    "mkdir build", "mv README.md OLD.md", "python3 script.py", "uv sync", "uv run --extra dev pytest -q", "npm install",
])
def test_ordinary_project_work_runs_in_the_write_sandbox(command, project, folders):
    v = grade(command.format(p=project), project, folders)
    assert (v.kind, v.risk) == ("auto", "medium") and v.root == str(project), (command, v.reason)


def test_git_dash_c_only_for_the_path_and_the_other_global_options_stay_refused(project, folders):
    assert grade(f"git -C {project} status", project, folders).kind == "auto"
    for cmd in ("git -c core.pager=evil log", "git --git-dir=/tmp/x log", f"git -C {project} -c a=b log",
                "git --work-tree=/tmp status", "git -C status"):
        assert grade(cmd, project, folders).kind in ("approval", "reject"), cmd
    assert grade("git -c core.pager=evil log", project, folders).kind == "approval"


def test_git_dash_c_in_a_read_only_folder_reads_but_asks_before_writing(project, folders, tmp_path):
    elsewhere = folders.__class__(folders.work_roots, (project,))
    assert grade(f"git -C {project} log -n 1", project, elsewhere).kind == "auto"
    assert grade(f"git -C {project} commit -m x", project, elsewhere).kind == "approval"


@pytest.mark.parametrize("command", ["open https://console.example.com/usage", "open mailto:a@b.c",
                                     "open -a Mail --args x", "open nothing-here", "open", "open -b com.x.y"])
def test_open_a_web_address_or_with_odd_options_asks(command, project, folders):
    assert grade(command, project, folders).kind == "approval"


def test_open_will_not_launch_programs_or_scripts(project, folders):
    for name in ("run.command", "tool.app", "x.scpt", "y.sh"):
        (project / name).mkdir() if name.endswith(".app") else (project / name).write_text("x")
        assert grade(f"open {name}", project, folders).kind == "approval", name


@pytest.mark.parametrize("command", ["tail -f README.md", "tail -nf 5 README.md", "lsof +D /", "lsof -r 2 -i",
                                     "lsof README.md", "sed -i s/a/b/ README.md", "sed -n -i p README.md",
                                     "git push", "git push --force", "git reset --hard",
                                     "git clean -fd", "git remote add x y", "git config user.name x", "pip install x",
                                     "brew install x", "curl https://x.test", "sudo ls", "/bin/ls", "frobnicate",
                                     "screen -dmS x top", "claude -p hi", "osascript -e x", "gh auth status"])
def test_high_risk_and_unknown_commands_ask(command, project, folders):
    assert grade(command, project, folders).kind == "approval", command


def test_critical_commands_are_graded_critical(project, folders):
    assert grade("sudo ls", project, folders).risk == "critical"
    assert grade("git push --force", project, folders).risk == "critical"
    assert grade("rm -rf ~/Documents", project, folders).risk == "critical"


@pytest.mark.parametrize("command", ["cat .env", "grep TOKEN .env", "cat ~/.ssh/id_rsa", "ls ~/.ssh", "git show HEAD:.env",
                                     "cat ~/Library/Keychains/login.keychain-db", "rm .env", "cp .env /tmp/x",
                                     "open .env", "git -C ~/.ssh status", "sed -n 1p .env.local"])
def test_secrets_are_refused_whatever_the_grade(command, project, folders):
    assert grade(command, project, folders).kind == "reject", command


@pytest.mark.parametrize("command", ["ls | head", "ls; rm x", "echo $(whoami)", "ls > out.txt", "ls && ls", "echo `x`",
                                     'echo "$HOME"', 'echo "a `x` b"', "echo 'a' > b", 'echo "a" | x',
                                     'echo "a\nb"', "echo \\'a > b"])
def test_shell_syntax_is_refused_not_interpreted(command, project, folders):
    v = grade(command, project, folders)
    assert v.kind == "reject" and "shell syntax" in v.reason


@pytest.mark.parametrize("command,arg", [
    ('git commit -m "Add hi. Co-Authored-By: Claude <noreply@anthropic.com>"',
     "Add hi. Co-Authored-By: Claude <noreply@anthropic.com>"),
    ("grep -n 'a|b' notes.txt", "a|b"),
    ("echo 'cost: $5; ok'", "cost: $5; ok"),
])
def test_quoted_shell_characters_are_plain_text(command, arg, project, folders):
    v = grade(command, project, folders)
    assert v.kind != "reject" and arg in v.argv, (command, v.reason)


def test_rm_inside_the_project_becomes_a_move_to_the_trash(project, folders):
    (project / "notes.txt").write_text("x")
    (project / "build").mkdir()
    v = grade("rm notes.txt", project, folders)
    assert v.kind == "trash" and v.trash == ((project / "notes.txt").resolve(),)
    assert grade("rm -rf build", project, folders).kind == "trash"
    assert grade("rm -r build notes.txt", project, folders).trash == (project / "build", project / "notes.txt")


@pytest.mark.parametrize("command", ["rm ../outside.txt", "rm -rf /tmp/x", "rm -rf ~/Documents", "rm -rf .", "rm .git/HEAD",
                                     "rm -P notes.txt", "rm"])
def test_rm_outside_the_root_or_in_git_asks_and_never_trashes(command, project, folders):
    assert grade(command, project, folders).kind == "approval", command


def test_nothing_is_writable_outside_the_work_roots_or_in_read_only_folders(tmp_path, folders):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    for command in ("touch x", "python3 s.py", "uv sync", "rm x"):
        assert grade(command, elsewhere, folders).kind == "approval", command
    assert grade("ls", elsewhere, folders).kind == "auto"  # reading is fine anywhere
    ro = folders.read_only[0]
    ro.mkdir()
    assert grade("touch x", ro, folders).kind == "approval"


def test_the_project_root_is_the_repo_containing_the_folder(project, folders):
    sub = project / "src" / "deep"
    sub.mkdir(parents=True)
    v = grade("touch x", sub, folders)
    assert v.root == str(project)


def test_key_folders_and_env_files_are_off_limits(tmp_path):
    from pathlib import Path
    assert is_secret_path(Path.home() / ".ssh" / "id_rsa") and is_secret_path(tmp_path / "x" / ".env")
    assert not is_secret_path(tmp_path / ".env.example") and not is_secret_path(tmp_path / "README.md")
