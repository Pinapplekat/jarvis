"""Music through the Spotify app (Apple Music is never touched).

The model only proposes a query and a kind (song, artist, playlist or any). Spotify's own search API
(SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET, client-credentials: no login to his account) picks the
best match, and the Spotify app plays its URI through AppleScript, which gets the URI as an argument,
never as script text. Success is only reported once Spotify's player says it's actually playing.
Without the keys, Spotify's search page is opened and the failure is reported as such.
"""

import re
import subprocess
import time
from urllib.parse import quote

import httpx

from jarvis import config

TOKEN_URL = "https://accounts.spotify.com/api/token"
SEARCH_URL = "https://api.spotify.com/v1/search"
TYPES = {"song": "track", "artist": "artist", "playlist": "playlist", "any": "track,artist,playlist"}
URI = re.compile(r"spotify:(track|artist|album|playlist):[A-Za-z0-9]{10,40}")
FS = "\x1f"  # field separator in the player-state answer
PLAY_WAIT = 6.0  # seconds to wait for Spotify to report it's playing

PLAY_SCRIPT = """
on run argv
    set u to item 1 of argv
    if application "Spotify" is not running then
        launch application "Spotify"
        delay 3
    end if
    tell application "Spotify" to play track u
end run
"""

STATE_SCRIPT = """
tell application "Spotify"
    if player state is playing then
        set t to current track
        return (name of t) & (character id 31) & (artist of t) & (character id 31) & (id of t)
    end if
    return ""
end tell
"""

_token: tuple[str, float] = ("", 0.0)


class SpotifyError(RuntimeError):
    pass


def _client() -> httpx.Client:
    return httpx.Client(timeout=10)


def _osascript(script: str, *args: str, timeout: int = 20) -> str:
    r = subprocess.run(["osascript", "-", *args], input=script, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise SpotifyError(r.stderr.strip()[:200] or "AppleScript failed")
    return r.stdout.rstrip("\n")


def installed() -> bool:
    return subprocess.run(["open", "-Ra", "Spotify"], capture_output=True, timeout=10).returncode == 0


def open_search(query: str) -> None:
    subprocess.run(["open", "spotify:search:" + quote(query, safe="")], capture_output=True, timeout=10)


def _access_token(http: httpx.Client) -> str:
    global _token
    if _token[0] and _token[1] > time.time() + 30:
        return _token[0]
    r = http.post(TOKEN_URL, data={"grant_type": "client_credentials"},
                  auth=(config.SPOTIFY_CLIENT_ID, config.SPOTIFY_CLIENT_SECRET))
    r.raise_for_status()
    body = r.json()
    _token = (body["access_token"], time.time() + int(body.get("expires_in", 3600)))
    return _token[0]


def search(query: str, kind: str = "any") -> dict:
    """Spotify's search results ({'tracks': {'items': [...]}, ...})."""
    try:
        with _client() as http:
            r = http.get(SEARCH_URL, params={"q": query, "type": TYPES[kind], "limit": 5,
                                             "market": config.SPOTIFY_MARKET},
                         headers={"Authorization": f"Bearer {_access_token(http)}"})
            r.raise_for_status()
            return r.json()
    except (httpx.HTTPError, KeyError, ValueError) as e:
        raise SpotifyError(f"Spotify search failed ({str(e)[:120]})")


def best(results: dict, query: str, kind: str = "any") -> tuple[str, str]:
    """(uri, what it is) for the best hit. With kind any: an exact artist, song or playlist name wins,
    otherwise the top song, then the top playlist."""
    found = {t: [i for i in (results.get(t + "s") or {}).get("items") or [] if i and i.get("uri")]
             for t in ("track", "artist", "playlist")}
    def label(t, item):
        if t == "track":
            return f"{item['name']} by {', '.join(a['name'] for a in item.get('artists') or []) or 'unknown'}"
        return f"the {t} {item['name']}"
    order = [TYPES[kind]] if kind != "any" else ["artist", "track", "playlist"]
    if kind == "any":
        for t in order:
            for item in found[t]:
                if str(item.get("name", "")).casefold() == query.casefold():
                    return item["uri"], label(t, item)
        order = ["track", "playlist", "artist"]
    for t in order:
        if found[t]:
            return found[t][0]["uri"], label(t, found[t][0])
    raise SpotifyError(f"Spotify found nothing for {query!r}")


def player() -> tuple[str, str] | None:
    """(track uri, 'Song by Artist') if Spotify is playing, else None."""
    try:
        raw = _osascript(STATE_SCRIPT, timeout=10)
    except (SpotifyError, subprocess.TimeoutExpired):
        return None
    if not raw:
        return None
    name, artist, uri = (raw.split(FS) + ["", ""])[:3]
    return uri, f"{name} by {artist}" if artist else name


def started(uri: str, before: tuple[str, str] | None, step: float = 0.5) -> str:
    """'Song by Artist' once Spotify is really playing what was asked (that exact song, or for an
    artist or playlist, something other than what was already on); '' if not within PLAY_WAIT seconds."""
    deadline = time.monotonic() + PLAY_WAIT
    while True:
        now = player()
        if now and (now[0] == uri if uri.startswith("spotify:track:") else now != before):
            return now[1]
        if time.monotonic() >= deadline:
            return ""
        time.sleep(step)


def play(query: str, kind: str = "any") -> str:
    """Finds and plays the best Spotify match. Returns what's playing; raises SpotifyError otherwise."""
    if not installed():
        raise SpotifyError("the Spotify app isn't installed on this Mac")
    if not (config.SPOTIFY_CLIENT_ID and config.SPOTIFY_CLIENT_SECRET):
        open_search(query)
        raise SpotifyError("I opened Spotify's search for it, but I can't pick and start a result without "
                           "SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET set")
    uri, what = best(search(query, kind), query, kind)
    if not URI.fullmatch(uri):
        raise SpotifyError(f"Spotify returned an odd link ({uri[:60]!r})")
    before = player()
    _osascript(PLAY_SCRIPT, uri, timeout=30)
    track = started(uri, before)
    if not track:
        raise SpotifyError(f"Spotify didn't start playing {what}")
    return what if what == track else f"{what}, starting with {track}"
