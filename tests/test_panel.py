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
