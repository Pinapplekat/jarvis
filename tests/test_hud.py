import json
import threading
import time

from jarvis import hud


def _ask(seq, text, at=None):
    hud.HUD_DIR.mkdir(parents=True, exist_ok=True)
    hud.REQUEST.write_text(json.dumps({"seq": seq, "text": text, "at": at or time.time()}))


def test_pending_only_new_recent_questions_with_words():
    assert hud.pending(0) is None  # nothing typed yet
    _ask(3, "  what's due?  ")
    assert hud.pending(0) == {"seq": 3, "text": "what's due?"}
    assert hud.pending(3) is None  # already answered
    _ask(4, "   ")
    assert hud.pending(3) is None  # no words
    _ask(5, "hello", at=time.time() - hud.MAX_AGE - 5)
    assert hud.pending(3) is None  # typed while JARVIS was down: not answered out of the blue


def test_answer_streams_then_finishes():
    seen = []

    def answer(text, on_text):
        on_text("Two things,")
        seen.append(json.loads(hud.REPLY.read_text()))
        return "Two things, sir."
    clock = iter([10.0, 10.0, 20.0, 20.0])
    assert hud.answer_one({"seq": 7, "text": "what's due"}, answer, clock=lambda: next(clock)) == "Two things, sir."
    assert seen[0] == {**seen[0], "seq": 7, "text": "Two things,", "state": "thinking"}
    assert json.loads(hud.REPLY.read_text()) == {**json.loads(hud.REPLY.read_text()),
                                                  "seq": 7, "text": "Two things, sir.", "state": "done"}


def test_a_failed_answer_still_finishes():
    def answer(text, on_text):
        raise RuntimeError("model down")
    reply = hud.answer_one({"seq": 1, "text": "hi"}, answer)
    assert "couldn't answer" in reply and json.loads(hud.REPLY.read_text())["state"] == "done"


def test_serve_answers_each_question_once():
    asked = []
    stop = threading.Event()

    def answer(text, on_text):
        asked.append(text)
        if len(asked) == 2:
            stop.set()
        return "ok"
    _ask(1, "first")
    t = threading.Thread(target=hud.serve, args=(answer,), kwargs={"stop": stop, "every": 0.01})
    t.start()
    for _ in range(200):
        if asked:
            break
        time.sleep(0.01)
    _ask(2, "second")
    t.join(5)
    assert asked == ["first", "second"]
