"""Apple Music, only as the backup for Spotify (see siri.music): used when Spotify is unavailable,
errors or has no match, never when Spotify plays.

AppleScript can't search the Apple Music catalog, so this plays a playlist in his Music library whose
name is the query, otherwise the top song in his library matching it. The query is passed to the
script as an argument, never as script text. Success is only reported once Music says it's playing.
"""

import subprocess

FS = "\x1f"  # field separator in the script's answer

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


class AppleMusicError(RuntimeError):
    pass


def play(query: str) -> str:
    """Plays the best match in the Music app. Returns what's playing; raises AppleMusicError otherwise."""
    try:
        r = subprocess.run(["osascript", "-", query], input=PLAY_SCRIPT, capture_output=True, text=True,
                           timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise AppleMusicError(f"the Music app didn't answer ({str(e)[:100]})")
    if r.returncode != 0:
        raise AppleMusicError(r.stderr.strip()[:200] or "AppleScript failed")
    raw = r.stdout.rstrip("\n")
    if not raw:
        raise AppleMusicError(f"nothing in the Music library matches {query!r}")
    if raw == "stopped":
        raise AppleMusicError("the Music app didn't start playing")
    kind, playlist, name, artist = (raw.split(FS) + ["", "", ""])[:4]
    track = f"{name} by {artist}" if artist else name
    return f"the playlist {playlist}, starting with {track}" if kind == "playlist" else track
