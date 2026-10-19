"""Apple Music, only as the backup for Spotify (see siri.music): used when Spotify is unavailable,
errors or has no match, never when Spotify plays.

First a playlist in his Music library whose name is the query, otherwise the top song in his library
matching it. AppleScript can't search the Apple Music catalog, so with no library match Apple's public
iTunes Search API (no key) finds the top catalog song, the Music app opens its page and System Events
presses its Play button, only ever inside Music (needs Accessibility permission and an Apple Music
subscription). The query and link are passed to the scripts as arguments, never as script text, and
success is only reported once Music says it's playing that song or artist.
"""

import re
import subprocess

import httpx

from jarvis.spotify import explain

FS = "\x1f"  # field separator in the script's answer
SEARCH_URL = "https://itunes.apple.com/search"
LINK = re.compile(r"https://music\.apple\.com/[a-z]{2}/(album|song)/[^\s\"'\\<>]{1,300}")

PLAY_SCRIPT = """
on run argv
    set q to item 1 of argv
    tell application "Music"
        set lists to (every user playlist whose name is q)
        if lists is not {} then
            set what to "playlist" & (character id 31) & (name of item 1 of lists)
            play item 1 of lists
        else
            set hits to search library playlist 1 for q
            if hits is {} then return ""
            set what to "track" & (character id 31) & ""
            play item 1 of hits
        end if
        delay 1
        if player state is not playing then return "stopped"
        set t to current track
        return what & (character id 31) & (name of t) & (character id 31) & (artist of t)
    end tell
end run
"""

# Opens a catalog song's page in Music and presses its Play button (Return on the selected song if
# there's none). Clicks and keys only go to Music, and only while it's in front.
CATALOG_SCRIPT = """
on run argv
    set u to item 1 of argv
    tell application "Music"
        activate
        open location u
    end tell
    delay 3
    tell application "System Events"
        tell process "Music"
            set frontmost to true
            delay 0.5
            if not frontmost then error "the Music app isn't in front, so I didn't press anything"
            set b to missing value
            repeat with e in (entire contents of window 1)
                set d to ""
                try
                    if class of e is button then set d to (description of e) as text
                end try
                considering case
                    set hit to d is "Play"
                end considering
                if hit then
                    set b to e
                    exit repeat
                end if
            end repeat
            if not frontmost then error "the Music app lost focus, so I didn't press anything"
            if b is missing value then
                key code 36
            else
                click b
            end if
        end tell
    end tell
    tell application "Music"
        repeat 12 times
            if player state is playing then
                set t to current track
                return (name of t) & (character id 31) & (artist of t)
            end if
            delay 0.5
        end repeat
    end tell
    return "stopped"
end run
"""


class AppleMusicError(RuntimeError):
    pass


def _client() -> httpx.Client:
    return httpx.Client(timeout=10)


def _osascript(script: str, *args: str, timeout: int = 30) -> str:
    try:
        r = subprocess.run(["osascript", "-", *args], input=script, capture_output=True, text=True,
                           timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise AppleMusicError(f"the Music app didn't answer ({str(e)[:100]})")
    if r.returncode != 0:
        raise AppleMusicError(explain(r.stderr))
    return r.stdout.rstrip("\n")


def catalog(query: str) -> tuple[str, str, str]:
    """(music:// link, song, artist) for the top Apple Music catalog song matching the query."""
    try:
        with _client() as http:
            r = http.get(SEARCH_URL, params={"term": query, "media": "music", "entity": "song", "limit": 5})
            r.raise_for_status()
            items = r.json().get("results") or []
    except (httpx.HTTPError, ValueError, AttributeError) as e:
        raise AppleMusicError(f"the Apple Music catalog search failed ({str(e)[:100]})")
    for item in items:
        url = str((item or {}).get("trackViewUrl") or "")
        if LINK.fullmatch(url) and item.get("trackName"):
            return "music://" + url[len("https://"):], str(item["trackName"])[:100], str(item.get("artistName") or "")[:100]
    raise AppleMusicError(f"nothing in the Music library or the Apple Music catalog matches {query!r}")


def play_catalog(query: str) -> str:
    """Plays the top catalog song. Returns what's playing; raises AppleMusicError otherwise."""
    url, name, artist = catalog(query)
    raw = _osascript(CATALOG_SCRIPT, url, timeout=60)
    if raw != "stopped":
        now, by = (raw.split(FS) + [""])[:2]
        if now.casefold() == name.casefold() or (artist and by.casefold() == artist.casefold()):
            return f"{now} by {by}" if by else now
    try:  # whatever did start isn't what was asked
        _osascript('tell application "Music" to pause', timeout=10)
    except AppleMusicError:
        pass
    raise AppleMusicError(f"the Music app opened {name} by {artist or 'unknown'} from the catalog but didn't start it")


def play(query: str) -> str:
    """Plays the best match in the Music app. Returns what's playing; raises AppleMusicError otherwise."""
    raw = _osascript(PLAY_SCRIPT, query)
    if not raw:
        return play_catalog(query)
    if raw == "stopped":
        raise AppleMusicError("the Music app didn't start playing")
    kind, playlist, name, artist = (raw.split(FS) + ["", "", ""])[:4]
    track = f"{name} by {artist}" if artist else name
    return f"the playlist {playlist}, starting with {track}" if kind == "playlist" else track
