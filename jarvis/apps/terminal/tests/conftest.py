import subprocess
import sys
from pathlib import Path

import pytest

from jarvis.apps.terminal import run as runner
from jarvis.apps.terminal.folders import Folders

MAC = pytest.mark.skipif(sys.platform != "darwin", reason="needs macOS sandbox-exec")
PROBE_DIR = Path(__file__).parent  # not under temp, which every sandbox may write to


def git(folder: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(folder), "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True,
                   capture_output=True, stdin=subprocess.DEVNULL)


@pytest.fixture
def work(tmp_path):
    """A work root holding one project (a git repo with a README and one commit)."""
    root = (tmp_path / "work").resolve()
    project = root / "proj"
    project.mkdir(parents=True)
    git(project, "init", "-q")
    (project / "README.md").write_text("hi\n")
    git(project, "add", "README.md")
    git(project, "commit", "-q", "-m", "first")
    return root


@pytest.fixture
def project(work):
    return work / "proj"


@pytest.fixture
def folders(work, tmp_path):
    return Folders((work,), ((tmp_path / "ro").resolve(),))


@pytest.fixture
def ctx(make_ctx, work, tmp_path):
    return make_ctx("terminal", work_roots=str(work), read_only=str(tmp_path / "ro"))


@pytest.fixture
def trash(tmp_path, monkeypatch):
    folder = tmp_path / "Trash"
    monkeypatch.setattr(runner, "TRASH_DIR", folder)
    return folder
