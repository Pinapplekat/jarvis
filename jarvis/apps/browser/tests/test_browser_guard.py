"""Which requests the browser refuses, and that the browser's copy of the checks agrees with the web app's."""

import socket

import pytest
from browser_fakes import Site, resolver

from jarvis.apps.browser import guard
from jarvis.apps.browser.engine import PageFailed
from jarvis.apps.web import page as web_page
from jarvis.contract.api import ActionError

REFUSED = [
    "http://127.0.0.1/", "http://localhost:8765/", "http://192.168.1.20/router", "http://10.0.0.5/",
    "http://169.254.169.254/latest/meta-data", "http://[::1]/", "https://inside.test/", "file:///etc/passwd",
    "ftp://example.com/", "https://www.chase.com/login", "https://paypal.com/", "https://bank.example.net/",
    "https://nowhere.invalid/",
]
NOT_ADDRESSES = ["https://javascript:alert(1)", "https://data:text/html,hi", "not a url", ""]
ALLOWED = ["https://example.com/", "http://example.com:8080/x?y=1", "https://canvas.test/api/v1/courses"]


@pytest.mark.parametrize("url", REFUSED)
def test_refused_here_and_in_web(url):
    resolve = resolver({"localhost": "127.0.0.1"})
    mine = guard.Guard(resolve=resolve).check(url)
    theirs = web_page.blocked_site(url) or web_page.check_url(url, resolve)
    assert mine and theirs, url


@pytest.mark.parametrize("url", ALLOWED)
def test_allowed_here_and_in_web(url):
    assert guard.Guard(resolve=resolver()).check(url) is None
    assert web_page.blocked_site(url) is None and web_page.check_url(url, resolver()) is None


def test_the_two_copies_have_the_same_rules():
    assert guard.BLOCKED_SITES == web_page.BLOCKED_SITES
    for url in REFUSED + ALLOWED:
        assert guard.blocked_site(url, "extra.test") == web_page.blocked_site(url, "extra.test"), url
        assert guard.check_url(url, resolver()) == web_page.check_url(url, resolver()), url
    assert guard.blocked_site("https://shop.extra.test/", "extra.test")


def test_a_page_the_page_itself_makes_is_checked(mgr, driver):
    driver.sites["https://example.com/"] = Site(subrequests=["http://192.168.1.1/admin", "https://cdn.test/app.js",
                                                             "http://127.0.0.1:8765/", "https://www.chase.com/x.js"])
    mgr.read("https://example.com/")
    assert driver.requests == [("https://example.com/", False), ("http://192.168.1.1/admin", True),
                               ("https://cdn.test/app.js", False), ("http://127.0.0.1:8765/", True),
                               ("https://www.chase.com/x.js", True)]


def test_a_redirect_into_the_network_is_refused(mgr, driver):
    driver.sites["https://example.com/"] = Site(redirect_to="http://192.168.1.1/")
    with pytest.raises(ActionError, match="local network"):
        mgr.read("https://example.com/")
    assert ("http://192.168.1.1/", True) in driver.requests


def test_a_name_that_resolves_inside_is_refused(mgr, driver):
    with pytest.raises(ActionError, match="local network"):
        mgr.read("https://inside.test/")
    assert driver.launches == []  # refused before a browser was even started


def test_a_banking_site_is_refused_even_by_redirect(mgr, driver):
    driver.sites["https://example.com/"] = Site(redirect_to="https://www.chase.com/")
    with pytest.raises(ActionError, match="banking"):
        mgr.read("https://example.com/")


def test_data_urls_and_blobs_are_the_pages_own_content():
    g = guard.Guard(resolve=resolver())
    assert g.check("data:text/html,hi") is None and g.check("blob:https://example.com/abc") is None


def test_the_reason_is_remembered_for_the_failed_page():
    g = guard.Guard(resolve=resolver())
    g.check("http://10.0.0.5/")
    assert g.refused == ["addresses on this Mac or the local network are blocked"]
    g.new_page()
    assert g.refused == []


@pytest.mark.parametrize("url", NOT_ADDRESSES)
def test_things_that_are_not_web_addresses_are_refused_not_crashed(url):
    assert guard.Guard(resolve=resolver()).check(url)
