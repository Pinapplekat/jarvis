import pytest

from jarvis.apps.music import actions as music
from jarvis.apps.music import applemusic, spotify
from jarvis.contract.api import ActionError


class FakeKeychain:
    def __init__(self, token=""):
        self.items = {"refresh_token": token} if token else {}

    def get(self, account):
        return self.items.get(account, "")

    def set(self, account, secret):
        self.items[account] = secret
        return True


class FakeSpotify:
    """Answers the Web API: routes are (method, url prefix) -> (status, body) or a list of them in turn."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, method, url, *, params=None, form=None, body=None, headers=None):
        self.calls.append((method, url, params, form, body, headers))
        for (m, prefix), answer in self.routes.items():
            if m == method and url.startswith(prefix):
                if isinstance(answer, list):
                    return answer.pop(0) if len(answer) > 1 else answer[0]
                return answer
        return 404, {}


TOKEN = {("POST", spotify.TOKEN_URL): (200, {"access_token": "at1", "expires_in": 3600})}
DEVICES = {("GET", spotify.DEVICES_URL): (200, {"devices": [{"id": "d2", "name": "Phone"},
                                                            {"id": "d1", "name": "MacBook", "is_active": True}]})}
SEARCH = {("GET", spotify.SEARCH_URL): (200, {
    "tracks": {"items": [{"uri": "spotify:track:" + "a" * 22, "name": "Redbone", "artists": [{"name": "Childish Gambino"}]}]},
    "artists": {"items": [{"uri": "spotify:artist:" + "b" * 22, "name": "Redbones"}]},
    "playlists": {"items": []}})}


@pytest.fixture
def sp(monkeypatch):
    spotify.forget()
    chain = FakeKeychain("rt1")
    monkeypatch.setattr(spotify, "keychain", chain)
    monkeypatch.setattr(spotify, "sleep", lambda s: None)

    def install(routes):
        fake = FakeSpotify(routes)
        monkeypatch.setattr(spotify, "send", fake)
        return fake

    yield install, chain
    spotify.forget()


@pytest.fixture
def apps(monkeypatch):
    state = {"open": [], "scripts": [], "answers": {}}

    def osa(script, *args, timeout=30, lang=None):
        state["scripts"].append((script, args, lang))
        for key, answer in state["answers"].items():
            if key in script:
                if isinstance(answer, Exception):
                    raise answer
                return answer
        return ""

    monkeypatch.setattr(applemusic, "osascript", osa)
    monkeypatch.setattr(applemusic, "running_players", lambda: list(state["open"]))
    return state


def ctx(make_ctx, client_id="cid"):
    c = make_ctx("music")
    c.secrets = {"SPOTIFY_CLIENT_ID": client_id} if client_id else {}
    return c


def test_the_keychain_item_is_falaks_own():
    assert spotify.KEYCHAIN_SERVICE == "Falak Spotify"


REDBONE = "spotify:track:" + "a" * 22
IDLE = (200, {})
ON_REDBONE = (200, {"device": {"name": "MacBook"}, "is_playing": True,
                    "item": {"name": "Redbone", "artists": [{"name": "Childish Gambino"}], "uri": REDBONE}})


def test_play_picks_the_exact_song_and_starts_it_on_the_active_device(make_ctx, sp, apps):
    install, _ = sp
    fake = install({**TOKEN, **DEVICES, **SEARCH, ("PUT", spotify.PLAY_URL): (204, {}),
                    ("GET", spotify.PLAYER_URL): [IDLE, IDLE, ON_REDBONE]})
    out = music.play(ctx(make_ctx), "redbone")
    assert out.text == "Playing Redbone by Childish Gambino on Spotify."
    put = next(c for c in fake.calls if c[0] == "PUT" and c[1] == spotify.PLAY_URL)
    assert put[2] == {"device_id": "d1"} and put[4] == {"uris": ["spotify:track:" + "a" * 22]}
    assert fake.calls[1][5]["Authorization"] == "Bearer at1"


def test_best_prefers_an_exact_name_then_the_top_song():
    results = SEARCH[("GET", spotify.SEARCH_URL)][1]
    assert spotify.best(results, "Redbones")[1] == "the artist Redbones"
    assert spotify.best(results, "gambino")[1] == "Redbone by Childish Gambino"
    with pytest.raises(spotify.NoMatch):
        spotify.best({}, "x")


def test_a_rotated_refresh_token_goes_to_the_keychain(sp):
    install, chain = sp
    install({("POST", spotify.TOKEN_URL): (200, {"access_token": "at2", "refresh_token": "rt2"})})
    assert spotify.Account("cid")._access_token() == "at2"
    assert chain.items["refresh_token"] == "rt2"


def test_a_401_refreshes_once_and_retries(make_ctx, sp):
    install, _ = sp
    fake = install({**TOKEN, ("POST", spotify.PLAYER_URL + "/next"): [(401, {}), (204, {})]})
    spotify.Account("cid").player("POST", "/next")
    assert [c[0] for c in fake.calls] == ["POST", "POST", "POST", "POST"]  # token, next(401), token, next


@pytest.mark.parametrize("status,body,fragment", [
    (403, {"error": {"message": "Player command failed: Premium required"}}, "Premium"),
    (404, {}, "no Spotify device is open"),
    (429, {}, "rate-limiting"),
])
def test_api_errors_are_plain(sp, status, body, fragment):
    install, _ = sp
    install({**TOKEN, ("POST", spotify.PLAYER_URL + "/next"): (status, body)})
    with pytest.raises(spotify.SpotifyError, match=fragment):
        spotify.Account("cid").player("POST", "/next")


def test_without_spotify_set_up_music_plays_from_the_library(make_ctx, sp, apps):
    sp[1].items.clear()
    apps["answers"]["user playlist"] = "track\x1f\x1fRedbone\x1fChildish Gambino"
    out = music.play(ctx(make_ctx, client_id=""), "Redbone")
    assert out.text == "Playing Redbone by Childish Gambino in Music (Spotify isn't set up)."
    script, args, _ = apps["scripts"][0]
    assert args == ("Redbone",) and "Redbone" not in script  # the query is an argument, never script text


def test_no_library_match_tries_the_catalog_and_checks_what_started(make_ctx, sp, apps, monkeypatch):
    sp[1].items.clear()
    monkeypatch.setattr(applemusic, "fetch_json", lambda url: {"results": [
        {"trackViewUrl": "https://music.apple.com/us/album/redbone/1?i=2", "trackName": "Redbone",
         "artistName": "Childish Gambino"}]})
    apps["answers"]["open location"] = "Something Else\x1fSomeone"
    with pytest.raises(ActionError, match="didn't start it"):
        music.play(ctx(make_ctx, client_id=""), "Redbone")
    assert any("pause" in s for s, _, _ in apps["scripts"])  # whatever started instead is stopped


def test_control_validates_ops_and_values():
    assert music.validate("volume", 30) == ("volume", 30)
    assert music.validate("volume_up") == ("volume_up", 10)
    assert music.validate("repeat", text="song") == ("repeat", "track")
    for op, n, t in (("volume", 101, ""), ("seek", None, ""), ("shuffle", None, "maybe"), ("queue", None, "")):
        with pytest.raises(ActionError):
            music.validate(op, n, t)


def test_volume_math_and_mute_remembers_the_level(make_ctx, sp, apps):
    install, _ = sp
    now = (200, {"device": {"name": "MacBook", "volume_percent": 40}, "item": {"name": "Redbone"}, "is_playing": True})
    fake = install({**TOKEN, ("GET", spotify.PLAYER_URL): now, ("PUT", spotify.PLAYER_URL + "/volume"): (204, {})})
    c = ctx(make_ctx)
    assert music.control(c, "volume_up", 15).text == "Volume up to 55%."
    assert music.control(c, "mute").text == "Muted."
    assert music.control(c, "unmute").text == "Volume back to 40%."
    assert [c[2]["volume_percent"] for c in fake.calls if c[1].endswith("/volume")] == [55, 0, 40]


def test_control_falls_back_to_the_open_app_then_the_media_key(make_ctx, sp, apps):
    sp[1].items.clear()
    apps["open"] = ["Music"]
    apps["answers"]["player state as text"] = "paused\x1f30\x1fRedbone\x1fChildish Gambino"
    assert music.control(ctx(make_ctx), "pause").text == "Paused in Music."
    apps["open"] = []
    assert music.control(ctx(make_ctx), "next").text == "Pressed next."
    assert apps["scripts"][-1][2] == "JavaScript"
    with pytest.raises(ActionError, match="Spotify and Music aren't open"):
        music.control(ctx(make_ctx), "volume", 20)


def test_now_playing_reads_spotify(make_ctx, sp, apps):
    install, _ = sp
    install({**TOKEN, ("GET", spotify.PLAYER_URL): (200, {
        "device": {"name": "MacBook"}, "is_playing": True,
        "item": {"name": "Redbone", "artists": [{"name": "Childish Gambino"}], "uri": "spotify:track:" + "a" * 22}})})
    assert music.now_playing(ctx(make_ctx)).text == "Playing Redbone by Childish Gambino on MacBook."


def test_login_checks_state_and_keeps_the_new_token(sp):
    install, chain = sp
    chain.items.clear()
    install({("POST", spotify.TOKEN_URL): (200, {"access_token": "at", "refresh_token": "fresh"})})
    acct = spotify.Account("cid")
    url = acct.login_url("v" * 50, "s1", "http://127.0.0.1:8888/callback")
    assert "code_challenge_method=S256" in url and "client_id=cid" in url
    with pytest.raises(spotify.SpotifyError, match="state doesn't match"):
        acct.finish_login("http://127.0.0.1:8888/callback?code=c&state=other", "v" * 50, "s1", "x")
    assert acct.finish_login("http://127.0.0.1:8888/callback?code=c&state=s1", "v" * 50, "s1", "x")
    assert chain.items["refresh_token"] == "fresh"


def test_unplayable_versions_are_skipped():
    results = {"tracks": {"items": [
        {"uri": "spotify:track:" + "x" * 22, "name": "Redbone", "is_playable": False, "artists": [{"name": "Cover"}]},
        {"uri": REDBONE, "name": "Redbone", "is_playable": True, "artists": [{"name": "Childish Gambino"}]}]}}
    assert spotify.best(results, "Redbone", "song") == (REDBONE, "Redbone by Childish Gambino")


def test_a_song_that_never_starts_is_not_reported_and_music_takes_over(make_ctx, sp, apps):
    install, _ = sp
    fake = install({**TOKEN, **DEVICES, **SEARCH, ("PUT", spotify.PLAY_URL): (204, {}),
                    ("PUT", spotify.PLAYER_URL + "/pause"): (204, {}), ("GET", spotify.PLAYER_URL): IDLE})
    apps["answers"]["user playlist"] = "track\x1f\x1fRedbone\x1fChildish Gambino"
    out = music.play(ctx(make_ctx), "redbone")
    assert out.text == ("Playing Redbone by Childish Gambino in Music (Spotify: Spotify took the command for "
                        "Redbone by Childish Gambino but nothing started playing).")
    assert any(c[1].endswith("/pause") for c in fake.calls)  # a late start can't play over Music


def test_a_regional_copy_of_the_song_counts_by_name(make_ctx, sp, apps):
    install, _ = sp
    swapped = (200, {"device": {"name": "MacBook"}, "is_playing": True,
                     "item": {"name": "Redbone", "artists": [{"name": "Childish Gambino"}], "uri": "spotify:track:" + "c" * 22}})
    install({**TOKEN, **DEVICES, **SEARCH, ("PUT", spotify.PLAY_URL): (204, {}),
             ("GET", spotify.PLAYER_URL): [IDLE, swapped]})
    assert music.play(ctx(make_ctx), "redbone").text == "Playing Redbone by Childish Gambino on Spotify."


def test_an_artist_counts_once_something_new_plays(make_ctx, sp, apps):
    install, _ = sp
    install({**TOKEN, **DEVICES, **SEARCH, ("PUT", spotify.PLAY_URL): (204, {}),
             ("GET", spotify.PLAYER_URL): [IDLE, ON_REDBONE]})
    out = music.play(ctx(make_ctx), "Redbones")
    assert out.text == "Playing the artist Redbones, starting with Redbone by Childish Gambino on Spotify."
