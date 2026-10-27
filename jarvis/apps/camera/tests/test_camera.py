import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from jarvis.apps.camera import actions as camera
from jarvis.contract.api import ActionError

AVF = """[AVFoundation indev @ 0x1] AVFoundation video devices:
[AVFoundation indev @ 0x1] [0] MacBook Neo Camera
[AVFoundation indev @ 0x1] [1] Elijah's iPhone Desk View Camera
[AVFoundation indev @ 0x1] [2] Capture screen 0
[AVFoundation indev @ 0x1] AVFoundation audio devices:
[AVFoundation indev @ 0x1] [0] MacBook Neo Microphone
"""


@pytest.fixture
def snap(monkeypatch):
    """Replace imagesnap: the fake process writes `write` into the file path it is given."""
    calls = []
    state = {"returncode": 0, "stderr": "", "write": b"jpegbytes", "raises": None}

    def fake_run(cmd, **kw):
        calls.append((cmd, kw))
        if state["raises"]:
            raise state["raises"]
        if state["write"] is not None:
            open(cmd[-1], "wb").write(state["write"])
        return SimpleNamespace(returncode=state["returncode"], stdout="", stderr=state["stderr"])

    monkeypatch.setattr(camera, "_imagesnap", lambda: "/fake/imagesnap")
    monkeypatch.setattr(camera.subprocess, "run", fake_run)
    state["calls"] = calls
    return state


def leftovers(ctx):
    return list(ctx._tmp_dir.iterdir())


def test_photo_is_untrusted_output():
    from jarvis.contract.loader import DEFAULT_APPS_DIR
    from jarvis.contract.manifest import load_manifest

    m = load_manifest(DEFAULT_APPS_DIR / "camera" / "manifest.toml")
    assert next(a for a in m.actions if a.name == "take_photo").untrusted_output is True
    assert m.sessions[0].untrusted_output is True and m.sessions[0].idle_minutes == 15


def test_takes_photo(make_ctx, snap):
    ctx = make_ctx("camera")
    result = camera.take_photo(ctx)
    (att,) = result.attachments
    assert att.kind == "image" and att.mime == "image/jpeg" and att.path.read_bytes() == b"jpegbytes"
    cmd, kw = snap["calls"][0]
    assert cmd[:4] == ["/fake/imagesnap", "-q", "-w", "1"] and "-d" not in cmd and kw["timeout"] == 10


def test_device_setting_is_passed(make_ctx, snap):
    camera.take_photo(make_ctx("camera", device="FaceTime HD"))
    assert snap["calls"][0][0][4:6] == ["-d", "FaceTime HD"]


@pytest.mark.parametrize("change,fragment", [
    ({"write": b"", "returncode": 0}, "Camera access is blocked"),
    ({"write": b"", "returncode": 1, "stderr": "No video devices found"}, "couldn't find a camera"),
    ({"write": b"", "returncode": 1, "stderr": "Not authorized"}, "Camera access is blocked"),
    ({"raises": subprocess.TimeoutExpired("imagesnap", 10)}, "didn't answer"),
])
def test_errors_are_plain_and_leave_no_file(make_ctx, snap, change, fragment):
    snap.update(change)
    ctx = make_ctx("camera")
    with pytest.raises(ActionError, match=fragment):
        camera.take_photo(ctx)
    assert leftovers(ctx) == []


def test_imagesnap_missing(make_ctx, monkeypatch):
    monkeypatch.setattr(camera, "_imagesnap", lambda: None)
    with pytest.raises(ActionError, match="brew install imagesnap"):
        camera.take_photo(make_ctx("camera"))


# ---- live view, with the processes faked ---------------------------------------------------------------


class FakeProc:
    def __init__(self, argv, log, alive=True):
        self.pid, self.argv, self.log, self.alive, self.killed = 4000 + len(FAKE_PROCS), argv, log, alive, False
        FAKE_PROCS.append(self)

    def poll(self):
        return None if self.alive else 1

    def wait(self, timeout=None):
        return 0


FAKE_PROCS: list[FakeProc] = []


@pytest.fixture
def live(monkeypatch):
    """ffmpeg and ffplay replaced: 'ffmpeg' writes a frame at once unless told not to."""
    FAKE_PROCS.clear()
    state = {"frame": b"frame1", "stderr": "", "dies": False}

    def spawn(argv, log=None):
        proc = FakeProc(argv, log, alive=not state["dies"])
        camera._procs[proc.pid] = proc
        if "ffmpeg" in argv[0]:
            if state["frame"] is not None and not state["dies"]:
                Path(argv[-1]).write_bytes(state["frame"])
            if log:
                log.write_text(state["stderr"])
        return proc

    def kill(pid):
        for p in FAKE_PROCS:
            if p.pid == pid:
                p.killed, p.alive = True, False
        camera._procs.pop(pid, None)

    monkeypatch.setattr(camera, "_spawn", spawn)
    monkeypatch.setattr(camera, "_kill", kill)
    monkeypatch.setattr(camera, "_tool", lambda name, fallbacks: f"/fake/{name}")
    monkeypatch.setattr(camera, "list_cameras", lambda ffmpeg: [("0", "MacBook Neo Camera"),
                                                                 ("1", "Elijah's iPhone Desk View Camera")])
    monkeypatch.setattr(camera, "FIRST_FRAME_WAIT", 0.3)
    return state


