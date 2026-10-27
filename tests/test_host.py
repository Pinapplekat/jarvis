import asyncio
import logging

import pytest
from fake_channel import FakeChannel
from scripted_brain import ScriptedBrain

from jarvis.core import loop
from jarvis.core.actions import Origin
from jarvis.contract.api import Attachment
from jarvis.core.channel import Incoming
from jarvis.core.settings import SettingsError, load_settings
from jarvis.host import Host, build_channels, check_channel_sections

SEND = ("echo__send", {"text": "hello", "to": "Sam"})


async def settle(host, n):
    """Wait until the host has sent n replies."""
    for _ in range(500):
        if sum(len(c.sent) for c in host.channels.values()) >= n:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("no reply")


def run_host(echo, brain, messages, channel=None, expect=None):
    ch = channel or FakeChannel()
    host = Host(echo, brain, [ch])

    async def go():
        task = asyncio.create_task(host.run())
        await asyncio.sleep(0)
        for m in messages:
            await ch.deliver(m)
        await settle(host, expect or len(messages))
        host.request_stop()
        await task

    asyncio.run(go())
    return host, ch


def test_replies_go_back_through_the_same_channel_with_reply_to(echo):
    brain = ScriptedBrain(text="Twelve, sir.")
    _, ch = run_host(echo, brain, [Incoming("discord", "15% of 80?", reply_to="dm-1")])
    assert ch.sent == [("Twelve, sir.", "dm-1", None)]
    assert ch.events == [("typing on", "dm-1"), ("typing off", "dm-1"), ("send", "Twelve, sir.")]
    assert "Channel: discord" in brain.prompts[0]


def test_turns_run_one_at_a_time_in_order(echo):
    running, overlap, order = [0], [], []

    class SlowBrain(ScriptedBrain):
        async def run(self, system, prompt, tools):
            running[0] += 1
            overlap.append(running[0])
            order.append(prompt.rsplit("Elijah says:\n", 1)[1])
            await asyncio.sleep(0.02)
            running[0] -= 1
            return await super().run(system, prompt, tools)

    msgs = [Incoming("discord", t, reply_to=t) for t in ("one", "two", "three")]
    _, ch = run_host(echo, SlowBrain(), msgs)
    assert order == ["one", "two", "three"] and max(overlap) == 1
    assert [s[1] for s in ch.sent] == ["one", "two", "three"]


def test_a_new_ask_passes_its_id_to_send(echo):
    _, ch = run_host(echo, ScriptedBrain([SEND], text="Shall I send it?"), [Incoming("discord", "tell Sam hello")])
    assert ch.sent[0][2] == 1 and echo.apps["echo"].module.SENT == []


def test_button_answers(echo):
    host = Host(echo, ScriptedBrain([SEND]), [FakeChannel()])

    async def go():
        await host.turn(Incoming("discord", "tell Sam hello"))
        ask_id = host.channels["discord"].sent[0][2]
        return [await host.answer(ask_id, True), await host.answer(ask_id, True)]

    first, second = asyncio.run(go())
    assert first.ok and not second.ok and "already approved" in second.message
    assert len(echo.apps["echo"].module.SENT) == 1


def test_declining_by_button(echo):
    host = Host(echo, ScriptedBrain([SEND]), [FakeChannel()])

    async def go():
        await host.turn(Incoming("discord", "tell Sam hello"))
        return await host.answer(host.channels["discord"].sent[0][2], False)

    result = asyncio.run(go())
    assert (result.ok, result.message) == (True, "Okay, I won't.")
    assert echo.apps["echo"].module.SENT == []


def test_own_channels_are_the_started_ones_plus_the_terminal(echo):
    host = Host(echo, ScriptedBrain(), [FakeChannel("discord")])
    assert host.own_channels == {"cli", "discord"}


def test_answer_ask_accepts_a_started_channel_and_refuses_another(echo):
    answer = lambda ask: ("falak__answer_ask", {"ask_id": ask, "yes": True})
    brain = ScriptedBrain([SEND], text="ok")
    host = Host(echo, brain, [FakeChannel("discord")])

    async def go():
        await host.turn(Incoming("discord", "send it"))
        ask = host.channels["discord"].sent[0][2]
        yes = ScriptedBrain([answer(ask)])
        host.brain = yes
        await host.turn(Incoming("discord", "yes"))
        return yes

    yes = asyncio.run(go())
    assert not yes.replies[0].is_error and len(echo.apps["echo"].module.SENT) == 1


