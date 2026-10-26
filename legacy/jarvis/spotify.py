"""Music through Spotify, the primary service (Apple Music is only the backup, see siri.music).

The model only proposes a query and a kind (song, artist, playlist or any). The Spotify Web API does
the rest, logged in to his account with SPOTIFY_CLIENT_ID + SPOTIFY_REFRESH_TOKEN (PKCE refresh; basic
auth instead if SPOTIFY_CLIENT_SECRET is set): search picks the best match and the player API starts
it on his active device (or the first open one). Needs Premium and an open Spotify somewhere; otherwise
it raises SpotifyError and Apple Music takes over. The tokens are never logged or put in messages.

The refresh token lives in the macOS Keychain (service "JARVIS Spotify"; the hour-long access token
only in memory):
`jarvis media login` signs in with PKCE and stores them there, a refresh token in .env is copied in once,
and one Spotify rotates replaces the stored copy. Playback control (pause, skip, volume, queue, like,
what's playing; see media.py) uses the same token and falls back to the apps when there's no device.

Only with SPOTIFY_APP_FALLBACK=1 is the old desktop-app driving tried as a last resort
(play_via_desktop): the app is asked to play the search URI, otherwise its search page is opened and
System Events clicks the top result's Play button, only ever inside Spotify (needs Accessibility).
"""

import base64
import hashlib
import re
import secrets
import subprocess
import time
from urllib.parse import parse_qs, quote, urlencode, urlparse

import httpx

from jarvis import config

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
SEARCH_URL = "https://api.spotify.com/v1/search"
PLAYER_URL = "https://api.spotify.com/v1/me/player"
LIBRARY_URL = "https://api.spotify.com/v1/me/library"
DEVICES_URL = PLAYER_URL + "/devices"
PLAY_URL = PLAYER_URL + "/play"
SCOPES = "user-modify-playback-state user-read-playback-state user-library-modify"
NO_DEVICE = "no Spotify device is open (open Spotify on the Mac or phone first)"
KEYCHAIN_SERVICE = "JARVIS Spotify"
TOKEN_CHARS = re.compile(r"[A-Za-z0-9._~+/=-]{1,2000}")  # what a stored token may contain
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
_refresh = ""  # the refresh token in use (from the Keychain, .env, or a newer one Spotify handed back)


class SpotifyError(RuntimeError):
    """Spotify is unavailable or failed (not installed, no keys, API or app error)."""


class NoMatch(SpotifyError):
    """Spotify works but found nothing for the query."""


class Restricted(SpotifyError):
    """Spotify refused a player command for this moment (pausing while paused, skipping an ad)."""


class Keychain:
    """The tokens in the login Keychain through the `security` CLI. Secrets go in on stdin (never in
    argv, where `ps` could see them) and are never logged; anything that doesn't look like a token
    isn't stored. Failures are quiet: JARVIS then keeps the token in memory only."""

    def get(self, account: str) -> str:
        try:
            r = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", account, "-w"],
                               capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return ""
        out = (r.stdout or "").strip()
        return out if r.returncode == 0 and TOKEN_CHARS.fullmatch(out) else ""

    def set(self, account: str, secret: str) -> bool:
        if not TOKEN_CHARS.fullmatch(secret) or not re.fullmatch(r"[a-z_]+", account):
            return False
        line = f'add-generic-password -U -s "{KEYCHAIN_SERVICE}" -a {account} -w "{secret}"\n'
        try:
            r = subprocess.run(["security", "-i"], input=line, capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return r.returncode == 0 and "error" not in (r.stderr or "").lower()


keychain = Keychain()  # tests swap in a fake so they never touch the real Keychain


def _stored_refresh() -> str:
    """The refresh token: the Keychain's, else .env's (copied into the Keychain once)."""
    stored = keychain.get("refresh_token")
    if stored:
        return stored
    if config.SPOTIFY_REFRESH_TOKEN:
        keychain.set("refresh_token", config.SPOTIFY_REFRESH_TOKEN)
    return config.SPOTIFY_REFRESH_TOKEN


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
    global _refresh
    if not config.SPOTIFY_CLIENT_ID:
        return False
    _refresh = _refresh or _stored_refresh()
    return bool(_refresh)


def _keep(body: dict) -> str:
    """Caches the access token from a token-endpoint answer; a new refresh token goes to the Keychain."""
    global _token, _refresh
    if not body.get("access_token"):
        raise SpotifyError("Spotify sign-in gave no access token")
    _token = (str(body["access_token"]), time.time() + int(body.get("expires_in") or 3600))
    if body.get("refresh_token") and body["refresh_token"] != _refresh:
        _refresh = str(body["refresh_token"])
        keychain.set("refresh_token", _refresh)
    return _token[0]


def _access_token(http: httpx.Client) -> str:
    """A user access token from the refresh token, cached until 60 seconds before it expires."""
    if _token[0] and _token[1] > time.time() + 60:
        return _token[0]
    if not configured():
        raise SpotifyError("Spotify isn't set up (SPOTIFY_CLIENT_ID in .env, then jarvis media login)")
    data = {"grant_type": "refresh_token", "refresh_token": _refresh}
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
    return _keep(_json(r))


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
                           "user-read-playback-state, user-library-modify; run jarvis media login)")
    if r.status_code == 403 and "restriction" in msg.lower():
        raise Restricted("Spotify won't do that right now")
    if r.status_code == 403 and (url.startswith(PLAYER_URL) or "premium" in msg.lower()):
        raise SpotifyError("Spotify Premium is needed to control playback")
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
            raise SpotifyError("Spotify isn't set up (SPOTIFY_CLIENT_ID in .env, then jarvis media login)")
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


