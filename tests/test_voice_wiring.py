"""The voice channels wired into a real registry: the profile comes from the voice app, settings are validated."""

import pytest

from jarvis.core import actions
from jarvis.core.settings import SettingsError, load_settings
from jarvis.host import Host, check_channel_sections
from jarvis.speech import VoiceProfile
from jarvis.voice_wiring import call_channel, voice_channel


class FakeHub:
    voice_sink = None

    def send_voice(self, node, frame):
        return True


def host_for(real_apps):
    from scripted_brain import ScriptedBrain

    return Host(real_apps, ScriptedBrain())


def test_the_desk_reads_its_voice_from_the_voice_app_and_a_change_applies_at_once(real_apps):
    host, hub = host_for(real_apps), FakeHub()
    desk = voice_channel(host, hub)
    assert desk is not None and desk.name == "desk" and hub.voice_sink == desk.on_frame
    assert host.voice_services is desk.services
    assert desk.services.engine() == "kokoro"
    assert desk.services.profile() == VoiceProfile(voice="bm_george", speed=1.0)
    out = actions.run(real_apps, "voice__set_voice", {"attributes": {"voice": "bm_george,am_onyx", "speed": 0.9}},
                      actions.Origin("desk"))
    assert isinstance(out, actions.Ran)
    assert desk.services.profile() == VoiceProfile(voice="bm_george,am_onyx", speed=0.9)  # the next sentence
    assert isinstance(actions.undo(real_apps), actions.Ran)
    assert desk.services.profile() == VoiceProfile(voice="bm_george", speed=1.0)  # "undo that"
    assert real_apps.conn.execute("SELECT COUNT(*) FROM action_log WHERE tool = 'voice__current_voice'").fetchone()[0] == 0


def test_the_desk_can_be_turned_off(data_dir):
    from jarvis.core.apps import load_apps

    data_dir.mkdir(parents=True)
    (data_dir / "settings.toml").write_text("[channels.desk]\nenabled = false\n")
    reg = load_apps(load_settings())
    try:
        assert voice_channel(host_for(reg), FakeHub()) is None
    finally:
        reg.close()


def test_calls_are_off_until_enabled_and_need_their_settings(data_dir, caplog):
    from jarvis.core.apps import load_apps

    data_dir.mkdir(parents=True)
    reg = load_apps(load_settings())
    try:
        assert call_channel(host_for(reg)) is None  # not enabled: nothing is said about it
    finally:
        reg.close()
    (data_dir / "settings.toml").write_text('[channels.discord]\nowner_id = 42\n[channels.discord_call]\nenabled = true\n')
    (data_dir / ".env").write_text("DISCORD_BOT_TOKEN=tok\n")
    reg = load_apps(load_settings())
    try:
        assert call_channel(host_for(reg)) is None and "guild_id" in caplog.text  # enabled, but no server given
    finally:
        reg.close()
    (data_dir / "settings.toml").write_text('[channels.discord]\nowner_id = 42\n[channels.discord_call]\n'
                                            'enabled = true\nguild_id = 7\nvoice_channel_id = 555\n')
    reg = load_apps(load_settings())
    try:
        channel = call_channel(host_for(reg))
        assert channel is not None and channel.name == "discord_call"
        assert reg.call_link() == "https://discord.com/channels/7/555"  # what "call me" sends
    finally:
        reg.close()


@pytest.mark.parametrize("section,problem", [
    ({"desk": {"voice_model": "haiku", "voice_max_turns": 4, "enabled": True}}, None),
    ({"desk": {"voice_model": 3}}, "voice_model"),
    ({"desk": {"voice_max_turns": 0}}, "voice_max_turns"),
    ({"desk": {"loudness": 11}}, "unknown setting 'loudness'"),
    ({"discord_call": {"guild_id": 7, "voice_channel_id": 5, "enabled": True}}, None),
    ({"discord_call": {"guild_id": "seven"}}, "guild_id"),
])
def test_channel_sections_for_the_voice_channels_are_validated(section, problem):
    from dataclasses import replace

    settings = replace(load_settings({"FALAK_DATA_DIR": "/nonexistent-falak-test"}), channels=section)
    if problem is None:
        assert check_channel_sections(settings) == []
    else:
        with pytest.raises(SettingsError, match=problem):
            check_channel_sections(settings)


def test_a_call_section_without_enabled_says_how_to_turn_it_on(data_dir, caplog):
    from jarvis.core.apps import load_apps

    data_dir.mkdir(parents=True)
    (data_dir / "settings.toml").write_text('[channels.discord_call]\nguild_id = 7\nvoice_channel_id = 555\n')
    reg = load_apps(load_settings())
    try:
        assert call_channel(host_for(reg)) is None and "enabled = true" in caplog.text
    finally:
        reg.close()
