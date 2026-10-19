import json
import subprocess
from datetime import datetime
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

import httpx
import pytest

from jarvis import applemusic, config, siri, spotify
from jarvis.chat import apply_actions, respond
from jarvis.memory import Memory

TRACK = "spotify:track:4uLU6hMCjMI75M1A2tKUQC"
PLAYLIST = "spotify:playlist:37i9dQZF1DWWQRwui0ExPn"
ARTIST = "spotify:artist:4tZwfgrHOc3mvqYlEYSvVi"
SECRET = "refresh-secret-xyz"


def _results(tracks=(), artists=(), playlists=()):
    return {"tracks": {"items": list(tracks)}, "artists": {"items": list(artists)},
            "playlists": {"items": list(playlists)}}


def _track(name, uri=TRACK, artist="Someone"):
    return {"name": name, "uri": uri, "artists": [{"name": artist}]}


class FakeMac:
    """Stands in for osascript and `open`: records every call, never runs anything."""
    def __init__(self, installed=True, playing=(TRACK, "Get Lucky", "Daft Punk"), before=None):
        self.calls, self.installed, self.playing, self.before, self.played = [], installed, playing, before, False
        self.music, self.apple_music = None, False  # what the Music app answers; whether it was asked
        self.search_plays, self.clicks_play, self.ui_error = False, True, ""  # Spotify desktop fallback
        self.catalog, self.catalog_playing = [], None  # catalog search hits; what Music plays after the click

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw.get("input")))
        out, code, err, script = "", 0, "", kw.get("input") or ""
        if argv[:2] == ["open", "-Ra"]:
            code = 0 if self.installed else 1
        elif argv[0] == "osascript" and 'tell application "Music"' in script:
            self.apple_music = True
            if "open location" in script:
                out = applemusic.FS.join(self.catalog_playing) if self.catalog_playing else "stopped"
            else:
                out = applemusic.FS.join(self.music) if self.music else ""
        elif argv[0] == "osascript" and 'tell process "Spotify"' in script:
            if self.ui_error:
                code, err = 1, self.ui_error
            else:
                out, self.played = "clicked", self.played or self.clicks_play
        elif argv[0] == "osascript" and "play track" in script:
            self.played = self.search_plays if argv[-1].startswith("spotify:search:") else True
        elif argv[0] == "osascript":
            now = self.playing if self.played else self.before
            out = spotify.FS.join(now[1:] + now[:1]) if now else ""
        return subprocess.CompletedProcess(argv, code, out, err)

    def spotify_scripts(self):
        return [c for c in self.calls if c[0][0] == "osascript" and "Spotify" in (c[1] or "")]


class FakeSpotify:
    """Stands in for the Spotify accounts service and Web API: records every request."""
    def __init__(self):
        self.requests, self.tokens = [], 0
        self.results = _results(tracks=[_track("Get Lucky", artist="Daft Punk")])
        self.devices = [{"id": "phone", "name": "Phone", "is_active": False},
                        {"id": "mac", "name": "MacBook", "is_active": True}]
        self.play_status, self.play_error = 204, {}
        self.expired = 0  # this many API calls answer 401 before working
        self.token_status, self.token_error, self.rotate = 200, "", ""

    def __call__(self, request):
        self.requests.append(request)
        if request.url.host == "accounts.spotify.com":
            self.tokens += 1
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": self.token_error})
            body = {"access_token": f"tok{self.tokens}", "expires_in": 3600}
            return httpx.Response(200, json=body | ({"refresh_token": self.rotate} if self.rotate else {}))
        if self.expired:
            self.expired -= 1
            return httpx.Response(401, json={"error": {"status": 401, "message": "The access token expired"}})
        if request.url.path == "/v1/search":
            return httpx.Response(200, json=self.results)
        if request.url.path == "/v1/me/player/devices":
            return httpx.Response(200, json={"devices": self.devices})
        if request.url.path == "/v1/me/player/play":
            if self.play_status >= 400:
                return httpx.Response(self.play_status, json={"error": self.play_error})
            return httpx.Response(204)
        return httpx.Response(404, json={"error": {"status": 404, "message": "nope"}})

    def token_forms(self):
        return [(parse_qs(r.content.decode()), r.headers.get("authorization", ""))
                for r in self.requests if r.url.host == "accounts.spotify.com"]

    def plays(self):
        return [(r.url.params["device_id"], json.loads(r.content)) for r in self.requests
                if r.url.path == "/v1/me/player/play"]


