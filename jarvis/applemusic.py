"""Apple Music, only as the backup for Spotify (see siri.music): used when Spotify is unavailable,
errors or has no match, never when Spotify plays.

First a playlist in his Music library whose name is the query, otherwise the top song in his library
matching it. AppleScript can't search the Apple Music catalog, so with no library match Apple's public
iTunes Search API (no key) finds the top catalog song, the Music app opens its page and System Events
presses its Play button, only ever inside Music (needs Accessibility permission and an Apple Music
subscription). The query and link are passed to the scripts as arguments, never as script text, and
success is only reported once Music says it's playing that song or artist.

Also the offline side of playback control (media.py): pause, skip, seek, volume, shuffle, repeat and
like in whichever of the Spotify and Music apps is open, plus the system media keys as a last resort.
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


# Playback control in the Spotify or Music app (the fallback for media.py when the Spotify Web API
# can't). The op is one of a fixed set and the value an argument, never script text.
CONTROL_SCRIPT = """
on run argv
    set op to item 1 of argv
    set v to item 2 of argv
    tell application "__APP__"
        if op is "pause" then
            pause
        else if op is "play" then
            play
        else if op is "next" then
            next track
        else if op is "previous" then
            previous track
        else if op is "seek" then
            set player position to (v as integer)
        else if op is "volume" then
            set sound volume to (v as integer)
        else if op is "shuffle" then
            __SHUFFLE__
        else if op is "repeat" then
            __REPEAT__
        else if op is "like" then
            __LIKE__
        end if
        if op is "next" or op is "previous" then delay 0.5
        set t to ""
        set a to ""
        try
            set t to name of current track
            set a to artist of current track
        end try
        return (player state as text) & (character id 31) & (sound volume as text) & (character id 31) & t & (character id 31) & a
    end tell
end run
"""
APP_TERMS = {
    "Spotify": {"__SHUFFLE__": 'set shuffling to (v is "on")', "__REPEAT__": 'set repeating to (v is not "off")',
                "__LIKE__": 'error "the Spotify app can\'t like songs"'},
    "Music": {"__SHUFFLE__": 'set shuffle enabled to (v is "on")',
              "__REPEAT__": 'if v is "track" then\n set song repeat to one\n else if v is "off" then\n'
                            ' set song repeat to off\n else\n set song repeat to all\n end if',
              "__LIKE__": "set favorited of current track to true"},
}
APP_OPS = {"Spotify": {"pause", "play", "next", "previous", "seek", "volume", "shuffle", "repeat", "status"},
           "Music": {"pause", "play", "next", "previous", "seek", "volume", "shuffle", "repeat", "like", "status"}}

# System media keys (play/pause, next, previous) for whatever player macOS routes them to. NX_KEYTYPE_*
# codes come in as an argument; posting events may need Accessibility for the background job.
MEDIA_KEYS = {"pause": 16, "play": 16, "next": 17, "previous": 18}
MEDIA_KEY_SCRIPT = """
ObjC.import('AppKit');
ObjC.import('CoreGraphics');
function run(argv) {
    const code = parseInt(argv[0], 10);
    for (const down of [true, false]) {
        const ev = $.NSEvent.otherEventWithTypeLocationModifierFlagsTimestampWindowNumberContextSubtypeData1Data2(
            14, $.NSMakePoint(0, 0), down ? 0xa00 : 0xb00, 0, 0, null, 8, (code << 16) | ((down ? 0xa : 0xb) << 8), -1);
        $.CGEventPost(0, ev.CGEvent);
    }
    return "sent";
}
"""


class AppleMusicError(RuntimeError):
    pass


def running_players() -> list[str]:
    """Which of Spotify and Music are open (without launching either)."""
    out = []
    for app in APP_OPS:
        try:
            if subprocess.run(["pgrep", "-xq", app], capture_output=True, timeout=5).returncode == 0:
                out.append(app)
        except (OSError, subprocess.TimeoutExpired):
            pass
    return out


def control(app: str, op: str, value: str = "") -> dict:
    """Runs one op in the app and returns its state afterwards: state ('playing', 'paused', 'stopped'),
    volume (0-100), track ('Song by Artist' or '')."""
    if app not in APP_OPS or op not in APP_OPS[app]:
        raise AppleMusicError(f"the {app} app can't do {op}")
    script = CONTROL_SCRIPT.replace("__APP__", app)
    for key, terms in APP_TERMS[app].items():
        script = script.replace(key, terms)
    raw = _osascript(script, op, str(value), timeout=15)
    state, volume, name, artist = (raw.split(FS) + ["", "", ""])[:4]
    return {"state": state.strip(), "volume": int(volume) if volume.strip().isdigit() else None,
            "track": f"{name} by {artist}" if name and artist else name}


def pick_player() -> str | None:
    """The open player to control: the one playing, else the first open one (Spotify first)."""
    open_ = running_players()
    if len(open_) < 2:
        return open_[0] if open_ else None
    for app in open_:
        try:
            if control(app, "status")["state"] == "playing":
                return app
        except AppleMusicError:
            pass
    return open_[0]


def media_key(op: str) -> None:
    """Presses the system play/pause, next or previous key."""
    if op not in MEDIA_KEYS:
        raise AppleMusicError(f"there's no media key for {op}")
    try:
        r = subprocess.run(["osascript", "-l", "JavaScript", "-", str(MEDIA_KEYS[op])], input=MEDIA_KEY_SCRIPT,
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise AppleMusicError(f"the media key didn't go through ({str(e)[:100]})")
    if r.returncode != 0:
        raise AppleMusicError(explain(r.stderr))


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
