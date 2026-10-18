from datetime import datetime

import pytest

from jarvis import config
from jarvis.memory import Memory
import threading
import time

from jarvis.voice import Announcer, SpeakQueue, should_speak, speakable, user_present

NOON = datetime(2026, 10, 15, 12, 0)
NIGHT = datetime(2026, 10, 15, 23, 30)


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    monkeypatch.setattr(config, "VOICE", True)
    monkeypatch.setattr(config, "QUIET_HOURS", "23-7")
    monkeypatch.setattr(config, "VOICE_IDLE_SECONDS", 180)


class FakeMessenger:
    def __init__(self):
        self.sent = []

    def send(self, text, buttons=None):
        self.sent.append(text)


def test_speakable_strips_emoji_links_and_dashes():
    assert speakable("⏰ It's nine, sir.  Econ time 🚨") == "It's nine, sir. Econ time"
    assert speakable("see https://x.test/a?b=1 now") == "see the link now"
    assert speakable("Due:\n- HW 3\n- Quiz") == "Due: HW 3 Quiz"
    long = "Sentence one. " * 60
    out = speakable(long)
    assert len(out) <= 400 and out.endswith(".")


def test_presence_and_quiet_hours(monkeypatch):
    assert user_present(30) and not user_present(600)
    monkeypatch.setattr("jarvis.voice.idle_seconds", lambda: None)  # couldn't read idle time
    assert not user_present()
    assert should_speak(NOON, idle=10)
    assert not should_speak(NIGHT, idle=10)
    assert not should_speak(NOON, idle=900)


def test_announcer_always_texts_and_speaks_only_when_present():
    m, msg, spoken = Memory(":memory:"), FakeMessenger(), []
    at_desk = Announcer(msg, m, now=lambda: NOON, speaker=lambda t: spoken.append(t) or True,
                        presence=lambda now: True)
    away = Announcer(msg, m, now=lambda: NOON, speaker=lambda t: spoken.append(t) or True,
                     presence=lambda now: False)
    at_desk.send("📧 Dr. G: Module 3 extended")
    away.send("🔔 Rent due tomorrow")
    assert msg.sent == ["📧 Dr. G: Module 3 extended", "🔔 Rent due tomorrow"]
    assert spoken == ["📧 Dr. G: Module 3 extended"]
    assert m.recent_events()[0]["content"] == "Dr. G: Module 3 extended"


def test_voice_off_never_speaks(monkeypatch):
    monkeypatch.setattr(config, "VOICE", False)
    assert not should_speak(NOON, idle=1)


class FakeClip:
    def __init__(self, text, log, gate=None):
        self.text, self.log, self.gate, self.stopped = text, log, gate, False

    def wait(self):
        if self.gate:
            self.gate.wait(2)
        self.log.append(("played", self.text, self.stopped))

    def terminate(self):
        self.stopped = True
        if self.gate:
            self.gate.set()


def test_speak_queue_renders_ahead_and_plays_in_order():
    log = []
    q = SpeakQueue(synth=lambda t: log.append(("rendered", t)) or t,
                   play=lambda clip: log.append(("playing", clip)) or FakeClip(clip, log))
    for s in ["One, sir.", "Two 🚀 https://x.test", "Three."]:
        q.say(s)
    assert q.wait(5)
    played = [e[1] for e in log if e[0] == "played"]
    assert played == ["One, sir.", "Two the link", "Three."]
    assert q.first_audio_at is not None
    q.reset()
    assert q.first_audio_at is None


def test_speak_queue_cancel_stops_playback_and_drops_the_rest():
    log, gate = [], threading.Event()
    rendered = []
    q = SpeakQueue(synth=lambda t: rendered.append(t) or t,
                   play=lambda clip: FakeClip(clip, log, gate if clip == "Long one." else None))
    q.say("Long one.")
    for _ in range(100):
        if rendered:
            break
        time.sleep(0.01)
    q.say("Stale two.")
    q.say("Stale three.")
    time.sleep(0.05)
    q.cancel()
    q.say("Fresh.")
    assert q.wait(5)
    played = [(e[1], e[2]) for e in log]
    assert played[0] == ("Long one.", True)  # cut off mid-sentence
    assert ("Fresh.", False) in played
    assert not any(t.startswith("Stale") for t, _ in played)