@pytest.fixture
def mac(monkeypatch):
    fake = FakeMac()
    fake.api = FakeSpotify()
    monkeypatch.setattr(spotify.subprocess, "run", fake)
    monkeypatch.setattr(spotify, "_client", lambda: httpx.Client(transport=httpx.MockTransport(fake.api)))
    catalog = lambda request: httpx.Response(200, json={"results": fake.catalog})
    monkeypatch.setattr(applemusic, "_client", lambda: httpx.Client(transport=httpx.MockTransport(catalog)))
    monkeypatch.setattr(spotify.time, "sleep", lambda s: None)
    monkeypatch.setattr(spotify, "PLAY_WAIT", 0.05)
    monkeypatch.setattr(config, "SPOTIFY_CLIENT_ID", "id")
    monkeypatch.setattr(config, "SPOTIFY_REFRESH_TOKEN", SECRET)
    monkeypatch.setattr(config, "SPOTIFY_CLIENT_SECRET", "")
    monkeypatch.setattr(config, "SPOTIFY_APP_FALLBACK", False)
    monkeypatch.setattr(spotify, "_token", ("", 0.0))
    monkeypatch.setattr(spotify, "_refresh", "")
    return fake


def test_best_picks_by_kind_and_exact_names():
    r = _results(tracks=[_track("Lo-Fi Beat")], artists=[{"name": "Daft Punk", "uri": ARTIST}],
                 playlists=[None, {"name": "Study Lo-Fi", "uri": PLAYLIST}])
    assert spotify.best(r, "study lo-fi", "playlist") == (PLAYLIST, "the playlist Study Lo-Fi")
    assert spotify.best(r, "daft punk", "any") == (ARTIST, "the artist Daft Punk")
    assert spotify.best(r, "study lo-fi", "any") == (PLAYLIST, "the playlist Study Lo-Fi")
    assert spotify.best(r, "something else", "any") == (TRACK, "Lo-Fi Beat by Someone")
    with pytest.raises(spotify.SpotifyError, match="nothing"):
        spotify.best(_results(), "zzz", "song")


# ----- the Web API -----
def test_search_uses_the_api_with_a_token(mac):
    mac.api.results = _results(playlists=[{"name": "Study Lo-Fi", "uri": PLAYLIST}])
    out = spotify.search("study lo-fi", "playlist")
    assert out["playlists"]["items"][0]["uri"] == PLAYLIST
    seen = mac.api.requests
    assert seen[1].headers["authorization"] == "Bearer tok1"
    assert seen[1].url.params["type"] == "playlist" and seen[1].url.params["q"] == "study lo-fi"


def test_token_refresh_uses_pkce_and_is_cached(mac):
    spotify.search("a")
    spotify.search("b")
    assert mac.api.tokens == 1  # the second search reused the cached token
    form, auth = mac.api.token_forms()[0]
    assert form == {"grant_type": ["refresh_token"], "refresh_token": [SECRET], "client_id": ["id"]}
    assert auth == ""


def test_token_refresh_uses_basic_auth_with_a_secret(mac, monkeypatch):
    monkeypatch.setattr(config, "SPOTIFY_CLIENT_SECRET", "shh")
    spotify.search("a")
    form, auth = mac.api.token_forms()[0]
    assert "client_id" not in form and auth.startswith("Basic ")


def test_token_is_refreshed_shortly_before_it_expires(mac, monkeypatch):
    spotify.search("a")
    monkeypatch.setattr(spotify, "_token", (spotify._token[0], spotify.time.time() + 30))
    spotify.search("b")
    assert mac.api.tokens == 2


