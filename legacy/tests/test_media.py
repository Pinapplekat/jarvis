import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from jarvis import applemusic, config, media, router, spotify
from jarvis.chat import apply_actions, respond
from jarvis.memory import Memory

TRACK = "spotify:track:4uLU6hMCjMI75M1A2tKUQC"
SECRET = "refresh-secret-xyz"


class PlayerAPI:
    """Stands in for Spotify's accounts service and player API: records every request."""
    def __init__(self):
        self.requests, self.tokens, self.expired = [], 0, 0
        self.active, self.volume, self.playing = True, 50, True
        self.status = {}  # path -> (code, error message) to answer instead
        self.results = {"tracks": {"items": [{"name": "Get Lucky", "uri": TRACK, "artists": [{"name": "Daft Punk"}]}]}}

    def __call__(self, request):
        self.requests.append(request)
        path = request.url.path
        if request.url.host == "accounts.spotify.com":
            self.tokens += 1
            return httpx.Response(200, json={"access_token": f"tok{self.tokens}", "expires_in": 3600})
        if self.expired:
            self.expired -= 1
            return httpx.Response(401, json={"error": {"status": 401, "message": "The access token expired"}})
        if path in self.status:
            code, msg = self.status[path]
            return httpx.Response(code, json={"error": {"status": code, "message": msg}})
        if path == "/v1/me/player" and request.method == "GET":
            if not self.active:
                return httpx.Response(204)
            return httpx.Response(200, json={
                "device": {"name": "MacBook", "volume_percent": self.volume}, "is_playing": self.playing,
                "shuffle_state": False, "repeat_state": "off", "progress_ms": 61000,
                "item": {"name": "Get Lucky", "uri": TRACK, "artists": [{"name": "Daft Punk"}]}})
        if path == "/v1/me/player/devices":
            return httpx.Response(200, json={"devices": [{"id": "phone", "name": "Phone"}]})
        if path == "/v1/search":
            return httpx.Response(200, json=self.results)
        if path.startswith("/v1/me/player/") or path == "/v1/me/library":
            if not self.active and path != "/v1/me/player/play":
                return httpx.Response(404, json={"error": {"status": 404, "message": "Player command failed: No active device found"}})
            if path == "/v1/me/player/volume":
                self.volume = int(request.url.params["volume_percent"])
            return httpx.Response(204)
        return httpx.Response(404, json={"error": {"status": 404, "message": "nope"}})

    def commands(self):
        return [(r.method, r.url.path.removeprefix("/v1/me"), dict(r.url.params)) for r in self.requests
                if r.url.host == "api.spotify.com" and not (r.method == "GET" and r.url.path in ("/v1/me/player", "/v1/search"))]


class FakeApps:
    """Stands in for pgrep, the Spotify/Music AppleScript and the media-key script."""
    def __init__(self, open_=(), state="playing", volume=40, track=("Song", "Band")):
        self.open, self.state, self.volume, self.track = list(open_), state, volume, track
        self.calls, self.scripts, self.keys, self.fail = [], [], [], ""

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        if argv[0] == "pgrep":
            return subprocess.CompletedProcess(argv, 0 if argv[-1] in self.open else 1, "", "")
        if argv[:3] == ["osascript", "-l", "JavaScript"]:
            if self.fail:
                return subprocess.CompletedProcess(argv, 1, "", self.fail)
            self.keys.append(argv[-1])
            return subprocess.CompletedProcess(argv, 0, "sent", "")
        if argv[:2] == ["osascript", "-"]:
            script = kw.get("input") or ""
            app = "Music" if 'tell application "Music"' in script else "Spotify"
            op, value = argv[2], argv[3]
            self.scripts.append((app, op, value, script))
            if self.fail:
                return subprocess.CompletedProcess(argv, 1, "", self.fail)
            if op == "volume":
                self.volume = int(value)
            elif op == "pause":
                self.state = "paused"
            elif op == "next":
                self.track = ("Next One", "Band")
            out = applemusic.FS.join([self.state, str(self.volume), *self.track])
            return subprocess.CompletedProcess(argv, 0, out, "")
        pytest.fail(f"unexpected command {argv}")

    def ops(self):
        return [(app, op, value) for app, op, value, _ in self.scripts if op != "status"]


