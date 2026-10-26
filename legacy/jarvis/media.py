"""Playback control by voice or chat: pause, resume, skip, back, seek, volume (set, up, down, mute),
shuffle, repeat, queue a song, like the current song, and what's playing.

media_control(op, value) tries the Spotify Web API first (his account, any device), then AppleScript
in whichever of the Spotify and Music apps is open on this Mac, then the system media keys (play/pause,
next, previous only). It returns one short line saying what really happened, or honestly why not.
The op comes from a fixed list and the value is checked here, whoever proposed it.
"""

import re

from jarvis import applemusic, spotify

OPS = ("pause", "play", "next", "previous", "seek", "volume", "volume_up", "volume_down", "mute", "unmute",
       "shuffle", "repeat", "queue", "like", "now_playing")
WORDS = {"pause": "pause", "play": "resume", "next": "skip", "previous": "go back", "seek": "jump there",
         "volume": "set the volume", "volume_up": "turn it up", "volume_down": "turn it down", "mute": "mute",
         "unmute": "unmute", "shuffle": "change shuffle", "repeat": "change repeat", "queue": "queue songs",
         "like": "like songs", "now_playing": "tell what's playing"}
STEP = 10  # volume up/down, in percent
REPEAT = {"": "context", "on": "context", "context": "context", "all": "context", "playlist": "context",
          "album": "context", "track": "track", "song": "track", "one": "track", "off": "off"}
REPEAT_LINES = {"track": "Repeating this song.", "context": "Repeat on.", "off": "Repeat off."}
_unmute_to: int | None = None  # the volume before "mute", for "unmute"


def _int(op: str, value, lo: int, hi: int, default: int | None = None) -> int:
    if value in (None, "") and default is not None:
        return default
    if isinstance(value, bool) or not (isinstance(value, int) or (isinstance(value, str) and value.strip().isdigit())):
        raise ValueError(f"{op} needs a whole number, not {value!r}")
    n = int(value)
    if not lo <= n <= hi:
        raise ValueError(f"{op} needs a number from {lo} to {hi}, not {n}")
    return n


def validate(op, value=None) -> tuple[str, object]:
    """(op, cleaned value) or ValueError: op from OPS; seek seconds, volume 0-100, an optional step for
    volume up/down, on/off for shuffle, off/track/context for repeat, song text for queue, else nothing."""
    if op not in OPS:
        raise ValueError(f"unknown media op {op!r}")
    if op == "seek":
        return op, _int(op, value, 0, 36000)
    if op == "volume":
        return op, _int(op, value, 0, 100)
    if op in ("volume_up", "volume_down"):
        return op, _int(op, value, 1, 100, default=STEP)
    if op == "shuffle":
        v = {True: "on", False: "off"}.get(value, value) if isinstance(value, bool) else str(value or "on").strip().lower()
        if v not in ("on", "off"):
            raise ValueError(f"shuffle is on or off, not {value!r}")
        return op, v
    if op == "repeat":
        v = "off" if value is False else str(value or "").strip().lower()
        if v not in REPEAT:
            raise ValueError(f"repeat is off, track or context, not {value!r}")
        return op, REPEAT[v]
    if op == "queue":
        q = str(value or "").strip() if isinstance(value, str) else ""
        if not q or len(q) > 200 or re.search(r"[\x00-\x1f\x7f]", q):
            raise ValueError("queue needs a song to look for (up to 200 characters)")
        return op, q
    if value not in (None, ""):
        raise ValueError(f"{op} takes no value")
    return op, None


def _clock(seconds: int) -> str:
    return f"{seconds // 60}:{seconds % 60:02d}"


def _target_volume(op: str, value, current: int | None, error=spotify.SpotifyError) -> int:
    global _unmute_to
    if op == "volume":
        return value
    if op == "unmute":
        return _unmute_to or 50
    if current is None:
        raise error("this player's volume can't be read")
    if op == "mute":
        if current:
            _unmute_to = current
        return 0
    return max(0, min(100, current + (value if op == "volume_up" else -value)))


def _volume_line(op: str, n: int) -> str:
    return {"mute": "Muted.", "unmute": f"Volume back to {n}%.", "volume_up": f"Volume up to {n}%.",
            "volume_down": f"Volume down to {n}%."}.get(op, f"Volume {n}%.")


def _via_spotify(op: str, value) -> str:
    if op == "pause":
        spotify.pause()
        return "Paused."
    if op == "play":
        spotify.resume()
        return "Resumed."
    if op == "next":
        spotify.next_track()
        return "Skipped."
    if op == "previous":
        spotify.previous_track()
        return "Back one."
    if op == "seek":
        spotify.seek(value)
        return f"Jumped to {_clock(value)}."
    if op == "shuffle":
        spotify.shuffle(value == "on")
        return f"Shuffle {value}."
    if op == "repeat":
        spotify.repeat(value)
        return REPEAT_LINES[value]
    if op == "queue":
        return f"Queued {spotify.add_to_queue(value)}."
    if op == "like":
        return f"Liked {spotify.like_current()}."
    now = spotify.now_playing()
    if now is None:
        raise spotify.SpotifyError(spotify.NO_DEVICE)
    if op == "now_playing":
        if not now["track"]:
            return "Nothing's playing on Spotify."
        on = f" on {now['device']}" if now["device"] else ""
        return f"{'Playing' if now['playing'] else 'Paused on'} {now['track']}{on}."
    n = _target_volume(op, value, now["volume"])
    spotify.set_volume(n)
    return _volume_line(op, n)


def _via_app(app: str, op: str, value) -> str:
    where = "" if app == "Spotify" else " in Music"
    if op in ("volume", "volume_up", "volume_down", "mute", "unmute"):
        n = _target_volume(op, value, applemusic.control(app, "status")["volume"], applemusic.AppleMusicError)
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


APP_OP = {"volume_up": "volume", "volume_down": "volume", "mute": "volume", "unmute": "volume",
          "now_playing": "status"}


def media_control(op, value=None) -> str:
    """Does op (see OPS) and returns one short line: what happened, or why it couldn't."""
    op, value = validate(op, value)
    notes = []
    if spotify.configured():
        try:
            return _via_spotify(op, value)
        except spotify.NoMatch:
            return f"Spotify found nothing to queue for {value!r}."
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
    return f"Couldn't {WORDS[op]}: " + "; ".join(notes) + "."
