import pytest

from jarvis import clicker, config


def shot(tmp_path):
    n = {"i": 0}

    def capture():
        n["i"] += 1
        p = tmp_path / f"s{n['i']}.jpg"
        p.write_bytes(b"jpg")
        return p
    return capture


def looker(found, worked=True):
    def look(system, prompt, image, schema):
        assert image.exists()
        return found if schema is clicker.FIND_SCHEMA else {"worked": worked, "note": "The login form opened."}
    return look


def test_off_by_default(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SCREEN_CLICK", False)
    with pytest.raises(ValueError, match="SCREEN_CLICK"):
        clicker.click("the Log In button", capture=shot(tmp_path))


def test_finds_clicks_and_checks(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SCREEN_CLICK", True)
    pressed = []
    out = clicker.click("the Canvas Log In button", capture=shot(tmp_path),
                        look=looker({"found": True, "x": 0.5, "y": 0.25, "label": "Log In"}),
                        points=lambda: (1440, 900), press=lambda x, y: pressed.append((x, y)), settle=0)
    assert pressed == [(720.0, 225.0)] and out.startswith("Clicked Log In.")
    assert list(tmp_path.iterdir()) == []  # both screenshots deleted


@pytest.mark.parametrize("target,label", [("the Send button", "Send"), ("the blue button", "Place order"),
                                          ("that one", "Delete account")])
def test_never_clicks_what_sends_buys_or_deletes(monkeypatch, tmp_path, target, label):
    monkeypatch.setattr(config, "SCREEN_CLICK", True)
    pressed = []
    with pytest.raises(ValueError):
        clicker.click(target, capture=shot(tmp_path), look=looker({"found": True, "x": .5, "y": .5, "label": label}),
                      points=lambda: (100, 100), press=lambda x, y: pressed.append((x, y)), settle=0)
    assert pressed == []


def test_says_so_when_it_did_not_work(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SCREEN_CLICK", True)
    out = clicker.click("Log In", capture=shot(tmp_path),
                        look=looker({"found": True, "x": .1, "y": .1, "label": "Log In"}, worked=False),
                        points=lambda: (100, 100), press=lambda x, y: None, settle=0)
    assert "doesn't look like it worked" in out