def test_rotated_refresh_token_is_used_next_time(mac, monkeypatch):
    mac.api.rotate = "newer-refresh"
    spotify.search("a")
    monkeypatch.setattr(spotify, "_token", ("", 0.0))
    spotify.search("b")
    assert mac.api.token_forms()[1][0]["refresh_token"] == ["newer-refresh"]


def test_expired_token_is_refreshed_and_retried_once(mac):
    spotify.search("a")
    mac.api.expired = 1
    assert spotify.play("get lucky", "song") == "Get Lucky by Daft Punk"
    assert mac.api.tokens == 2 and mac.api.plays() == [("mac", {"uris": [TRACK]})]
    mac.api.expired = 5
    with pytest.raises(spotify.SpotifyError, match="even after refreshing"):
        spotify.search("b")


def test_refused_refresh_token_is_explained_without_leaking_it(mac):
    mac.api.token_status, mac.api.token_error = 400, "invalid_grant"
    with pytest.raises(spotify.SpotifyError, match="refused the refresh token") as e:
        spotify.play("get lucky", "song")
    assert SECRET not in str(e.value) and "tok1" not in str(e.value)


def test_play_starts_the_track_on_the_active_device(mac):
    assert spotify.play("get lucky", "song") == "Get Lucky by Daft Punk"
    assert mac.api.plays() == [("mac", {"uris": [TRACK]})]
    assert not mac.spotify_scripts()  # no app driving at all


def test_play_a_playlist_on_the_first_open_device_when_none_is_active(mac):
    mac.api.results = _results(playlists=[{"name": "Study Lo-Fi", "uri": PLAYLIST}])
    mac.api.devices = [{"id": "tv", "name": "TV", "is_restricted": True},
                       {"id": "phone", "name": "Phone", "is_active": False}]
    assert spotify.play("study lo-fi", "playlist") == "the playlist Study Lo-Fi"
    assert mac.api.plays() == [("phone", {"context_uri": PLAYLIST})]


def test_no_device_open(mac):
    mac.api.devices = []
    with pytest.raises(spotify.SpotifyError, match="no Spotify device is open"):
        spotify.play("get lucky", "song")
    assert not mac.api.plays()
    mac.api.devices = [{"id": "mac", "name": "MacBook", "is_active": True}]
    mac.api.play_status, mac.api.play_error = 404, {"status": 404, "message": "Device not found"}
    with pytest.raises(spotify.SpotifyError, match="no Spotify device is open"):
        spotify.play("get lucky", "song")


def test_premium_required_and_missing_scopes(mac):
    mac.api.play_status = 403
    mac.api.play_error = {"status": 403, "message": "Player command failed: Premium required", "reason": "PREMIUM_REQUIRED"}
    with pytest.raises(spotify.SpotifyError, match="Premium is needed"):
        spotify.play("get lucky", "song")
    mac.api.play_error = {"status": 403, "message": "Insufficient client scope"}
    with pytest.raises(spotify.SpotifyError, match="user-modify-playback-state"):
        spotify.play("get lucky", "song")


def test_no_match(mac, monkeypatch):
    mac.api.results = _results()
    with pytest.raises(spotify.NoMatch, match="found nothing"):
        spotify.play("zzzqqq", "song")
    assert not mac.api.plays() and not mac.played


def test_query_only_goes_to_the_api_as_a_parameter(mac):
    nasty = 'x" \nend tell\ndo shell script "touch /tmp/pwned'
    mac.api.results = _results(tracks=[_track(nasty)])
    spotify.play(nasty, "song")
    search = next(r for r in mac.api.requests if r.url.path == "/v1/search")
    assert search.url.params["q"] == nasty
    assert not [c for c in mac.calls if c[0][0] == "osascript"]


def test_odd_uri_from_the_api_is_refused(mac):
    mac.api.results = _results(tracks=[_track("x", uri='spotify:track:x" & quit')])
    with pytest.raises(spotify.SpotifyError, match="odd link"):
        spotify.play("x", "song")
    assert not mac.api.plays()