def test_a_channel_outside_the_own_set_cannot_approve(echo):
    host = Host(echo, ScriptedBrain([SEND]), [FakeChannel("discord")])
    other = ScriptedBrain([("falak__answer_ask", {"ask_id": 1, "yes": True})])

    async def go():
        await host.turn(Incoming("discord", "send it"))
        return await loop.handle_message(echo, other, "yes", Origin("telegram"), own_channels=host.own_channels)

    asyncio.run(go())
    assert other.replies[0].is_error and "own channels" in other.replies[0].text
    assert echo.apps["echo"].module.SENT == []


def test_a_turn_is_logged_without_the_message_text(echo, caplog):
    caplog.set_level(logging.INFO)
    run_host(echo, ScriptedBrain([("echo__shout", {"text": "x"})]), [Incoming("discord", "my secret plan")])
    lines = [r.getMessage() for r in caplog.records if r.name == "jarvis.host"]
    assert len(lines) == 1 and "channel=discord" in lines[0] and "tools=echo__shout" in lines[0]
    assert "secret" not in caplog.text


def test_shutdown_stops_channels_and_closes_the_registry(echo):
    host, ch = run_host(echo, ScriptedBrain(), [Incoming("discord", "hi")])
    assert ch.stopped
    with pytest.raises(Exception):
        echo.conn.execute("SELECT 1")


def test_shutdown_lets_the_running_turn_finish(echo):
    class Slow(ScriptedBrain):
        async def run(self, *a):
            await asyncio.sleep(0.05)
            return await super().run(*a)

    ch = FakeChannel()
    host = Host(echo, Slow(text="finished"), [ch])

    async def go():
        task = asyncio.create_task(host.run())
        await asyncio.sleep(0)
        await ch.deliver(Incoming("discord", "slow one"))
        await ch.deliver(Incoming("discord", "never answered"))
        await asyncio.sleep(0.01)
        host.request_stop()
        await task

    asyncio.run(go())
    assert [s[0] for s in ch.sent] == ["finished"]


def test_a_channel_that_crashes_does_not_take_the_host_down(echo):
    class Crashy(FakeChannel):
        async def start(self, deliver):
            raise RuntimeError("boom")

    ok = FakeChannel("other")
    host = Host(echo, ScriptedBrain(text="hi"), [Crashy("crashy"), ok])

    async def go():
        task = asyncio.create_task(host.run())
        await asyncio.sleep(0.01)
        await ok.deliver(Incoming("other", "x"))
        await settle(host, 1)
        host.request_stop()
        await task

    asyncio.run(go())
    assert ok.sent[0][0] == "hi"


def test_a_missing_token_skips_discord_with_a_message(data_dir, caplog):
    data_dir.mkdir()
    (data_dir / "settings.toml").write_text("[channels.discord]\nowner_id = 5\n")
    settings = load_settings()
    host = Host.__new__(Host)
    assert build_channels(settings, host) == []
    assert "Discord: skipped, DISCORD_BOT_TOKEN is missing from .env" in caplog.text


def test_a_missing_owner_id_skips_discord_with_a_message(data_dir, caplog):
    data_dir.mkdir()
    (data_dir / ".env").write_text("DISCORD_BOT_TOKEN=abc123\n")
    assert build_channels(load_settings(), Host.__new__(Host)) == []
    assert "owner_id is missing" in caplog.text and "abc123" not in caplog.text


def test_discord_starts_with_token_and_owner_id(data_dir):
    data_dir.mkdir()
    (data_dir / ".env").write_text("DISCORD_BOT_TOKEN=abc123\n")
    (data_dir / "settings.toml").write_text("[channels.discord]\nowner_id = 5\n")
    (channel,) = build_channels(load_settings(), Host.__new__(Host))
    assert channel.name == "discord" and channel.owner_id == 5


def test_discord_can_be_switched_off(data_dir):
    data_dir.mkdir()
    (data_dir / ".env").write_text("DISCORD_BOT_TOKEN=abc123\n")
    (data_dir / "settings.toml").write_text("[channels.discord]\nowner_id = 5\nenabled = false\n")
    assert build_channels(load_settings(), Host.__new__(Host)) == []


def test_unknown_channel_key_is_an_error_and_unknown_channel_a_warning(data_dir):
    data_dir.mkdir()
    (data_dir / "settings.toml").write_text("[channels.discord]\nowner = 5\n")
    with pytest.raises(SettingsError, match=r"\[channels.discord\].*owner"):
        check_channel_sections(load_settings())
    (data_dir / "settings.toml").write_text("[channels.telegram]\nx = 1\n")
    assert "no channel named telegram" in check_channel_sections(load_settings())[0]


def test_bad_channel_value_is_an_error(data_dir):
    data_dir.mkdir()
    (data_dir / "settings.toml").write_text('[channels.discord]\nowner_id = "me"\n')
    with pytest.raises(SettingsError, match="owner_id"):
        check_channel_sections(load_settings())