@pytest.fixture
def api(monkeypatch):
    fake = PlayerAPI()
    monkeypatch.setattr(spotify, "_client", lambda: httpx.Client(transport=httpx.MockTransport(fake)))
    monkeypatch.setattr(config, "SPOTIFY_CLIENT_ID", "id")
    monkeypatch.setattr(config, "SPOTIFY_REFRESH_TOKEN", SECRET)
    monkeypatch.setattr(config, "SPOTIFY_CLIENT_SECRET", "")
    monkeypatch.setattr(spotify, "_token", ("", 0.0))
    monkeypatch.setattr(spotify, "_refresh", "")
    monkeypatch.setattr(media, "_unmute_to", None)
    fake.apps = FakeApps()
    monkeypatch.setattr(applemusic.subprocess, "run", fake.apps)
    return fake


@pytest.fixture
def offline(api, monkeypatch):
    """No Spotify keys: only the apps and media keys."""
    monkeypatch.setattr(config, "SPOTIFY_CLIENT_ID", "")
    return api


# ----- Spotify Web API first -----
@pytest.mark.parametrize("op, value, line, command", [
    ("pause", None, "Paused.", ("PUT", "/player/pause", {})),
    ("play", None, "Resumed.", ("PUT", "/player/play", {})),
    ("next", None, "Skipped.", ("POST", "/player/next", {})),
    ("previous", None, "Back one.", ("POST", "/player/previous", {})),
    ("seek", 90, "Jumped to 1:30.", ("PUT", "/player/seek", {"position_ms": "90000"})),
    ("volume", 30, "Volume 30%.", ("PUT", "/player/volume", {"volume_percent": "30"})),
    ("shuffle", "on", "Shuffle on.", ("PUT", "/player/shuffle", {"state": "true"})),
    ("shuffle", "off", "Shuffle off.", ("PUT", "/player/shuffle", {"state": "false"})),
    ("repeat", "track", "Repeating this song.", ("PUT", "/player/repeat", {"state": "track"})),
    ("repeat", None, "Repeat on.", ("PUT", "/player/repeat", {"state": "context"})),
    ("repeat", "off", "Repeat off.", ("PUT", "/player/repeat", {"state": "off"})),
    ("queue", "get lucky", "Queued Get Lucky by Daft Punk.", ("POST", "/player/queue", {"uri": TRACK})),
    ("like", None, "Liked Get Lucky by Daft Punk.", ("PUT", "/library", {"uris": TRACK})),
])
def test_each_op_goes_through_the_spotify_api(api, op, value, line, command):
    assert media.media_control(op, value) == line
    assert api.commands() == [command]
    assert not api.apps.calls  # no AppleScript, no media keys


def test_now_playing_from_spotify(api):
    assert media.media_control("now_playing") == "Playing Get Lucky by Daft Punk on MacBook."
    api.playing = False
    assert media.media_control("now_playing") == "Paused on Get Lucky by Daft Punk on MacBook."
    assert not api.commands()


def test_volume_up_and_down_are_relative_and_clamped(api):
    assert media.media_control("volume_up") == "Volume up to 60%."
    assert media.media_control("volume_down", 25) == "Volume down to 35%."
    assert media.media_control("volume_down", 100) == "Volume down to 0%."
    api.volume = 95
    assert media.media_control("volume_up") == "Volume up to 100%."


def test_mute_then_unmute_restores_the_volume(api):
    api.volume = 42
    assert media.media_control("mute") == "Muted."
    assert api.volume == 0
    assert media.media_control("unmute") == "Volume back to 42%."
    assert api.volume == 42


def test_expired_token_is_refreshed_once(api):
    media.media_control("pause")
    api.expired = 1
    assert media.media_control("next") == "Skipped."
    assert api.tokens == 2


def test_pausing_while_paused_is_said_plainly(api):
    api.status["/v1/me/player/pause"] = (403, "Player command failed: Restriction violated")
    assert media.media_control("pause") == "Already paused."
    assert not api.apps.calls


def test_queue_with_no_match(api):
    api.results = {"tracks": {"items": []}}
    assert media.media_control("queue", "zzzqqq") == "Spotify found nothing to queue for 'zzzqqq'."
    assert not api.commands()


def test_queue_text_only_goes_to_spotify_as_a_parameter(api):
    nasty = 'x" & do shell script "touch /tmp/pwned'
    media.media_control("queue", nasty)
    search = next(r for r in api.requests if r.url.path == "/v1/search")
    assert search.url.params["q"] == nasty and not api.apps.calls


def test_resume_with_no_active_device_uses_the_first_open_one(api):
    api.active = False
    assert media.media_control("play") == "Resumed."
    assert ("PUT", "/player/play", {"device_id": "phone"}) in api.commands()


# ----- then the apps, then media keys -----
def test_no_spotify_device_falls_back_to_the_open_app(api):
    api.active = False
    api.apps.open = ["Music"]
    assert media.media_control("pause") == "Paused in Music."
    assert api.apps.ops() == [("Music", "pause", "")]
    assert ("PUT", "/player/pause", {}) in api.commands()  # Spotify was tried first


