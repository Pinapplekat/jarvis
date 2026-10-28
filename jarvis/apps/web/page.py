"""Fetching one page with the standard library: which addresses may be opened, and the page's readable text."""

from __future__ import annotations

import ipaddress
import re
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

MAX_BYTES = 2 * 1024 * 1024
MAX_TEXT = 15_000
MAX_LINKS = 60
THIN = 60  # characters: less than this from a page with scripts means it needs a browser
TIMEOUT = 20
TYPES = ("text/html", "text/plain", "application/xhtml+xml")
AGENT = "Mozilla/5.0 (Macintosh; Falak; +personal assistant) AppleWebKit/537.36 (KHTML, like Gecko)"

BLOCKED_SITES = [
    "paypal.com", "venmo.com", "cash.app", "zellepay.com", "stripe.com", "squareup.com", "wise.com",
    "chase.com", "bankofamerica.com", "wellsfargo.com", "capitalone.com", "citi.com", "usbank.com",
    "americanexpress.com", "discover.com", "schwab.com", "fidelity.com", "vanguard.com", "robinhood.com",
    "coinbase.com", "kraken.com", "binance.com", "becu.org", "navyfederal.org", "ally.com", "sofi.com",
    "chime.com", "creditkarma.com", "intuit.com", "mint.com", "affirm.com", "klarna.com", "afterpay.com",
]


def blocked_site(url: str, extra: str = "") -> str | None:
    """Why this site is off limits (banking, payments), or None."""
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    for domain in BLOCKED_SITES + [d.strip().lower() for d in extra.split(",") if d.strip()]:
        if host == domain or host.endswith("." + domain):
            return f"{domain} is a banking or payment site, and I don't open those"
    if re.search(r"(^|\.)(bank|banking|creditunion|cu)\.", host) or host.startswith(("bank.", "banking.", "pay.")):
        return "that looks like a banking or payment site, and I don't open those"
    return None


def check_url(url: str, resolve=socket.getaddrinfo) -> str | None:
    """Why a URL must not be opened, or None. Only http(s), and never an address on this Mac or the local network,
    judged by every address the name resolves to (a public-looking name can point inside)."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return "only http and https links can be opened"
    try:
        port = parsed.port
    except ValueError:  # "https://javascript:alert(1)" is not an address at all
        return "only http and https links can be opened"
    try:
        infos = resolve(parsed.hostname, port or (443 if parsed.scheme == "https" else 80))
    except (socket.gaierror, UnicodeError):
        return "that site doesn't exist (its name didn't resolve)"
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast \
                or ip.is_unspecified:
            return "addresses on this Mac or the local network are blocked"
    return None


class Refused(Exception):
    """The page can't be read; the message is one plain sentence."""


class _Guard(urllib.request.HTTPRedirectHandler):
    """Re-check every redirect hop, so a public page can't bounce the request onto this Mac or the network."""

    def __init__(self, check) -> None:
        self.check = check

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        problem = self.check(newurl)
        if problem:
            raise Refused(f"the page redirected somewhere I can't open ({problem})")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def opener(check) -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_Guard(check))


@dataclass
class Page:
    url: str
    title: str = ""
    text: str = ""
    links: list[tuple[str, str]] = field(default_factory=list)  # (link text, absolute href)
    thin: bool = False  # it had scripts and almost no text: it needs a browser


def fetch(url: str, check, open_url) -> Page:
    """Download and read one page. `check(url)` vets every address; `open_url(request)` does the request."""
    problem = check(url)
    if problem:
        raise Refused(problem)
    req = urllib.request.Request(url, headers={"User-Agent": AGENT, "Accept": "text/html,text/plain;q=0.9"})
    try:
        with open_url(req) as resp:
            kind = resp.headers.get_content_type()
            if kind not in TYPES:
                raise Refused(f"that link is a {kind} file, not a page I can read")
            raw = resp.read(MAX_BYTES + 1)[:MAX_BYTES]
            charset = resp.headers.get_content_charset() or "utf-8"
            final = resp.geturl()
    except Refused:
        raise
    except urllib.error.HTTPError as e:
        raise Refused(f"the site answered {e.code} {e.reason}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        reason = getattr(e, "reason", e)
        raise Refused(f"the site can't be reached ({reason})") from None
    body = raw.decode(charset, errors="replace")
    if kind == "text/plain":
        return Page(final, text=_clean(body)[:MAX_TEXT])
    return read_html(body, final)


SKIP = {"script", "style", "noscript", "template", "svg", "nav", "header", "footer", "form", "iframe"}
BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "table", "ul",
         "ol", "pre", "blockquote", "dd", "dt"}


class _Reader(HTMLParser):
    def __init__(self, base: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base, self.skip, self.scripts = base, 0, 0
        self.parts: list[str] = []
        self.title, self.in_title = "", False
        self.links: list[tuple[str, str]] = []
        self.link: list[str] | None = None
        self.href = ""

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.scripts += 1
        if tag in SKIP:
            self.skip += 1
        elif tag == "title":
            self.in_title = True
        elif tag == "a":
            href = dict(attrs).get("href") or ""
            self.href, self.link = urljoin(self.base, href), []
        if tag in BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag == "title":
            self.in_title = False
        elif tag == "a" and self.link is not None:
            if self.href.startswith(("http://", "https://")):
                self.links.append((" ".join("".join(self.link).split())[:80], self.href))
            self.link = None
        if tag in BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        elif not self.skip:
            self.parts.append(data)
            if self.link is not None:
                self.link.append(data)


def read_html(body: str, url: str) -> Page:
    r = _Reader(url)
    r.feed(body)
    r.close()
    text = _clean("".join(r.parts))
    links = list(dict.fromkeys(r.links))[:MAX_LINKS]
    return Page(url, " ".join(r.title.split())[:200], text[:MAX_TEXT], links, thin=len(text) < THIN and r.scripts > 0)


def _clean(text: str) -> str:
    lines = [" ".join(line.split()) for line in text.splitlines()]
    out, blank = [], 0
    for line in lines:
        blank = blank + 1 if not line else 0
        if blank <= 1:
            out.append(line)
    return "\n".join(out).strip()
