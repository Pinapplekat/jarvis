"""Spotify's Web API, Elijah's account: search, play on his active device, playback control, sign-in (PKCE).

Ported from legacy/jarvis/spotify.py with urllib instead of httpx. The refresh token lives in the login Keychain
under Falak's own service, never the old JARVIS's: Spotify rotates refresh tokens, so sharing one would sign the
live JARVIS out. The hour-long access token is only kept in memory. Tokens are never logged or put in messages.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets as pysecrets
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
SEARCH_URL = "https://api.spotify.com/v1/search"
PLAYER_URL = "https://api.spotify.com/v1/me/player"
LIBRARY_URL = "https://api.spotify.com/v1/me/library"
DEVICES_URL = PLAYER_URL + "/devices"
PLAY_URL = PLAYER_URL + "/play"
SCOPES = "user-modify-playback-state user-read-playback-state user-library-modify"
NO_DEVICE = "no Spotify device is open (open Spotify on the Mac or phone first)"
KEYCHAIN_SERVICE = "Falak Spotify"  # not "JARVIS Spotify": that one belongs to the old JARVIS
TOKEN_CHARS = re.compile(r"[A-Za-z0-9._~+/=-]{1,2000}")
TYPES = {"song": "track", "artist": "artist", "playlist": "playlist", "any": "track,artist,playlist"}
URI = re.compile(r"spotify:(track|artist|album|playlist):[A-Za-z0-9]{10,40}")
TIMEOUT = 10
PLAY_WAIT, PLAY_STEP = 4.0, 0.5  # seconds to see playback really start after Spotify accepts the command


class SpotifyError(Exception):
    pass


class NoMatch(SpotifyError):
    pass


class Restricted(SpotifyError):
    """Spotify won't do it right now (already paused, nothing to skip to)."""


class Keychain:
    """The refresh token in the login Keychain through the `security` CLI. Secrets go in on stdin, never in argv
    where `ps` could see them. Failures are quiet: the token is then kept in memory only."""

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
sleep = time.sleep  # tests make waiting instant
_token: tuple[str, float] = ("", 0.0)  # access token, expiry (memory only)
_refresh = ""  # the refresh token in use


def send(method: str, url: str, *, params: dict | None = None, form: dict | None = None, body: dict | None = None,
         headers: dict | None = None) -> tuple[int, dict]:
    """One HTTP request: (status, JSON body or {}). Tests replace this."""
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data, hdrs = None, dict(headers or {})
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    elif method in ("PUT", "POST"):
        data = b""
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            status, raw = resp.status, resp.read(1024 * 1024)
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read(64 * 1024)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise SpotifyError(f"couldn't reach Spotify ({getattr(e, 'reason', e)})") from None
    try:
        parsed = json.loads(raw) if raw else {}
    except ValueError:
        parsed = {}
    return status, parsed if isinstance(parsed, dict) else {}