def test_premium_missing_falls_back_too(api):
    api.status["/v1/me/player/next"] = (403, "Player command failed: Premium required")
    api.apps.open = ["Spotify"]
    assert media.media_control("next") == "Skipped to Next One by Band."
    assert api.apps.ops() == [("Spotify", "next", "")]


@pytest.mark.parametrize("op, value, line, sent", [
    ("pause", None, "Paused.", ("Spotify", "pause", "")),
    ("play", None, "Resumed.", ("Spotify", "play", "")),
    ("seek", 75, "Jumped to 1:15.", ("Spotify", "seek", "75")),
    ("volume", 20, "Volume 20%.", ("Spotify", "volume", "20")),
    ("volume_up", None, "Volume up to 50%.", ("Spotify", "volume", "50")),
    ("shuffle", "off", "Shuffle off.", ("Spotify", "shuffle", "off")),
    ("repeat", "track", "Repeating this song.", ("Spotify", "repeat", "track")),
])
def test_offline_ops_in_the_spotify_app(offline, op, value, line, sent):
    offline.apps.open = ["Spotify"]
    assert media.media_control(op, value) == line
    assert offline.apps.ops() == [sent]
    assert not offline.requests  # no keys: the API is never called


def test_offline_like_and_now_playing_in_music(offline):
    offline.apps.open = ["Music"]
    assert media.media_control("like") == "Liked Song by Band in Music."
    assert media.media_control("now_playing") == "Playing Song by Band in Music."
    script = next(s for app, op, v, s in offline.apps.scripts if op == "like")
    assert "favorited of current track" in script and 'tell application "Music"' in script


def test_the_playing_app_is_chosen_when_both_are_open(offline):
    offline.apps.open = ["Spotify", "Music"]
    offline.apps.state = "paused"
    assert applemusic.pick_player() == "Spotify"  # neither playing: Spotify first


def test_unsupported_ops_on_a_fallback_say_so(offline):
    offline.apps.open = ["Spotify"]
    assert media.media_control("like") == "The Spotify app can't like songs on its own."
    assert media.media_control("queue", "get lucky") == "The Spotify app can't queue songs on its own."
    assert not offline.apps.ops()


def test_unsupported_fallback_mentions_why_spotify_failed(api):
    api.active = False
    api.apps.open = ["Music"]
    out = media.media_control("queue", "get lucky")
    assert out.startswith("The Music app can't queue songs on its own (Spotify: no Spotify device is open")


def test_nothing_open_presses_the_media_key(offline):
    assert media.media_control("pause") == "Pressed play/pause."
    assert media.media_control("next") == "Pressed next."
    assert media.media_control("previous") == "Pressed previous."
    assert offline.apps.keys == ["16", "17", "18"]


def test_app_failure_falls_to_the_media_key(offline):
    offline.apps.open = ["Music"]
    offline.apps.fail = "Not authorized to send Apple events to Music. (-1743)"
    out = media.media_control("pause")
    assert out.startswith("Couldn't pause: Music: Not authorized") and "media key" in out


def test_nothing_works_is_said_honestly(offline):
    out = media.media_control("volume", 30)
    assert out == "Couldn't set the volume: Spotify and Music aren't open on the Mac."
    assert not offline.apps.keys


def test_app_values_are_arguments_not_script_text(offline):
    offline.apps.open = ["Music"]
    media.media_control("seek", 12)
    app, op, value, script = offline.apps.scripts[-1]
    assert (op, value) == ("seek", "12") and "item 2 of argv" in script


# ----- validation -----
@pytest.mark.parametrize("op, value", [
    ("explode", None), ("seek", None), ("seek", -1), ("seek", "1:30"), ("seek", True), ("volume", 101),
    ("volume", "loud"), ("volume_up", 0), ("shuffle", "maybe"), ("repeat", "forever"), ("queue", ""),
    ("queue", "x" * 201), ("queue", "a\nb"), ("queue", 5), ("pause", "now"), ("like", 3),
])
def test_bad_ops_and_values_are_refused(op, value):
    with pytest.raises(ValueError):
        media.validate(op, value)


def test_values_are_normalised():
    assert media.validate("volume", "40") == ("volume", 40)
    assert media.validate("volume_up", None) == ("volume_up", 10)
    assert media.validate("shuffle", None) == ("shuffle", "on")
    assert media.validate("shuffle", False) == ("shuffle", "off")
    assert media.validate("repeat", "song") == ("repeat", "track")
    assert media.validate("repeat", "on") == ("repeat", "context")
    assert media.validate("queue", "  get lucky ") == ("queue", "get lucky")