def test_missing_keys_never_call_spotify(mac, monkeypatch):
    monkeypatch.setattr(config, "SPOTIFY_REFRESH_TOKEN", "")
    with pytest.raises(spotify.SpotifyError, match="isn't set up"):
        spotify.play("get lucky")
    assert not mac.api.requests and not mac.spotify_scripts()


# ----- the desktop app, only with SPOTIFY_APP_FALLBACK=1 -----
@pytest.fixture
def keyless(mac, monkeypatch):
    monkeypatch.setattr(config, "SPOTIFY_CLIENT_ID", "")
    monkeypatch.setattr(config, "SPOTIFY_APP_FALLBACK", True)
    monkeypatch.setattr(spotify, "search", lambda q, kind: pytest.fail("used the API without keys"))
    return mac


def test_without_keys_the_desktop_app_searches_and_plays(keyless):
    assert spotify.play('lo-fi" & do shell script "rm -rf ~') == "Get Lucky by Daft Punk"
    uri = "spotify:search:lo-fi%22%20%26%20do%20shell%20script%20%22rm%20-rf%20~"
    assert ["open", uri] in [c[0] for c in keyless.calls]  # the query only ever goes in encoded
    assert ["osascript", "-", uri] in [c[0] for c in keyless.calls]
    scripts = [c for c in keyless.calls if c[0][0] == "osascript"]
    assert all("rm -rf" not in (c[1] or "") for c in scripts)
    ui = next(c[1] for c in scripts if 'tell process "Spotify"' in (c[1] or ""))
    assert 'tell process "Music"' not in ui and "if not frontmost then error" in ui  # targets Spotify only


def test_api_failure_tries_the_app_only_when_enabled(mac, monkeypatch):
    mac.api.devices = []
    with pytest.raises(spotify.SpotifyError, match="no Spotify device"):
        spotify.play("get lucky", "song")
    assert not mac.spotify_scripts()
    monkeypatch.setattr(config, "SPOTIFY_APP_FALLBACK", True)
    assert spotify.play("get lucky", "song") == "Get Lucky by Daft Punk"
    assert mac.spotify_scripts()


def test_without_keys_a_search_uri_that_plays_skips_the_clicking(keyless):
    keyless.search_plays = True
    assert spotify.play("study lo-fi") == "Get Lucky by Daft Punk"
    assert not any('tell process "Spotify"' in (c[1] or "") for c in keyless.calls)


def test_without_keys_nothing_starting_is_a_failure(keyless):
    keyless.clicks_play = False
    with pytest.raises(spotify.SpotifyError, match="nothing started"):
        spotify.play("study lo-fi")
    assert any("to pause" in (c[1] or "") for c in keyless.calls)


def test_without_keys_missing_accessibility_is_explained(keyless):
    keyless.ui_error = "System Events got an error: osascript is not allowed assistive access. (-25211)"
    with pytest.raises(spotify.SpotifyError, match="Accessibility permission"):
        spotify.play("study lo-fi")


def test_spotify_app_not_installed(keyless):
    keyless.installed = False
    with pytest.raises(spotify.SpotifyError, match="isn't installed"):
        spotify.play("lo-fi")


def test_keyless_spotify_success_never_touches_apple_music(keyless):
    out = siri.music(Memory(":memory:"), "study lo-fi", "playlist")
    assert out == "Playing Get Lucky by Daft Punk on Spotify."
    assert not keyless.apple_music


def test_keyless_spotify_fail_falls_back_to_the_apple_music_catalog(keyless):
    keyless.clicks_play = False
    keyless.catalog = [{"trackName": "Lofi Study", "artistName": "Chill Beats",
                        "trackViewUrl": "https://music.apple.com/us/album/lofi-study/123?i=456&uo=4"}]
    keyless.catalog_playing = ("Lofi Study", "Chill Beats")
    out = siri.music(Memory(":memory:"), "study lo-fi", "playlist")
    assert out.startswith("Spotify couldn't play it (") and "nothing started" in out
    assert out.endswith("so I played Lofi Study by Chill Beats on Apple Music.")
    opened = next(c for c in keyless.calls if "open location" in (c[1] or ""))
    assert opened[0] == ["osascript", "-", "music://music.apple.com/us/album/lofi-study/123?i=456&uo=4"]


