"""A browser that is only a script: sites are dictionaries, requests are recorded, nothing is launched."""

import ipaddress
import socket
from dataclasses import dataclass, field

from jarvis.apps.browser.engine import Loaded, PageFailed
from jarvis.apps.browser.steps import Control


def ctl(index, kind, label, options=(), autocomplete="", name="", disabled=False):
    return Control(index, kind, label, tuple(options), autocomplete, name, disabled)


@dataclass
class Site:
    title: str = "A page"
    text: str = "Some words on the page, long enough to count as a real page."
    links: list = field(default_factory=lambda: [("Next", "https://example.com/next")])
    controls: list = field(default_factory=list)
    redirect_to: str | None = None
    subrequests: list = field(default_factory=list)  # what the page asks for after it loads
    after_press: str | None = None  # the page a press leads to


class FakePage:
    def __init__(self, session):
        self.session, self.url, self.closed = session, "", False

    def goto(self, url, load_seconds):
        d = self.session.driver
        chain, seen = [url], 0
        while d.sites.get(chain[-1]) and d.sites[chain[-1]].redirect_to and seen < 5:
            chain.append(d.sites[chain[-1]].redirect_to)
            seen += 1
        for hop in chain:  # every hop is a request, as Playwright's route sees redirects
            why = self.session.check(hop)
            d.requests.append((hop, bool(why)))
            if why:
                raise PageFailed("net::ERR_BLOCKED_BY_CLIENT")
        self.url = chain[-1]
        for sub in d.sites.get(self.url, Site()).subrequests:
            d.requests.append((sub, bool(self.session.check(sub))))
        return 200

    def _site(self):
        return self.session.driver.sites.get(self.url, Site())

    def read(self):
        s = self._site()
        return Loaded(self.url, s.title, s.text, list(s.links))

    def screenshot(self):
        return b"\xff\xd8jpeg"

    def controls(self):
        return list(self._site().controls)

    def run(self, steps):
        self.session.driver.ran.append([s.words() for s, _ in steps])
        if steps[-1][0].verb == "press" and self._site().after_press:
            self.url = self._site().after_press

    def follow(self, link_text, load_seconds):
        hits = [h for t, h in self._site().links if t == link_text]
        if len(hits) != 1:
            raise PageFailed(f"the page has no link called “{link_text}”")
        self.goto(hits[0], load_seconds)

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, driver, profile, headed, check):
        self.driver, self.profile, self.headed, self.check = driver, profile, headed, check
        self.open, self.pages = True, []

    def new_page(self):
        self.driver.contexts += 1 if self.profile is None else 0  # a fresh context per page
        page = FakePage(self)
        self.pages.append(page)
        return page

    def alive(self):
        return self.open

    def pump(self, ms):
        import time
        self.pumped = getattr(self, "pumped", 0) + 1
        time.sleep(ms / 1000)

    def get_json(self, url, check):
        if (why := check(url)):
            raise PageFailed(why)
        self.driver.gets.append(url)
        found = self.driver.json.get(url)
        if found is None and self.driver.json_fallback:
            found = self.driver.json_fallback(url)
        return found or (404, None, None, url)

    def close(self):
        self.open = False


class FakeDriver:
    def __init__(self, sites=None):
        self.sites = dict(sites or {})
        self.json = {}  # url -> (status, data, next, final url)
        self.json_fallback = None  # url -> the same, for addresses the test didn't list
        self.launches, self.requests, self.ran, self.gets = [], [], [], []
        self.contexts = 0
        self.sessions = []

    def launch(self, profile, headed, check, browsers):
        self.launches.append((profile is not None, headed))
        s = FakeSession(self, profile, headed, check)
        self.sessions.append(s)
        return s


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now




PUBLIC = "93.184.215.14"
NAMES = {"example.com": PUBLIC, "canvas.test": PUBLIC, "evil.test": PUBLIC, "inside.test": "10.0.0.5",
         "sso.test": PUBLIC, "bank.example.net": PUBLIC, "www.chase.com": PUBLIC, "cdn.test": PUBLIC}


def resolver(table=None):
    names = NAMES | (table or {})

    def resolve(host, port):
        if host in names:
            return [(socket.AF_INET, 0, 0, "", (names[host], 0))]
        try:
            ipaddress.ip_address(host.strip("[]"))  # an address literal is its own answer
        except ValueError:
            raise socket.gaierror("no such name") from None
        return [(socket.AF_INET6 if ":" in host else socket.AF_INET, 0, 0, "", (host.strip("[]"), 0))]

    return resolve
