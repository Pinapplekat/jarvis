"""What the model is told about a page: the same shape as web.read_page."""

from __future__ import annotations

from jarvis.apps.browser.manager import Read

MAX_TEXT = 15_000
MAX_LINKS = 60
ACT_TEXT = 1_500


def clean(text: str) -> str:
    lines = [" ".join(line.split()) for line in text.splitlines()]
    out, blank = [], 0
    for line in lines:
        blank = blank + 1 if not line else 0
        if blank <= 1:
            out.append(line)
    return "\n".join(out).strip()


def links_of(read: Read) -> list[tuple[str, str]]:
    return list(dict.fromkeys((" ".join(t.split())[:80], h) for t, h in read.loaded.links))[:MAX_LINKS]


def page_text(read: Read) -> str:
    got = read.loaded
    text = clean(got.text)
    cut = " (cut short)" if len(text) > MAX_TEXT else ""
    links = "\n".join(f"- {t or '(no text)'}: {h}" for t, h in links_of(read))
    return (f"{got.title or got.url}\n{got.url}\n\nText{cut}:\n{text[:MAX_TEXT] or '(none)'}\n\n"
            f"Links:\n{links or '(none)'}")


def page_data(read: Read) -> dict:
    return {"url": read.loaded.url, "title": read.loaded.title, "links": [list(x) for x in links_of(read)],
            "signed_in": read.signed_in, "memory_mb": read.memory_mb}


def after_act(read: Read) -> str:
    got = read.loaded
    return f"{got.title or got.url} ({got.url}). The page now says:\n{clean(got.text)[:ACT_TEXT] or '(nothing)'}"