def test_keyless_both_failing_says_why(keyless):
    keyless.ui_error = "osascript is not allowed assistive access. (-25211)"
    with pytest.raises(ValueError) as e:
        siri.music(Memory(":memory:"), "study lo-fi")
    msg = str(e.value)
    assert "on Spotify (" in msg and "Accessibility permission" in msg
    assert "Apple Music (nothing in the Music library or the Apple Music catalog matches 'study lo-fi')" in msg


def test_app_not_starting_pauses_it_before_the_backup(keyless):
    keyless.clicks_play = False
    keyless.music = ("track", "", "Get Lucky", "Daft Punk")
    assert siri.music(Memory(":memory:"), "get lucky", "song").endswith("on Apple Music.")
    scripts = [c[1] or "" for c in keyless.calls if c[0][0] == "osascript"]
    pause = next(i for i, s in enumerate(scripts) if "to pause" in s)
    assert pause < next(i for i, s in enumerate(scripts) if 'tell application "Music"' in s)


# ----- what JARVIS says -----
def test_music_action_reports_the_real_result(mac):
    said, m = [], Memory(":memory:")
    assert apply_actions(m, [{"type": "play_music", "query": "get lucky", "kind": "song"}],
                         ZoneInfo("UTC"), said=said) == []
    assert said == ["Playing Get Lucky by Daft Punk on Spotify."]
    assert not mac.apple_music
    mac.api.devices = []
    problems = apply_actions(m, [{"type": "play_music", "query": "get lucky", "kind": "song"}], ZoneInfo("UTC"))
    assert problems and "couldn't play" in problems[0] and "no Spotify device" in problems[0]
    assert "Apple Music" in problems[0]


def test_reply_says_playing_only_after_it_plays(mac, monkeypatch):
    monkeypatch.setattr(config, "SIRI", True)
    mac.api.results = _results(playlists=[{"name": "Study Lo-Fi", "uri": PLAYLIST}])
    runner = lambda system, prompt, **kw: {"reply": "Putting it on, sir.", "actions": [
        {"type": "play_music", "query": "study lo-fi", "kind": "playlist"}]}
    ask = lambda: respond(Memory(":memory:"), "play some nice study lo-fi", datetime.now(ZoneInfo("UTC")), "",
                          runner=runner)
    reply = ask()
    assert "Playing the playlist Study Lo-Fi on Spotify." in reply
    mac.api.play_status, mac.api.play_error = 403, {"status": 403, "message": "Premium required"}
    reply = ask()
    assert "Playing" not in reply and "couldn't play" in reply and "Premium" in reply
    assert SECRET not in reply


def test_siri_play_music_goes_to_spotify(monkeypatch):
    monkeypatch.setattr(spotify, "play", lambda q, kind: f"{q}/{kind}")
    assert siri.play_music("lo-fi", "playlist") == "lo-fi/playlist"


# ----- Apple Music, only as the backup -----
def test_spotify_success_never_touches_apple_music(mac):
    mac.music = ("track", "", "Other Song", "Other Band")
    assert siri.music(Memory(":memory:"), "get lucky", "song") == "Playing Get Lucky by Daft Punk on Spotify."
    assert not mac.apple_music


def test_no_match_falls_back_to_apple_music(mac):
    mac.api.results = _results()
    mac.music = ("track", "", "Get Lucky", "Daft Punk")
    out = siri.music(Memory(":memory:"), "get lucky", "song")
    assert out == "Spotify had nothing for it, so I played Get Lucky by Daft Punk on Apple Music."
    assert not mac.api.plays()
    music = next(c for c in mac.calls if 'tell application "Music"' in (c[1] or ""))
    assert music[0] == ["osascript", "-", "get lucky"]  # the query is an argument, not script text


