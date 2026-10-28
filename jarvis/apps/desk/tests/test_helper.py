"""The framing between the node and the Swift app, the build stamp, and the manifest."""

import io
import shutil
import struct
import tempfile
from pathlib import Path

import pytest

from jarvis.apps.desk import actions, build, helper
from jarvis.contract.loader import DEFAULT_APPS_DIR
from jarvis.contract.manifest import load_manifest


def test_frames_round_trip_even_when_the_pipe_gives_one_byte_at_a_time():
    audio, events = [], []
    data = helper.encode(helper.AUDIO, b"\x01\x02" * 480) + helper.encode(helper.JSON, b'{"event": "ready"}\n')
    stream = io.BytesIO(data)
    helper.read_frames(lambda n: stream.read(1), audio.append, events.append)
    assert audio == [b"\x01\x02" * 480] and events == [{"event": "ready"}]


def test_a_bad_frame_stops_the_reader_and_junk_json_is_skipped():
    audio, events = [], []
    junk = helper.encode(helper.JSON, b"not json") + helper.encode(helper.JSON, b'{"event": "idle"}')
    stream = io.BytesIO(junk + struct.pack(">BI", 9, 4) + b"xxxx" + helper.encode(helper.JSON, b'{"event": "late"}'))
    helper.read_frames(stream.read, audio.append, events.append)
    assert events == [{"event": "idle"}]  # the unknown type ended the stream
    stream = io.BytesIO(struct.pack(">BI", helper.AUDIO, helper.MAX_FRAME + 1))
    helper.read_frames(stream.read, audio.append, events.append)
    assert audio == []


def test_the_pipe_helper_writes_commands_as_json_lines_and_audio_as_frames():
    out, closed = [], []
    link = helper.PipeHelper(out.append, lambda: closed.append(1))
    link.play(b"\x00\x01")
    link.command({"cmd": "stop"})
    link.close()
    assert out[0] == struct.pack(">BI", 1, 2) + b"\x00\x01"
    assert out[1] == struct.pack(">BI", 2, len(b'{"cmd": "stop"}\n')) + b'{"cmd": "stop"}\n'
    assert b"quit" in out[2] and closed == [1]
    link = helper.PipeHelper(lambda data: (_ for _ in ()).throw(BrokenPipeError()), lambda: None)
    link.play(b"x")  # a helper that is gone is logged, not raised


def test_the_build_stamp_changes_with_the_identity_and_the_source(tmp_path):
    a, b = build.stamp(""), build.stamp("Falak Dev")
    assert a != b and len(a) == 16
    assert not build.built(tmp_path)
    app = build.app_path(tmp_path)
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "Resources").mkdir(parents=True)
    (app / "Contents" / "MacOS" / "falak-ears").write_text("x")
    (app / "Contents" / "Resources" / "stamp").write_text(a)
    assert build.built(tmp_path) and not build.built(tmp_path, "Falak Dev")


@pytest.mark.skipif(shutil.which("swiftc") is None or shutil.which("codesign") is None, reason="needs Swift")
def test_the_swift_app_compiles_signs_and_is_not_rebuilt(tmp_path):
    assert build.build(tmp_path) is True and build.built(tmp_path)
    assert build.build(tmp_path) is False
    import subprocess

    out = subprocess.run([str(build.app_path(tmp_path) / "Contents" / "MacOS" / "falak-ears"), "--selftest"],
                         capture_output=True, timeout=30).stdout  # framing only; it never touches the microphone
    events = []
    helper.read_frames(io.BytesIO(out).read, lambda p: None, events.append)
    assert events[0]["event"] == "ready" and events[0]["selftest"] is True


def test_the_manifest_is_a_node_app_that_uses_the_voice_profile():
    manifest = load_manifest(DEFAULT_APPS_DIR / "desk" / "manifest.toml")
    assert manifest.runs_on == "node:desk" and manifest.uses == ("voice",)
    assert manifest.start == "start" and manifest.stop == "stop"
    for key in ("push_to_talk", "end_silence_ms", "barge_in_ms", "follow_up_seconds", "chimes", "wake_word"):
        assert key in manifest.settings


def test_status_before_the_desk_starts(make_ctx):
    assert actions.status(make_ctx("desk")).data == {"state": "off"}


def _fake_open(monkeypatch, connect=True, returncode=0):
    """Stands in for `/usr/bin/open`: returns at once, then the 'app' connects to the socket it was given."""
    import socket
    import subprocess
    import threading
    calls = []

    def run(argv, **kw):
        calls.append((argv, kw))
        path = argv[argv.index("--socket") + 1]

        def app():
            c = socket.socket(socket.AF_UNIX)
            c.connect(path)
            c.sendall(helper.encode(helper.JSON, b'{"event": "ready"}\n'))
            calls.append(c)
        if connect:
            threading.Thread(target=app).start()
        return subprocess.CompletedProcess(argv, returncode, "", "")

    monkeypatch.setattr(helper.subprocess, "run", run)
    return calls


def test_the_app_is_opened_with_a_socket_never_with_pipes(monkeypatch, tmp_path):
    # `open --stdin/--stdout` on named pipes hung LaunchServices and no other app could open (2026-10-28).
    calls = _fake_open(monkeypatch)
    events = []
    run_dir = tmp_path / "run"
    link = helper.launch(tmp_path / "Falak Ears.app", run_dir, lambda pcm: None, events.append)
    argv, kw = calls[0]
    assert "--stdin" not in argv and "--stdout" not in argv and argv[argv.index("--args") + 1] == "--socket"
    assert kw["timeout"] == helper.OPEN_TIMEOUT  # `open` itself can never hang Falak
    socket_dir = Path(argv[argv.index("--socket") + 1]).parent
    assert not socket_dir.exists()  # a private folder, removed once the app joined
    import time
    for _ in range(100):
        if events:
            break
        time.sleep(0.01)
    assert events == [{"event": "ready"}]
    link.close()


def test_an_app_that_never_connects_fails_plainly(monkeypatch, tmp_path):
    _fake_open(monkeypatch, connect=False)
    with pytest.raises(RuntimeError, match="didn't connect"):
        helper.launch(tmp_path / "Falak Ears.app", tmp_path / "run", lambda pcm: None, lambda ev: None, connect_timeout=0.2)
    assert not list(Path(tempfile.gettempdir()).glob("falak-ears-*"))


def test_the_no_echo_cancellation_notice_is_said_once_ever(make_ctx):
    ctx = make_ctx("desk")
    said = []
    ctx.notify_hook = lambda name, text: said.append(text)
    runner = actions.Runner(ctx)
    runner._helper_error("no echo cancellation")
    runner._helper_error("no echo cancellation")  # the next start
    assert len(said) == 1 and "echo cancellation" in said[0]
