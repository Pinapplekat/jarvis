"""Per-step timing for one reply (transcribe, route, peek, model rounds, first audio, total), so
speed changes can be compared before and after. One turn per thread; each finished turn is one
'timing' event holding JSON (step -> seconds). Timings and step names only, never message text."""

import json
import threading
import time
from contextlib import contextmanager

_local = threading.local()


def start(channel: str) -> dict:
    """Begin timing a turn on this thread (replacing any unfinished one)."""
    _local.turn = {"channel": channel, "t0": time.time(), "steps": {}}
    return _local.turn


def current() -> dict | None:
    return getattr(_local, "turn", None)


def ensure(channel: str) -> bool:
    """Start a turn unless one is already running here. True if this call started it."""
    if current() is None:
        start(channel)
        return True
    return False


def mark(step: str, seconds: float, turn: dict | None = None) -> None:
    """Add `seconds` to a step (repeats add up). `turn` lets another thread mark this one."""
    turn = turn or current()
    if turn is not None:
        turn["steps"][step] = round(turn["steps"].get(step, 0.0) + seconds, 3)


def since_start(step: str, turn: dict | None = None) -> None:
    """Record a moment (e.g. first audio) as seconds since the turn began; first one wins."""
    turn = turn or current()
    if turn is not None and step not in turn["steps"]:
        turn["steps"][step] = round(time.time() - turn["t0"], 3)


@contextmanager
def step(name: str):
    began = time.time()
    try:
        yield
    finally:
        mark(name, time.time() - began)


def finish(mem=None, **extra) -> dict | None:
    """End the turn, log it as a 'timing' event, and return it."""
    turn = current()
    _local.turn = None
    if turn is None:
        return None
    out = {"channel": turn["channel"], **turn["steps"], "total": round(time.time() - turn["t0"], 3), **extra}
    if mem is not None:
        try:
            mem.log("timing", json.dumps(out))
        except Exception:
            pass  # timing must never break a reply
    return out


def summary(rows: list[dict]) -> str:
    """Median and p90 per step over finished turns (for `jarvis latency`)."""
    from statistics import median
    if not rows:
        return "No per-step timings yet."
    steps = sorted({k for r in rows for k, v in r.items() if isinstance(v, (int, float))})
    out = [f"per-step timings, {len(rows)} turns (median / p90 seconds):"]
    for s in steps:
        xs = sorted(r[s] for r in rows if isinstance(r.get(s), (int, float)))
        p90 = xs[min(len(xs) - 1, int(round(0.9 * (len(xs) - 1))))]
        out.append(f"- {s:<14} n={len(xs):<4} {median(xs):6.2f} / {p90:6.2f}")
    return "\n".join(out)
