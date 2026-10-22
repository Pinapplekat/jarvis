import json
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


# ----- JARVIS Camera.app (a fake one: it answers the request file like the real app) -----
def fake_app(data_dir, answer="captured", write=b"\xff\xd8APP"):
    seen = []

    def launch():
        req = json.loads((data_dir / "camera-app" / "request.json").read_text())
        seen.append(req)
        if answer is None:
            return  # opened, but never answers
        if write:
            with open(req["out"], "r+b") as f:  # into JARVIS's own 0600 file, like the app
                f.write(write)
        body = {"id": req["id"], **({"state": "captured"} if answer == "captured" else {"error": answer})}
        (data_dir / "camera-app" / "ack.json").write_text(json.dumps(body))
    launch.seen = seen
    return launch


@pytest.fixture
def preview(monkeypatch):
    monkeypatch.setattr(config, "CAMERA_PREVIEW", True)
    monkeypatch.setattr(config, "CAMERA_APP_TIMEOUT", 0.2)


def test_the_app_is_asked_by_request_file_and_its_ack_is_read(data_dir, preview):
    app, run = fake_app(data_dir), fake_run()
    path = camera.capture_frame(run=run, tool="imagesnap", launch=app, sleep=lambda s: None)
    [req] = app.seen
    assert req["out"] == str(path) and req["preview"] == config.CAMERA_PREVIEW_SECONDS
    assert req["hold"] == config.CAMERA_HOLD_SECONDS
    assert path.read_bytes() == b"\xff\xd8APP" and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert [c[0] for c in run.calls] == ["/usr/bin/sips"]  # no imagesnap: the app took it
    assert camera.last_how == "with the preview"
    assert not (data_dir / "camera-app" / "request.json").exists()  # a late app finds nothing to do


@pytest.mark.parametrize("launch_fails", [True, False])
def test_an_app_that_cant_open_or_doesnt_answer_falls_back_to_imagesnap(data_dir, preview, launch_fails):
    def broken():
        raise camera.AppUnavailable("the camera app didn't open")
    app = broken if launch_fails else fake_app(data_dir, answer=None)
    run = fake_run(write=b"\xff\xd8SNAP")
    path = camera.capture_frame(run=run, tool="imagesnap", launch=app, sleep=lambda s: None)
    assert path.read_bytes() == b"\xff\xd8SNAP" and run.calls[0][0] == "imagesnap"
    assert camera.last_how.startswith("no preview") and "imagesnap" in camera.last_how
    assert [p for p in (data_dir / "tmp" / "camera").iterdir()] == [path]  # the app's unused file is gone


def test_preview_off_goes_straight_to_imagesnap(data_dir, monkeypatch):
    monkeypatch.setattr(config, "CAMERA_PREVIEW", False)
    path = camera.capture_frame(run=fake_run(), tool="imagesnap", launch=lambda: pytest.fail("app opened"))
    assert path.exists() and "CAMERA_PREVIEW=0" in camera.last_how


def test_the_apps_errors_say_so_and_leave_nothing(data_dir, preview):
    with pytest.raises(RuntimeError, match="^camera not allowed"):
        camera.capture_frame(run=fake_run(), launch=fake_app(data_dir, answer="camera not allowed", write=b""),
                             sleep=lambda s: None)
    with pytest.raises(RuntimeError, match="no picture"):
        camera.capture_frame(run=fake_run(), launch=fake_app(data_dir, write=b""), sleep=lambda s: None)
    assert list((data_dir / "tmp" / "camera").iterdir()) == []


@pytest.mark.parametrize("vision_fails", [False, True])
def test_the_apps_still_is_deleted_after_the_look_on_success_and_on_error(data_dir, preview, monkeypatch, vision_fails):
    from jarvis import chat
    seen_paths = []
    real = camera.capture_frame
    monkeypatch.setattr(camera, "capture_frame", lambda: real(run=fake_run(), launch=fake_app(data_dir),
                                                              sleep=lambda s: None))

    def vision(system, prompt, image):
        seen_paths.append(Path(image))
        assert Path(image).read_bytes() == b"\xff\xd8APP"  # handed over at once, while it exists
        if vision_fails:
            raise RuntimeError("vision down")
        return "A mug, sir."
    if vision_fails:
        with pytest.raises(RuntimeError):
            chat.look_through_camera("what am I holding?", "", vision)
    else:
        assert chat.look_through_camera("what am I holding?", "", vision) == "A mug, sir."
    assert len(seen_paths) == 1 and not seen_paths[0].exists()
    assert list((data_dir / "tmp" / "camera").iterdir()) == []


def test_the_camera_app_holds_the_permission_and_never_saves():
    from jarvis import inbox, panel
    assert "NSCameraUsageDescription" in camera.INFO
    assert "NSCameraUsageDescription" not in panel.INFO and "NSCameraUsageDescription" not in inbox.INFO
    assert "AVCapture" not in panel.SWIFT and "AVCapture" not in inbox.SWIFT
    # it writes only into the private file JARVIS made (never creates one) and checks where that is
    assert "FileHandle(forWritingAtPath:" in camera.SWIFT and "createFile" not in camera.SWIFT
    assert 'stillPrefix = (appDir as NSString).deletingLastPathComponent + "/tmp/camera/camera-"' in camera.SWIFT


def test_his_own_question_about_himself_can_use_the_camera(data_dir, monkeypatch):
    """"What color is my shirt" from his Telegram: the camera step the chat model proposed runs,
    though he never said "let me show you". The same words from anywhere else still can't."""
    from jarvis import chat
    taken = []

    def capture(run=None, tool=None):
        path = data_dir / f"camera-{len(taken)}.jpg"
        path.write_bytes(b"\xff\xd8CAM")
        taken.append(path)
        return path
    monkeypatch.setattr(camera, "capture_frame", capture)
    vision = lambda system, prompt, image: "A navy crew-neck, sir."  # noqa: E731
    assert chat.camera_refusal("what color is my shirt", "telegram") == ""
    assert chat.camera_step("what color is my shirt", "telegram", "his shirt", vision) == ("ok", "A navy crew-neck, sir.")
    assert len(taken) == 1 and not taken[0].exists()  # one still, deleted
    for channel in (None, "email", "web"):
        assert chat.camera_step("what color is my shirt", channel, "", vision)[0] == "refused"
    assert chat.camera_step("how's my week looking?", "telegram", "", vision)[0] == "refused"  # not about him
    assert len(taken) == 1


def test_only_chat_can_use_the_camera():
    """Background jobs (tick, heartbeat, watches, mail, tasks) never reach the capture: only the
    chat's validated action calls it, and the CLI only sweeps leftovers."""
    src = Path(camera.__file__).parent
    for f in src.glob("*.py"):
        if f.name in ("camera.py", "chat.py"):
            continue
        text = f.read_text()
        assert "capture_frame" not in text and "look_through_camera(" not in text, f.name
