"""Which addresses the browser may open. A port of the checks in the web app (apps can't import each other);
tests/test_guard.py fails if the two copies ever disagree.

Applied to the first URL and then to every request the page makes, so a public page can't point the browser at this
Mac, the local network or a bank."""

from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlparse

BLOCKED_SITES = [
    "paypal.com", "venmo.com", "cash.app", "zellepay.com", "stripe.com", "squareup.com", "wise.com",
    "chase.com", "bankofamerica.com", "wellsfargo.com", "capitalone.com", "citi.com", "usbank.com",
    "americanexpress.com", "discover.com", "schwab.com", "fidelity.com", "vanguard.com", "robinhood.com",
    "coinbase.com", "kraken.com", "binance.com", "becu.org", "navyfederal.org", "ally.com", "sofi.com",
    "chime.com", "creditkarma.com", "intuit.com", "mint.com", "affirm.com", "klarna.com", "afterpay.com",
]
LOCAL_SCHEMES = ("data", "blob", "about")  # the page's own content, not a request to anywhere


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
        infos = resolve(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    except (socket.gaierror, UnicodeError):
        return "that site doesn't exist (its name didn't resolve)"
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast \
                or ip.is_unspecified:
            return "addresses on this Mac or the local network are blocked"
    return None


class Guard:
    """The one check every request goes through. Remembers why it refused, so a failed page can say so."""

    def __init__(self, extra_blocked: str = "", resolve=socket.getaddrinfo) -> None:
        self.extra, self.resolve = extra_blocked, resolve
        self.refused: list[str] = []
        self._seen: dict[str, str | None] = {}

    def check(self, url: str) -> str | None:
        """Why `url` must not be requested, or None. A host is looked up once per page (`new_page` forgets)."""
        if urlparse(url).scheme in LOCAL_SCHEMES:
            return None
        host = (urlparse(url).hostname or "").lower()
        if host not in self._seen:
            self._seen[host] = blocked_site(url, self.extra) or check_url(url, self.resolve)
        why = self._seen[host]
        if why and why not in self.refused:
            self.refused.append(why)
        return why

    def new_page(self) -> None:
        self.refused.clear()
        self._seen.clear()
