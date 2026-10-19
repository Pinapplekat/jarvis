"""Music through the Spotify app, the primary service (Apple Music is only the backup, see siri.music).

The model only proposes a query and a kind (song, artist, playlist or any). Spotify's own search API
(SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET, client-credentials: no login to his account) picks the
best match, and the Spotify app plays its URI through AppleScript, which gets the URI as an argument,
never as script text. Success is only reported once Spotify's player says it's actually playing.
Without the keys, the desktop app does the search (play_via_desktop): it's asked to play the search
URI (some versions start the top result), otherwise its search page is opened and System Events
clicks the top result's Play button, only ever inside Spotify (needs Accessibility permission).
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

# Run with Spotify's search page open: clicks the first "Play <name>" button (the top result's; the
# player bar's own button is just "Play"). Keys and clicks only go to Spotify, and only while it's in front.
SEARCH_PLAY_SCRIPT = """
tell application "Spotify" to activate
delay 3
tell application "System Events"
    tell process "Spotify"
        set frontmost to true
        delay 0.5
        if not frontmost then error "Spotify isn't in front, so I didn't click anything"
        try
            set value of attribute "AXManualAccessibility" to true
        end try
        repeat with e in (entire contents of window 1)
            set d to ""
            try
                if class of e is button then set d to (description of e) as text
            end try
            considering case
                set hit to d starts with "Play "
            end considering
            if hit then
                if not frontmost then error "Spotify lost focus, so I didn't click anything"
                click e
                return "clicked"
            end if
        end repeat
    end tell
end tell
error "couldn't find a Play button on Spotify's search page"
"""

_token: tuple[str, float] = ("", 0.0)


class SpotifyError(RuntimeError):
    """Spotify is unavailable or failed (not installed, no keys, API or app error)."""


class NoMatch(SpotifyError):
    """Spotify works but found nothing for the query."""


def _client() -> httpx.Client:
    return httpx.Client(timeout=10)


def explain(stderr: str) -> str:
    """An AppleScript error, plus which macOS permission is missing when that's the cause."""
    why = stderr.strip()[:200] or "AppleScript failed"
    if "assistive access" in why or "-25211" in why or "-1719" in why:
        return "System Events needs Accessibility permission (Privacy & Security → Accessibility)"
    if "Not authorized to send Apple events" in why or "-1743" in why:
        return "Not authorized to send Apple events (Privacy & Security → Automation)"
    return why


def _osascript(script: str, *args: str, timeout: int = 20) -> str:
    r = subprocess.run(["osascript", "-", *args], input=script, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise SpotifyError(explain(r.stderr))
    return r.stdout.rstrip("\n")


def _pause() -> None:
    """So a late start can't play over the Apple Music backup."""
    try:
        _osascript('tell application "Spotify" to pause', timeout=10)
    except (SpotifyError, subprocess.TimeoutExpired):
        pass


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
    raise NoMatch(f"Spotify found nothing for {query!r}")


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


def started(uri: str, before: tuple[str, str] | None, step: float = 0.5, wait: float | None = None) -> str:
    """'Song by Artist' once Spotify is really playing what was asked (that exact song, or for an
    artist, playlist or search, something other than what was already on); '' if not within PLAY_WAIT seconds."""
    deadline = time.monotonic() + (PLAY_WAIT if wait is None else wait)
    while True:
        now = player()
        if now and (now[0] == uri if uri.startswith("spotify:track:") else now != before):
            return now[1]
        if time.monotonic() >= deadline:
            return ""
        time.sleep(step)


def play_via_desktop(query: str) -> str:
    """No API keys: the Spotify app searches and starts its top result. Returns what's playing;
    raises SpotifyError (after pausing Spotify) when nothing new starts."""
    before = player()
    uri = "spotify:search:" + quote(query, safe="")
    try:
        _osascript(PLAY_SCRIPT, uri, timeout=30)
        track = started(uri, before, wait=PLAY_WAIT / 2)
    except (SpotifyError, subprocess.TimeoutExpired):
        track = ""
    if track:
        return track
    open_search(query)
    try:
        _osascript(SEARCH_PLAY_SCRIPT, timeout=60)
    except subprocess.TimeoutExpired:
        _pause()
        raise SpotifyError("Spotify's search page didn't answer in time")
    except SpotifyError as e:
        _pause()
        raise SpotifyError(f"couldn't start a search result: {e}")
    track = started(uri, before)
    if not track:
        _pause()
        raise SpotifyError(f"Spotify's search for {query!r} opened but nothing started playing")
    return track


def play(query: str, kind: str = "any") -> str:
    """Finds and plays the best Spotify match. Returns what's playing; raises NoMatch when Spotify has
    nothing for it and SpotifyError when Spotify is unavailable or fails."""
    if not installed():
        raise SpotifyError("the Spotify app isn't installed on this Mac")
    if not (config.SPOTIFY_CLIENT_ID and config.SPOTIFY_CLIENT_SECRET):
        return play_via_desktop(query)
    uri, what = best(search(query, kind), query, kind)
    if not URI.fullmatch(uri):
        raise SpotifyError(f"Spotify returned an odd link ({uri[:60]!r})")
    before = player()
    _osascript(PLAY_SCRIPT, uri, timeout=30)
    track = started(uri, before)
    if not track:
        _pause()
        raise SpotifyError(f"Spotify didn't start playing {what}")
    return what if what == track else f"{what}, starting with {track}"
