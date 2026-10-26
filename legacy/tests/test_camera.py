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


# ----- the preview stays up: reuse, close, fallback notes -----
def answer_on_wait(data_dir, write=b"\xff\xd8APP"):
    """An app that's already open: it answers the request while JARVIS waits (no launch)."""
    def sleep(s):
        d = data_dir / "camera-app"
        req = json.loads((d / "request.json").read_text())
        with open(req["out"], "r+b") as f:
            f.write(write)
        (d / "ack.json").write_text(json.dumps({"id": req["id"], "state": "captured"}))
    return sleep


def open_preview(data_dir, pid=None):
    import os
    d = data_dir / "camera-app"
    d.mkdir(parents=True, exist_ok=True)
    (d / "state.json").write_text(json.dumps({"pid": pid or os.getpid()}))


def test_the_request_asks_for_a_moment_then_a_preview_that_stays(data_dir, preview):
    app = fake_app(data_dir)
    camera.capture_frame(run=fake_run(), launch=app, sleep=lambda s: None)
    [req] = app.seen
    assert req["max"] == config.CAMERA_PREVIEW_MAX_SECONDS
    assert camera.last_note == camera.STILL_UP and "close the camera" in camera.last_note
    src = camera.SWIFT
    assert 'clamp(o["preview"], 1)' in src  # about a second of live preview before the flash
    assert src.count("stopRunning()") == 1  # only closing stops the camera, not the picture
    assert "● LIVE" in src and "keyCode == 53" in src and "#selector(close)" in src  # plainly live, closable
    assert "closePath" in src and "statePath" in src


def test_a_second_look_reuses_the_open_preview(data_dir, preview):
    open_preview(data_dir)
    assert camera.preview_open()
    path = camera.capture_frame(run=fake_run(), launch=lambda: pytest.fail("a second app opened"),
                                sleep=answer_on_wait(data_dir))
    assert path.read_bytes() == b"\xff\xd8APP" and camera.last_how == "with the preview (already open)"
    path.unlink()


def test_a_crashed_apps_state_isnt_an_open_preview(data_dir):
    open_preview(data_dir, pid=2 ** 22 + 12345)  # no such process
    assert not camera.preview_open()
    assert not (data_dir / "camera-app" / "state.json").exists()


def test_close_preview_asks_the_app_and_waits_for_it(data_dir):
    assert camera.close_preview() is False  # nothing open
    open_preview(data_dir)
    d = data_dir / "camera-app"

    def app_closes(s):
        assert (d / "close.json").exists()
        (d / "state.json").unlink()
    assert camera.close_preview(sleep=app_closes) is True
    open_preview(data_dir)
    with pytest.raises(RuntimeError, match="didn't close"):
        camera.close_preview(timeout=0.05, sleep=lambda s: None)


def test_no_preview_says_so_plainly_and_still_takes_the_picture(data_dir, preview):
    def broken():
        raise camera.AppUnavailable("the camera app isn't built")
    path = camera.capture_frame(run=fake_run(), tool="imagesnap", launch=broken, sleep=lambda s: None)
    assert path.exists() and camera.last_note.startswith("The camera preview didn't open")
    assert "isn't built" in camera.last_note
    # headless (no screen): the app says so and imagesnap takes it
    path = camera.capture_frame(run=fake_run(), tool="imagesnap", sleep=lambda s: None,
                                launch=fake_app(data_dir, answer=camera.NO_SCREEN, write=b""))
    assert path.exists() and camera.NO_SCREEN in camera.last_note
    # the app has no permission but imagesnap does
    path = camera.capture_frame(run=fake_run(), tool="imagesnap", sleep=lambda s: None,
                                launch=fake_app(data_dir, answer="camera not allowed", write=b""))
    assert path.exists() and "not allowed" in camera.last_note


def test_closing_the_preview_mid_look_takes_no_picture_at_all(data_dir, preview):
    run = fake_run()
    with pytest.raises(camera.Closed):
        camera.capture_frame(run=run, tool="imagesnap", sleep=lambda s: None,
                             launch=fake_app(data_dir, answer=camera.CLOSED_EARLY, write=b""))
    assert run.calls == [] and list((data_dir / "tmp" / "camera").iterdir()) == []


