"""Music on the Mac: play something, control playback, say what's playing.

Spotify's Web API first (his account, any device), then AppleScript in whichever of Spotify and Music is open, then
the system media keys. Ported from legacy/jarvis/media.py. Each answer is one short line saying what really
happened, or honestly why not. The op comes from a fixed list and its value is checked here.
"""

from __future__ import annotations

import re

from jarvis.apps.music import applemusic, spotify
from jarvis.contract.api import ActionError, AppContext, Result

OPS = ("pause", "play", "next", "previous", "seek", "volume", "volume_up", "volume_down", "mute", "unmute",
       "shuffle", "repeat", "queue", "like")
WORDS = {"pause": "pause", "play": "resume", "next": "skip", "previous": "go back", "seek": "jump there",
         "volume": "set the volume", "volume_up": "turn it up", "volume_down": "turn it down", "mute": "mute",
         "unmute": "unmute", "shuffle": "change shuffle", "repeat": "change repeat", "queue": "queue songs",
         "like": "like songs", "now_playing": "tell what's playing"}
STEP = 10  # volume up/down, in percent
REPEAT = {"": "context", "on": "context", "context": "context", "all": "context", "playlist": "context",
          "album": "context", "track": "track", "song": "track", "one": "track", "off": "off"}
REPEAT_LINES = {"track": "Repeating this song.", "context": "Repeat on.", "off": "Repeat off."}
APP_OP = {"volume_up": "volume", "volume_down": "volume", "mute": "volume", "unmute": "volume", "now_playing": "status"}
_unmute_to: int | None = None  # the volume before "mute", for "unmute"


def _account(ctx: AppContext) -> spotify.Account:
    return spotify.Account(ctx.secrets.get("SPOTIFY_CLIENT_ID", ""), ctx.secrets.get("SPOTIFY_CLIENT_SECRET", ""),
                           str(ctx.settings["market"]))


def validate(op: str, number: int | None = None, text: str = "") -> tuple[str, object]:
    """(op, value) or ActionError: seek seconds, volume 0-100, an optional step for volume up/down, on/off for
    shuffle, off/track/context for repeat, a song for queue, nothing for the rest."""
    if op not in OPS and op != "now_playing":
        raise ActionError(f"I can't do '{op}' with music.")
    text = text.strip()
    if op == "seek":
        if number is None or not 0 <= number <= 36000:
            raise ActionError("seek needs the position in seconds (0 to 36000).")
        return op, number
    if op == "volume":
        if number is None or not 0 <= number <= 100:
            raise ActionError("volume needs a number from 0 to 100.")
        return op, number
    if op in ("volume_up", "volume_down"):
        n = STEP if number is None else number
        if not 1 <= n <= 100:
            raise ActionError(f"{op} takes a step from 1 to 100.")
        return op, n
    if op == "shuffle":
        v = (text or "on").lower()
        if v not in ("on", "off"):
            raise ActionError("shuffle is on or off.")
        return op, v
    if op == "repeat":
        v = text.lower()
        if v not in REPEAT:
            raise ActionError("repeat is off, track or context.")
        return op, REPEAT[v]
    if op == "queue":
        if not text or len(text) > 200 or re.search(r"[\x00-\x1f\x7f]", text):
            raise ActionError("queue needs a song to look for (up to 200 characters).")
        return op, text
    return op, None


def _clock(seconds: int) -> str:
    return f"{seconds // 60}:{seconds % 60:02d}"


def _target_volume(op: str, value, current: int | None) -> int:
    global _unmute_to
    if op == "volume":
        return value
    if op == "unmute":
        return _unmute_to or 50
    if current is None:
        raise spotify.SpotifyError("this player's volume can't be read")
    if op == "mute":
        if current:
            _unmute_to = current
        return 0
    return max(0, min(100, current + (value if op == "volume_up" else -value)))


def _volume_line(op: str, n: int) -> str:
    return {"mute": "Muted.", "unmute": f"Volume back to {n}%.", "volume_up": f"Volume up to {n}%.",
            "volume_down": f"Volume down to {n}%."}.get(op, f"Volume {n}%.")


