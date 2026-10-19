import json

import pytest

from jarvis import cli, config, panel
from jarvis.memory import Memory
from jarvis.router import panel_command


def fake_app(seq, want, timeout=0, sleep=None):
    """Stands in for JARVIS Panel.app: acknowledges whatever was asked."""
    panel.ACK.write_text(json.dumps({"seq": seq, "state": want}))
    return True


def test_plot_is_safe_and_draws_a_line():
    out = panel.plot("sin(x) + 2")
    assert out["title"] == "y = sin(x) + 2" and "<path d=\"M" in out["html"]
    assert "default-src 'none'" in out["html"]  # nothing on the panel can reach the network
    assert panel.compile_formula("2x^2 - 3")(2) == 5
    for bad in ['__import__("os").system("ls")', "x.real", "open(1)", "(lambda: 1)()", "[x for x in ()]"]:
        with pytest.raises(ValueError):
            panel.compile_formula(bad)
    with pytest.raises(ValueError, match="no values"):  # huge powers don't hang: there's just nothing to draw
        panel.plot("9^9^9")


def test_show_then_close_bigger_and_back():
    m = Memory(":memory:")
    assert panel.show(m, {"type": "show", "kind": "plot", "expr": "sin(x)"}, wait=fake_app).startswith("On your screen")
    assert panel.visible() and "sin(x)" in panel.describe()
    panel.show(m, {"type": "show", "kind": "card", "title": "Due this week", "lines": ["Econ Sunday"]}, wait=fake_app)
    assert panel.resize(m, 1, wait=fake_app) == "Bigger, sir." and panel.current()["width"] > 560
    assert panel.back(m, wait=fake_app).startswith("On your screen") and "sin(x)" in panel.current()["title"]
    assert panel.close(m, wait=fake_app) == "Closed, sir." and not panel.visible()
    assert panel.close(m, wait=fake_app) == "Nothing's showing, sir."


def test_it_only_says_its_showing_once_the_app_confirms():
    with pytest.raises(ValueError, match="didn't respond"):
        panel.show(Memory(":memory:"), {"kind": "card", "title": "x", "lines": []}, wait=lambda *a: False)


def test_latest_screenshot_and_private_paths(tmp_path, monkeypatch):
    from jarvis import attachments
    shots = tmp_path / "shots"
    shots.mkdir()
    (shots / "screen-1.png").write_bytes(b"\x89PNG\r\n\x1a\nold")
    (shots / "screen-2.png").write_bytes(b"\x89PNG\r\n\x1a\nnew")
    monkeypatch.setattr(attachments, "SHOTS_DIR", shots)
    import os
    os.utime(shots / "screen-1.png", (1, 1))
    out = panel.image("latest")
    assert out["title"] == "screen-2.png" and "data:image/png;base64," in out["html"]
    with pytest.raises(ValueError):
        panel.image(str(tmp_path / "elsewhere.png"))


def test_page_html_cant_loosen_the_policy():
    out = panel.raw_html('<meta http-equiv="Content-Security-Policy" content="default-src *"><p>hi</p>')
    assert "default-src *" not in out["html"] and "<p>hi</p>" in out["html"]


def test_panel_phrases():
    assert panel_command("show me the screenshot from earlier", False) == "screenshot"
    assert panel_command("close that", True) == "close" and panel_command("close that", False) is None
    assert panel_command("make it bigger", True) == "bigger" and panel_command("go back", True) == "back"
    assert panel_command("go back", False) is None  # then it's the music's "previous"


def test_voice_close_goes_through_the_instant_path(monkeypatch):
    m = Memory(":memory:")
    monkeypatch.setattr(config, "PANEL", True)
    panel.show(m, {"kind": "card", "title": "x", "lines": []}, wait=fake_app)
    monkeypatch.setattr(panel, "_wait_ack", fake_app)
    monkeypatch.setattr(panel, "PANEL_OPS", {**panel.PANEL_OPS, "close": lambda mem: panel.close(mem, wait=fake_app)})
    assert cli._answer(m, "close that", spoken=True) == "Closed, sir."


def test_screenshot_titles_are_spoken_nicely(tmp_path, monkeypatch):
    from jarvis import attachments
    monkeypatch.setattr(attachments, "SHOTS_DIR", tmp_path)
    (tmp_path / "screen-20261018-155546.png").write_bytes(b"\x89PNG\r\n\x1a\nx")
    assert panel.image("latest")["title"] == "your screenshot from Sun 3:55 PM"


def test_deadlines_this_week_grouped_by_day(monkeypatch, tmp_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from jarvis.canvas import import_deadlines
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)  # no Canvas feed cached: the weekly list only
    tz = ZoneInfo(config.TIMEZONE)
    now = datetime(2026, 10, 19, 10, 0, tzinfo=tz)  # a Monday
    m = Memory(":memory:")
    import_deadlines(m, [{"course": "English", "title": "Read ch. 4", "due": "2026-10-22 10:30"},
                         {"course": "English", "title": "Essay", "due": "2026-10-22"},
                         {"course": "English", "title": "Poem", "due": "2026-10-22"},
                         {"course": "Econ", "title": "Problem set", "due": "2026-10-25"},
                         {"course": "English", "title": "Wiki entry", "due": "2026-10-20"},
                         {"course": "English", "title": "Old essay", "due": "2026-10-12"},
                         {"course": "Econ", "title": "Quiz", "due": "2026-10-19 09:00", "done": True},
                         {"course": "English", "title": "Next Monday", "due": "2026-10-26"}], tz)
    out = panel.deadlines(m, now)
    html = out["html"]
    assert html.index("Tomorrow") < html.index("Wiki entry") < html.index("Thursday") < html.index("Sunday")
    assert "Old essay" not in html and "Next Monday" not in html  # passed (status unknown) / not this week
    assert "10:30 AM" in html and "11:59" not in html  # end of day goes unsaid
    assert '<li class="row done"><span class="title">Quiz' in html  # finished work stays, struck through
    assert "Thursday's the heavy one, sir." in html and "·" not in html
    assert out["title"] == "5 due this week" and out["source"]["label"] == "Weekly plan"
    assert panel.deadlines(m, datetime(2026, 10, 24, 10, 0, tzinfo=tz))["title"] == "2 due by next Sunday"
