"""The composition root: loads the channels, routes each message through the agent loop, one turn at a time."""

from __future__ import annotations

import asyncio
import base64
import importlib
import logging
import signal
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from jarvis.contract.api import ActionError, Think
from jarvis.core import actions, loop, outbox, sessions
from jarvis.core.actions import Origin
from jarvis.core.apps import Registry, tmp_dir
from jarvis.core.brain import Brain
from jarvis.core.channel import AskResult, Channel, ChannelUnavailable, Incoming, Picture
from jarvis.core.scheduler import Scheduler, Skip
from jarvis.contract.schema import validate
from jarvis.contract.envfile import load_secrets
from jarvis.core.settings import Settings, SettingsError

log = logging.getLogger("jarvis.host")

CHANNEL_MODULES = {"discord": "jarvis.channels.discord"}  # channels that settings.toml and .env can switch on
TERMINAL = "cli"
SWEEP_EVERY = 30  # seconds


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
        self._queued = 0  # chat messages delivered and not yet answered; thinking turns let them go first
        self._budget_logged = None  # the local day the over-budget line was last logged
        self.sender = outbox.Sender(reg.conn, self._notify_channel, lambda: datetime.now(timezone.utc),
                                    reg.settings.tz())
        self._scheduler: Scheduler | None = None
        self._loop: asyncio.AbstractEventLoop | None = None  # set by run(); app threads notify through it
        for app in reg.apps.values():
            app.ctx.notify_hook = self._app_notify

    def _app_notify(self, app: str, text: str) -> None:
        """ctx.notify of a host app. Its handler may run in a worker thread, but the outbox connection is the
        loop's, so a call from elsewhere is handed over."""
        loop = self._loop
        if loop is not None and loop.is_running():
            try:
                asyncio.get_running_loop()
            except RuntimeError:  # not on the loop's thread
                loop.call_soon_threadsafe(self.notify, text, f"app:{app}")
                return
        self.notify(text, f"app:{app}")

    def add_channel(self, channel: Channel) -> None:
        self.channels[channel.name] = channel

    @property
    def own_channels(self) -> frozenset[str]:
        return frozenset({TERMINAL, *self.channels})  # every delivered message comes from Elijah

    async def deliver(self, incoming: Incoming) -> None:
        if self._stopping:
            _discard(incoming)
            return
        self._queued += 1
        await self._queue.put(incoming)

    @property
    def scheduler(self) -> Scheduler:
        if self._scheduler is None:  # only `serve` and `jobs run` need it; chat and ask don't touch the jobs table
            self._scheduler = Scheduler(self.reg, self.notify, self.think)
        return self._scheduler

    def _notify_channel(self) -> Channel | None:
        """Where Falak speaks first: the notify_channel setting, else the first channel that's running."""
        return self.channels.get(self.reg.settings.notify_channel) or next(iter(self.channels.values()), None)

    def notify(self, text: str, source: str, not_before: datetime | None = None) -> None:
        """Say something to Elijah unprompted: one row in the outbox, and the sender is woken."""
        outbox.add(self.reg.conn, text, source, not_before=not_before)
        self.sender.nudge()

    async def think(self, app: str, job: str, think: Think, not_before: datetime | None = None) -> str:
        """A job's thinking turn: waits behind chat, counts against the daily budget, and delivers via the outbox."""
        limit = self.reg.settings.thinking_runs_per_day
        if loop.thinking_runs_today(self.reg) >= limit:
            today = datetime.now(self.reg.settings.tz()).date()
            if self._budget_logged != today:
                self._budget_logged = today
                log.warning("thinking turns: the budget of %d a day is used up; skipping until tomorrow", limit)
            raise Skip("budget")
        source = f"job:{app}.{job}"
        while self._queued or self._turn_lock.locked():  # never ahead of a chat message, never in the middle of one
            await asyncio.sleep(0.05)
        async with self._turn_lock:
            reply = await loop.handle_job(self.reg, self.brain, think.prompt, Origin(source, think.untrusted),
                                          own_channels=self.own_channels)
        log.info("turn channel=%s ms=%d tools=%s error=%s", source, reply.ms, ",".join(reply.tools) or "-",
                 reply.error or "-")
        if reply.error:
            raise ActionError(f"the thinking turn failed: {reply.error}")
        if not think.deliver:
            return "thought, not sent"
        if reply.text == "(nothing)":
            return "nothing to say"
        self.notify(reply.text, source, not_before)
        return "replied"

    async def answer(self, ask_id: int, yes: bool) -> AskResult:
        """A Yes/No button was pressed: answer the ask exactly as the loop's answer_ask would."""
        async with self._turn_lock:
            out = await asyncio.to_thread(actions.answer_ask, self.reg, ask_id, yes)
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
        pictures = tuple(Picture(img.mime, base64.b64decode(img.data)) for img in reply.images)
        await channel.send(reply.text, reply_to=incoming.reply_to, ask_id=ask_id, images=pictures)
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
            finally:
                self._queued -= 1

    async def _run_channel(self, channel: Channel) -> None:
        try:
            await channel.start(self.deliver)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("channel %s stopped with an error", channel.name)

    async def _sweep(self) -> None:
        """Every 30 s, close live views whose device left or that sat idle (each turn also checks)."""
        while True:
            await asyncio.sleep(SWEEP_EVERY)
            try:
                async with self._turn_lock:
                    await asyncio.to_thread(sessions.sweep, self.reg)
            except Exception:
                log.exception("the session sweep failed")

    def request_stop(self) -> None:
        self._stop.set()

    async def run(self, handle_signals: bool = False) -> None:
        """Run until stopped (Ctrl-C) or until every channel has finished. Closes the registry."""
        self._loop = asyncio.get_running_loop()
        if handle_signals:
            for sig in (signal.SIGINT, signal.SIGTERM):
                asyncio.get_running_loop().add_signal_handler(sig, self.request_stop)
        worker = asyncio.create_task(self._work())
        runners = [asyncio.create_task(self._run_channel(c)) for c in self.channels.values()]
        stopper = asyncio.create_task(self._stop.wait())
        sweeper = asyncio.create_task(self._sweep())
        background = [asyncio.create_task(self.scheduler.run()), asyncio.create_task(self.sender.run())]
        try:
            live = set(runners)
            while live and not self._stop.is_set():
                done, _ = await asyncio.wait({*live, stopper}, return_when=asyncio.FIRST_COMPLETED)
                live -= done
        finally:
            sweeper.cancel()
            self.scheduler.stop()
            self.sender.stop()
            for task in background:
                task.cancel()
            await asyncio.gather(*background, return_exceptions=True)
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
                self._queued -= 1
        if dropped:
            log.warning("shutting down with %d unanswered message(s)", dropped)
        self._queue.put_nowait(_Stop())
        await worker  # a running turn finishes or hits its own timeout
        self.close()

    def close(self) -> None:
        sessions.stop_all(self.reg, "host stopped")  # the devices' capture processes must not outlive us
        if self.reg.nodes is not None:
            self.reg.nodes.stop()
        self.reg.close()


def _discard(incoming: Incoming) -> None:
    for att in incoming.attachments:
        att.path.unlink(missing_ok=True)