def test_list_cameras_skips_screens_and_audio(monkeypatch):
    monkeypatch.setattr(camera.subprocess, "run", lambda *a, **k: SimpleNamespace(stderr=AVF, stdout="", returncode=1))
    assert camera.list_cameras("ffmpeg") == [("0", "MacBook Neo Camera"), ("1", "Elijah's iPhone Desk View Camera")]


@pytest.mark.parametrize("wanted,expected", [("", "MacBook Neo Camera"), ("desk view", "Elijah's iPhone Desk View Camera"),
                                             ("DESK", "Elijah's iPhone Desk View Camera")])
def test_choose_camera_by_substring(wanted, expected):
    cams = [("0", "MacBook Neo Camera"), ("1", "Elijah's iPhone Desk View Camera")]
    assert camera.choose_camera(cams, wanted)[1] == expected


def test_no_match_lists_the_cameras():
    with pytest.raises(ActionError, match="'webcam'.*MacBook Neo Camera, Elijah's iPhone Desk View Camera"):
        camera.choose_camera([("0", "MacBook Neo Camera"), ("1", "Elijah's iPhone Desk View Camera")], "webcam")


def test_live_view_start_sample_stop(make_ctx, live):
    ctx = make_ctx("camera")
    started = camera.start_live(ctx, "desk view")
    assert "Desk View Camera" in started.text
    ffmpeg, ffplay = FAKE_PROCS
    assert ["-i", "1"] == ffmpeg.argv[ffmpeg.argv.index("-i"):ffmpeg.argv.index("-i") + 2]
    assert "fps=2,scale='min(1280,iw)':-2" in ffmpeg.argv and ffplay.argv.count(camera.PREVIEW_TITLE) == 1
    first = camera.sample_live(ctx, started.state)
    assert first.attachments[0].path.read_bytes() == b"frame1"
    assert first.attachments[0].path != Path(started.state["frame"])  # a copy; the frame file keeps being rewritten
    camera.stop_live(ctx, started.state)
    assert ffmpeg.killed and ffplay.killed
    assert not Path(started.state["frame"]).exists() and not Path(started.state["log"]).exists()


def test_preview_can_be_turned_off(make_ctx, live):
    camera.start_live(make_ctx("camera", preview=False))
    assert len(FAKE_PROCS) == 1


def test_default_camera_comes_from_the_setting(make_ctx, live):
    started = camera.start_live(make_ctx("camera", device="desk"))
    assert started.state["device"] == "Elijah's iPhone Desk View Camera"


@pytest.mark.parametrize("stderr,fragment", [
    ("Error: not authorized to capture video", "Camera access is blocked"),
    ("Device or resource busy", "in use by another app"),
    ("something odd", "didn't start \\(something odd\\)"),
])
def test_start_errors_are_plain_and_clean_up(make_ctx, live, stderr, fragment):
    live.update(dies=True, stderr=stderr)
    ctx = make_ctx("camera")
    with pytest.raises(ActionError, match=fragment):
        camera.start_live(ctx)
    assert leftovers(ctx) == []


def test_no_first_frame_times_out(make_ctx, live):
    live["frame"] = None
    ctx = make_ctx("camera")
    with pytest.raises(ActionError, match="didn't produce a picture"):
        camera.start_live(ctx)
    assert leftovers(ctx) == []


def test_ffmpeg_missing(make_ctx, live, monkeypatch):
    monkeypatch.setattr(camera, "_tool", lambda name, fallbacks: None)
    with pytest.raises(ActionError, match="brew install ffmpeg"):
        camera.start_live(make_ctx("camera"))


def test_sample_reports_a_dead_or_frozen_feed(make_ctx, live):
    ctx = make_ctx("camera")
    state = camera.start_live(ctx).state
    old = time.time() - 60
    import os
    os.utime(state["frame"], (old, old))
    with pytest.raises(ActionError, match="frozen"):
        camera.sample_live(ctx, state)
    FAKE_PROCS[0].alive = False
    with pytest.raises(ActionError, match="has stopped"):
        camera.sample_live(ctx, state)