def test_the_still_is_deleted_after_the_look_while_the_preview_stays(data_dir, preview, monkeypatch):
    from jarvis import chat
    open_preview(data_dir)
    real = camera.capture_frame
    monkeypatch.setattr(camera, "capture_frame", lambda: real(run=fake_run(), sleep=answer_on_wait(data_dir)))
    assert chat.look_through_camera("look at me", "", lambda s, p, img: "You, sir.") == "You, sir."
    assert list((data_dir / "tmp" / "camera").iterdir()) == [] and camera.preview_open()
    assert chat.preview_note() == camera.STILL_UP


@pytest.mark.parametrize("message", ["look at me", "Look at what I’m holding", "look at what I am holding",
                                     "can you look at this thing", "what's in my hand?", "take a picture of me",
                                     "have a look at what I've got", "check me out, jarvis"])
def test_his_explicit_asks_to_look_at_him_pass_the_gate(message):
    from jarvis import chat
    assert chat.camera_refusal(message, "telegram") == ""
    assert chat.camera_refusal(message, "email") == chat.CAMERA_NOT_FROM_HIM


@pytest.mark.parametrize("message", ["what's the weather", "> look at me\nwhat does this quote mean?",
                                     "summarize my week"])
def test_other_words_and_quoted_text_dont(message):
    from jarvis import chat
    assert chat.camera_refusal(message, "telegram") == chat.CAMERA_NOT_HIS


def test_close_camera_is_an_action_and_an_instant_command(data_dir):
    from jarvis import chat, router
    from jarvis.memory import Memory
    m = Memory(":memory:")
    said = []
    assert chat.apply_actions(m, [{"type": "close_camera"}], None, said=said) == []
    assert said == [camera.NOT_OPEN]
    assert "- close_camera:" in chat.system_prompt()
    assert router.camera_command("close the camera", False) == "close"
    assert router.camera_command("Turn the camera off please", False) == "close"
    assert router.camera_command("close that", True) == "close"
    assert router.camera_command("close that", False) is None  # not about the camera when it isn't up
    assert router.camera_command("what camera should I buy", True) is None


@pytest.mark.parametrize("sign_fails", [False, True])
def test_the_app_is_signed_last_so_its_stamp_is_sealed(tmp_path, monkeypatch, sign_fails):
    """codesign seals everything in the bundle, so the stamp must already be there when it runs;
    a write after signing breaks the signature and the app won't launch."""
    app = tmp_path / "JARVIS Camera.app"
    monkeypatch.setattr(camera, "APP", app)
    monkeypatch.setattr(camera, "EXECUTABLE", app / "Contents" / "MacOS" / "jarvis-camera")
    stamp = app / "Contents" / "Resources" / "stamp"
    calls = []

    def run(argv, **kw):
        calls.append(argv[0])
        if argv[0] == "swiftc":
            # An explicit deployment target, so the binary's minos isn't newer than this Mac (-10825).
            assert argv[argv.index("-target") + 1].endswith("-apple-macos13")
            Path(argv[argv.index("-o") + 1]).write_bytes(b"bin")
        if argv[0] == "codesign":
            assert stamp.read_text() == camera._stamp()  # already in place when sealed
            if sign_fails:
                return subprocess.CompletedProcess(argv, 1, "", "no identity")
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(camera.subprocess, "run", run)
    if sign_fails:
        with pytest.raises(RuntimeError, match="couldn't sign the camera app: no identity"):
            camera.build()
        assert not stamp.exists() and not camera.built()  # an unsigned app gets rebuilt next time
    else:
        assert camera.build() is True and camera.built()
        assert camera.build() is False
        monkeypatch.setattr(camera, "BUILD_VERSION", "old")  # an app built by an older build()
        assert not camera.built()
    assert calls[-1] == "codesign"


def test_only_chat_can_use_the_camera():
    """Background jobs (tick, heartbeat, watches, mail, tasks) never reach the capture: only the
    chat's validated action calls it, and the CLI only sweeps leftovers."""
    src = Path(camera.__file__).parent
    for f in src.glob("*.py"):
        if f.name in ("camera.py", "chat.py"):
            continue
        text = f.read_text()
        assert "capture_frame" not in text and "look_through_camera(" not in text, f.name
