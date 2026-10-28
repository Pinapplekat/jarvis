"""Which kind of browser a page gets, and when it closes."""

import stat

import pytest
from browser_fakes import Site, ctl

from jarvis.apps.browser.manager import WINDOW_MINUTES
from jarvis.contract.api import ActionError


def test_a_site_not_on_the_list_opens_in_a_fresh_context(mgr, driver, sites):
    got = mgr.read("example.com")
    assert got.signed_in is False and driver.launches == [(False, False)]
    assert not sites.profile.exists()  # the profile isn't even created


def test_a_listed_site_uses_the_profile(mgr, driver, sites):
    sites.add("canvas.test")
    driver.sites["https://canvas.test/courses"] = Site(title="Courses")
    got = mgr.read("https://canvas.test/courses")
    assert got.signed_in is True and driver.launches == [(True, False)]
    assert sites.profile.is_dir()


def test_a_page_under_a_listed_site_counts_and_a_lookalike_does_not(mgr, sites):
    sites.add("canvas.test")
    assert sites.covers("https://x.canvas.test/a") == "canvas.test"
    assert sites.covers("https://notcanvas.test/") is None and sites.covers("https://evil.test/?u=canvas.test") is None


def test_the_profile_folder_is_owner_only(mgr, driver, sites):
    sites.add("canvas.test")
    driver.sites["https://canvas.test/"] = Site()
    mgr.read("https://canvas.test/")
    for folder in (sites.folder, sites.profile):
        assert stat.S_IMODE(folder.stat().st_mode) == 0o700


def test_a_fresh_page_never_shares_a_context_with_the_profile(mgr, driver, sites):
    sites.add("canvas.test")
    driver.sites["https://canvas.test/"] = Site()
    mgr.read("https://canvas.test/")
    mgr.read("https://example.com/")  # switching kinds closes the profile browser first
    assert driver.launches == [(True, False), (False, False)] and not driver.sessions[0].open


def test_one_browser_closes_after_idle_minutes(mgr, driver, clock):
    mgr.read("https://example.com/")
    clock.now += 4 * 60
    assert mgr.run(mgr.sweep) is False and mgr.open()
    clock.now += 61
    assert mgr.run(mgr.sweep) is True and not mgr.open()
    mgr.read("https://example.com/")  # and it comes back by itself
    assert len(driver.launches) == 2


def test_use_keeps_it_open(mgr, clock):
    for _ in range(4):
        mgr.read("https://example.com/")
        clock.now += 4 * 60
        assert mgr.run(mgr.sweep) is False


def test_show_reopens_the_page_in_a_window_and_it_closes_by_itself(mgr, driver, clock):
    mgr.read("https://example.com/next")
    shown = mgr.show("")  # the page we were just on
    assert shown == "https://example.com/next" and driver.launches == [(False, False), (False, True)]
    assert not driver.sessions[0].open
    clock.now += WINDOW_MINUTES * 60 + 1
    assert mgr.run(mgr.sweep) is True
    mgr.read("https://example.com/")
    assert driver.launches[-1] == (False, False)  # and back to headless


def test_a_window_elijah_closed_is_noticed(mgr, driver):
    mgr.show("https://example.com/")
    driver.sessions[-1].open = False
    assert mgr.run(mgr.sweep) is True


def test_nothing_else_opens_while_a_window_is_open(mgr, driver):
    mgr.show("https://example.com/")
    with pytest.raises(ActionError, match="window is open"):
        mgr.read("https://example.com/next")
    assert mgr.close_window() is True
    mgr.read("https://example.com/next")


def test_show_with_nothing_opened_asks_for_an_address(mgr):
    with pytest.raises(ActionError, match="nothing to show"):
        mgr.show("")


def test_sign_in_lists_the_site_and_opens_a_window_on_the_profile(mgr, driver, sites):
    driver.sites["https://canvas.test/"] = Site()
    assert mgr.sign_in("https://Canvas.test/login") == ("canvas.test", True)
    assert sites.listed() == ["canvas.test"] and driver.launches == [(True, True)]
    assert mgr.sign_in("canvas.test") == ("canvas.test", False)


def test_sign_in_refuses_a_bank_and_leaves_the_list_alone(mgr, sites):
    with pytest.raises(ActionError, match="banking"):
        mgr.sign_in("www.chase.com")
    assert sites.listed() == []


def test_a_failed_sign_in_window_does_not_leave_the_site_listed(mgr, driver, sites):
    driver.sites["https://canvas.test/"] = Site(redirect_to="http://10.0.0.5/")
    with pytest.raises(ActionError):
        mgr.sign_in("canvas.test")
    assert sites.listed() == []


def test_browse_looks_follows_and_reopens_after_the_browser_closed(mgr, driver, clock):
    mgr.browse_start("https://example.com/")
    assert mgr.browse_look().loaded.title == "A page"
    assert mgr.browse_follow("Next").loaded.title == "Next page"
    clock.now += 10 * 60
    mgr.run(mgr.sweep)
    assert mgr.browse_look().loaded.url == "https://example.com/next"  # it reopened where it was
    mgr.browse_stop()
    with pytest.raises(ActionError, match="No browse session"):
        mgr.browse_look()


def test_following_a_link_into_the_network_is_refused(mgr, driver):
    driver.sites["https://example.com/"] = Site(links=[("Router", "http://192.168.1.1/")])
    mgr.browse_start("https://example.com/")
    with pytest.raises(ActionError, match="local network"):
        mgr.browse_follow("Router")


def test_stop_closes_everything(mgr, driver):
    mgr.read("https://example.com/")
    mgr.stop()
    assert not driver.sessions[0].open


def test_controls_helper_builds_what_steps_match():
    assert ctl(0, "text", "Email").label == "Email"


def test_an_open_window_keeps_the_browser_running_between_calls(mgr, driver):
    # Playwright only runs its request checks while its thread is inside one of its calls. The sign-in window hung on
    # every click until something else used the browser (2026-10-28), so an open window is pumped until the next call.
    import time

    mgr.sign_in("example.com")
    session = mgr._session
    deadline = time.monotonic() + 2
    while getattr(session, "pumped", 0) < 3 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert getattr(session, "pumped", 0) >= 3  # it kept going with nothing else asking
    assert mgr.close_window()  # a real call gets the thread back (it would hang here if the pump never gave way)
    before = getattr(session, "pumped", 0)
    time.sleep(0.3)
    assert getattr(session, "pumped", 0) == before  # no window, no pump