def test_spotify_error_falls_back_to_apple_music(mac, monkeypatch):
    def down(q, kind):
        raise spotify.SpotifyError("Spotify search failed (503)")
    monkeypatch.setattr(spotify, "search", down)
    mac.music = ("playlist", "Study Lo-Fi", "Lo-Fi Beat", "Someone")
    out = siri.music(Memory(":memory:"), "study lo-fi", "playlist")
    assert out == ("Spotify couldn't play it (Spotify search failed (503)), so I played the playlist "
                   "Study Lo-Fi, starting with Lo-Fi Beat by Someone on Apple Music.")


def test_no_device_falls_back_to_apple_music(mac):
    mac.api.devices = []
    mac.music = ("track", "", "Get Lucky", "Daft Punk")
    out = siri.music(Memory(":memory:"), "get lucky")
    assert out == (f"Spotify couldn't play it ({spotify.NO_DEVICE}), so I played Get Lucky by Daft Punk "
                   "on Apple Music.")
    assert not mac.spotify_scripts()


def test_missing_keys_fall_back_to_apple_music(mac, monkeypatch):
    monkeypatch.setattr(config, "SPOTIFY_CLIENT_ID", "")
    mac.music = ("track", "", "Get Lucky", "Daft Punk")
    out = siri.music(Memory(":memory:"), "get lucky")
    assert "isn't set up" in out and out.endswith("Get Lucky by Daft Punk on Apple Music.")
    assert not mac.api.requests and not mac.spotify_scripts()


def test_refused_token_falls_back_without_leaking_it(mac):
    mac.api.token_status, mac.api.token_error = 400, "invalid_grant"
    mac.music = ("track", "", "Get Lucky", "Daft Punk")
    out = siri.music(Memory(":memory:"), "get lucky")
    assert "refused the refresh token" in out and out.endswith("on Apple Music.")
    assert SECRET not in out


def test_both_failing_is_reported_cleanly(mac):
    mac.api.results = _results()
    with pytest.raises(ValueError) as e:
        siri.music(Memory(":memory:"), "zzzqqq", "song")
    msg = str(e.value)
    assert "couldn't play 'zzzqqq' on Spotify" in msg and "found nothing" in msg
    assert "Apple Music (nothing in the Music library or the Apple Music catalog matches 'zzzqqq')" in msg


def test_apple_music_query_cant_inject_applescript(mac):
    nasty = 'x" \nend tell\ndo shell script "touch /tmp/pwned'
    mac.music = ("track", "", "x", "y")
    applemusic.play(nasty)
    call = next(c for c in mac.calls if 'tell application "Music"' in (c[1] or ""))
    assert call[0] == ["osascript", "-", nasty] and nasty not in call[1]


def test_apple_music_errors(mac, monkeypatch):
    mac.music = ("stopped",)
    with pytest.raises(applemusic.AppleMusicError, match="didn't start"):
        applemusic.play("lo-fi")
    monkeypatch.setattr(applemusic.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a[0], 1, "", "Not authorized to send Apple events"))
    with pytest.raises(applemusic.AppleMusicError, match="Not authorized"):
        applemusic.play("lo-fi")


def test_apple_music_catalog_refuses_odd_links_and_wrong_songs(mac):
    mac.catalog = [{"trackName": "x", "trackViewUrl": 'https://evil.example/x" & quit'},
                   {"trackName": "Lofi Study", "artistName": "Chill Beats",
                    "trackViewUrl": "https://music.apple.com/us/album/lofi-study/123?i=456"}]
    mac.catalog_playing = ("Old Song", "Old Band")  # the click resumed something else
    with pytest.raises(applemusic.AppleMusicError, match="didn't start it"):
        applemusic.play("study lo-fi")
    opened = [c[0] for c in mac.calls if "open location" in (c[1] or "")]
    assert opened == [["osascript", "-", "music://music.apple.com/us/album/lofi-study/123?i=456"]]
    assert any("to pause" in (c[1] or "") and "Music" in (c[1] or "") for c in mac.calls)


def test_apple_music_library_match_skips_the_catalog(mac, monkeypatch):
    mac.music = ("track", "", "Get Lucky", "Daft Punk")
    monkeypatch.setattr(applemusic, "catalog", lambda q: pytest.fail("searched the catalog"))
    assert applemusic.play("get lucky") == "Get Lucky by Daft Punk"
