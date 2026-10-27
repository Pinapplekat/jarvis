"""The host router and a node, over a real localhost WebSocket (ephemeral port, nothing leaves the machine)."""

import threading
from types import SimpleNamespace

import pytest
from node_harness import FIXTURES, NodeThread, eye_module, fake_node, pair, start_hub, wait_for

from jarvis.core import actions, nodes
from jarvis.core.apps import load_apps
from jarvis.core.dispatch import Target
from jarvis.core.settings import load_settings
from jarvis.protocol import messages as m

CLI = actions.Origin("cli")
SECRET_DETAIL = "secret detail"


@pytest.fixture
def link(settings, tmp_path):
    reg = load_apps(settings, FIXTURES)
    hub = start_hub(reg)
    key = pair(reg)
    node = NodeThread(tmp_path, hub.port, key).start()
    node.wait_connected()
    wait_for(hub.connected, what="the hub to see the node")
    yield SimpleNamespace(reg=reg, hub=hub, node=node, key=key, tmp=tmp_path, tmp_host=settings.data_dir / "tmp")
    node.stop()
    hub.stop()
    reg.close()


def run(link, tool, args=None):
    return actions.run(link.reg, tool, args or {}, CLI)


def test_a_call_round_trips_with_an_attachment(link):
    out = run(link, "eye__snap")
    (att,) = out.result.attachments
    assert out.result.text == "snap plain" and att.path.read_bytes() == b"jpeg-bytes"
    assert att.path.parent == link.tmp_host.resolve()  # written into the host's tmp, an ordinary Attachment
    assert list(link.node.config.tmp_dir.iterdir()) == []  # the node deleted its copy once sent
    actions.release(out)
    assert list(link.tmp_host.iterdir()) == []
    assert actions.recent_log(link.reg)[-1]["status"] == "ran"


def test_an_action_error_comes_back_as_failed_with_its_message(link):
    out = run(link, "eye__fail")
    assert isinstance(out, actions.Failed) and out.message == "The eye is shut."


def test_a_crash_comes_back_generic(link):
    out = run(link, "eye__boom")
    assert isinstance(out, actions.Failed) and out.message == "Something went wrong in eye"
    assert SECRET_DETAIL not in actions.recent_log(link.reg)[-1]["result"]


def test_a_node_app_has_no_database(link):
    assert run(link, "eye__db_peek").message == "Something went wrong in eye"


def test_settings_reach_the_node_from_the_host(settings, tmp_path):
    settings.data_dir.mkdir(parents=True)
    (settings.data_dir / "settings.toml").write_text('[apps.eye]\nlabel = "custom"\n')
    reg = load_apps(load_settings(), FIXTURES)
    hub = start_hub(reg)
    node = NodeThread(tmp_path, hub.port, pair(reg)).start()
    try:
        node.wait_connected()
        assert actions.run(reg, "eye__hello", {}, CLI).result.text == "hello from the node (custom)"
    finally:
        node.stop(); hub.stop(); reg.close()


def test_a_disabled_app_is_not_run_on_the_node(settings, tmp_path):
    settings.data_dir.mkdir(parents=True)
    (settings.data_dir / "settings.toml").write_text("[apps.eye]\nenabled = false\n")
    reg = load_apps(load_settings(), FIXTURES)
    hub = start_hub(reg)
    node = NodeThread(tmp_path, hub.port, pair(reg)).start()
    try:
        node.wait_connected()
        assert node.runtime._apps["eye"].ctx is None  # the host sent no settings for it
    finally:
        node.stop(); hub.stop(); reg.close()


def test_undo_runs_on_the_node_too(link):
    eye = eye_module()
    before = eye.COUNT[0]
    run(link, "eye__bump")
    out = actions.undo(link.reg)
    assert isinstance(out, actions.Ran) and eye.COUNT[0] == before


def test_node_apps_never_run_on_the_host(link):
    assert link.reg.apps["eye"].module is None
    assert link.reg.apps["echo"].module is not None  # host apps still run in-process
    assert run(link, "echo__shout", {"text": "hi"}).result.text == "HI"


def test_the_node_times_out_a_slow_handler(link):
    eye = eye_module()
    eye.RELEASE.clear()
    out = run(link, "eye__slow")
    assert isinstance(out, actions.Failed) and "didn't finish within 1 seconds" in out.message
    eye.RELEASE.set()
    wait_for(lambda: list(link.node.config.tmp_dir.iterdir()) == [], what="the abandoned result to be deleted")


def test_no_node_with_the_capability(settings):
    reg = load_apps(settings, FIXTURES)
    try:
        out = actions.run(reg, "eye__hello", {}, CLI)  # no hub at all
        assert out.message == "That needs eye, and no device with it is connected."
        reg.nodes = start_hub(reg)
        assert actions.run(reg, "eye__hello", {}, CLI).message == out.message  # a hub, but nobody connected
    finally:
        reg.nodes.stop() if reg.nodes else None
        reg.close()


def test_the_host_times_out_a_silent_node(link, monkeypatch):
    monkeypatch.setattr("jarvis.core.node_hub.ANSWER_GRACE", 0.2)
    link.node.stop()
    ws, welcome = fake_node(link.hub.port, link.key)
    assert isinstance(welcome, m.Welcome)
    try:
        out = run(link, "eye__slow")
        assert out.message == 'The device "mac" didn\'t answer within 1 seconds.'
    finally:
        ws.close()


