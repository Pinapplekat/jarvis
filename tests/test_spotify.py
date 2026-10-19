import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from jarvis import applemusic, config, siri, spotify
from jarvis.chat import apply_actions, respond
from jarvis.memory import Memory

TRACK = "spotify:track:4uLU6hMCjMI75M1A2tKUQC"
PLAYLIST = "spotify:playlist:37i9dQZF1DWWQRwui0ExPn"
ARTIST = "spotify:artist:4tZwfgrHOc3mvqYlEYSvVi"


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
        self.search_plays, self.clicks_play, self.ui_error = False, True, ""  # keyless Spotify desktop path
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


@pytest.fixture
def mac(monkeypatch):
    fake = FakeMac()
    monkeypatch.setattr(spotify.subprocess, "run", fake)
    catalog = lambda request: httpx.Response(200, json={"results": fake.catalog})
    monkeypatch.setattr(applemusic, "_client", lambda: httpx.Client(transport=httpx.MockTransport(catalog)))
    monkeypatch.setattr(spotify.time, "sleep", lambda s: None)
    monkeypatch.setattr(spotify, "PLAY_WAIT", 0.05)
    monkeypatch.setattr(config, "SPOTIFY_CLIENT_ID", "id")
    monkeypatch.setattr(config, "SPOTIFY_CLIENT_SECRET", "secret")
    monkeypatch.setattr(spotify, "_token", ("", 0.0))
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