# ----- fast-path phrases (live chat and mic) -----
@pytest.mark.parametrize("text, hit", [
    ("pause", ("pause", None)), ("Pause.", ("pause", None)), ("pause the music please", ("pause", None)),
    ("Hey Jarvis, pause", ("pause", None)), ("resume", ("play", None)), ("play", ("play", None)),
    ("skip", ("next", None)), ("skip this song", ("next", None)), ("next track", ("next", None)),
    ("go back", ("previous", None)), ("previous song", ("previous", None)),
    ("volume up", ("volume_up", None)), ("turn it down", ("volume_down", None)), ("louder", ("volume_up", None)),
    ("set the volume to 40", ("volume", 40)), ("volume 25%", ("volume", 25)),
    ("mute", ("mute", None)), ("unmute", ("unmute", None)),
    ("shuffle", ("shuffle", "on")), ("turn off shuffle", ("shuffle", "off")),
    ("repeat this song", ("repeat", "track")), ("repeat", ("repeat", "context")), ("repeat off", ("repeat", "off")),
    ("queue Get Lucky", ("queue", "Get Lucky")), ("add Bohemian Rhapsody to the queue", ("queue", "Bohemian Rhapsody")),
    ("play One More Time next", ("queue", "One More Time")),
    ("like this song", ("like", None)), ("I love this song", ("like", None)),
    ("what's playing?", ("now_playing", None)), ("What’s this song?", ("now_playing", None)),
    ("what am I listening to", ("now_playing", None)),
    ("skip to 1:30", ("seek", 90)), ("jump to 45 seconds", ("seek", 45)),
])
def test_phrases_map_to_ops(text, hit):
    assert router.media_command(text) == hit


@pytest.mark.parametrize("text", [
    "stop", "play some Daft Punk", "pause heartbeat", "what's on my screen?", "set the volume to 400",
    "can you pause the build and check the tests", "queue it", "remind me to skip lunch", "",
])
def test_other_messages_go_to_chat(text):
    assert router.media_command(text) is None


def test_fast_path_answers_without_the_model(api, monkeypatch):
    from jarvis import cli
    monkeypatch.setattr(config, "SIRI", True)
    monkeypatch.setattr(router, "route", lambda *a, **k: pytest.fail("asked the model"))
    mem = Memory(":memory:")
    assert cli._answer(mem, "pause", spoken=True) == "Paused."
    history = [(h["role"], h["text"]) for h in mem.chat_history(2)]
    assert ("assistant", "Paused.") in history and ("user", "(said out loud, transcribed) pause") in history


def test_fast_path_off_with_siri_off(monkeypatch):
    from jarvis import cli
    monkeypatch.setattr(config, "SIRI", False)
    monkeypatch.setattr(cli, "_media_fast", lambda *a, **k: pytest.fail("ran the fast path"))
    monkeypatch.setattr(router, "route", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("to chat")))
    with pytest.raises(RuntimeError, match="to chat"):
        cli._answer(Memory(":memory:"), "pause")


# ----- the chat action -----
def test_media_control_action_runs_and_confirms(api, monkeypatch):
    monkeypatch.setattr(config, "SIRI", True)
    said, mem = [], Memory(":memory:")
    assert apply_actions(mem, [{"type": "media_control", "op": "volume", "value": 35}], ZoneInfo("UTC"), said=said) == []
    assert said == ["Volume 35%."]


def test_media_control_action_is_validated(api, monkeypatch):
    monkeypatch.setattr(config, "SIRI", True)
    mem = Memory(":memory:")
    problems = apply_actions(mem, [{"type": "media_control", "op": "rm -rf"},
                                   {"type": "media_control", "op": "volume", "value": "999"}], ZoneInfo("UTC"))
    assert len(problems) == 2 and "unknown media op" in problems[0]
    assert not api.requests and not api.apps.calls
    monkeypatch.setattr(config, "SIRI", False)
    assert "turned off" in apply_actions(mem, [{"type": "media_control", "op": "pause"}], ZoneInfo("UTC"))[0]


def test_reply_carries_the_one_line_result(api, monkeypatch):
    monkeypatch.setattr(config, "SIRI", True)
    runner = lambda system, prompt, **kw: {"reply": "Right away, sir.", "actions": [
        {"type": "media_control", "op": "next"}]}
    reply = respond(Memory(":memory:"), "skip this one would you, it's awful", datetime.now(ZoneInfo("UTC")), "",
                    runner=runner)
    assert "Right away, sir." in reply and "Skipped." in reply and SECRET not in reply