def test_channels_must_be_a_table_of_tables(data_dir):
    data_dir.mkdir()
    (data_dir / "settings.toml").write_text("channels = 3\n")
    with pytest.raises(SettingsError, match="channels"):
        load_settings()


def test_images_reach_the_brain_and_are_gone_afterwards(echo):
    tmp = echo.settings.data_dir / "tmp"
    path = tmp / "pic.png"
    path.write_bytes(b"png")
    brain = ScriptedBrain([("falak__view_attachment", {"n": 1})], text="A cat.")
    host = Host(echo, brain, [FakeChannel()])
    asyncio.run(host.turn(Incoming("discord", "what is this?", (Attachment("image", path, "image/png"),))))
    (image,) = brain.replies[0].images
    assert (image.mime, image.data) == ("image/png", "cG5n")
    assert "Elijah attached 1 image" in brain.prompts[0]
    assert not path.exists()


def test_viewing_an_image_counts_as_outside_content(echo):
    path = echo.settings.data_dir / "tmp" / "pic.png"
    path.write_bytes(b"png")
    att = (Attachment("image", path, "image/png"),)
    ask = ScriptedBrain([SEND])
    host = Host(echo, ask, [FakeChannel()])
    asyncio.run(host.turn(Incoming("discord", "tell Sam hello")))
    brain = ScriptedBrain([("falak__view_attachment", {"n": 1}), ("falak__answer_ask", {"ask_id": 1, "yes": True})])
    host.brain = brain
    asyncio.run(host.turn(Incoming("discord", "yes", att)))
    assert not brain.replies[0].is_error
    assert brain.replies[1].is_error and "outside content" in brain.replies[1].text
    assert echo.apps["echo"].module.SENT == []


def test_unviewed_attachments_are_deleted_too_and_the_tool_is_absent_without_images(echo):
    path = echo.settings.data_dir / "tmp" / "pic.png"
    path.write_bytes(b"png")
    host = Host(echo, ScriptedBrain(), [FakeChannel()])
    asyncio.run(host.turn(Incoming("discord", "", (Attachment("image", path, "image/png"),))))
    assert not path.exists()
    names = []

    class Peek(ScriptedBrain):
        async def run(self, system, prompt, tools):
            names.extend(t.name for t in tools)
            return await super().run(system, prompt, tools)

    asyncio.run(Host(echo, Peek(), [FakeChannel()]).turn(Incoming("discord", "hi")))
    assert "falak__view_attachment" not in names


def test_bad_view_attachment_numbers_are_errors(echo):
    path = echo.settings.data_dir / "tmp" / "pic.png"
    path.write_bytes(b"png")
    brain = ScriptedBrain([("falak__view_attachment", {"n": 2})])
    asyncio.run(Host(echo, brain, [FakeChannel()]).turn(Incoming("discord", "x", (Attachment("image", path, "image/png"),))))
    assert brain.replies[0].is_error


def test_a_photo_goes_out_with_the_reply_only_when_the_model_sends_it(echo):
    brain = ScriptedBrain([("echo__photo", {}), ("falak__send_image", {"n": 1})], text="Here you go, sir.")
    _, ch = run_host(echo, brain, [Incoming("discord", "send me a photo", reply_to="dm-1")])
    assert brain.replies[0].text == "Took it (image 1 of this turn)"
    (picture,) = ch.images[0]
    assert (picture.mime, picture.data) == ("image/jpeg", b"jpeg")
    assert list((echo.settings.data_dir / "tmp").iterdir()) == []  # the photo is in memory, not on disk



def test_a_photo_the_model_only_looked_at_isnt_sent(echo):
    ch = FakeChannel()
    asyncio.run(Host(echo, ScriptedBrain([("echo__photo", {})], text="A desk."), [ch]).turn(Incoming("discord", "look")))
    assert ch.images == [()]


def test_send_image_needs_an_image_from_this_turn(echo):
    brain = ScriptedBrain([("falak__send_image", {"n": 1}), ("echo__photo", {}), ("falak__send_image", {"n": 2})])
    run_host(echo, brain, [Incoming("discord", "send it")])
    assert brain.replies[0].is_error and "No tool has returned an image" in brain.replies[0].text
    assert brain.replies[2].is_error and "from 1 to 1" in brain.replies[2].text


def test_job_turns_have_no_send_image_tool(echo):
    names = []

    class Peek(ScriptedBrain):
        async def run(self, system, prompt, tools):
            names.append([t.name for t in tools])
            return await super().run(system, prompt, tools)

    asyncio.run(loop.handle_job(echo, Peek(), "morning", Origin("job:checkins.morning")))
    asyncio.run(Host(echo, Peek(), [FakeChannel()]).turn(Incoming("discord", "hi")))
    assert "falak__send_image" not in names[0] and "falak__send_image" in names[1]
