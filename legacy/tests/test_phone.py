import re
import threading
from datetime import datetime

import pytest

from jarvis import phone
from jarvis.phone import PhoneLine, allowed_caller, is_goodbye, signature, spoken_reply, valid_signature

BASE = "https://jarvis.example.ts.net"
TOKEN = "secret-token"
OWNER = "+12065550100"


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make_line(answer=lambda text, session: "Nothing due until Sunday, sir.", **kw):
    sent, ended, rendered = [], [], []

    def render(text):
        rendered.append(text)
        return b"RIFF" + text.encode()

    opts = dict(send_text=sent.append, start_call=lambda: "session", end_call=ended.append,
                clock=Clock(), first_wait=2.0, wait=2.0)
    opts.update(kw)
    line = PhoneLine(answer, opts.pop("render", render), BASE, TOKEN, OWNER, **opts)
    return line, sent, ended, rendered


def post(line, path, sid="CA1", sign=True, **params):
    params = {"CallSid": sid, "From": OWNER, "To": "+12065559999", **params}
    sig = signature(BASE + path, params, TOKEN) if sign else "bogus"
    status, ctype, body = line.handle("POST", path, params, sig)
    return status, body.decode()


def plays(line, twiml):
    """The audio bytes each <Play> in the TwiML points at."""
    out = []
    for url in re.findall(r"<Play>([^<]+)</Play>", twiml):
        status, ctype, body = line.handle("GET", url[len(BASE):], {}, None)
        assert status == 200 and ctype == "audio/wav"
        out.append(body[4:].decode())
    return out


def test_signature_matches_twilio_docs_example():
    params = {"CallSid": "CA1234567890ABCDE", "Caller": "+12349013030", "Digits": "1234",
              "From": "+12349013030", "To": "+18005551212"}
    url = "https://mycompany.com/myapp.php?foo=1&bar=2"
    assert signature(url, params, "12345") == "0/KCTR6DLpKmkAf8muzZqo1nDgQ="
    assert valid_signature(url, params, "0/KCTR6DLpKmkAf8muzZqo1nDgQ=", "12345")
    assert not valid_signature(url + "&x=1", params, "0/KCTR6DLpKmkAf8muzZqo1nDgQ=", "12345")
    assert not valid_signature(url, params, None, "12345")
    assert not valid_signature(url, params, "0/KCTR6DLpKmkAf8muzZqo1nDgQ=", "")


def test_unsigned_requests_are_refused():
    line, *_ = make_line()
    status, body = post(line, "/voice/incoming", sign=False)
    assert status == 403
    assert not line.calls


def test_other_callers_are_rejected_without_answering():
    calls = []
    line, *_ = make_line(answer=lambda t, s: calls.append(t) or "hi")
    status, body = post(line, "/voice/incoming", From="+14255550123")
    assert status == 200 and '<Reject reason="rejected"/>' in body
    status, body = post(line, "/voice/heard", From="+14255550123", SpeechResult="what's due")
    assert "<Reject" in body and not calls


def test_forged_caller_id_is_rejected():
    assert allowed_caller({"From": "+1 (206) 555-0100", "StirVerstat": "TN-Validation-Passed-A"}, OWNER)
    assert allowed_caller({"From": OWNER}, OWNER)
    assert not allowed_caller({"From": OWNER, "StirVerstat": "TN-Validation-Failed-B"}, OWNER)
    assert not allowed_caller({"From": OWNER}, "")


def test_owner_is_greeted_and_heard():
    line, sent, ended, rendered = make_line()
    status, body = post(line, "/voice/incoming")
    assert status == 200
    assert f'action="{BASE}/voice/heard"' in body and 'input="speech"' in body
    assert plays(line, body)[0].startswith("Good ")
    assert line.calls["CA1"].session == "session"


def test_question_goes_through_chat_and_reply_is_spoken():
    asked = []
    line, sent, *_ = make_line(answer=lambda text, session: asked.append((text, session)) or "Module 3 is due tonight, sir.")
    post(line, "/voice/incoming")
    status, body = post(line, "/voice/heard", SpeechResult="what's  due tonight")
    assert asked == [("what's due tonight", "session")]
    assert plays(line, body) == ["Module 3 is due tonight, sir."]
    assert 'input="speech"' in body  # listens for the next thing
    assert not sent


def test_speech_before_the_call_was_answered_is_ignored():
    asked = []
    line, *_ = make_line(answer=lambda t, s: asked.append(t) or "hi")
    status, body = post(line, "/voice/heard", SpeechResult="read my email")
    assert "<Hangup/>" in body and not asked


def test_pin_guards_the_call():
    asked = []
    line, *_ = make_line(answer=lambda t, s: asked.append(t) or "hi", pin="4321")
    body = post(line, "/voice/incoming")[1]
    assert 'input="dtmf"' in body and 'numDigits="4"' in body
    assert "sir" not in "".join(plays(line, body))  # nothing gives away whose line it is
    assert "<Hangup/>" in post(line, "/voice/heard", SpeechResult="what's due")[1] and not asked
    assert 'input="dtmf"' in post(line, "/voice/pin", Digits="1111")[1]
    body = post(line, "/voice/pin", Digits="4321")[1]
    assert 'input="speech"' in body
    post(line, "/voice/heard", SpeechResult="what's due")
    assert asked == ["what's due"]


def test_wrong_pin_twice_hangs_up():
    line, _, ended, _ = make_line(pin="4321")
    post(line, "/voice/incoming")
    post(line, "/voice/pin", Digits="0000")
    body = post(line, "/voice/pin", Digits="0000")[1]
    assert "<Hangup/>" in body and "CA1" not in line.calls
    assert ended == []  # no Claude session was ever started for it


