"""Ambient location: where Elijah's phone is, from the locations he shares in Telegram.

A shared location (or each update of a live share, which arrives as an edited message) is
stored on this Mac. Nearby updates extend the last point instead of adding one, so a point
that spans 20+ minutes is a stay. Stays become places; JARVIS asks once what each new one is
("school", "work") and learns which days and hours he's usually where, so it can guess whether
he made it to class. The only things that leave the Mac are reverse-geocode lookups to
OpenStreetMap Nominatim: rounded, cached place names, and the street address of the latest
point (at most once a minute, only when he's moved, only the newest kept; REVERSE_GEOCODE=0
turns it off). Coordinates never go into chat context or replies. Telegram live sharing ends
after at most 8 hours; JARVIS reminds him once when it lapses.
"""

import json
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Callable

from jarvis import config
from jarvis.memory import Memory

SAME_SPOT_M = 75         # an update this close to the last point extends it...
SAME_SPOT_GAP = timedelta(hours=1)  # ...unless there was no word for this long
PLACE_RADIUS_M = 150     # a point this close to a place's centre is at that place
DWELL = timedelta(minutes=20)
MAX_ACCURACY_M = 1000    # rougher fixes are ignored
FOREVER = 0x7FFFFFFF     # Telegram's live_period for "until I turn it off"
LAPSE_WINDOW = timedelta(hours=2)  # only remind about a share that ended recently
GEO_ROUND = 3            # decimals sent to the geocoder for place names (about 100 m)
ADDRESS_ROUND = 5        # decimals sent for the street address (about 1 m)
ADDRESS_MOVE_M = 30      # look the address up again only after moving this far...
ADDRESS_EVERY_S = 60     # ...and at most once a minute
ADDRESS_KEY = "location_address"  # the latest lookup only, replaced each time
NOMINATIM = "https://nominatim.openstreetmap.org/reverse"
USER_AGENT = "JARVIS-personal-assistant/0.1 (single user, cached lookups)"
# Backstop for replies: "47.60621, -122.33207", "47.6° N, 122.3° W" and the like.
COORDS = re.compile(r"-?\d{1,3}\.\d{2,}\s*°?\s*[NS]?\s*,\s*-?\d{1,3}\.\d{2,}\s*°?\s*[EW]?", re.I)

Geocoder = Callable[[float, float], str | None]


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _dt(text: str) -> datetime:
    return datetime.fromisoformat(text)


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6_371_000 * 2 * math.asin(math.sqrt(a))


# ----- storing points -----
def from_message(m: dict, edited: bool) -> dict | None:
    """A Telegram message (or edited message) with a location -> a point, or None."""
    loc = m.get("location")
    if not loc or "latitude" not in loc or "longitude" not in loc:
        return None
    sent = datetime.fromtimestamp(m.get("date", 0), timezone.utc)
    at = datetime.fromtimestamp(m.get("edit_date") or m.get("date", 0), timezone.utc)
    period = loc.get("live_period")
    live_until = None
    if period and period != FOREVER:
        live_until = sent + timedelta(seconds=int(period))
    return {"lat": float(loc["latitude"]), "lon": float(loc["longitude"]),
            "accuracy": loc.get("horizontal_accuracy"), "at": at,
            "source": "live" if period else "static", "live_until": live_until,
            "first": not edited}


