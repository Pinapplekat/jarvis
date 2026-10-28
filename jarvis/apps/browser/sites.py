"""The sites Elijah signed in to. Only these are opened with Falak's browser profile; every other site gets a fresh,
empty context, so a random page never sees his sessions."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse

SIGN_IN_PATH = re.compile(r"/(log-?in|sign-?in|saml\w*|sso|auth(?:orize)?|oauth2?|cas|adfs|idp|wayf|authn)(/|$|\?)", re.I)
SIGN_IN_HOST = re.compile(r"^(login|signin|sso|auth|accounts|idp|adfs|cas)\.|(^|\.)(microsoftonline|okta|duosecurity)\.com$",
                          re.I)


def host_of(site: str) -> str:
    """'https://Canvas.example.edu/courses' or 'canvas.example.edu' -> 'canvas.example.edu'."""
    text = site.strip()
    host = urlparse(text if "//" in text else f"//{text}").hostname or ""
    return host.lower().rstrip(".")


def looks_like_sign_in(url: str) -> bool:
    parts = urlparse(url)
    return bool(SIGN_IN_PATH.search(parts.path + ("?" if parts.query else ""))
                or SIGN_IN_HOST.search((parts.hostname or "").lower()))


class Sites:
    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.path = folder / "sites.json"

    @property
    def profile(self) -> Path:
        return self.folder / "profile"

    def make_profile(self) -> Path:
        """The profile is a live login: owner-only, created before the first browser touches it."""
        self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.profile.mkdir(mode=0o700, exist_ok=True)
        for p in (self.folder, self.profile):
            p.chmod(0o700)
        return self.profile

    def listed(self) -> list[str]:
        try:
            found = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return []
        return sorted({h for h in map(str, found) if h}) if isinstance(found, list) else []

    def covers(self, url: str) -> str | None:
        """The listed site this URL belongs to (the site itself or a page under it), or None."""
        host = host_of(url)
        return next((s for s in self.listed() if host == s or host.endswith("." + s)), None)

    def _save(self, hosts: list[str]) -> None:
        self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(sorted(set(hosts))))
        os.replace(tmp, self.path)

    def add(self, host: str) -> bool:
        """True when the site was not on the list before."""
        have = self.listed()
        if host in have:
            return False
        self._save([*have, host])
        return True

    def remove(self, host: str) -> bool:
        have = self.listed()
        if host not in have:
            return False
        self._save([h for h in have if h != host])
        return True
