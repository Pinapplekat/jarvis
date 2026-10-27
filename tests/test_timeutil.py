from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

import pytest

from jarvis.core.timeutil import in_quiet_hours, next_clock, parse_quiet_hours, quiet_ends

LA, NY = ZoneInfo("America/Los_Angeles"), ZoneInfo("America/New_York")


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def test_parse_quiet_hours():
    assert parse_quiet_hours("23-7") == (time(23), time(7))
    assert parse_quiet_hours("") is None and parse_quiet_hours("  ") is None
    for bad in ("23", "a-b", "25-7", "7-"):
        with pytest.raises(ValueError):
            parse_quiet_hours(bad)


@pytest.mark.parametrize("hour,expected", [(22, False), (23, True), (0, True), (6, True), (7, False), (12, False)])
def test_a_window_that_crosses_midnight(hour, expected):
    assert in_quiet_hours(time(hour, 30 if hour == 6 else 0), parse_quiet_hours("23-7")) is expected


def test_a_window_inside_one_day_and_none():
    window = parse_quiet_hours("13-15")
    assert in_quiet_hours(time(13), window) and not in_quiet_hours(time(15), window)
    assert not in_quiet_hours(time(3), None)


def test_quiet_ends_is_the_next_window_end():
    window = parse_quiet_hours("23-7")
    assert quiet_ends(datetime(2026, 10, 26, 23, 30, tzinfo=LA), window) == datetime(2026, 10, 27, 7, 0, tzinfo=LA)
    assert quiet_ends(datetime(2026, 10, 27, 3, 0, tzinfo=LA), window) == datetime(2026, 10, 27, 7, 0, tzinfo=LA)
    assert quiet_ends(datetime(2026, 10, 27, 12, 0, tzinfo=LA), window) is None
    assert quiet_ends(datetime(2026, 10, 27, 3, 0, tzinfo=LA), None) is None


def test_next_clock_picks_the_earliest_time_after_now():
    times = [time(7), time(21)]
    assert next_clock(datetime(2026, 10, 26, 12, 0, tzinfo=LA), times) == datetime(2026, 10, 26, 21, 0, tzinfo=LA)
    assert next_clock(datetime(2026, 10, 26, 21, 0, tzinfo=LA), times) == datetime(2026, 10, 27, 7, 0, tzinfo=LA)
    assert next_clock(datetime(2026, 10, 26, 22, 0, tzinfo=LA), times) == datetime(2026, 10, 27, 7, 0, tzinfo=LA)


def test_next_clock_across_the_spring_forward_gap():
    # 2:00 AM on 2026-03-08 doesn't exist in Los Angeles; the job runs once, when the clock jumps to 3:00.
    after = datetime(2026, 3, 7, 23, 0, tzinfo=LA)
    first = next_clock(after, [time(2)])
    assert first.astimezone(timezone.utc) == utc(2026, 3, 8, 10, 0) and first.astimezone(LA).hour == 3
    assert next_clock(first, [time(2)]).astimezone(timezone.utc) == utc(2026, 3, 9, 9, 0)  # Monday 2:00 AM PDT


def test_next_clock_across_the_fall_back_hour_runs_once():
    first = next_clock(datetime(2026, 10, 31, 23, 0, tzinfo=LA), [time(1, 30)])
    assert first.astimezone(timezone.utc) == utc(2026, 11, 1, 8, 30)  # 1:30 AM PDT, the first of the two 1:30s
    second = next_clock(first, [time(1, 30)])
    assert second.astimezone(timezone.utc) == utc(2026, 11, 2, 9, 30)  # not the second 1:30 (PST) that night
    # asked again from inside the repeated hour (1:10 AM the second time round), it still isn't "an hour ago"
    again = next_clock(utc(2026, 11, 1, 9, 10).astimezone(LA), [time(1, 30)])
    assert again.astimezone(timezone.utc) == utc(2026, 11, 2, 9, 30)


def test_next_clock_follows_the_timezone():
    same_instant = datetime(2026, 10, 26, 12, 0, tzinfo=LA)
    assert next_clock(same_instant, [time(21)]) == utc(2026, 10, 27, 4, 0)
    assert next_clock(same_instant.astimezone(NY), [time(21)]) == utc(2026, 10, 27, 1, 0)
