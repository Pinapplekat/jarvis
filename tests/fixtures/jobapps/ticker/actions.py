"""Every job reads CONTROL, so a test decides what the next run does."""

import threading
import time

from jarvis.contract.api import ActionError, JobResult, Result, Think

CALLS: list[tuple[str, str, bool, str]] = []  # (job, reason, quiet, thread name)
CONTROL: dict = {}


def reset():
    CALLS.clear()
    CONTROL.clear()


def _run(job, ctx, run):
    CALLS.append((job, run.reason, run.quiet, threading.current_thread().name))
    ctrl = CONTROL.get(job, {})
    if ctrl.get("sleep"):
        time.sleep(ctrl["sleep"])
    if ctrl.get("db"):  # prove the thread has a connection of its own
        CONTROL.setdefault("conns", []).append(id(ctx.db))
    if ctrl.get("fail"):
        raise ActionError(ctrl["fail"]) if ctrl.get("action_error", True) else RuntimeError(ctrl["fail"])
    return JobResult(ctrl.get("summary", "ok"), tuple(ctrl.get("messages", ())), ctrl.get("think"), ctrl.get("wake_at"))


def beat(ctx, run):
    return _run("beat", ctx, run)


def daily(ctx, run):
    return _run("daily", ctx, run)


def silent(ctx, run):
    return _run("silent", ctx, run)


def needy(ctx, run):
    return _run("needy", ctx, run)


def thinker(ctx, run):
    return _run("thinker", ctx, run)


def named(ctx, run):
    return _run("named", ctx, run)


def poke(ctx):
    ctx.wake("beat")
    return Result("poked")


def context(ctx):
    return ""
