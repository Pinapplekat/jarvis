"""Cut streaming text into sentences that can be said as soon as they are complete."""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator

# Words whose trailing period doesn't end a sentence ("Dr. Who", "e.g. this", "5 p.m. today").
ABBREVIATIONS = {"dr", "mr", "mrs", "ms", "prof", "st", "jr", "sr", "vs", "etc", "e.g", "i.e", "a.m",
                 "p.m", "no", "approx", "dept", "est", "fig", "inc", "ltd", "mt", "ave", "jan", "feb",
                 "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec", "mon", "tue",
                 "wed", "thu", "fri", "sat", "sun", "u.s"}
_BOUNDARY = re.compile(r"[.!?]+[\"”')\]]*(?=\s)|\n")
_WORD_BEFORE = re.compile(r"([A-Za-z][A-Za-z.]*)\.$")
_QUOTES = '"“”'


def _abbreviation(chunk: str) -> bool:
    """Does `chunk` end with a period that belongs to an abbreviation or an initial?"""
    m = _WORD_BEFORE.search(chunk) if chunk.endswith(".") else None
    if not m:
        return False
    word = m[1].lower()
    return word in ABBREVIATIONS or (len(word) == 1 and m[1].isupper())


def _inside_quote(chunk: str) -> bool:
    """An odd number of double quotes so far: the end mark we found is inside a quoted name or phrase."""
    return sum(chunk.count(q) for q in _QUOTES) % 2 == 1


class SentenceBuffer:
    """Feed it streaming text; get back each sentence once it is clearly finished (its end mark is followed by
    whitespace, so "3.5" and "example.com" never split, or the line ended). `flush()` returns the tail."""

    def __init__(self) -> None:
        self.text, self.consumed = "", 0

    def feed(self, delta: str) -> list[str]:
        self.text += delta
        out, start = [], self.consumed
        for m in _BOUNDARY.finditer(self.text, start):
            chunk = self.text[start:m.end()]
            if m[0] != "\n" and (_abbreviation(self.text[start:m.start() + 1]) or _inside_quote(chunk)):
                continue
            if sentence := chunk.strip():
                out.append(sentence)
            start = m.end()
        self.consumed = start
        return out

    def flush(self) -> list[str]:
        tail = self.text[self.consumed:].strip()
        self.consumed = len(self.text)
        return [tail] if tail else []


def sentences(stream: Iterable[str]) -> Iterator[str]:
    """Sentences of a stream of text deltas, each as soon as it is complete, then the unfinished tail."""
    buf = SentenceBuffer()
    for delta in stream:
        yield from buf.feed(delta)
    yield from buf.flush()
