from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from jarvis import approvals, chat, config, location
from jarvis.memory import Memory
from jarvis.telegram import TelegramClient, Update

TZ = ZoneInfo("America/Los_Angeles")
SCHOOL = (47.6163, -122.3214)
HOME = (47.6600, -122.3130)


@pytest.fixture
def mem(monkeypatch):
    monkeypatch.setattr(config, "LOCATION", True)
    monkeypatch.setattr(config, "LOCATION_GEOCODE", True)
    monkeypatch.setattr(config, "LOCATION_STALE_MINUTES", 30)
    return Memory(":memory:")


def at(day, hh, mm=0):
    return datetime(2026, 10, day, hh, mm, tzinfo=TZ)


def stay(mem, spot, start, minutes, step=5):
    """A phone sitting still: updates every `step` minutes."""
    for k in range(0, minutes + 1, step):
        location.record_point(mem, *spot, start + timedelta(minutes=k), accuracy=20, source="live")


# ----- Telegram parsing -----
class FakeHTTP:
    def __init__(self, result):
        self.result = result

    def post(self, url, json=None, **kw):
        class R:
            status_code = 200
            headers = {"content-type": "application/json"}

            def __init__(self, body):
                self.body = body

            def json(self):
                return self.body
        return R({"ok": True, "result": self.result})


def parse(raw):
    tg = TelegramClient(token="x", chat_id="1")
    tg.http = FakeHTTP(raw)
    return tg.updates(None)


def test_static_live_and_edited_updates_parse():
    loc = {"latitude": SCHOOL[0], "longitude": SCHOOL[1], "horizontal_accuracy": 12}
    static, live, edit, text_edit = parse([
        {"update_id": 1, "message": {"from": {"id": 7}, "date": 1_760_000_000, "location": loc}},
        {"update_id": 2, "message": {"from": {"id": 7}, "date": 1_760_000_000,
                                     "location": {**loc, "live_period": 3600}}},
        {"update_id": 3, "edited_message": {"from": {"id": 7}, "date": 1_760_000_000, "edit_date": 1_760_000_600,
                                            "location": {**loc, "live_period": 3600}}},
        {"update_id": 4, "edited_message": {"from": {"id": 7}, "date": 1, "text": "typo fix"}},
    ])
    assert static.location["source"] == "static" and static.location["first"] and static.location["live_until"] is None
    assert live.location["source"] == "live"
    assert live.location["live_until"] == datetime.fromtimestamp(1_760_003_600, timezone.utc)
    assert not edit.location["first"] and edit.location["at"] == datetime.fromtimestamp(1_760_000_600, timezone.utc)
    assert edit.location["accuracy"] == 12
    assert text_edit.location is None and text_edit.text is None  # edited text is ignored


def test_until_turned_off_has_no_end():
    [u] = parse([{"update_id": 1, "message": {"from": {"id": 7}, "date": 5,
                  "location": {"latitude": 1, "longitude": 2, "live_period": 0x7FFFFFFF}}}])
    assert u.location["source"] == "live" and u.location["live_until"] is None


class FakeTG:
    def __init__(self, updates):
        self._updates = updates
        self.sent = []

    def updates(self, offset, wait):
        return self._updates

    def send(self, text, buttons=None):
        self.sent.append(text)


def test_only_elijahs_locations_are_handled(mem):
    point = {"lat": 1.0, "lon": 2.0}
    seen = []
    tg = FakeTG([Update(1, 999, datetime.now(timezone.utc), location=point),
                 Update(2, 7, datetime.now(timezone.utc), location=point)])
    approvals.handle_updates(mem, tg, 7, on_text=lambda u: seen.append("text"), on_location=seen.append)
    assert [u.update_id for u in seen] == [2]


# ----- storage -----
def test_nearby_updates_extend_the_point(mem):
    stay(mem, SCHOOL, at(17, 9), 60)
    rows = location.recent(mem, at(17, 0))
    assert len(rows) == 1
    assert location._dt(rows[0]["last_seen"]) == at(17, 10)
    location.record_point(mem, *HOME, at(17, 11))
    assert len(location.recent(mem, at(17, 0))) == 2