class Account:
    """Spotify with one app's credentials (from the node's .env)."""

    def __init__(self, client_id: str, client_secret: str = "", market: str = "US") -> None:
        self.client_id, self.client_secret, self.market = client_id, client_secret, market

    # -- tokens ------------------------------------------------------------------------------------------------

    def configured(self) -> bool:
        global _refresh
        if not self.client_id:
            return False
        _refresh = _refresh or keychain.get("refresh_token")
        return bool(_refresh)

    def _keep(self, body: dict) -> str:
        global _token, _refresh
        if not body.get("access_token"):
            raise SpotifyError("Spotify sign-in gave no access token")
        _token = (str(body["access_token"]), time.time() + int(body.get("expires_in") or 3600))
        if body.get("refresh_token") and body["refresh_token"] != _refresh:  # Spotify rotated it
            _refresh = str(body["refresh_token"])
            keychain.set("refresh_token", _refresh)
        return _token[0]

    def _access_token(self) -> str:
        if _token[0] and _token[1] > time.time() + 60:
            return _token[0]
        if not self.configured():
            raise SpotifyError("Spotify isn't set up (SPOTIFY_CLIENT_ID in Falak-node/.env, then jarvis node music-login)")
        form = {"grant_type": "refresh_token", "refresh_token": _refresh}
        headers = {}
        if self.client_secret:
            pair = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
            headers["Authorization"] = f"Basic {pair}"
        else:
            form["client_id"] = self.client_id
        status, body = send("POST", TOKEN_URL, form=form, headers=headers)
        if status == 400 and body.get("error") == "invalid_grant":
            raise SpotifyError("Spotify refused the sign-in (revoked or expired; run jarvis node music-login again)")
        if status != 200:
            raise SpotifyError(f"Spotify sign-in failed (HTTP {status})")
        return self._keep(body)

    def call(self, method: str, url: str, **kw) -> tuple[int, dict]:
        """One Web API request; refreshes and retries once on 401; errors become a plain SpotifyError."""
        global _token
        for attempt in (1, 2):
            status, body = send(method, url, headers={"Authorization": f"Bearer {self._access_token()}"}, **kw)
            if status != 401 or attempt == 2:
                break
            _token = ("", 0.0)
        if status < 400:
            return status, body
        err = body.get("error")
        msg = str(err.get("message") or "") if isinstance(err, dict) else ""
        if status == 401:
            raise SpotifyError("Spotify rejected the sign-in even after refreshing it")
        if status == 403 and "scope" in msg.lower():
            raise SpotifyError("the Spotify sign-in lacks the playback permissions (run jarvis node music-login)")
        if status == 403 and "restriction" in msg.lower():
            raise Restricted("Spotify won't do that right now")
        if status == 403 and (url.startswith(PLAYER_URL) or "premium" in msg.lower()):
            raise SpotifyError("Spotify Premium is needed to control playback")
        if status == 404 and url.startswith(PLAYER_URL):
            raise SpotifyError(NO_DEVICE)
        if status == 429:
            raise SpotifyError("Spotify is rate-limiting me right now")
        raise SpotifyError(f"Spotify API error {status}" + (f" ({msg[:80]})" if msg else ""))

    # -- playing ------------------------------------------------------------------------------------------------

    def search(self, query: str, kind: str = "any") -> dict:
        _, body = self.call("GET", SEARCH_URL, params={"q": query, "type": TYPES[kind], "limit": 5,
                                                         "market": self.market})
        if not body:
            raise SpotifyError("Spotify search gave an unreadable answer")
        return body

    def _devices(self) -> list[dict]:
        _, body = self.call("GET", DEVICES_URL)
        return [d for d in body.get("devices") or [] if isinstance(d, dict) and d.get("id") and not d.get("is_restricted")]

    def play(self, query: str, kind: str = "any") -> str:
        """Starts the best match on his active device (or the first open one). Returns what is really playing.
        Spotify answers 204 even when nothing starts (a track it can't play here), so it's only reported once
        the player says so; otherwise it's paused and SpotifyError says what happened."""
        uri, what = best(self.search(query, kind), query, kind)
        if not URI.fullmatch(uri):
            raise SpotifyError(f"Spotify returned an odd link ({uri[:60]!r})")
        devices = self._devices()
        if not devices:
            raise SpotifyError(NO_DEVICE)
        device = next((d for d in devices if d.get("is_active")), devices[0])
        before = self.now_playing()
        body = {"uris": [uri]} if uri.startswith("spotify:track:") else {"context_uri": uri}
        self.call("PUT", PLAY_URL, params={"device_id": device["id"]}, body=body)
        playing = self._started(uri, what, before)
        if playing:
            return playing
        try:
            self.player("PUT", "/pause")  # so a late start can't play over the Music backup
        except SpotifyError:
            pass
        raise SpotifyError(f"Spotify took the command for {what} but nothing started playing")

    def _started(self, uri: str, what: str, before: dict | None) -> str:
        """'Song by Artist' once the player is really playing what was asked: that song (by link, or by name when
        Spotify swapped in its regional copy), or for an artist or playlist, something new. '' if not in time."""
        waited = 0.0
        while True:
            now = self.now_playing()
            if now and now["playing"] and now["track"]:
                if uri.startswith("spotify:track:"):
                    if now["uri"] == uri or now["track"].casefold() == what.casefold():
                        return now["track"]
                elif not (before and before["playing"] and before["uri"] == now["uri"]):
                    return f"{what}, starting with {now['track']}"
            if waited >= PLAY_WAIT:
                return ""
            sleep(PLAY_STEP)
            waited += PLAY_STEP

    def now_playing(self) -> dict | None:
        """track ('Song by Artist'), uri, playing, volume (0-100 or None), device, shuffle, repeat, progress (s)."""
        status, body = self.call("GET", PLAYER_URL, params={"market": self.market, "additional_types": "episode"})
        if status != 200 or not body.get("device"):
            return None
        item, device = body.get("item") or {}, body.get("device") or {}
        volume = device.get("volume_percent")
        return {"track": describe(item), "uri": str(item.get("uri") or ""), "playing": bool(body.get("is_playing")),
                "volume": volume if isinstance(volume, int) else None, "device": str(device.get("name") or ""),
                "shuffle": bool(body.get("shuffle_state")), "repeat": str(body.get("repeat_state") or "off"),
                "progress": int(body.get("progress_ms") or 0) // 1000}

    def player(self, method: str, path: str, **params) -> None:
        self.call(method, PLAYER_URL + path, params=params or None)

    def resume(self) -> None:
        if self.now_playing() is not None:
            self.player("PUT", "/play")
            return
        devices = self._devices()
        if not devices:
            raise SpotifyError(NO_DEVICE)
        self.call("PUT", PLAY_URL, params={"device_id": devices[0]["id"]})

    def queue(self, query: str) -> str:
        uri, what = best(self.search(query, "song"), query, "song")
        if not URI.fullmatch(uri) or not uri.startswith("spotify:track:"):
            raise SpotifyError(f"Spotify returned an odd link ({uri[:60]!r})")
        self.player("POST", "/queue", uri=uri)
        return what

    def like_current(self) -> str:
        now = self.now_playing()
        if now is None:
            raise SpotifyError(NO_DEVICE)
        if not now["uri"].startswith("spotify:track:") or not URI.fullmatch(now["uri"]):
            raise SpotifyError("nothing likeable is playing on Spotify")
        self.call("PUT", LIBRARY_URL, params={"uris": now["uri"]})
        return now["track"]

    # -- signing in: jarvis node music-login ---------------------------------------------------------------------

    def login_url(self, verifier: str, state: str, redirect_uri: str) -> str:
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        return AUTH_URL + "?" + urllib.parse.urlencode({
            "client_id": self.client_id, "response_type": "code", "redirect_uri": redirect_uri,
            "code_challenge_method": "S256", "code_challenge": challenge, "scope": SCOPES, "state": state})

    def finish_login(self, redirected: str, verifier: str, state: str, redirect_uri: str) -> bool:
        """Trade the code in the address Spotify redirected to for tokens. Returns whether they're in the Keychain."""
        global _refresh
        query = urllib.parse.parse_qs(urllib.parse.urlparse(redirected.strip()).query)
        if query.get("state", [""])[0] != state:
            raise SpotifyError("that address isn't from this sign-in (state doesn't match)")
        if query.get("error"):
            raise SpotifyError(f"Spotify sign-in was refused ({query['error'][0][:60]})")
        code = query.get("code", [""])[0]
        if not code:
            raise SpotifyError("that address has no sign-in code in it")
        status, body = send("POST", TOKEN_URL, form={
            "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
            "client_id": self.client_id, "code_verifier": verifier})
        if status != 200:
            raise SpotifyError(f"Spotify sign-in failed (HTTP {status})")
        if not body.get("refresh_token"):
            raise SpotifyError("Spotify sign-in gave no refresh token")
        _refresh = ""
        self._keep(body)
        return keychain.get("refresh_token") == _refresh


def new_verifier() -> str:
    return pysecrets.token_urlsafe(64)[:96]


def best(results: dict, query: str, kind: str = "any") -> tuple[str, str]:
    """(uri, what it is) for the best hit. With kind any: an exact artist, song or playlist name wins, otherwise
    the top song, then the top playlist."""
    found = {t: [i for i in (results.get(t + "s") or {}).get("items") or []
                 if i and i.get("uri") and i.get("is_playable") is not False]  # unplayable in his market
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


def describe(item: dict | None) -> str:
    if not isinstance(item, dict) or not item.get("name"):
        return ""
    by = ", ".join(str(a.get("name")) for a in item.get("artists") or [] if isinstance(a, dict) and a.get("name"))
    by = by or str((item.get("show") or {}).get("name") or "")
    return f"{item['name']} by {by}" if by else str(item["name"])


def forget() -> None:
    """Drop the in-memory tokens (tests, and a new sign-in)."""
    global _token, _refresh
    _token, _refresh = ("", 0.0), ""