# ----- playback control (media.py words the answers and falls back to the apps) -----
def _player(method: str, path: str, **params) -> httpx.Response:
    """One player command on the active device. No device raises SpotifyError(NO_DEVICE)."""
    with _client() as http:
        return _call(http, method, PLAYER_URL + path, params=params or None)


def _describe(item: dict | None) -> str:
    if not isinstance(item, dict) or not item.get("name"):
        return ""
    by = ", ".join(str(a.get("name")) for a in item.get("artists") or [] if isinstance(a, dict) and a.get("name"))
    by = by or str((item.get("show") or {}).get("name") or "")
    return f"{item['name']} by {by}" if by else str(item["name"])


def now_playing() -> dict | None:
    """What his active device is doing: track ('Song by Artist'), uri, playing, volume (0-100 or None),
    device, shuffle, repeat, progress (seconds). None if no device is active."""
    with _client() as http:
        r = _call(http, "GET", PLAYER_URL, params={"market": config.SPOTIFY_MARKET, "additional_types": "episode"})
    body = _json(r) if r.status_code == 200 else {}
    if not body.get("device"):
        return None
    item, device = body.get("item") or {}, body.get("device") or {}
    volume = device.get("volume_percent")
    return {"track": _describe(item), "uri": str(item.get("uri") or ""), "playing": bool(body.get("is_playing")),
            "volume": volume if isinstance(volume, int) else None, "device": str(device.get("name") or ""),
            "shuffle": bool(body.get("shuffle_state")), "repeat": str(body.get("repeat_state") or "off"),
            "progress": int(body.get("progress_ms") or 0) // 1000}


def pause() -> None:
    _player("PUT", "/pause")


def resume() -> None:
    """Resumes the active device, or the first open one if none is active."""
    if now_playing() is not None:
        _player("PUT", "/play")
        return
    with _client() as http:
        devices = [d for d in _json(_call(http, "GET", DEVICES_URL)).get("devices") or []
                   if isinstance(d, dict) and d.get("id") and not d.get("is_restricted")]
        if not devices:
            raise SpotifyError(NO_DEVICE)
        _call(http, "PUT", PLAY_URL, params={"device_id": devices[0]["id"]})


def next_track() -> None:
    _player("POST", "/next")


def previous_track() -> None:
    _player("POST", "/previous")


def seek(seconds: int) -> None:
    _player("PUT", "/seek", position_ms=int(seconds) * 1000)


def set_volume(percent: int) -> None:
    _player("PUT", "/volume", volume_percent=max(0, min(100, int(percent))))


def shuffle(on: bool) -> None:
    _player("PUT", "/shuffle", state="true" if on else "false")


def repeat(state: str) -> None:
    if state not in ("off", "track", "context"):
        raise ValueError(f"bad repeat state {state!r}")
    _player("PUT", "/repeat", state=state)


def add_to_queue(query: str) -> str:
    """Finds the best song for the query and queues it. Returns 'Song by Artist'; NoMatch if none."""
    uri, what = best(search(query, "song"), query, "song")
    if not URI.fullmatch(uri) or not uri.startswith("spotify:track:"):
        raise SpotifyError(f"Spotify returned an odd link ({uri[:60]!r})")
    _player("POST", "/queue", uri=uri)
    return what


def like_current() -> str:
    """Saves what's playing to his Liked Songs. Returns 'Song by Artist'."""
    now = now_playing()
    if now is None:
        raise SpotifyError(NO_DEVICE)
    if not URI.fullmatch(now["uri"]) or not now["uri"].startswith("spotify:track:"):
        raise SpotifyError("nothing likeable is playing on Spotify")
    with _client() as http:
        _call(http, "PUT", LIBRARY_URL, params={"uris": now["uri"]})
    return now["track"]


# ----- signing in (PKCE): jarvis media login -----
def new_verifier() -> str:
    return secrets.token_urlsafe(64)[:96]


def login_url(verifier: str, state: str) -> str:
    """The page where he approves JARVIS's scopes; Spotify then redirects to SPOTIFY_REDIRECT_URI."""
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return AUTH_URL + "?" + urlencode({
        "client_id": config.SPOTIFY_CLIENT_ID, "response_type": "code", "redirect_uri": config.SPOTIFY_REDIRECT_URI,
        "code_challenge_method": "S256", "code_challenge": challenge, "scope": SCOPES, "state": state})


def finish_login(redirected: str, verifier: str, state: str) -> bool:
    """Trades the code in the address Spotify redirected to for tokens and stores them. Returns
    whether they're in the Keychain (False: memory only, for this run)."""
    global _token, _refresh
    query = parse_qs(urlparse(redirected.strip()).query)
    if query.get("state", [""])[0] != state:
        raise SpotifyError("that address isn't from this sign-in (state doesn't match)")
    if query.get("error"):
        raise SpotifyError(f"Spotify sign-in was refused ({query['error'][0][:60]})")
    code = query.get("code", [""])[0]
    if not code:
        raise SpotifyError("that address has no sign-in code in it")
    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": config.SPOTIFY_REDIRECT_URI,
            "client_id": config.SPOTIFY_CLIENT_ID, "code_verifier": verifier}
    try:
        with _client() as http:
            r = http.post(TOKEN_URL, data=data)
    except httpx.HTTPError as e:
        raise SpotifyError(f"couldn't reach Spotify ({type(e).__name__})")
    if r.status_code != 200:
        raise SpotifyError(f"Spotify sign-in failed (HTTP {r.status_code})")
    body = _json(r)
    if not body.get("refresh_token"):
        raise SpotifyError("Spotify sign-in gave no refresh token")
    _refresh = ""
    _keep(body)
    return keychain.get("refresh_token") == _refresh
