"""Real Chromium, in a scratch profile. Skipped unless FALAK_BROWSER_LIVE=1 (it needs `falak browser setup` first, and the
first test needs the network). FALAK_NODE_DIR must point at the scratch node folder that holds the Chromium."""

import os
import socket
from pathlib import Path

import pytest

from jarvis.apps.browser.engine import PlaywrightDriver
from jarvis.apps.browser.manager import Manager
from jarvis.apps.browser.sites import Sites
from jarvis.contract.api import ActionError

pytestmark = pytest.mark.skipif(os.environ.get("FALAK_BROWSER_LIVE") != "1", reason="set FALAK_BROWSER_LIVE=1")

FORM = """<title>Contact</title><body><h1>Contact us</h1><div id=out></div>
<label>Your name <input id=n></label><label>Password <input type=password></label>
<label>Topic <select><option>Billing</option><option>Other</option></select></label>
<label><input type=checkbox> Send me a copy</label>
<button onclick="document.getElementById('out').innerText='sent '+document.getElementById('n').value">Send</button>
<script>document.title = document.title + ' (scripted)'</script></body>"""


PAGES = {"https://pages.test/form": FORM, "https://pages.test/inside": (
    "<img src='http://127.0.0.1:9/x.png'><img src='http://192.168.1.1/y.png'><p>hello</p>")}


class PagesDriver(PlaywrightDriver):
    """Chromium for real, but https://pages.test/... is answered from PAGES (there is no network for it)."""

    def launch(self, profile, headed, check, browsers):
        session = super().launch(profile, headed, check, browsers)
        make = session.new_page

        def new_page():
            page = make()
            page._p.route("https://pages.test/**", lambda r: r.fulfill(
                body=PAGES[r.request.url.split("?")[0]], content_type="text/html"))
            return page

        session.new_page = new_page
        return session


@pytest.fixture
def live(tmp_path):
    chromium = Path(os.environ["FALAK_NODE_DIR"]) / "browser" / "chromium"
    assert chromium.is_dir(), "run `falak browser setup` with FALAK_NODE_DIR set to a scratch folder first"
    assert "Falak-node" not in str(tmp_path) and "Falak-dev" not in str(chromium)
    m = Manager(PagesDriver(), Sites(tmp_path / "browser"), browsers=chromium, autosweep=False,
                resolve=lambda host, port: (
                    [(socket.AF_INET, 0, 0, "", ("93.184.215.14", 0))] if host == "pages.test" else socket.getaddrinfo(host, port)))
    yield m
    m.stop()


def test_a_javascript_only_page_is_read(live):
    got = live.read("https://quotes.toscrape.com/js/", picture=True)
    assert "Albert Einstein" in got.loaded.text and got.picture[:2] == b"\xff\xd8"
    assert ("Next →", "https://quotes.toscrape.com/js/page/2/") in got.loaded.links


def test_a_page_that_points_at_this_mac_is_refused_for_real(live):
    got = live.read("https://pages.test/inside")
    assert "hello" in got.loaded.text
    assert live.guard.refused == ["addresses on this Mac or the local network are blocked"]  # aborted in Chromium
    with pytest.raises(ActionError, match="local network"):
        live.read("http://127.0.0.1:9/")


def test_act_fills_and_presses_for_real_and_never_types_a_password(live):
    url = "https://pages.test/form"
    with pytest.raises(ActionError, match="always yours"):
        live.act(url, ['fill "Password" with "hunter2"'])
    got = live.act(url, ['fill "Your name" with "Elijah"', 'select "Topic" as "Other"', 'check "Send me a copy"',
                         'press "Send"'])
    assert "sent Elijah" in got.loaded.text and "(scripted)" in got.loaded.title
    with pytest.raises(ActionError, match="no fill target"):
        live.act(url, ['fill "Nope" with "x"'])


def test_fetch_json_is_a_real_get_with_the_next_page_and_a_signed_out_answer(live):
    live.sites.add("api.github.com")
    got = live.fetch_json("https://api.github.com/repos/python/cpython/issues?per_page=1")
    assert got["signed_out"] is False and len(got["json"]) == 1 and "rel" not in str(got["next"]) and got["next"]
    assert live.fetch_json("https://api.github.com/user")["signed_out"] is True  # a 401: no login
    assert live.fetch_json("https://example.com/")["listed"] is False