def test_stop_speaking_cuts_off_say(monkeypatch):
    from jarvis import voice

    class FakeSay:
        def __init__(self, argv, **kw):
            self.argv, self.done = argv, False

        def poll(self):
            return 0 if self.done else None

        def terminate(self):
            self.done = True

    monkeypatch.setattr(voice.subprocess, "Popen", FakeSay)
    assert voice.speak("Three things, sir.")
    proc = voice._speaking
    assert voice.stop_speaking() and proc.done
    assert not voice.stop_speaking()  # nothing left talking


def test_speak_queue_reports_playing_and_cancel_stops_it():
    log, gate = [], threading.Event()
    q = SpeakQueue(synth=lambda t: t, play=lambda clip: FakeClip(clip, log, gate))
    assert not q.playing
    q.say("Long one.")
    for _ in range(100):
        if q.playing:
            break
        time.sleep(0.01)
    assert q.playing
    q.cancel()  # what a barge-in does
    assert q.wait(5) and not q.playing and log == [("played", "Long one.", True)]


def test_speak_queue_survives_a_failed_render():
    log = []

    def synth(t):
        if t == "Bad.":
            raise RuntimeError("say failed")
        return t
    q = SpeakQueue(synth=synth, play=lambda clip: FakeClip(clip, log))
    q.say("Bad.")
    q.say("Good.")
    assert q.wait(5) and [e[1] for e in log] == ["Good."]


def test_desk_mic_can_ask_whats_on_screen_and_hears_text_only(tmp_path, monkeypatch):
    """No send_photo at the desk mic: the look still works, and no picture is taken for Telegram."""
    from jarvis import attachments, screen
    from jarvis.chat import respond
    shot = tmp_path / "vision.jpg"
    monkeypatch.setattr(screen, "capture_for_vision", lambda run=None: shot.write_bytes(b"JPEG") and shot)
    monkeypatch.setattr(attachments, "capture_screen", lambda run=None: pytest.fail("no picture for Telegram"))
    prompts, looks, streamed = [], [], []

    def run(system, prompt, on_text=None):
        prompts.append(prompt)
        if on_text:
            on_text("Having a look, sir.")
        return {"reply": "Having a look, sir.", "actions": [{"type": "look_at_screen"}]}

    def vision(system, prompt, image):
        looks.append(prompt)
        return "Your econ essay, sir; two paragraphs in."
    m = Memory(":memory:")
    reply = respond(m, "(said out loud, transcribed) what's on my screen", NOON, "", runner=run,
                    on_text=streamed.append, vision=vision)
    assert reply == "Your econ essay, sir; two paragraphs in."  # what the desk speaks: just text
    assert "(said out loud" in looks[0] and not shot.exists() and len(prompts) == 1


def test_screenshot_asked_for_by_voice_takes_no_picture(monkeypatch):
    from jarvis import attachments
    from jarvis.chat import respond
    monkeypatch.setattr(attachments, "capture_screen", lambda run=None: pytest.fail("can't be sent from here"))
    rounds = [{"reply": "One moment.", "actions": [{"type": "screenshot"}]},
              {"reply": "I can't send pictures from the desk, sir; ask me what's on it instead.", "actions": []}]
    prompts = []

    def run(system, prompt, on_text=None):
        prompts.append(prompt)
        return rounds[min(len(prompts), 2) - 1]
    reply = respond(Memory(":memory:"), "(said out loud, transcribed) send me a screenshot", NOON, "", runner=run)
    assert "use look_at_screen" in prompts[1] and "couldn't do" not in reply


def test_latency_summary_includes_first_audio():
    from jarvis.cli import latency_summary
    out = latency_summary(["time_to_first_audio=900ms", "time_to_first_audio=1500ms",
                           "2.0s total, first sentence 1.0s, model haiku warm"])
    assert "time to first audio median 1.2s" in out and "target 1s" in out