def test_search_uses_the_api_with_a_token(mac, monkeypatch):
    seen = []
    def handler(request):
        seen.append(request)
        if request.url.host == "accounts.spotify.com":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        return httpx.Response(200, json=_results(playlists=[{"name": "Study Lo-Fi", "uri": PLAYLIST}]))
    monkeypatch.setattr(spotify, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    out = spotify.search("study lo-fi", "playlist")
    assert out["playlists"]["items"][0]["uri"] == PLAYLIST
    assert seen[1].headers["authorization"] == "Bearer tok"
    assert seen[1].url.params["type"] == "playlist" and seen[1].url.params["q"] == "study lo-fi"


def test_play_success_only_when_spotify_really_plays(mac, monkeypatch):
    monkeypatch.setattr(spotify, "search", lambda q, kind: _results(tracks=[_track("Get Lucky", artist="Daft Punk")]))
    assert spotify.play("get lucky", "song") == "Get Lucky by Daft Punk"
    play = next(c for c in mac.calls if "play track" in (c[1] or ""))
    assert play[0] == ["osascript", "-", TRACK]  # the URI is an argument, not script text


def test_play_failure_when_nothing_starts(mac, monkeypatch):
    mac.playing = None
    monkeypatch.setattr(spotify, "search", lambda q, kind: _results(playlists=[{"name": "Study Lo-Fi", "uri": PLAYLIST}]))
    with pytest.raises(spotify.SpotifyError, match="didn't start"):
        spotify.play("study lo-fi", "playlist")


def test_play_failure_when_something_else_keeps_playing(mac, monkeypatch):
    other = ("spotify:track:0000000000AAAAAAAAAAAA", "Old Song", "Old Band")
    mac.before = mac.playing = other
    monkeypatch.setattr(spotify, "search", lambda q, kind: _results(tracks=[_track("Get Lucky")]))
    with pytest.raises(spotify.SpotifyError, match="didn't start"):
        spotify.play("get lucky", "song")


def test_spotify_not_installed(mac, monkeypatch):
    mac.installed = False
    monkeypatch.setattr(spotify, "search", lambda q, kind: pytest.fail("searched without the app"))
    with pytest.raises(spotify.SpotifyError, match="isn't installed"):
        spotify.play("lo-fi")


def test_no_match(mac, monkeypatch):
    monkeypatch.setattr(spotify, "search", lambda q, kind: _results())
    with pytest.raises(spotify.SpotifyError, match="found nothing"):
        spotify.play("zzzqqq", "song")
    assert not mac.played


@pytest.fixture
def keyless(mac, monkeypatch):
    monkeypatch.setattr(config, "SPOTIFY_CLIENT_ID", "")
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


def test_query_cant_inject_applescript(mac, monkeypatch):
    nasty = 'x" \nend tell\ndo shell script "touch /tmp/pwned'
    monkeypatch.setattr(spotify, "search", lambda q, kind: _results(tracks=[_track(nasty)]))
    spotify.play(nasty, "song")
    scripts = [c for c in mac.calls if c[0][0] == "osascript"]
    assert scripts and all(nasty not in (c[1] or "") and nasty not in c[0] for c in scripts)


def test_odd_uri_from_the_api_is_refused(mac, monkeypatch):
    monkeypatch.setattr(spotify, "search", lambda q, kind: _results(tracks=[_track("x", uri='spotify:track:x" & quit')]))
    with pytest.raises(spotify.SpotifyError, match="odd link"):
        spotify.play("x", "song")
    assert not mac.played


def test_music_action_reports_the_real_result(mac, monkeypatch):
    monkeypatch.setattr(spotify, "search", lambda q, kind: _results(tracks=[_track("Get Lucky", artist="Daft Punk")]))
    said, m = [], Memory(":memory:")
    assert apply_actions(m, [{"type": "play_music", "query": "get lucky", "kind": "song"}],
                         ZoneInfo("UTC"), said=said) == []
    assert said == ["Playing Get Lucky by Daft Punk on Spotify."]
    assert not mac.apple_music
    mac.played, mac.playing = False, None
    problems = apply_actions(m, [{"type": "play_music", "query": "get lucky", "kind": "song"}], ZoneInfo("UTC"))
    assert problems and "couldn't play" in problems[0] and "didn't start" in problems[0]
    assert "Apple Music" in problems[0]


def test_reply_says_playing_only_after_it_plays(mac, monkeypatch):
    monkeypatch.setattr(config, "SIRI", True)
    monkeypatch.setattr(spotify, "search", lambda q, kind: _results(playlists=[{"name": "Study Lo-Fi", "uri": PLAYLIST}]))
    runner = lambda system, prompt, **kw: {"reply": "Putting it on, sir.", "actions": [
        {"type": "play_music", "query": "study lo-fi", "kind": "playlist"}]}
    ask = lambda: respond(Memory(":memory:"), "play some nice study lo-fi", datetime.now(ZoneInfo("UTC")), "",
                          runner=runner)
    reply = ask()
    assert "Playing the playlist Study Lo-Fi, starting with Get Lucky by Daft Punk on Spotify." in reply
    mac.played, mac.playing = False, None
    reply = ask()
    assert "Playing" not in reply and "couldn't play" in reply


def test_siri_play_music_goes_to_spotify(monkeypatch):
    monkeypatch.setattr(spotify, "play", lambda q, kind: f"{q}/{kind}")
    assert siri.play_music("lo-fi", "playlist") == "lo-fi/playlist"


# ----- Apple Music, only as the backup -----
def test_spotify_success_never_touches_apple_music(mac, monkeypatch):
    monkeypatch.setattr(spotify, "search", lambda q, kind: _results(tracks=[_track("Get Lucky", artist="Daft Punk")]))
    mac.music = ("track", "", "Other Song", "Other Band")
    assert siri.music(Memory(":memory:"), "get lucky", "song") == "Playing Get Lucky by Daft Punk on Spotify."
    assert not mac.apple_music


def test_no_match_falls_back_to_apple_music(mac, monkeypatch):
    monkeypatch.setattr(spotify, "search", lambda q, kind: _results())
    mac.music = ("track", "", "Get Lucky", "Daft Punk")
    out = siri.music(Memory(":memory:"), "get lucky", "song")
    assert out == "Spotify had nothing for it, so I played Get Lucky by Daft Punk on Apple Music."
    assert not mac.played
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


def test_spotify_unavailable_falls_back_to_apple_music(mac):
    mac.installed = False
    mac.music = ("track", "", "Get Lucky", "Daft Punk")
    out = siri.music(Memory(":memory:"), "get lucky")
    assert "isn't installed" in out and out.endswith("Get Lucky by Daft Punk on Apple Music.")


def test_spotify_not_starting_pauses_it_before_the_backup(mac, monkeypatch):
    mac.playing = None
    monkeypatch.setattr(spotify, "search", lambda q, kind: _results(tracks=[_track("Get Lucky")]))
    mac.music = ("track", "", "Get Lucky", "Daft Punk")
    assert siri.music(Memory(":memory:"), "get lucky", "song").endswith("on Apple Music.")
    scripts = [c[1] or "" for c in mac.calls if c[0][0] == "osascript"]
    pause = next(i for i, s in enumerate(scripts) if "to pause" in s)
    assert pause < next(i for i, s in enumerate(scripts) if 'tell application "Music"' in s)


def test_both_failing_is_reported_cleanly(mac, monkeypatch):
    monkeypatch.setattr(spotify, "search", lambda q, kind: _results())
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