def record_point(mem: Memory, lat: float, lon: float, at: datetime, accuracy: float | None = None,
                 source: str = "static", live_until: datetime | None = None) -> int | None:
    """Store a point (or extend the last one if he hasn't moved). Returns its id, or None if ignored."""
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (accuracy and accuracy > MAX_ACCURACY_M):
        return None
    until = _iso(live_until) if live_until else None
    last = latest(mem)
    if (last and timedelta(0) <= at - _dt(last["last_seen"]) <= SAME_SPOT_GAP and distance_m(last["lat"], last["lon"], lat, lon) <= SAME_SPOT_M):
        mem.db.execute("UPDATE location_points SET last_seen = ?, source = ?, live_until = ? WHERE id = ?",
                       (_iso(at), source, until, last["id"]))
        mem.db.commit()
        return last["id"]
    cur = mem.db.execute(
        "INSERT INTO location_points (at, last_seen, lat, lon, accuracy, source, live_until) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (_iso(at), _iso(at), lat, lon, accuracy, source, until))
    mem.db.commit()
    return cur.lastrowid


def latest(mem: Memory):
    return mem.db.execute("SELECT * FROM location_points ORDER BY last_seen DESC, id DESC LIMIT 1").fetchone()


def recent(mem: Memory, since: datetime) -> list:
    return mem.db.execute("SELECT * FROM location_points WHERE last_seen >= ? ORDER BY at, id",
                          (_iso(since),)).fetchall()


def prune(mem: Memory, now: datetime | None = None) -> int:
    """Keep at most LOCATION_KEEP_POINTS points from the last LOCATION_KEEP_DAYS days."""
    now = now or datetime.now(timezone.utc)
    cutoff = _iso(now - timedelta(days=config.LOCATION_KEEP_DAYS))
    cur = mem.db.execute("DELETE FROM location_points WHERE last_seen < ?", (cutoff,))
    removed = cur.rowcount
    cur = mem.db.execute("""DELETE FROM location_points WHERE id NOT IN
                            (SELECT id FROM location_points ORDER BY last_seen DESC LIMIT ?)""",
                         (config.LOCATION_KEEP_POINTS,))
    removed += cur.rowcount
    visits_cutoff = (now - timedelta(days=120)).date().isoformat()
    mem.db.execute("DELETE FROM place_visits WHERE day < ?", (visits_cutoff,))
    mem.db.commit()
    return removed


def forget_all(mem: Memory) -> None:
    for table in ("location_points", "places", "place_visits"):
        mem.db.execute(f"DELETE FROM {table}")
    mem.db.execute("DELETE FROM kv WHERE key LIKE 'geo:%' OR key LIKE 'location_%'")
    mem.db.commit()


def is_stale(point, now: datetime) -> bool:
    return now - _dt(point["last_seen"]) > timedelta(minutes=config.LOCATION_STALE_MINUTES)


# ----- place names -----
def _geo_key(lat: float, lon: float) -> str:
    return f"geo:{lat:.{GEO_ROUND}f},{lon:.{GEO_ROUND}f}"


def nominatim(lat: float, lon: float) -> str | None:
    """One reverse-geocode lookup of rounded coordinates. None if it fails."""
    import httpx
    try:
        r = httpx.get(NOMINATIM, params={"lat": f"{lat:.{GEO_ROUND}f}", "lon": f"{lon:.{GEO_ROUND}f}",
                                         "format": "jsonv2", "zoom": 17},
                      headers={"User-Agent": USER_AGENT}, timeout=5)
        data = r.json() if r.status_code == 200 else {}
    except (httpx.HTTPError, ValueError):
        return None
    addr = data.get("address") or {}
    spot = data.get("name") or " ".join(filter(None, [addr.get("house_number"), addr.get("road")]))
    area = addr.get("neighbourhood") or addr.get("suburb") or addr.get("city") or addr.get("town")
    name = ", ".join(dict.fromkeys(filter(None, [spot, area])))
    return name[:80] or None


def reverse_geocode(lat: float, lon: float, get=None) -> str | None:
    """The street address at a point, e.g. '1701 Broadway, Capitol Hill, Seattle'. Sends only
    the coordinates (rounded to ADDRESS_ROUND decimals) and the User-Agent. None if it fails."""
    import httpx
    get = get or httpx.get
    try:
        r = get(NOMINATIM, params={"lat": f"{lat:.{ADDRESS_ROUND}f}", "lon": f"{lon:.{ADDRESS_ROUND}f}",
                                   "format": "jsonv2", "addressdetails": 1},
                headers={"User-Agent": USER_AGENT}, timeout=5)
        data = r.json() if r.status_code == 200 else {}
    except (httpx.HTTPError, ValueError):
        return None
    a = data.get("address") or {}
    street = " ".join(filter(None, [a.get("house_number"), a.get("road") or a.get("pedestrian")]))
    area = a.get("neighbourhood") or a.get("suburb")
    city = a.get("city") or a.get("town") or a.get("village")
    text = ", ".join(dict.fromkeys(filter(None, [street, area, city])))
    return text[:120] or None


def street_address(mem: Memory, lat: float, lon: float, now: float | None = None,
                   geocoder: Geocoder | None = None) -> str | None:
    """The latest point's street address. Looked up only when he's moved ADDRESS_MOVE_M since
    the last lookup, at most every ADDRESS_EVERY_S; only the newest answer is kept."""
    import time
    if not config.REVERSE_GEOCODE:
        return None
    now = now or time.time()
    try:
        last = json.loads(mem.get_kv(ADDRESS_KEY) or "{}")
    except ValueError:
        last = {}
    if last and distance_m(last["lat"], last["lon"], lat, lon) < ADDRESS_MOVE_M:
        return last.get("address")
    if last and now - last.get("at", 0) < ADDRESS_EVERY_S:
        return None  # moved, but too soon to ask again: say the street area instead
    address = (geocoder or reverse_geocode)(lat, lon)
    if address:
        mem.set_kv(ADDRESS_KEY, json.dumps({"lat": lat, "lon": lon, "address": address, "at": now}))
    else:  # a failure still counts towards the once-a-minute limit
        mem.set_kv(ADDRESS_KEY, json.dumps({**last, "at": now}) if last else "")
    return address


def strip_coordinates(text: str) -> str:
    """Remove anything that looks like a latitude/longitude pair (a backstop for replies)."""
    return COORDS.sub("(location withheld)", text)


def place_name(mem: Memory, lat: float, lon: float, geocoder: Geocoder | None = None,
               lookup: bool = True, now: float | None = None) -> str | None:
    """A readable name: cached by rounded coordinates; looked up at most once a second."""
    import time
    key = _geo_key(lat, lon)
    if (cached := mem.get_kv(key)) is not None:
        return cached or None
    if not lookup or not config.LOCATION_GEOCODE:
        return None
    now = now or time.time()
    last_call = float(mem.get_kv("location_geo_last") or 0)
    if now - last_call < 1:  # Nominatim's usage policy: at most one request per second
        return None
    mem.set_kv("location_geo_last", str(now))
    name = (geocoder or nominatim)(round(lat, GEO_ROUND), round(lon, GEO_ROUND))
    if name:  # failures aren't cached, so the next update tries again
        mem.set_kv(key, name)
    return name


def places(mem: Memory) -> list:
    return mem.db.execute("SELECT * FROM places ORDER BY id").fetchall()


def place_at(mem: Memory, lat: float, lon: float, labelled_only: bool = False):
    best = None
    for p in places(mem):
        if labelled_only and not p["name"]:
            continue
        d = distance_m(lat, lon, p["lat"], p["lon"])
        if d <= PLACE_RADIUS_M and (best is None or d < best[0]):
            best = (d, p)
    return best[1] if best else None


def label_place(mem: Memory, place_id: int, name: str) -> bool:
    name = name.strip().lower()[:40]
    if not name:
        return False
    cur = mem.db.execute("UPDATE places SET name = ? WHERE id = ?", (name, place_id))
    mem.db.commit()
    return cur.rowcount > 0


def describe_spot(mem: Memory, lat: float, lon: float) -> str:
    p = place_at(mem, lat, lon)
    near = place_name(mem, lat, lon, lookup=False)
    if p and p["name"]:
        return p["name"] + (f" ({near})" if near else "")
    return f"near {near}" if near else "at a spot with no name yet"


# ----- learning places and routine -----
def _local(dt: datetime, tz) -> datetime:
    return dt.astimezone(tz)


def learn(mem: Memory, now: datetime, ask: Callable[[str], None] | None = None,
          geocoder: Geocoder | None = None) -> str:
    """Turn stays into places, log visits by day, and ask about one new place at a time.
    `now` is local time (tz-aware). `ask(text)` sends Elijah the question."""
    tz = now.tzinfo
    asked = 0
    new = 0
    for pt in recent(mem, now - timedelta(days=2)):
        start, end = _local(_dt(pt["at"]), tz), _local(_dt(pt["last_seen"]), tz)
        day = start.date().isoformat()
        _visit(mem, day, 0, start, end)
        place = place_at(mem, pt["lat"], pt["lon"])
        if place is None and end - start >= DWELL:
            cur = mem.db.execute("INSERT INTO places (lat, lon, created_at) VALUES (?, ?, ?)",
                                 (pt["lat"], pt["lon"], _iso(now)))
            mem.db.commit()
            place = mem.db.execute("SELECT * FROM places WHERE id = ?", (cur.lastrowid,)).fetchone()
            new += 1
        if place is not None and end - start >= DWELL / 2:
            _visit(mem, day, place["id"], start, end)
    if ask:
        for p in places(mem):
            if p["name"] or p["asked_at"]:
                continue
            near = place_name(mem, p["lat"], p["lon"], geocoder=geocoder)
            spot = f"near {near}" if near else "at a spot I don't know"
            ask(f"📍 You've spent a while {spot}, sir. What is it to you: school, work, home? "
                "Just reply, or ignore me.")
            mem.db.execute("UPDATE places SET asked_at = ? WHERE id = ?", (_iso(now), p["id"]))
            mem.db.commit()
            asked += 1
            break  # one question at a time
    return f"{new} new places, asked {asked}"


def _visit(mem: Memory, day: str, place_id: int, start: datetime, end: datetime) -> None:
    first, last = f"{start:%H:%M}", f"{end:%H:%M}" if end.date() == start.date() else "23:59"
    mem.db.execute(
        """INSERT INTO place_visits (day, place_id, first, last) VALUES (?, ?, ?, ?)
           ON CONFLICT(day, place_id) DO UPDATE SET first = MIN(first, excluded.first),
             last = MAX(last, excluded.last)""", (day, place_id, first, last))
    mem.db.commit()


def usually_at(mem: Memory, now: datetime, weeks: int = 6) -> list[str]:
    """Labelled places he was at, at this time of day, on most recent same weekdays with data."""
    hm = f"{now:%H:%M}"
    days = [(now.date() - timedelta(days=7 * k)).isoformat() for k in range(1, weeks + 1)]
    marks = ",".join("?" * len(days))
    with_data = {r["day"] for r in mem.db.execute(
        f"SELECT day FROM place_visits WHERE place_id = 0 AND day IN ({marks})", days)}
    if len(with_data) < 2:
        return []
    out = []
    for p in places(mem):
        if not p["name"]:
            continue
        hits = mem.db.execute(
            f"SELECT COUNT(*) FROM place_visits WHERE place_id = ? AND day IN ({marks}) AND first <= ? AND last >= ?",
            (p["id"], *days, hm, hm)).fetchone()[0]
        if hits * 2 >= len(with_data):
            out.append(p["name"])
    return out


def routine_guess(mem: Memory, now: datetime) -> str | None:
    """One line like 'at school as usual for a Friday' or 'not at school; usually is by now on
    Mondays'. A guess from his own history; None when there's too little to go on."""
    last = latest(mem)
    if not last or is_stale(last, now):
        return None
    here = place_at(mem, last["lat"], last["lon"], labelled_only=True)
    usual = usually_at(mem, now)
    day = f"{now:%A}"
    if here and here["name"] in usual:
        return f"at {here['name']} as usual for a {day} at this hour"
    if usual:
        return f"not at {usual[0]}; he usually is by now on {day}s"
    if here:
        return f"at {here['name']}, which isn't his usual for a {day} at this hour"
    return None


# ----- what JARVIS sees -----
def _ago(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes < 1:
        return "just now"
    if minutes < 90:
        return f"{minutes} min ago"
    if minutes < 48 * 60:
        return f"{minutes // 60} h ago"
    return f"{minutes // 1440} days ago"


def movement(mem: Memory, now: datetime, hours: int = 4) -> list[str]:
    """Recent stays and moves, oldest first, collapsed: 'home 1:05-2:30 PM', 'on the move', ..."""
    segments: list[list] = []
    for pt in recent(mem, now - timedelta(hours=hours)):
        start, end = _local(_dt(pt["at"]), now.tzinfo), _local(_dt(pt["last_seen"]), now.tzinfo)
        label = describe_spot(mem, pt["lat"], pt["lon"]) if end - start >= timedelta(minutes=5) else "on the move"
        if segments and segments[-1][0] == label:
            segments[-1][2] = end
        else:
            segments.append([label, start, end])
    out = []
    for label, start, end in segments[-6:]:
        out.append(label if label == "on the move" else f"{label} {start:%-I:%M}-{end:%-I:%M %p}")
    return out


def for_prompt(mem: Memory, now: datetime, geocoder: Geocoder | None = None) -> str:
    """The ambient block for chat, every message. Never contains coordinates. May look up the
    street address of a fresh point (see street_address for the limits)."""
    if not config.LOCATION:
        return "(location is turned off)"
    last = latest(mem)
    if not last:
        return "(no location shared yet; he can share it in Telegram, live sharing lasts up to 8 hours)"
    seen = _dt(last["last_seen"])
    stale = is_stale(last, now)
    lines = [f"Last known: {describe_spot(mem, last['lat'], last['lon'])}, updated {_ago(now - seen)}"
             + (" (STALE: he may have moved since)" if stale else "")]
    if not stale and (address := street_address(mem, last["lat"], last["lon"], geocoder=geocoder)):
        lines.append(f"Street address (from OpenStreetMap, approximate): {address}")
    if last["source"] == "live":
        until = _dt(last["live_until"]) if last["live_until"] else None
        if until is None:
            lines.append("Live sharing: on, until he turns it off")
        elif until > now:
            lines.append(f"Live sharing: on until {until.astimezone(now.tzinfo):%-I:%M %p}")
        else:
            lines.append(f"Live sharing: ended {_ago(now - until)}")
    if (moves := movement(mem, now)):
        lines.append("Last few hours: " + " → ".join(moves))
    if (guess := routine_guess(mem, now)):
        lines.append(f"Routine (a guess): {guess}")
    unlabelled = [p for p in places(mem) if not p["name"] and p["asked_at"]]
    if unlabelled:
        lines.append("Places you asked about, not yet named (id for label_place only, never say it): " + ", ".join(
            f"#{p['id']} {describe_spot(mem, p['lat'], p['lon'])}" for p in unlabelled[-3:]))
    return "\n".join(lines)


def check_sharing(mem: Memory, now: datetime, quiet: bool) -> str | None:
    """Once per live share: if it has ended recently, return the reminder text (and mark it sent).
    Waits out quiet hours (if it's still recent when they end, it goes then)."""
    last = latest(mem)
    if not config.LOCATION or not last or last["source"] != "live" or not last["live_until"]:
        return None
    until = _dt(last["live_until"])
    if until > now or now - until > LAPSE_WINDOW or quiet:
        return None
    key = f"location_lapse:{last['live_until']}"
    if mem.get_kv(key):
        return None
    mem.set_kv(key, _iso(now))
    return (f"📍 Your live location stopped sharing at {until.astimezone(now.tzinfo):%-I:%M %p}, sir. "
            "To keep me in the loop, share it again in Telegram (📎 → Location → Share My Live Location; "
            "\"until I turn it off\" avoids the 8-hour limit). A background reporter on the phone "
            "(OwnTracks or a Shortcuts automation) could do this for good; say if you'd like that planned.")