def test_a_node_dropping_mid_call(link):
    link.node.stop()
    ws, _ = fake_node(link.hub.port, link.key)
    threading.Timer(0.3, ws.close).start()
    out = run(link, "eye__slow")
    assert out.message == 'The device "mac" disconnected.'


def test_the_node_reconnects_after_the_host_restarts(link):
    port = link.hub.port
    link.hub.stop()
    assert run(link, "eye__hello").message.startswith("That needs eye")
    link.hub = link.reg.nodes = start_hub(link.reg, port=port)
    wait_for(link.hub.connected, what="the node to reconnect on its own")
    assert run(link, "eye__hello").result.text.startswith("hello from the node")


def test_a_silent_node_is_dropped_after_the_ping_deadline(settings):
    reg = load_apps(settings, FIXTURES)
    hub = start_hub(reg, ping_every=0.05, ping_deadline=0.25)
    key = pair(reg)
    ws, welcome = fake_node(hub.port, key)
    try:
        assert isinstance(welcome, m.Welcome)
        assert len(hub.connected()) == 1
        wait_for(lambda: not hub.connected(), what="the silent node to be dropped")
    finally:
        ws.close(); hub.stop(); reg.close()


def test_pings_keep_a_chatty_node_alive(settings, tmp_path):
    reg = load_apps(settings, FIXTURES)
    hub = start_hub(reg, ping_every=0.05, ping_deadline=0.25)
    node = NodeThread(tmp_path, hub.port, pair(reg)).start()
    try:
        node.wait_connected()
        threading.Event().wait(0.8)
        assert len(hub.connected()) == 1
    finally:
        node.stop(); hub.stop(); reg.close()


# ---- pairing and identity ------------------------------------------------------------------------------


def test_only_the_hash_of_the_key_is_stored(link):
    row = link.reg.conn.execute("SELECT * FROM nodes").fetchone()
    assert row["key_hash"] == nodes.hash_key(link.key) and link.key not in tuple(row)


@pytest.mark.parametrize("name,key_ok,v,reason", [
    ("mac", False, 1, "Unknown node or wrong key"),
    ("stranger", True, 1, "Unknown node or wrong key"),
    ("mac", True, 2, "protocol version 1"),
])
def test_bad_hellos_are_rejected(link, name, key_ok, v, reason):
    ws, reply = fake_node(link.hub.port, link.key if key_ok else "wrong", name=name, v=v)
    assert isinstance(reply, m.Reject) and reason in reply.reason
    ws.close()


def test_a_revoked_node_is_rejected_and_can_be_paired_again(link):
    nodes.revoke(link.reg.conn, "mac")
    ws, reply = fake_node(link.hub.port, link.key, name="mac2")
    assert isinstance(reply, m.Reject)
    ws.close()
    link.node.stop()
    ws, reply = fake_node(link.hub.port, link.key)
    assert isinstance(reply, m.Reject) and "revoked" in reply.reason
    ws.close()
    new_key = nodes.pair(link.reg.conn, "mac")
    ws, reply = fake_node(link.hub.port, new_key)
    assert isinstance(reply, m.Welcome)
    ws.close()


def test_a_second_connection_with_the_same_name_replaces_the_first(link):
    first = link.hub.connected()[0]
    ws, reply = fake_node(link.hub.port, link.key)
    try:
        assert isinstance(reply, m.Welcome)
        wait_for(lambda: [c.conn for c in link.hub.connected()] not in ([first.conn], []), what="the replacement")
        (only,) = link.hub.connected()
        assert only.conn != first.conn and not link.hub.is_live(Target(first.node_id, first.conn))
    finally:
        ws.close()


def test_pairing_rules(link):
    with pytest.raises(nodes.PairingError, match="already"):
        nodes.pair(link.reg.conn, "mac")
    with pytest.raises(nodes.PairingError, match="lowercase"):
        nodes.pair(link.reg.conn, "Bad Name")
    with pytest.raises(nodes.PairingError, match="no active node"):
        nodes.revoke(link.reg.conn, "nobody")


def test_the_most_recent_node_with_a_capability_gets_the_call(link):
    key2 = nodes.pair(link.reg.conn, "mac2")
    ws, _ = fake_node(link.hub.port, key2, name="mac2")
    try:
        out = []
        t = threading.Thread(target=lambda: out.append(run(link, "eye__hello")))
        t.start()
        call = m.parse(ws.recv(timeout=5))
        assert isinstance(call, m.Call) and call.app == "eye"
        ws.send(m.CallResult(call.id, True, "from mac2").to_json())
        t.join(5)
        assert out[0].result.text == "from mac2"
    finally:
        ws.close()


def test_unknown_and_malformed_frames_do_not_hurt_the_host(link):
    ws, _ = fake_node(link.hub.port, nodes.pair(link.reg.conn, "mac3"), name="mac3")
    ws.send("garbage"); ws.send('{"type": "teleport"}'); ws.send('{"type": "result", "id": 99, "ok": true}')
    ws.send(m.Ping(1).to_json())
    assert isinstance(m.parse(ws.recv(timeout=5)), m.Pong)
    ws.close()
    wait_for(lambda: len(link.hub.connected()) == 1, what="the extra node to leave")
    assert run(link, "eye__hello").result.text.startswith("hello")
