"""The composition root: loads the channels, routes each message through the agent loop, one turn at a time."""

from __future__ import annotations

import asyncio
import importlib
import logging
import signal
from collections.abc import Callable
from dataclasses import dataclass

from jarvis.core import actions, loop
from jarvis.core.actions import Origin
from jarvis.core.apps import Registry, tmp_dir
from jarvis.core.brain import Brain
from jarvis.core.channel import AskResult, Channel, ChannelUnavailable, Incoming
from jarvis.contract.schema import validate
from jarvis.core.settings import Settings, SettingsError, load_secrets

log = logging.getLogger("jarvis.host")

CHANNEL_MODULES = {"discord": "jarvis.channels.discord"}  # channels that settings.toml and .env can switch on
TERMINAL = "cli"


def check_channel_sections(settings: Settings) -> list[str]:
    """Validate [channels.<name>] sections. Raises SettingsError for a bad key; returns warnings for unknown channels."""
    warnings = []
    for name, section in sorted(settings.channels.items()):
        if name not in CHANNEL_MODULES:
            warnings.append(f"settings.toml has [channels.{name}] but there is no channel named {name}")
            continue
        known = importlib.import_module(CHANNEL_MODULES[name]).SETTINGS
        for key, value in section.items():
            if key == "enabled":
                if not isinstance(value, bool):
                    raise SettingsError(f"[channels.{name}] 'enabled' must be true or false")
            elif key not in known:
                raise SettingsError(
                    f"[channels.{name}]: unknown setting '{key}' (known: {', '.join(['enabled', *known])})"
                )
            else:
                problems = validate(known[key], value, key)
                if problems:
                    raise SettingsError(f"[channels.{name}] {problems[0]}")
    return warnings


def build_channels(settings: Settings, host: Host) -> list[Channel]:
    """The enabled channels from settings. One plain line for each one that has to be skipped."""
    for warning in check_channel_sections(settings):
        log.warning(warning)
    secrets = load_secrets(settings.data_dir)
    channels: list[Channel] = []
    for name, module_path in CHANNEL_MODULES.items():
        section = settings.channels.get(name, {})
        if section.get("enabled", True) is False:
            continue
        module = importlib.import_module(module_path)
        try:
            channels.append(module.create(
                {k: v for k, v in section.items() if k != "enabled"},
                {k: secrets[k] for k in module.SECRETS if k in secrets},
                tmp_dir(settings),
                host.answer,
            ))
        except ChannelUnavailable as e:
            log.warning("%s: skipped, %s", name.capitalize(), e)
    return channels


@dataclass(frozen=True)
class _Stop:
    """Queue marker: no more turns."""


class Host:
    def __init__(self, reg: Registry, brain: Brain, channels: list[Channel] | None = None,
                 on_reply: Callable[[Incoming, loop.Reply], None] | None = None) -> None:
        self.reg, self.brain, self.on_reply = reg, brain, on_reply
        self.channels: dict[str, Channel] = {}
        for ch in channels or []:
            self.add_channel(ch)
        self._queue: asyncio.Queue[Incoming | _Stop] = asyncio.Queue()
        self._turn_lock = asyncio.Lock()  # a turn or a button answer, never two at once (one SQLite thread)
        self._stop = asyncio.Event()
        self._stopping = False

    def add_channel(self, channel: Channel) -> None:
        self.channels[channel.name] = channel

    @property
    def own_channels(self) -> frozenset[str]:
        return frozenset({TERMINAL, *self.channels})  # every delivered message comes from Elijah

    async def deliver(self, incoming: Incoming) -> None:
        if self._stopping:
            _discard(incoming)
            return
        await self._queue.put(incoming)

    async def answer(self, ask_id: int, yes: bool) -> AskResult:
        """A Yes/No button was pressed: answer the ask exactly as the loop's answer_ask would."""
        async with self._turn_lock:
            out = actions.answer_ask(self.reg, ask_id, yes)
        if isinstance(out, actions.Ran):
            actions.release(out)
            return AskResult(True, out.result.text)
        if isinstance(out, actions.Declined):
            return AskResult(True, "Okay, I won't.")
        return AskResult(False, out.message)

    async def turn(self, incoming: Incoming) -> loop.Reply:
        """One message in, one reply sent back through the channel it came from."""
        channel = self.channels[incoming.channel]
        async with self._turn_lock:
            async with channel.typing(incoming.reply_to):
                reply = await loop.handle_message(
                    self.reg, self.brain, incoming.text, Origin(incoming.channel),
                    own_channels=self.own_channels, attachments=incoming.attachments,
                )
        log.info("turn channel=%s ms=%d tools=%s error=%s", incoming.channel, reply.ms,
                 ",".join(reply.tools) or "-", reply.error or "-")
        ask_id = reply.new_asks[-1] if reply.new_asks else None  # buttons go on the latest ask only
        await channel.send(reply.text, reply_to=incoming.reply_to, ask_id=ask_id)
        if self.on_reply:
            self.on_reply(incoming, reply)
        return reply

    async def _work(self) -> None:
        while True:
            item = await self._queue.get()
            if isinstance(item, _Stop):
                return
            try:
                await self.turn(item)
            except Exception:
                log.exception("a turn failed on %s", item.channel)

    async def _run_channel(self, channel: Channel) -> None:
        try:
            await channel.start(self.deliver)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("channel %s stopped with an error", channel.name)

    def request_stop(self) -> None:
        self._stop.set()

    async def run(self, handle_signals: bool = False) -> None:
        """Run until stopped (Ctrl-C) or until every channel has finished. Closes the registry."""
        if handle_signals:
            for sig in (signal.SIGINT, signal.SIGTERM):
                asyncio.get_running_loop().add_signal_handler(sig, self.request_stop)
        worker = asyncio.create_task(self._work())
        runners = [asyncio.create_task(self._run_channel(c)) for c in self.channels.values()]
        stopper = asyncio.create_task(self._stop.wait())
        try:
            live = set(runners)
            while live and not self._stop.is_set():
                done, _ = await asyncio.wait({*live, stopper}, return_when=asyncio.FIRST_COMPLETED)
                live -= done
        finally:
            await self._shutdown(worker, runners, stopper)

    async def _shutdown(self, worker: asyncio.Task, runners: list[asyncio.Task], stopper: asyncio.Task) -> None:
        self._stopping = True
        stopper.cancel()
        for ch in self.channels.values():
            try:
                await ch.stop()
            except Exception:
                log.exception("stopping channel %s failed", ch.name)
        for task in runners:
            task.cancel()
        await asyncio.gather(*runners, return_exceptions=True)
        dropped = 0
        while not self._queue.empty():
            item = self._queue.get_nowait()
            if isinstance(item, Incoming):
                _discard(item)
                dropped += 1
        if dropped:
            log.warning("shutting down with %d unanswered message(s)", dropped)
        self._queue.put_nowait(_Stop())
        await worker  # a running turn finishes or hits its own timeout
        self.close()

    def close(self) -> None:
        self.reg.close()


def _discard(incoming: Incoming) -> None:
    for att in incoming.attachments:
        att.path.unlink(missing_ok=True)
