"""Wiring the voice channels (the desk and Discord calls) into a running host. Like host.py this is a composition
module: it is where channels, the speech engines, the node hub and the apps meet."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from jarvis.contract.envfile import load_secrets
from jarvis.core.channel import Channel, ChannelUnavailable

if TYPE_CHECKING:
    from jarvis.host import Host

log = logging.getLogger("jarvis.voice_wiring")


def voice_channel(host: "Host", hub) -> Channel | None:
    """The desk channel, when the voice and desk apps are on and `[channels.desk]` isn't disabled: it takes the audio
    frames the node hub receives and sends its speech back through the hub."""
    from jarvis.channels import desk
    from jarvis.core import share
    from jarvis.speech import VoiceProfile

    reg = host.reg
    if "voice" not in reg.apps or "desk" not in reg.apps or reg.settings.channels.get("desk", {}).get("enabled") is False:
        return None
    voice = reg.apps["voice"].ctx.settings

    def profile() -> VoiceProfile:
        return VoiceProfile.from_dict(share.call(reg, "desk", "voice", "current_voice", {}).data["profile"])

    services = desk.make_services(data_dir=reg.settings.data_dir, engine=lambda: voice["engine"], profile=profile,
                                  idle_minutes=voice["idle_minutes"])
    host.voice_services = services
    channel = desk.DeskChannel(services=services, send_frame=hub.send_voice)
    hub.voice_sink = channel.on_frame
    return channel


def call_channel(host: "Host") -> Channel | None:
    """Discord voice calls, which are off until `[channels.discord_call] enabled = true` (they need Node 22, a bot with
    the voice intents, and the server and channel ids). Shares the desk's listener and loaded voice."""
    from jarvis.channels import desk, discord_call
    from jarvis.core import share
    from jarvis.speech import VoiceProfile
    settings = host.reg.settings
    section = settings.channels.get("discord_call", {})
    if section.get("enabled") is not True:
        return None
    owner = settings.channels.get("discord", {}).get("owner_id")
    token = load_secrets(settings.data_dir).get("DISCORD_BOT_TOKEN")
    if not (owner and token and section.get("guild_id")):
        log.warning("Discord calls: skipped, they need DISCORD_BOT_TOKEN, [channels.discord] owner_id and "
                    "[channels.discord_call] guild_id")
        return None
    services = host.voice_services
    if services is None:  # no desk: the same engines, with the profile read straight from the voice app
        voice = host.reg.apps["voice"].ctx.settings

        def profile() -> VoiceProfile:
            return VoiceProfile.from_dict(share.call(host.reg, "voice", "voice", "current_voice", {}).data["profile"])

        services = desk.make_services(data_dir=settings.data_dir, engine=lambda: voice["engine"], profile=profile,
                                      idle_minutes=voice["idle_minutes"])
    model = settings.data_dir / "models" / "silero_vad.onnx"

    def check() -> None:
        discord_call.check_node(discord_call.node_version())
        if not model.exists():
            raise ChannelUnavailable("the speech detector is missing; run `jarvis voice setup`")

    channel = discord_call.CallChannel(
        services=services, vad=lambda pcm: _vad(model)(pcm), token=token, owner_id=owner, guild_id=section["guild_id"],
        voice_channel_id=section.get("voice_channel_id"), run_dir=settings.data_dir / "run", check=check)
    host.reg.call_link = channel.link
    return channel


_VAD = []


def _vad(model):
    from jarvis.speech.vad import SileroVad

    if not _VAD:
        _VAD.append(SileroVad(model))
    return _VAD[0]
