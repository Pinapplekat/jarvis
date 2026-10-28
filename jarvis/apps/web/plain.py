"""Pages that only show their text through JavaScript, but whose host offers a plain view of the same thing.

Google Docs and Drive folders, and sites built on them (You Need A Wiki, which Elijah's English class uses), render with
JavaScript, so read_page gets nothing. Google serves a shared Doc as plain text (`/export?format=txt`) and a shared
folder as a plain HTML list (`/embeddedfolderview`), so those are read instead. Only what is shared publicly comes
back; nothing is signed in."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

_ID = r"([A-Za-z0-9_-]{20,})"
_DOC = re.compile(rf"^/document/(?:u/\d+/)?d/{_ID}")
_FOLDER = re.compile(rf"^/drive/(?:u/\d+/)?folders/{_ID}")
_WIKI_PAGE = re.compile(rf"^/app/page/{_ID}")


def doc_text(doc_id: str) -> str:
    return f"https://docs.google.com/document/d/{doc_id}/export?format=txt"


def folder_list(folder_id: str) -> str:
    return f"https://drive.google.com/embeddedfolderview?id={folder_id}"


def extra_links(url: str) -> list[tuple[str, str]]:
    """Links worth adding to what was read: a wiki page's text has no navigation, so its Drive folder is offered."""
    parts = urlparse(url)
    if (parts.hostname or "").lower() in ("youneedawiki.com", "www.youneedawiki.com"):
        if folder := parse_qs(parts.query).get("p"):
            return [("the wiki's other pages (its Drive folder)", f"https://drive.google.com/drive/folders/{folder[0]}")]
    return []


def plain_view(url: str) -> str | None:
    """The URL to fetch instead of `url`, or None when `url` is read as it is."""
    parts = urlparse(url)
    host, path = (parts.hostname or "").lower(), parts.path
    if host == "docs.google.com" and (m := _DOC.match(path)) and not path.endswith("/export"):
        return doc_text(m[1])
    if host == "drive.google.com":
        if m := _FOLDER.match(path):
            return folder_list(m[1])
        if path == "/open" and (ids := parse_qs(parts.query).get("id")):
            return folder_list(ids[0])  # an /open link is usually a folder; a doc answers with its own link
    if host in ("youneedawiki.com", "www.youneedawiki.com") and (m := _WIKI_PAGE.match(path)):
        return doc_text(m[1])  # /app/page/<the page's Google Doc>?p=<the wiki's Drive folder>
    return None
