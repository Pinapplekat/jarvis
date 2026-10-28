"""The handlers as the model uses them: reading, pictures, the sites list, show, and the browse session."""

import pytest
from browser_fakes import Site

from jarvis.apps.browser import actions
from jarvis.contract.api import ActionError
from jarvis.core import actions as core


def test_read_has_the_shape_of_web_read_page(app, driver):
    driver.sites["https://example.com/"] = Site(title="Hello", text="Line one\n\n\n\nLine two", links=[
        ("Docs", "https://example.com/docs"), ("Docs", "https://example.com/docs"), ("", "https://example.com/x")])
    out = actions.read(app, "https://example.com/")
    assert out.text == ("Hello\nhttps://example.com/\n\nText:\nLine one\n\nLine two\n\n"
                        "Links:\n- Docs: https://example.com/docs\n- (no text): https://example.com/x")
    assert out.data["links"][0] == ["Docs", "https://example.com/docs"] and out.attachments == ()


def test_read_caps_text_and_links(app, driver):
    driver.sites["https://example.com/"] = Site(text="x" * 20_000, links=[(f"l{i}", f"https://example.com/{i}") for i in range(90)])
    out = actions.read(app, "https://example.com/")
    assert "(cut short)" in out.text and len(out.text) < 17_000 and out.text.count("\n- ") == 60


def test_a_screenshot_is_a_temp_file_for_the_core_to_release(app, driver):
    out = actions.read(app, "https://example.com/", screenshot=True)
    (att,) = out.attachments
    assert att.mime == "image/jpeg" and att.path.read_bytes().startswith(b"\xff\xd8") and att.path.parent == app._tmp_dir


def test_read_is_a_read_that_asks_after_outside_content(real_apps):
    ours = core.run(real_apps, "browser__read", {"url": "https://example.com/"}, core.Origin("cli", outside_content=True))
    assert isinstance(ours, core.Asked) and ours.question == "Open https://example.com/ in the browser?"


def test_a_page_that_wont_load_says_why(app, driver):
    with pytest.raises(ActionError, match="doesn't exist"):
        actions.read(app, "https://nowhere.invalid/")


def test_sign_in_and_forget_site_undo(app, driver):
    out = actions.sign_in(app, "canvas.test")
    assert "Sign in there yourself" in out.text and actions.sites(app).data["sites"] == ["canvas.test"]
    actions.undo_sign_in(app, out.undo)
    assert actions.sites(app).data["sites"] == []
    actions.manager(app).sites.add("canvas.test")
    gone = actions.forget_site(app, "https://canvas.test/")
    assert actions.sites(app).data["sites"] == []
    actions.undo_forget_site(app, gone.undo)
    assert actions.sites(app).data["sites"] == ["canvas.test"]
    with pytest.raises(ActionError, match="isn't on the list"):
        actions.forget_site(app, "example.com")


def test_undoing_a_sign_in_that_was_already_listed_keeps_the_site(app):
    out = actions.sign_in(app, "canvas.test")
    again = actions.sign_in(app, "canvas.test")
    assert again.undo["added"] is False
    actions.undo_sign_in(app, again.undo)
    assert actions.sites(app).data["sites"] == ["canvas.test"] and out.undo["added"]


def test_show_and_close_window(app, driver):
    actions.read(app, "https://example.com/next")
    assert "https://example.com/next" in actions.show(app).text
    assert actions.close_window(app).text == "Closed the window."
    assert actions.close_window(app).text == "No window was open."


def test_the_browse_session_reads_then_follows_then_closes(app, driver):
    started = actions.start_browse(app, "https://example.com/")
    first = actions.sample_browse(app, started.state)
    assert "Links:" in first.text and first.attachments
    assert "Next page" in actions.follow(app, "Next").text
    assert actions.stop_browse(app, started.state).text == "Closed the page."
    with pytest.raises(ActionError, match="No browse session"):
        actions.follow(app, "Next")


def test_follow_asks_after_outside_content_but_not_otherwise(real_apps):
    plain = core.run(real_apps, "browser__follow", {"link_text": "Next"}, core.Origin("cli"))
    assert not isinstance(plain, core.Asked)  # it ran (and failed: no node), it didn't ask
    steered = core.run(real_apps, "browser__follow", {"link_text": "Next"}, core.Origin("cli", outside_content=True))
    assert isinstance(steered, core.Asked)


def test_settings_reach_the_manager(app):
    app.settings = {**app.settings, "idle_minutes": 9, "load_seconds": 7, "blocklist": "shop.test"}
    m = actions.manager(app)
    assert (m.idle_minutes, m.load_seconds, m.guard.extra) == (9, 7, "shop.test")