def _via_spotify(acct: spotify.Account, op: str, value) -> str:
    simple = {"pause": ("PUT", "/pause", "Paused."), "next": ("POST", "/next", "Skipped."),
              "previous": ("POST", "/previous", "Back one.")}
    if op in simple:
        method, path, line = simple[op]
        acct.player(method, path)
        return line
    if op == "play":
        acct.resume()
        return "Resumed."
    if op == "seek":
        acct.player("PUT", "/seek", position_ms=value * 1000)
        return f"Jumped to {_clock(value)}."
    if op == "shuffle":
        acct.player("PUT", "/shuffle", state="true" if value == "on" else "false")
        return f"Shuffle {value}."
    if op == "repeat":
        acct.player("PUT", "/repeat", state=value)
        return REPEAT_LINES[value]
    if op == "queue":
        return f"Queued {acct.queue(value)}."
    if op == "like":
        return f"Liked {acct.like_current()}."
    now = acct.now_playing()
    if now is None:
        raise spotify.SpotifyError(spotify.NO_DEVICE)
    if op == "now_playing":
        if not now["track"]:
            return "Nothing's playing on Spotify."
        on = f" on {now['device']}" if now["device"] else ""
        return f"{'Playing' if now['playing'] else 'Paused on'} {now['track']}{on}."
    n = _target_volume(op, value, now["volume"])
    acct.player("PUT", "/volume", volume_percent=n)
    return _volume_line(op, n)


def _via_app(app: str, op: str, value) -> str:
    where = "" if app == "Spotify" else " in Music"
    if op in ("volume", "volume_up", "volume_down", "mute", "unmute"):
        try:
            n = _target_volume(op, value, applemusic.control(app, "status")["volume"])
        except spotify.SpotifyError as e:
            raise applemusic.AppleMusicError(str(e)) from None
        applemusic.control(app, "volume", str(n))
        return _volume_line(op, n)[:-1] + where + "."
    state = applemusic.control(app, "status" if op == "now_playing" else op, "" if value is None else str(value))
    track = state["track"]
    if op == "now_playing":
        if state["state"] == "playing" and track:
            return f"Playing {track}{where}."
        return f"Paused on {track}{where}." if state["state"] == "paused" and track else "Nothing's playing."
    if op in ("next", "previous") and track:
        return f"{'Skipped' if op == 'next' else 'Back'} to {track}{where}."
    if op == "like":
        return f"Liked {track}{where}." if track else "Nothing's playing to like."
    if op == "seek":
        line = f"Jumped to {_clock(value)}."
    elif op == "shuffle":
        line = f"Shuffle {value}."
    elif op == "repeat":
        line = REPEAT_LINES[value]
    else:
        line = {"pause": "Paused.", "play": "Resumed.", "next": "Skipped.", "previous": "Back one."}[op]
    return line[:-1] + where + "."


def media_control(ctx: AppContext, op: str, value) -> str:
    acct, notes = _account(ctx), []
    if acct.configured():
        try:
            return _via_spotify(acct, op, value)
        except spotify.NoMatch:
            return f"Spotify found nothing to queue for '{value}'."
        except spotify.Restricted:
            return {"pause": "Already paused.", "play": "Already playing."}.get(op, "Spotify won't do that right now.")
        except spotify.SpotifyError as e:
            notes.append(f"Spotify: {str(e)[:120]}")
    app = applemusic.pick_player()
    if app:
        if APP_OP.get(op, op) not in applemusic.APP_OPS[app]:
            why = f" ({notes[0]})" if notes else ""
            return f"The {app} app can't {WORDS[op]} on its own{why}."
        try:
            return _via_app(app, op, value)
        except applemusic.AppleMusicError as e:
            notes.append(f"{app}: {str(e)[:120]}")
    if op in applemusic.MEDIA_KEYS:
        try:
            applemusic.media_key(op)
            return {"next": "Pressed next.", "previous": "Pressed previous."}.get(op, "Pressed play/pause.")
        except applemusic.AppleMusicError as e:
            notes.append(f"media key: {str(e)[:120]}")
    if not app:
        notes.append("Spotify and Music aren't open on the Mac")
    raise ActionError(f"Couldn't {WORDS[op]}: " + "; ".join(notes) + ".")


# -- actions ------------------------------------------------------------------------------------------------------

def play(ctx: AppContext, query: str, kind: str = "any") -> Result:
    """Spotify first; Apple Music when Spotify isn't set up, fails or has no match."""
    query = " ".join(query.split())
    acct, why = _account(ctx), ""
    if acct.configured():
        try:
            return Result(f"Playing {acct.play(query, kind)} on Spotify.")
        except spotify.NoMatch:
            why = "Spotify found nothing"
        except spotify.SpotifyError as e:
            why = f"Spotify: {e}"
    else:
        why = "Spotify isn't set up"
    try:
        return Result(f"Playing {applemusic.play(query)} in Music ({why}).")
    except applemusic.AppleMusicError as e:
        raise ActionError(f"I couldn't play '{query}'. {why[:1].upper()}{why[1:]}, and Music: {e}.") from None


def control(ctx: AppContext, op: str, number: int | None = None, text: str = "") -> Result:
    op, value = validate(op, number, text)
    return Result(media_control(ctx, op, value))


def now_playing(ctx: AppContext) -> Result:
    return Result(media_control(ctx, "now_playing", None))
