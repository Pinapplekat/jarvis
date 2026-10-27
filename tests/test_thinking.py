"""Thinking turns: the agent loop run on a job's behalf."""

import asyncio
from dataclasses import replace

import pytest
from fake_channel import FakeChannel
from scripted_brain import ScriptedBrain

from jarvis.contract.api import ActionError, Think
from jarvis.core import outbox
from jarvis.core.channel import Incoming
from jarvis.core.scheduler import Skip
from jarvis.host import Host

SEND = ("echo__send", {"text": "hello", "to": "Sam"})


def unsent(reg):
    return [r["text"] for r in outbox.unsent(reg.conn)]


def think(host, prompt="Write the brief.", **kw):
    return host.think("checkins", "morning", Think(prompt, **kw))


def test_the_reply_goes_to_the_outbox_and_the_prompt_is_not_a_user_message(echo):
    brain = ScriptedBrain(text="Good morning, sir.")
    host = Host(echo, brain, [FakeChannel()])
    assert asyncio.run(think(host)) == "replied"
    assert unsent(echo) == ["Good morning, sir."]
    assert echo.conn.execute("SELECT COUNT(*) FROM chat_log").fetchone()[0] == 0  # the sender adds it when it sends
    turn = echo.conn.execute("SELECT channel FROM turns").fetchone()
    assert turn["channel"] == "job:checkins.morning"
    prompt = brain.prompts[0]
    assert "Your task now" in prompt and "Write the brief." in prompt and "Elijah says:" not in prompt
    assert "speaking first" in brain.systems[0] and "(nothing)" in brain.systems[0]


def test_the_prompt_has_the_local_time_and_the_offset(echo):
    brain = ScriptedBrain(text="ok")
    asyncio.run(think(Host(echo, brain, [FakeChannel()])))
    assert "It is " in brain.prompts[0] and ("PDT" in brain.prompts[0] or "PST" in brain.prompts[0])
    assert "(UTC-0" in brain.prompts[0]


def test_nothing_sends_nothing(echo):
    host = Host(echo, ScriptedBrain(text="(nothing)"), [FakeChannel()])
    assert asyncio.run(think(host)) == "nothing to say"
    assert unsent(echo) == []


def test_a_job_that_only_uses_tools_delivers_nothing(echo):
    host = Host(echo, ScriptedBrain([("echo__note", {"text": "n"})], text="All saved."), [FakeChannel()])
    assert asyncio.run(think(host, deliver=False)) == "thought, not sent"
    assert unsent(echo) == [] and echo.apps["echo"].module.NOTES == ["n"]  # but its tools did run


def test_a_failed_turn_raises_and_sends_no_apology(echo):
    host = Host(echo, ScriptedBrain(error="the model fell over"), [FakeChannel()])
    with pytest.raises(ActionError, match="the model fell over"):
        asyncio.run(think(host))
    assert unsent(echo) == []


def test_the_origin_is_the_job_and_answer_ask_refuses_there(echo):
    host = Host(echo, ScriptedBrain([SEND], text="Shall I?"), [FakeChannel("discord")])

    async def go():
        await host.turn(Incoming("discord", "tell Sam hello"))  # Elijah's own channel creates an ask
        ask = host.channels["discord"].sent[0][2]
        job_brain = ScriptedBrain([("falak__answer_ask", {"ask_id": ask, "yes": True})], text="done")
        host.brain = job_brain
        await think(host)
        return job_brain

    job_brain = asyncio.run(go())
    assert job_brain.replies[0].is_error and "own channels" in job_brain.replies[0].text
    assert echo.apps["echo"].module.SENT == []


def test_a_job_is_not_one_of_elijahs_channels(echo):
    host = Host(echo, ScriptedBrain(), [FakeChannel("discord")])
    assert not any(c.startswith("job:") for c in host.own_channels)


@pytest.mark.parametrize("untrusted", [False, True])
def test_untrusted_marks_the_turn_as_driven_by_outside_content(echo, untrusted):
    host = Host(echo, ScriptedBrain([SEND], text="ok"), [FakeChannel()])
    asyncio.run(think(host, untrusted=untrusted))
    origin = echo.conn.execute("SELECT origin_json FROM asks").fetchone()["origin_json"]
    assert f'"outside_content": {"true" if untrusted else "false"}' in origin and '"job:checkins.morning"' in origin


