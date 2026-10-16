import shutil
from datetime import datetime, timedelta, timezone

import pytest

from jarvis.approvals import handle_updates, notify_pending
from jarvis.memory import Memory
from jarvis.telegram import Update

ME, STRANGER = 111, 999


class FakeTelegram:
    def __init__(self):
        self.sent, self.answers, self.edits, self.inbox, self.downloads = [], [], [], [], []

    def send(self, text, buttons=None):
        self.sent.append(text)

    def answer(self, cid, text):
        self.answers.append(text)

    def edit(self, mid, text):
        self.edits.append(text)

    def action(self, kind):
        pass

    def download(self, file_id):
        self.downloads.append(file_id)
        return b"ogg-bytes:" + file_id.encode()

    def updates(self, offset, wait=0):
        return [u for u in self.inbox if offset is None or u.update_id >= offset]


def voice(uid, file_id, sender=ME):
    return Update(uid, sender, datetime.now(timezone.utc) + timedelta(seconds=1), voice_file_id=file_id)


def test_voice_notes_are_transcribed_then_handled_like_text():
    m, t = Memory(":memory:"), FakeTelegram()
    aid = m.queue_action("send_email", {"from": "a@x", "to": ["b@x"], "subject": "s", "body": "b"})
    notify_pending(m, t)
    t.inbox = [voice(1, "approve-note"), voice(2, "chat-note"), voice(3, "stranger-note", STRANGER)]
    said = {"ogg-bytes:approve-note": f"yes {aid}", "ogg-bytes:chat-note": "what's due this week"}
    chats = []
    changed = handle_updates(m, t, ME, on_text=lambda u: chats.append((u.text, u.via_voice)),
                             transcribe=lambda audio: said[audio.decode()])
    assert changed == 1 and m.get_action(aid)["status"] == "approved"
    assert chats == [("what's due this week", True)]
    assert t.downloads == ["approve-note", "chat-note"]  # the stranger's note is never fetched


def test_unclear_voice_note_asks_again_and_no_transcriber_skips():
    m, t = Memory(":memory:"), FakeTelegram()
    t.inbox = [voice(1, "mumble")]
    handle_updates(m, t, ME, on_text=lambda u: None, transcribe=lambda audio: "")
    assert "Didn't catch that" in t.sent[-1]

    m2, t2 = Memory(":memory:"), FakeTelegram()
    t2.inbox = [voice(1, "x")]
    handle_updates(m2, t2, ME, on_text=lambda u: pytest.fail("should not run"))
    assert t2.downloads == []


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("say"), reason="needs macOS say + ffmpeg")
def test_voice_note_round_trip():
    from jarvis.speech import SAMPLE_RATE, decode, voice_note
    ogg = voice_note("⏰ It's nine, sir. https://x.test")
    assert ogg[:4] == b"OggS"
    samples = decode(ogg)
    assert samples.dtype.name == "float32" and samples.size > SAMPLE_RATE  # over a second of speech


def test_sentence_buffer_splits_streaming_text():
    from jarvis.speech import SentenceBuffer
    buf, got = SentenceBuffer(), []
    for delta in ["Two th", "ings, sir.", " Dr. G moved", " it to 5 p.m. Friday", ". It's 3.5 hours", " away!", " And"]:
        got += buf.feed(delta)
    assert got == ["Two things, sir.", "Dr. G moved it to 5 p.m. Friday.", "It's 3.5 hours away!"]
    assert buf.flush() == ["And"] and buf.flush() == []


def test_sentence_buffer_newlines_initials_and_whole_text():
    from jarvis.speech import SentenceBuffer, sentences
    assert sentences("Due:\n- HW 3\n- Quiz") == ["Due:", "- HW 3", "- Quiz"]
    assert sentences("Ask J. Smith, e.g. tomorrow. Done.") == ["Ask J. Smith, e.g. tomorrow.", "Done."]
    assert sentences("Really?! Yes.") == ["Really?!", "Yes."]
    assert sentences("") == []
    buf = SentenceBuffer()
    assert buf.feed_text("One. Tw") == ["One."]
    assert buf.feed_text("One. Two. Th") == ["Two."]
    assert buf.feed_text("Different now.") == []  # stream rewrote itself: ignored
