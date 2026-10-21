import stat
import subprocess
from pathlib import Path

import pytest

from jarvis import camera, config


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CAMERA", True)
    return tmp_path


def fake_run(write=b"\xff\xd8JPEG", code=0, stderr=""):
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        if argv[0] == "imagesnap" and write:
            Path(argv[-1]).write_bytes(write)
        return subprocess.CompletedProcess(argv, code if argv[0] == "imagesnap" else 0, "", stderr)
    run.calls = calls
    return run


def test_a_frame_is_private_and_in_its_own_folder(data_dir):
    run = fake_run()
    path = camera.capture_frame(run=run, tool="imagesnap")
    assert path.read_bytes() == b"\xff\xd8JPEG" and path.name.startswith("camera-")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert run.calls[0][0] == "imagesnap" and run.calls[1][0] == "/usr/bin/sips"


def test_denied_permission_and_a_missing_camera_say_so_plainly_and_leave_nothing(data_dir):
    with pytest.raises(RuntimeError, match="^camera not allowed"):
        camera.capture_frame(run=fake_run(write=b"", code=1, stderr="Error: access not authorized"), tool="imagesnap")
    with pytest.raises(RuntimeError, match="^camera not allowed"):  # macOS's silent no: an empty frame
        camera.capture_frame(run=fake_run(write=b""), tool="imagesnap")
    with pytest.raises(RuntimeError, match="^camera not found"):
        camera.capture_frame(run=fake_run(write=b"", code=1, stderr="No video devices found."), tool="imagesnap")
    with pytest.raises(RuntimeError, match="^camera not found"):
        camera.capture_frame(run=fake_run(), tool="")  # no snapshot tool installed
    assert list((data_dir / "tmp" / "camera").iterdir()) == []


def test_a_crash_mid_capture_leaves_nothing(data_dir):
    def run(argv, **kw):
        Path(argv[-1]).write_bytes(b"half")
        raise subprocess.TimeoutExpired(argv, 20)
    with pytest.raises(subprocess.TimeoutExpired):
        camera.capture_frame(run=run, tool="imagesnap")
    assert list((data_dir / "tmp" / "camera").iterdir()) == []


def test_off_switch(monkeypatch):
    monkeypatch.setattr(config, "CAMERA", False)
    with pytest.raises(RuntimeError, match="off"):
        camera.capture_frame(run=lambda *a, **k: pytest.fail("no capture"), tool="imagesnap")


def test_startup_sweep_deletes_leftover_stills(data_dir):
    d = data_dir / "tmp" / "camera"
    d.mkdir(parents=True)
    (d / "camera-old.jpg").write_bytes(b"x")
    (d / "other.txt").write_text("keep")
    assert camera.sweep() == 1
    assert [p.name for p in d.iterdir()] == ["other.txt"]


def test_only_chat_can_use_the_camera():
    """Background jobs (tick, heartbeat, watches, mail, tasks) never reach the capture: only the
    chat's validated action calls it, and the CLI only sweeps leftovers."""
    src = Path(camera.__file__).parent
    for f in src.glob("*.py"):
        if f.name in ("camera.py", "chat.py"):
            continue
        text = f.read_text()
        assert "capture_frame" not in text and "look_through_camera(" not in text, f.name
