import time

from jarvis.chat import VISION_SYSTEM, EarlyPeek, WarmVision
from jarvis.memory import Memory
from jarvis.warm import SessionDied


def fake_capture(tmp_path):
    def capture():
        p = tmp_path / "shot.jpg"
        p.write_bytes(b"\xff\xd8jpeg")
        return p
    return capture


def peeks(m):
    return [e["content"] for e in m.recent_events() if e["kind"] == "screen_peek"]


def test_early_peek_captures_then_looks_and_deletes_the_picture(tmp_path):
    m = Memory(":memory:")
    seen_paths = []
    early = EarlyPeek(idle=lambda: 3, locked=lambda: False, capture=fake_capture(tmp_path))
    wait = early.look("(said out loud, transcribed) what's this error",
                      (True, "about the screen"), vision=lambda s, p, img: seen_paths.append(img) or "a TypeError")
    assert wait(m) == "a TypeError"
    assert not seen_paths[0].exists()  # deleted right after the look
    assert peeks(m) == ["looked privately (not sent; picture deleted): about the screen"]


def test_early_peek_respects_idle_and_lock(tmp_path):
    m = Memory(":memory:")
    calls = []
    for idle, locked, why in ((900, False, "idle"), (3, True, "locked")):
        early = EarlyPeek(idle=lambda i=idle: i, locked=lambda lk=locked: lk, capture=fake_capture(tmp_path))
        assert early.look("x", (True, "r"), vision=lambda *a: calls.append(a) or "no")(m) is None
    assert calls == [] and all(p.startswith("skipped") for p in peeks(m))


def test_discard_deletes_an_unused_capture(tmp_path):
    early = EarlyPeek(idle=lambda: 1, locked=lambda: False, capture=fake_capture(tmp_path))
    early.discard()
    for _ in range(250):  # the capture and the delete both run in the background
        if early._captured.is_set() and not (tmp_path / "shot.jpg").exists():
            break
        time.sleep(0.02)
    assert not (tmp_path / "shot.jpg").exists()


class FakeSession:
    started = 0

    def __init__(self, die=False):
        FakeSession.started += 1
        self.turns, self.die, self.closed = 0, die, False

    def alive(self):
        return not self.closed

    def ask(self, blocks):
        if self.die:
            raise SessionDied("gone")
        self.turns += 1
        assert blocks[0]["type"] == "image"
        return {"answer": f"look {self.turns}"}

    def close(self):
        self.closed = True


def test_warm_vision_reuses_one_session_then_retires_it(tmp_path):
    img = tmp_path / "a.jpg"
    img.write_bytes(b"x")
    FakeSession.started = 0
    v = WarmVision(model="haiku", start=FakeSession, fallback=lambda *a: "cold")
    answers = [v(VISION_SYSTEM, "q", img) for _ in range(5)]
    assert answers == ["look 1", "look 2", "look 3", "look 4", "look 1"]
    assert FakeSession.started == 2  # four looks per session, then a fresh one
    v.close()


def test_warm_vision_falls_back_when_the_session_dies(tmp_path):
    img = tmp_path / "a.jpg"
    img.write_bytes(b"x")
    v = WarmVision(model="haiku", start=lambda: FakeSession(die=True), fallback=lambda *a: "cold answer")
    assert v(VISION_SYSTEM, "q", img) == "cold answer"
    v.close()
