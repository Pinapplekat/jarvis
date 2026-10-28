import json
import logging

import pytest

from jarvis.protocol import messages as m

SAMPLES = [
    m.Hello("mac", "k", ["camera"], ["camera"]),
    m.Welcome(3, {"camera": {"preview": True}}),
    m.Reject("no"),
    m.Call(1, "camera", "take_photo", {}, 15, False),
    m.Call(2, "camera", "sample_live", {"pid": 1}, 20.5, True),
    m.CallResult(1, True, "ok", {"state": {}}, {"u": 1}, "s", [{"kind": "image", "mime": "image/jpeg", "data": "AA=="}]),
    m.CallResult(2, False, error="busy"),
    m.CallResult(3, False, crash=True),
    m.Ping(1.5),
    m.Pong(1.5),
    m.VoiceStart(7, "push"),
    m.VoiceEnd(7),
    m.SpeakStart(8),
    m.SpeakEnd(8),
    m.SpeakStop(8),
    m.PlaybackStopped(8, 1200, "barge_in"),
    m.Presence(3.5, True),
]


@pytest.mark.parametrize("msg", SAMPLES, ids=lambda x: f"{x.type}-{id(x) % 1000}")
def test_every_message_round_trips(msg):
    assert m.from_json(msg.to_json()) == msg


def test_every_frame_carries_type_and_version():
    for msg in SAMPLES:
        data = json.loads(msg.to_json())
        assert data["type"] == msg.type and data["v"] == m.VERSION


@pytest.mark.parametrize("frame", [
    "not json", "[1]", "{}", '{"type": "nope"}', '{"type": "ping"}', '{"type": "ping", "t": "now"}',
    '{"type": "call", "id": true, "app": "a", "handler": "h", "args": {}, "timeout_s": 1}',
    '{"type": "result", "id": 1, "ok": 1}', b"\xff\xfe",
])
def test_bad_frames_are_dropped_and_logged_not_raised(frame, caplog):
    with caplog.at_level(logging.WARNING):
        assert m.parse(frame) is None
    assert caplog.records and "dropped a frame" in caplog.records[0].getMessage()
    with pytest.raises(m.ProtocolError):
        m.from_json(frame)


def test_optional_fields_default():
    msg = m.from_json('{"type": "result", "id": 5, "ok": true}')
    assert msg == m.CallResult(5, True) and msg.attachments == [] and msg.crash is False


def test_hello_carries_its_version():
    old = m.from_json(json.dumps({**json.loads(m.Hello("a", "k", [], []).to_json()), "v": 2}))
    assert old.v == 2


def test_protocol_has_no_third_party_imports():
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path(m.__file__).read_text())
    roots = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    roots |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert roots <= {"__future__", "json", "logging", "struct", "dataclasses", "typing"}


def test_audio_frames_round_trip_with_their_header():
    frame = m.AudioFrame(7, 42, m.MIC, b"\x01\x02\x03\x04")
    raw = frame.to_bytes()
    assert m.is_audio(raw) and not m.is_audio(m.Ping(1).to_json())
    assert m.AudioFrame.from_bytes(raw) == frame and frame.rate == 16000
    assert m.AudioFrame(1, 0, m.PLAYBACK, b"").rate == 24000
    big = m.AudioFrame(2**32 - 1, 2**32 - 1, m.PLAYBACK, bytes(range(256)) * 20)
    assert m.AudioFrame.from_bytes(big.to_bytes()) == big


@pytest.mark.parametrize("raw", [b"", b"\x00\x00\x00", m.AudioFrame(1, 1, 9, b"x").to_bytes()])
def test_bad_audio_frames_raise(raw):
    with pytest.raises(m.ProtocolError):
        m.AudioFrame.from_bytes(raw)


def test_voice_messages_check_their_choices():
    for frame in ('{"type": "playback_stopped", "stream": 1, "played_ms": 5, "reason": "gone"}',
                  '{"type": "voice_start", "stream": 1, "source": "mic"}',
                  '{"type": "voice_end", "stream": "1"}'):
        assert m.parse(frame) is None
    assert m.parse('{"type": "voice_start", "stream": 1}') == m.VoiceStart(1, "wake")  # the source has a default


def test_a_version_1_hello_is_understood_and_both_versions_are_supported():
    assert m.SUPPORTED_VERSIONS == (1, 2) and m.VERSION == 2
    hello = m.parse('{"type": "hello", "v": 1, "node": "mac", "key": "k", "capabilities": [], "apps": []}')
    assert hello.v == 1