def test_slow_answer_holds_then_delivers():
    release = threading.Event()
    line, *_ = make_line(answer=lambda t, s: release.wait(5) and "Done, sir.", first_wait=0.05, wait=0.05)
    post(line, "/voice/incoming")
    body = post(line, "/voice/heard", SpeechResult="plan my week")[1]
    assert plays(line, body) == ["One moment, sir."]
    url = re.search(r"<Redirect method=\"POST\">([^<]+)</Redirect>", body)[1]
    assert url.startswith(f"{BASE}/voice/wait?turn=")
    path = url[len(BASE):]
    assert "<Redirect" in post(line, path)[1]  # still thinking
    release.set()
    line.turns[path.split("=")[1]].done.wait(2)
    assert plays(line, post(line, path)[1]) == ["Done, sir."]


def test_answer_that_takes_too_long_goes_to_telegram():
    release = threading.Event()
    line, sent, *_ = make_line(answer=lambda t, s: release.wait(5) and "Here's the plan, sir.",
                               first_wait=0.01, wait=0.01)
    post(line, "/voice/incoming")
    body = post(line, "/voice/heard", SpeechResult="plan my week")[1]
    turn_id = re.search(r"turn=([\w-]+)", body)[1]
    line.clock.t += phone.TURN_LIMIT + 1
    body = post(line, f"/voice/wait?turn={turn_id}")[1]
    assert "Telegram" in plays(line, body)[0] and 'input="speech"' in body
    release.set()
    for _ in range(100):
        if sent:
            break
        threading.Event().wait(0.02)
    assert sent == ["From our call:\nHere's the plan, sir."]


def test_long_reply_is_cut_and_sent_in_full_to_telegram():
    long = "First, sir, the essay is due Friday. " + "Then a lot more detail about readings. " * 20
    line, sent, *_ = make_line(answer=lambda t, s: long)
    post(line, "/voice/incoming")
    said = plays(line, post(line, "/voice/heard", SpeechResult="what's this week")[1])[0]
    assert said.startswith("First, sir") and said.endswith("The rest is in Telegram, sir.")
    assert len(said) < phone.MAX_SPOKEN + 40
    assert sent == [f"From our call:\n{long}"]


def test_spoken_reply_flags_links_and_cuts():
    assert spoken_reply("All clear, sir.") == ("All clear, sir.", False)
    said, cut = spoken_reply("The agenda is at https://example.com/agenda, sir.")
    assert "the link" in said and cut


def test_silence_reprompts_then_hangs_up():
    line, _, ended, _ = make_line()
    post(line, "/voice/incoming")
    for _ in range(phone.MAX_MISSES - 1):
        body = post(line, "/voice/heard", SpeechResult="")[1]
        assert plays(line, body) == ["Sorry, sir, I didn't catch that."]
    body = post(line, "/voice/heard", SpeechResult="")[1]
    assert "<Hangup/>" in body
    threading.Event().wait(0.05)  # the session is closed in the background
    assert ended == ["session"]


@pytest.mark.parametrize("said", ["Goodbye.", "Thanks, bye!", "That's all, Jarvis.", "okay bye"])
def test_goodbye_hangs_up_without_asking_claude(said):
    asked = []
    line, *_ = make_line(answer=lambda t, s: asked.append(t) or "hi")
    post(line, "/voice/incoming")
    body = post(line, "/voice/heard", SpeechResult=said)[1]
    assert "<Hangup/>" in body and not asked and "CA1" not in line.calls


def test_not_every_bye_is_a_goodbye():
    assert not is_goodbye("say bye to the old logs")
    assert not is_goodbye("what's due before I hang up the laundry")


def test_audio_falls_back_to_twilio_voice_and_text_is_escaped():
    def broken(text):
        raise RuntimeError("no say")
    line, *_ = make_line(answer=lambda t, s: "Q&A is <tomorrow>, sir.", render=broken)
    post(line, "/voice/incoming")
    body = post(line, "/voice/heard", SpeechResult="when's the q and a")[1]
    assert '<Say voice="Polly.Brian">Q&amp;A is &lt;tomorrow&gt;, sir.</Say>' in body


def test_audio_tokens_expire_and_unknown_paths_404():
    line, *_ = make_line()
    url = re.search(r"<Play>([^<]+)</Play>", post(line, "/voice/incoming")[1])[1][len(BASE):]
    assert line.handle("GET", url, {}, None)[0] == 200
    line.clock.t += phone.AUDIO_TTL + 1
    assert line.handle("GET", url, {}, None)[0] == 404
    assert line.handle("GET", "/voice/audio/../../.env", {}, None)[0] == 404
    assert line.handle("POST", "/admin", {}, None)[0] == 404


def test_call_ended_closes_session():
    line, _, ended, _ = make_line()
    post(line, "/voice/incoming")
    post(line, "/voice/status", CallStatus="completed")
    threading.Event().wait(0.05)
    assert ended == ["session"] and not line.calls


def test_idle_calls_are_forgotten():
    line, _, ended, _ = make_line()
    post(line, "/voice/incoming")
    line.clock.t += phone.CALL_IDLE + 1
    line.sweep()
    threading.Event().wait(0.05)
    assert ended == ["session"]


def test_greeting_by_time_of_day():
    assert phone.greeting(datetime(2026, 10, 16, 8)) == "Good morning, sir."
    assert phone.greeting(datetime(2026, 10, 16, 14)) == "Good afternoon, sir."
    assert phone.greeting(datetime(2026, 10, 16, 21)) == "Good evening, sir."


def test_pin_must_be_digits():
    with pytest.raises(ValueError):
        make_line(pin="12ab")
