import io
import json
import socket
import urllib.error
import urllib.request
from email.message import Message

import pytest

from jarvis.apps.web import page
from jarvis.core import actions

CLI = actions.Origin("cli")
PUBLIC = [(socket.AF_INET, 0, 0, "", ("93.184.215.14", 0))]


def resolver(table):
    def resolve(host, port):
        if host not in table:
            raise socket.gaierror("nope")
        return [(socket.AF_INET, 0, 0, "", (ip, 0)) for ip in table[host]]
    return resolve


class Resp(io.BytesIO):
    def __init__(self, body: bytes, kind="text/html; charset=utf-8", url="https://example.com/"):
        super().__init__(body)
        self.headers = Message()
        self.headers["Content-Type"] = kind
        self._url = url

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeOpener:
    def __init__(self, answer):
        self.answer, self.requests = answer, []

    def open(self, req, timeout=None):
        self.requests.append(req)
        if isinstance(self.answer, Exception):
            raise self.answer
        return Resp(json.dumps(self.answer).encode(), "application/json")


@pytest.fixture
def web(real_apps, monkeypatch):
    app = real_apps.apps["web"]
    app.ctx.secrets = {"SEARCH_API_KEY": "k"}
    return app


def search(reg, **args):
    return actions.run(reg, "web__search", args, CLI)


BRAVE = {"web": {"results": [{"title": "Spring <strong>calendar</strong>", "description": "Dates &amp; more",
                              "url": "https://seattlecolleges.edu/cal"},
                             {"title": "bad", "description": "x", "url": "javascript:alert(1)"}]}}


def test_search_cleans_the_query_counts_it_and_returns_untrusted_results(web, real_apps, monkeypatch):
    opener = FakeOpener(BRAVE)
    monkeypatch.setattr(web.module, "_opener", lambda: opener)
    out = search(real_apps, query="spring calendar elijah@x.edu https://canvas.test/feed?token=1", n=3)
    assert isinstance(out, actions.Ran)
    assert "1. Spring calendar\n   Dates & more\n   https://seattlecolleges.edu/cal" in out.result.text
    assert len(out.result.data["results"]) == 1  # the javascript: link is dropped
    req = opener.requests[0]
    assert "q=spring+calendar&count=3" in req.full_url and "token" not in req.full_url and "elijah" not in req.full_url
    assert req.get_header("X-subscription-token") == "k"
    assert real_apps.conn.execute("SELECT COUNT(*) FROM web_searches").fetchone()[0] == 1
    assert real_apps.find("web__search")[1].untrusted_output


def test_search_has_a_daily_cap(web, real_apps, monkeypatch):
    monkeypatch.setattr(web.module, "_opener", lambda: FakeOpener(BRAVE))
    web.ctx.settings["daily_limit"] = 1
    assert isinstance(search(real_apps, query="one"), actions.Ran)
    out = search(real_apps, query="two")
    assert isinstance(out, actions.Failed) and "searches are used up" in out.message


@pytest.mark.parametrize("error,fragment", [
    (urllib.error.HTTPError("u", 401, "no", {}, None), "API key was refused"),
    (urllib.error.HTTPError("u", 429, "slow", {}, None), "rate limit"),
    (urllib.error.URLError("offline"), "can't be reached"),
])
def test_search_failures_are_plain_sentences(web, real_apps, monkeypatch, error, fragment):
    monkeypatch.setattr(web.module, "_opener", lambda: FakeOpener(error))
    out = search(real_apps, query="x")
    assert isinstance(out, actions.Failed) and fragment in out.message


def test_search_without_a_key_says_how_to_set_it_up(web, real_apps):
    web.ctx.secrets = {}
    assert "add SEARCH_API_KEY" in search(real_apps, query="x").message


def test_google_needs_its_engine_id(web, real_apps, monkeypatch):
    web.ctx.settings["provider"] = "google"
    assert "SEARCH_ENGINE_ID" in search(real_apps, query="x").message
    web.ctx.secrets["SEARCH_ENGINE_ID"] = "cx1"
    opener = FakeOpener({"items": [{"title": "T", "snippet": "S", "link": "https://a.example/"}]})
    monkeypatch.setattr(web.module, "_opener", lambda: opener)
    out = search(real_apps, query="x")
    assert "https://a.example/" in out.result.text and "cx=cx1" in opener.requests[0].full_url


# -- which addresses may be opened --------------------------------------------------------------------------------

TABLE = {"example.com": ["93.184.215.14"], "sneaky.example": ["192.168.1.20"], "dual.example": ["8.8.8.8", "127.0.0.1"]}