def test_thinking_waits_behind_a_chat_turn_and_queued_chat_goes_first(echo):
    order = []

    class Slow(ScriptedBrain):
        async def run(self, system, prompt, tools):
            who = "job" if "Your task now" in prompt else prompt.rsplit("Elijah says:\n", 1)[1]
            order.append(f"start {who}")
            await asyncio.sleep(0.05)
            order.append(f"end {who}")
            return await super().run(system, prompt, tools)

    ch = FakeChannel()
    host = Host(echo, Slow(text="ok"), [ch])

    async def go():
        runner = asyncio.create_task(host.run())
        await asyncio.sleep(0)
        await ch.deliver(Incoming("discord", "one", reply_to="a"))
        await asyncio.sleep(0.01)  # "one" is running
        job = asyncio.create_task(think(host))
        await ch.deliver(Incoming("discord", "two", reply_to="b"))  # arrives while the job is waiting
        await job
        host.request_stop()
        await runner

    asyncio.run(go())
    assert order == ["start one", "end one", "start two", "end two", "start job", "end job"]


def test_two_thinking_turns_never_overlap(echo):
    running, seen = [0], []

    class Slow(ScriptedBrain):
        async def run(self, system, prompt, tools):
            running[0] += 1
            seen.append(running[0])
            await asyncio.sleep(0.03)
            running[0] -= 1
            return await super().run(system, prompt, tools)

    host = Host(echo, Slow(text="(nothing)"), [FakeChannel()])

    async def go():
        await asyncio.gather(think(host), think(host))

    asyncio.run(go())
    assert seen == [1, 1]


def test_the_daily_budget_skips_the_extra_run_and_leaves_chat_alone(echo, caplog):
    echo.settings = replace(echo.settings, thinking_runs_per_day=2)
    host = Host(echo, ScriptedBrain(text="(nothing)"), [FakeChannel()])

    async def go():
        await think(host)
        await host.turn(Incoming("discord", "hi"))  # chat turns don't count
        await think(host)
        with pytest.raises(Skip, match="budget"):
            await think(host)
        with pytest.raises(Skip):
            await think(host)
        return await host.turn(Incoming("discord", "still here?"))  # and chat still works over budget

    reply = asyncio.run(go())
    assert reply.error is None
    assert echo.conn.execute("SELECT COUNT(*) FROM turns WHERE channel LIKE 'job:%'").fetchone()[0] == 2
    assert caplog.text.count("budget of 2 a day") == 1  # logged once a day, not once per skip


def test_the_budget_counts_the_local_day(echo):
    from datetime import datetime, timedelta, timezone

    from jarvis.core import loop

    echo.settings = replace(echo.settings, timezone="America/Los_Angeles")
    now = datetime(2026, 10, 26, 20, 0, tzinfo=timezone.utc)  # 1:00 PM in Los Angeles
    for hours, channel in ((-14, "job:a.b"), (-5, "job:a.b"), (-1, "job:a.b"), (-1, "discord"), (3, "job:a.b")):
        echo.conn.execute("INSERT INTO turns (at, channel, ms, model) VALUES (?,?,?,?)",
                          ((now + timedelta(hours=hours)).isoformat(timespec="seconds"), channel, 1, "m"))
    # LA midnight is 07:00 UTC: -14h (6 AM UTC) is yesterday; -5h and -1h and +3h are today; one is a chat turn.
    assert loop.thinking_runs_today(echo, now) == 3


def test_a_check_in_whose_turn_fails_is_retried_and_counted_once(real_apps):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    class FailsOnce(ScriptedBrain):
        async def run(self, system, prompt, tools):
            self.raises = None if self.prompts else RuntimeError("model down")
            return await super().run(system, prompt, tools)

    host = Host(real_apps, FailsOnce(text="Morning, sir."), [FakeChannel()])
    sched = host.scheduler
    sched.online = lambda: True
    job = sched.jobs[("checkins", "morning")]
    at = datetime(2026, 10, 26, 8, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    first = asyncio.run(sched._run(job, "at", at))
    assert first.status == "failed" and unsent(real_apps) == []
    assert real_apps.conn.execute("SELECT COUNT(*) FROM checkins_sent").fetchone()[0] == 0
    retry = asyncio.run(sched._run(job, "every", at.replace(minute=2)))
    assert retry.status == "ok" and unsent(real_apps) == ["Morning, sir."]
    assert [tuple(r) for r in real_apps.conn.execute("SELECT day, kind FROM checkins_sent")] == [("2026-10-26", "morning")]
    third = asyncio.run(sched._run(job, "every", at.replace(minute=30)))
    assert third.summary == "already sent today" and unsent(real_apps) == ["Morning, sir."]