def test_bad_or_rough_points_are_ignored(mem):
    assert location.record_point(mem, 95, 0, at(17, 9)) is None
    assert location.record_point(mem, *SCHOOL, at(17, 9), accuracy=5000) is None
    assert location.latest(mem) is None


def test_prune_caps_count_and_age(mem, monkeypatch):
    monkeypatch.setattr(config, "LOCATION_KEEP_POINTS", 5)
    monkeypatch.setattr(config, "LOCATION_KEEP_DAYS", 14)
    location.record_point(mem, 10, 10, at(1, 9))  # 16 days before "now"
    for k in range(8):
        location.record_point(mem, 20 + k, 20, at(17, 9, k))
    location.prune(mem, now=at(17, 12))
    rows = mem.db.execute("SELECT lat FROM location_points ORDER BY id").fetchall()
    assert [r["lat"] for r in rows] == [23, 24, 25, 26, 27]


def test_staleness(mem):
    location.record_point(mem, *SCHOOL, at(17, 9))
    assert not location.is_stale(location.latest(mem), at(17, 9, 20))
    assert location.is_stale(location.latest(mem), at(17, 9, 45))
    assert "STALE" in location.for_prompt(mem, at(17, 10))


# ----- geocoding -----
def test_geocoder_gets_rounded_coords_and_is_cached(mem):
    calls = []

    def geo(lat, lon):
        calls.append((lat, lon))
        return "Seattle Central College, Capitol Hill"
    assert location.place_name(mem, 47.616321, -122.321488, geocoder=geo, now=100) == "Seattle Central College, Capitol Hill"
    assert calls == [(47.616, -122.321)]
    assert location.place_name(mem, 47.61634, -122.32142, geocoder=geo, now=200) == "Seattle Central College, Capitol Hill"
    assert len(calls) == 1


def test_geocoder_failure_falls_back_to_rounded_coordinates(mem):
    assert location.place_name(mem, *SCHOOL, geocoder=lambda lat, lon: None, now=100) is None
    location.record_point(mem, *SCHOOL, at(17, 9))
    text = location.for_prompt(mem, at(17, 9, 5))
    assert "around 47.62, -122.32" in text and "47.6163" not in text
    # a failure isn't cached: the next lookup tries again
    assert location.place_name(mem, *SCHOOL, geocoder=lambda lat, lon: "Broadway", now=200) == "Broadway"


def test_geocoder_rate_limit(mem):
    calls = []
    geo = lambda lat, lon: calls.append(1) or "x"  # noqa: E731
    location.place_name(mem, 1, 1, geocoder=geo, now=100.0)
    assert location.place_name(mem, 2, 2, geocoder=geo, now=100.5) is None
    assert len(calls) == 1


def test_lookups_off(mem, monkeypatch):
    monkeypatch.setattr(config, "LOCATION_GEOCODE", False)
    assert location.place_name(mem, *SCHOOL, geocoder=lambda *a: pytest.fail("called")) is None


# ----- places and routine -----
def test_stays_become_places_and_he_is_asked_once(mem):
    stay(mem, SCHOOL, at(17, 9), 40)
    asked = []
    location.learn(mem, at(17, 10), ask=asked.append, geocoder=lambda lat, lon: "Broadway")
    location.learn(mem, at(17, 10, 5), ask=asked.append, geocoder=lambda lat, lon: "Broadway")
    [place] = location.places(mem)
    assert len(asked) == 1 and f"place #{place['id']}" in asked[0] and "near Broadway" in asked[0]
    assert "#1 near Broadway" in location.for_prompt(mem, at(17, 10))
    # a quick stop isn't a place
    location.record_point(mem, *HOME, at(17, 11))
    location.learn(mem, at(17, 11, 5))
    assert len(location.places(mem)) == 1


