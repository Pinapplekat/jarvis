"""Music through Spotify, the primary service (Apple Music is only the backup, see siri.music).

The model only proposes a query and a kind (song, artist, playlist or any). The Spotify Web API does
the rest, logged in to his account with SPOTIFY_CLIENT_ID + SPOTIFY_REFRESH_TOKEN (PKCE refresh; basic
auth instead if SPOTIFY_CLIENT_SECRET is set): search picks the best match and the player API starts
it on his active device (or the first open one). Needs Premium and an open Spotify somewhere; otherwise
it raises SpotifyError and Apple Music takes over. The tokens are never logged or put in messages.

Only with SPOTIFY_APP_FALLBACK=1 is the old desktop-app driving tried as a last resort
(play_via_desktop): the app is asked to play the search URI, otherwise its search page is opened and
System Events clicks the top result's Play button, only ever inside Spotify (needs Accessibility).
"""

import re
import subprocess
import time
from urllib.parse import quote

import httpx

from jarvis import config

TOKEN_URL = "https://accounts.spotify.com/api/token"
SEARCH_URL = "https://api.spotify.com/v1/search"
PLAYER_URL = "https://api.spotify.com/v1/me/player"
DEVICES_URL = PLAYER_URL + "/devices"
PLAY_URL = PLAYER_URL + "/play"
NO_DEVICE = "no Spotify device is open (open Spotify on the Mac or phone first)"
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

_token: tuple[str, float] = ("", 0.0)  # access token, expiry
_refresh = ""  # a newer refresh token Spotify handed back (PKCE rotates them); memory only


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


def configured() -> bool:
    return bool(config.SPOTIFY_CLIENT_ID and config.SPOTIFY_REFRESH_TOKEN)


def _access_token(http: httpx.Client) -> str:
    """A user access token from the refresh token, cached until 60 seconds before it expires."""
    global _token, _refresh
    if _token[0] and _token[1] > time.time() + 60:
        return _token[0]
    if not configured():
        raise SpotifyError("Spotify isn't set up (SPOTIFY_CLIENT_ID and SPOTIFY_REFRESH_TOKEN in .env)")
    data = {"grant_type": "refresh_token", "refresh_token": _refresh or config.SPOTIFY_REFRESH_TOKEN}
    auth = None
    if config.SPOTIFY_CLIENT_SECRET:
        auth = (config.SPOTIFY_CLIENT_ID, config.SPOTIFY_CLIENT_SECRET)
    else:
        data["client_id"] = config.SPOTIFY_CLIENT_ID
    try:
        r = http.post(TOKEN_URL, data=data, auth=auth)
    except httpx.HTTPError as e:
        raise SpotifyError(f"couldn't reach Spotify ({type(e).__name__})")
    if r.status_code == 400 and _json(r).get("error") == "invalid_grant":
        raise SpotifyError("Spotify refused the refresh token (revoked or expired; it needs a new one)")
    if r.status_code != 200:
        raise SpotifyError(f"Spotify sign-in failed (HTTP {r.status_code})")
    body = _json(r)
    if not body.get("access_token"):
        raise SpotifyError("Spotify sign-in gave no access token")
    _token = (str(body["access_token"]), time.time() + int(body.get("expires_in") or 3600))
    _refresh = str(body.get("refresh_token") or _refresh)
    return _token[0]


def _json(r: httpx.Response) -> dict:
    try:
        body = r.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _call(http: httpx.Client, method: str, url: str, **kw) -> httpx.Response:
    """One Web API request; refreshes and retries once on 401, turns errors into a clear SpotifyError."""
    global _token
    for attempt in (1, 2):
        try:
            r = http.request(method, url, headers={"Authorization": f"Bearer {_access_token(http)}"}, **kw)
        except httpx.HTTPError as e:
            raise SpotifyError(f"couldn't reach Spotify ({type(e).__name__})")
        if r.status_code != 401 or attempt == 2:
            break
        _token = ("", 0.0)  # expired or revoked early: get a fresh one
    if r.status_code < 400:
        return r
    err = _json(r).get("error")
    msg = str(err.get("message") or "") if isinstance(err, dict) else ""
    if r.status_code == 401:
        raise SpotifyError("Spotify rejected the access token even after refreshing it")
    if r.status_code == 403 and "scope" in msg.lower():
        raise SpotifyError("the Spotify token lacks the playback scopes (user-modify-playback-state, "
                           "user-read-playback-state)")
    if r.status_code == 403 and (url.startswith(PLAYER_URL) or "premium" in msg.lower()):
        raise SpotifyError("Spotify Premium is needed to start playback")
    if r.status_code == 404 and url.startswith(PLAYER_URL):
        raise SpotifyError(NO_DEVICE)
    if r.status_code == 429:
        raise SpotifyError("Spotify is rate-limiting JARVIS right now")
    raise SpotifyError(f"Spotify API error {r.status_code}" + (f" ({msg[:80]})" if msg else ""))


def search(query: str, kind: str = "any") -> dict:
    """Spotify's search results ({'tracks': {'items': [...]}, ...})."""
    with _client() as http:
        r = _call(http, "GET", SEARCH_URL, params={"q": query, "type": TYPES[kind], "limit": 5,
                                                   "market": config.SPOTIFY_MARKET})
    body = _json(r)
    if not body:
        raise SpotifyError("Spotify search gave an unreadable answer")
    return body


def play_on_active_device(uri: str) -> str:
    """Starts uri on his active Spotify device (or the first open one). Returns the device's name."""
    with _client() as http:
        devices = [d for d in _json(_call(http, "GET", DEVICES_URL)).get("devices") or []
                   if isinstance(d, dict) and d.get("id") and not d.get("is_restricted")]
        if not devices:
            raise SpotifyError(NO_DEVICE)
        device = next((d for d in devices if d.get("is_active")), devices[0])
        body = {"uris": [uri]} if uri.startswith("spotify:track:") else {"context_uri": uri}
        _call(http, "PUT", PLAY_URL, params={"device_id": device["id"]}, json=body)
    return str(device.get("name") or "his Spotify device")


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
    """Finds the best Spotify match and starts it on his active device through the Web API. Returns
    what's playing; raises NoMatch when Spotify has nothing for it and SpotifyError when Spotify is
    unavailable or fails (after the desktop app, only if SPOTIFY_APP_FALLBACK is on)."""
    try:
        if not configured():
            raise SpotifyError("Spotify isn't set up (SPOTIFY_CLIENT_ID and SPOTIFY_REFRESH_TOKEN in .env)")
        uri, what = best(search(query, kind), query, kind)
        if not URI.fullmatch(uri):
            raise SpotifyError(f"Spotify returned an odd link ({uri[:60]!r})")
        play_on_active_device(uri)
        return what
    except NoMatch:
        raise
    except SpotifyError as e:
        if not config.SPOTIFY_APP_FALLBACK:
            raise
        api = str(e)[:80]
    if not installed():
        raise SpotifyError(f"{api}, and the Spotify app isn't installed on this Mac")
    try:
        return play_via_desktop(query)
    except SpotifyError as e:
        raise SpotifyError(f"{e} (Web API: {api})")
