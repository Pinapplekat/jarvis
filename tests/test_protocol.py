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
    assert roots <= {"__future__", "json", "logging", "dataclasses", "typing"}