def test_label_place_through_chat(mem):
    stay(mem, SCHOOL, at(17, 9), 40)
    location.learn(mem, at(17, 10), ask=lambda t: None, geocoder=lambda *a: None)
    assert chat.apply_actions(mem, [{"type": "label_place", "id": 1, "name": "School"}], TZ) == []
    assert location.places(mem)[0]["name"] == "school"
    assert "school" in location.for_prompt(mem, at(17, 10))
    assert chat.apply_actions(mem, [{"type": "label_place", "id": 99, "name": "x"}], TZ)


def learned_school(mem):
    """Fridays Oct 2 and 9: at school 9-11. Labelled."""
    for day in (2, 9):
        stay(mem, SCHOOL, at(day, 9), 120, step=10)
        location.learn(mem, at(day, 12))
    location.label_place(mem, location.places(mem)[0]["id"], "school")


def test_routine_at_school_as_usual(mem):
    learned_school(mem)
    stay(mem, SCHOOL, at(16, 9), 30)
    location.learn(mem, at(16, 9, 30))
    assert location.routine_guess(mem, at(16, 9, 35)) == "at school as usual for a Friday at this hour"


def test_routine_not_at_school(mem):
    learned_school(mem)
    stay(mem, HOME, at(16, 9), 30)
    location.learn(mem, at(16, 9, 30))
    guess = location.routine_guess(mem, at(16, 9, 35))
    assert guess == "not at school; he usually is by now on Fridays"
    assert "Routine (a guess): not at school" in location.for_prompt(mem, at(16, 9, 35))


def test_routine_needs_history(mem):
    stay(mem, SCHOOL, at(16, 9), 30)
    location.learn(mem, at(16, 9, 30))
    location.label_place(mem, 1, "school")
    assert location.routine_guess(mem, at(16, 9, 35)) == "at school, which isn't his usual for a Friday at this hour"
    assert location.routine_guess(mem, at(16, 12)) is None  # stale: no guess


def test_movement_summary(mem):
    stay(mem, HOME, at(17, 8), 30)
    location.record_point(mem, 47.64, -122.32, at(17, 8, 40))
    stay(mem, SCHOOL, at(17, 9), 30)
    location.learn(mem, at(17, 9, 30))
    location.label_place(mem, 1, "home")
    location.label_place(mem, 2, "school")
    assert location.movement(mem, at(17, 9, 30)) == ["home 8:00-8:30 AM", "on the move", "school 9:00-9:30 AM"]


# ----- live sharing lapse -----
def test_lapse_reminder_once_and_not_in_quiet_hours(mem):
    location.record_point(mem, *SCHOOL, at(17, 9), source="live", live_until=at(17, 17))
    assert location.check_sharing(mem, at(17, 16), quiet=False) is None  # still sharing
    assert location.check_sharing(mem, at(17, 17, 10), quiet=True) is None  # waits out quiet hours
    text = location.check_sharing(mem, at(17, 17, 10), quiet=False)
    assert "stopped sharing at 5:00 PM" in text and "OwnTracks" in text
    assert location.check_sharing(mem, at(17, 17, 20), quiet=False) is None
    assert "Live sharing: ended" in location.for_prompt(mem, at(17, 17, 20))


def test_old_lapse_is_not_reminded(mem):
    location.record_point(mem, *SCHOOL, at(17, 9), source="live", live_until=at(17, 10))
    assert location.check_sharing(mem, at(17, 20), quiet=False) is None


def test_chat_prompt_has_location_block_without_raw_coordinates(mem):
    location.record_point(mem, *SCHOOL, at(17, 9), source="live", live_until=at(17, 17))
    prompt = chat.build_prompt(mem, "where am I", at(17, 9, 5), "")
    block = prompt[prompt.index("<location"):prompt.index("</location>")]
    assert "updated 5 min ago" in block and "Live sharing: on until 5:00 PM" in block
    assert str(SCHOOL[0]) not in prompt


def test_forget_all(mem):
    stay(mem, SCHOOL, at(17, 9), 40)
    location.learn(mem, at(17, 10))
    mem.set_kv("geo:47.616,-122.321", "x")
    location.forget_all(mem)
    assert location.latest(mem) is None and location.places(mem) == [] and mem.get_kv("geo:47.616,-122.321") is None
