"""Voice frames between a node app and the host over the real WebSocket: ordering, version 1 nodes, no queuing."""

import sys
from types import SimpleNamespace

import pytest
from node_harness import FIXTURES, NodeThread, fake_node, pair, start_hub, wait_for

from jarvis.core import actions
from jarvis.core.apps import load_apps
from jarvis.protocol import messages as m

EARS = FIXTURES.parent / "earapps"
CLI = actions.Origin("cli")


@pytest.fixture
def ears(settings, tmp_path):
    reg = load_apps(settings, EARS)
    hub = start_hub(reg)
    got = []
    hub.voice_sink = lambda node, frame: got.append((node, frame))
    key = pair(reg)
    node = NodeThread(tmp_path, hub.port, key, apps_dir=EARS).start()
    node.wait_connected()
    wait_for(hub.connected, what="the hub to see the node")
    module = sys.modules["falak_app_ear_actions"]
    module.FRAMES.clear()
    yield SimpleNamespace(reg=reg, hub=hub, node=node, key=key, got=got, module=module)
    node.stop()
    hub.stop()
    reg.close()


def test_frames_from_a_node_app_reach_the_hosts_sink_in_order(ears):
    for kind in ("json", "audio", "json"):
        assert actions.run(ears.reg, "ear__send", {"kind": kind}, CLI).result.text == "sent"
    wait_for(lambda: len(ears.got) == 3, what="three frames")
    assert [type(f) for _, f in ears.got] == [str, bytes, str]
    assert ears.got[0][0] == "mac" and m.parse(ears.got[0][1]) == m.VoiceStart(5, "push")
    assert m.AudioFrame.from_bytes(ears.got[1][1]) == m.AudioFrame(5, 1, m.MIC, b"\x01\x02")


def test_frames_from_the_host_reach_the_node_app_in_order_and_intact(ears):
    frames = [m.SpeakStart(9).to_json()]
    frames += [m.AudioFrame(9, i, m.PLAYBACK, bytes([i % 256]) * 960).to_bytes() for i in range(200)]
    frames += [m.SpeakEnd(9).to_json()]
    for f in frames:
        assert ears.hub.send_voice("mac", f)
    wait_for(lambda: len(ears.module.FRAMES) == len(frames), what="all the frames")
    assert ears.module.FRAMES == frames


def test_a_version_1_node_gets_no_voice_and_its_voice_frames_are_ignored(ears):
    ears.node.stop()
    wait_for(lambda: not ears.hub.connected(), what="the node to leave")
    ws, reply = fake_node(ears.hub.port, ears.key, name="mac", caps=(), apps=(), v=1)
    try:
        assert isinstance(reply, m.Welcome)
        wait_for(ears.hub.connected, what="the old node to connect")
        assert not ears.hub.send_voice("mac", m.SpeakEnd(1).to_json())
        ws.send(m.VoiceStart(1, "wake").to_json())
        ws.send(m.AudioFrame(1, 0, m.MIC, b"xx").to_bytes())
        ws.send(m.Ping(1.0).to_json())
        assert isinstance(m.parse(ws.recv(timeout=5)), m.Pong)  # the link still works for everything else
        assert ears.got == []
    finally:
        ws.close()


def test_nothing_is_sent_to_a_node_that_is_not_there(ears):
    assert not ears.hub.send_voice("nobody", m.SpeakEnd(1).to_json())
    ears.node.stop()
    wait_for(lambda: not ears.hub.connected(), what="the node to leave")
    assert not ears.hub.send_voice("mac", m.SpeakEnd(1).to_json())
