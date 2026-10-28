"""A brain that calls tools in a scripted order and returns scripted text. No Claude involved."""

import asyncio

from jarvis.core.brain import BrainResult


class ScriptedBrain:
    """`steps`: (tool name, args) pairs; args may be a function of the replies so far. Records what it saw."""

    def __init__(self, steps=(), text="Done, sir.", error=None, raises=None, hang=False):
        self.steps, self.text, self.error, self.raises, self.hang = list(steps), text, error, raises, hang
        self.systems, self.prompts, self.replies = [], [], []

    async def run(self, system, prompt, tools, on_text=None):
        self.systems.append(system)
        self.prompts.append(prompt)
        if self.hang:
            await asyncio.sleep(30)
        if self.raises:
            raise self.raises
        by_name = {t.name: t for t in tools}
        used = []
        for name, args in self.steps:
            args = args(self.replies) if callable(args) else args
            self.replies.append(await by_name[name].call(args))
            used.append(name)
        if on_text:
            on_text(self.text)
        return BrainResult(self.text, used, num_turns=len(used) + 1, cost_usd=0.01, error=self.error)