@pytest.mark.parametrize("url,fragment", [
    ("http://127.0.0.1:8765/", "local network"),
    ("http://192.168.1.1/", "local network"),
    ("http://[::1]/", "local network"),
    ("http://sneaky.example/", "local network"),
    ("http://dual.example/", "local network"),
    ("file:///etc/passwd", "only http"),
    ("http://nowhere.example/", "didn't resolve"),
])
def test_check_url_refuses_this_mac_and_the_network(url, fragment):
    assert fragment in page.check_url(url, resolver({**TABLE, "127.0.0.1": ["127.0.0.1"], "192.168.1.1": ["192.168.1.1"],
                                                      "::1": ["::1"]}))


def test_check_url_allows_a_public_site():
    assert page.check_url("https://example.com/a", resolver(TABLE)) is None


def test_banking_sites_are_blocked_and_the_setting_adds_more():
    assert "banking" in page.blocked_site("https://www.chase.com/login")
    assert page.blocked_site("https://news.example/") is None
    assert "myschool.example" in page.blocked_site("https://x.myschool.example/", "myschool.example, other.test")


def test_a_redirect_onto_the_network_is_refused():
    check = lambda url: page.check_url(url, resolver(TABLE))
    guard = page._Guard(check)
    req = urllib.request.Request("https://example.com/")
    with pytest.raises(page.Refused, match="redirected"):
        guard.redirect_request(req, None, 302, "Found", Message(), "http://sneaky.example/admin")
    assert guard.redirect_request(req, None, 302, "Found", Message(), "https://example.com/b") is not None


# -- reading a page ----------------------------------------------------------------------------------------------

HTML = b"""<html><head><title>Spring  Calendar</title><style>p{}</style><script>var x=1</script></head>
<body><nav>Home | About</nav><h1>Spring quarter</h1><p>Classes start <b>April 1</b>.</p>
<p>See the <a href="/finals">finals schedule</a> or <a href="mailto:x@y.z">mail</a>.</p><footer>(c) 2026</footer></body></html>"""


def read(reg, monkeypatch, body=HTML, kind="text/html; charset=utf-8", url="https://example.com/cal"):
    app = reg.apps["web"]
    monkeypatch.setattr(app.module, "_check", lambda ctx, u: page.blocked_site(u) or page.check_url(u, resolver(TABLE)))
    monkeypatch.setattr(app.module, "_page_opener", lambda ctx: lambda req, timeout=None: Resp(body, kind, url))
    return actions.run(reg, "web__read_page", {"url": url}, CLI)


def test_read_page_returns_the_readable_text_and_links(real_apps, monkeypatch):
    out = read(real_apps, monkeypatch)
    text = out.result.text
    assert text.startswith("Spring Calendar\nhttps://example.com/cal")
    assert "Spring quarter" in text and "Classes start April 1." in text
    assert "Home | About" not in text and "var x" not in text and "(c) 2026" not in text
    assert "- finals schedule: https://example.com/finals" in text and "mailto" not in text


def test_a_script_only_page_says_it_needs_a_browser(real_apps, monkeypatch):
    out = read(real_apps, monkeypatch, b"<html><body><div id=app></div><script src=app.js></script></body></html>")
    assert "needs a browser" in out.result.text and out.result.data["thin"]


def test_read_page_refuses_files_and_blocked_addresses(real_apps, monkeypatch):
    out = read(real_apps, monkeypatch, b"%PDF", "application/pdf")
    assert isinstance(out, actions.Failed) and "application/pdf file" in out.message
    out = read(real_apps, monkeypatch, url="http://sneaky.example/")
    assert isinstance(out, actions.Failed) and "local network" in out.message
    out = read(real_apps, monkeypatch, url="https://chase.com/")
    assert isinstance(out, actions.Failed) and "banking" in out.message


def test_plain_text_pages_are_read_too(real_apps, monkeypatch):
    out = read(real_apps, monkeypatch, b"line one\n\n\n\nline two", "text/plain")
    assert "line one\n\nline two" in out.result.text


# -- leaving the Mac after reading outside content ---------------------------------------------------------------

def test_search_runs_in_a_plain_turn_and_asks_after_outside_content(real_apps, monkeypatch):
    monkeypatch.setattr(real_apps.apps["web"].module, "_opener", lambda: FakeOpener(BRAVE))
    real_apps.apps["web"].ctx.secrets = {"SEARCH_API_KEY": "k"}
    assert isinstance(search(real_apps, query="spring calendar"), actions.Ran)
    steered = actions.Origin("discord", outside_content=True)
    out = actions.run(real_apps, "web__search", {"query": "what the page said"}, steered)
    assert isinstance(out, actions.Asked) and out.question == 'Search the web for "what the page said"?'

