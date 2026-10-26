from datetime import datetime
from zoneinfo import ZoneInfo

from jarvis import spotting
from jarvis.chat import respond
from jarvis.judge import Answer, JevError
from jarvis.memory import Memory
from jarvis.web import Page, page_for_prompt

TEXT = """Week 3 Checklist
Reread The Limner Wrings His Hands by Monday
Lyric essay reflection due Oct 19 at 11:59pm
Published March 3, 2019
Sale ends tonight!
Just a normal line"""


def judge_lines(deadline_words):
    def fast(state, questions):
        lines = state.split("\n")[2:] if state.startswith("Source") else state.split("\n")[1:]
        return {q.key: Answer(any(w in lines[int(q.key[1:])] for w in deadline_words), 0.9) for q in questions}
    return fast


def test_candidates_find_date_mentions():
    found = spotting.candidates(TEXT)
    assert found == ["Reread The Limner Wrings His Hands by Monday",
                     "Lyric essay reflection due Oct 19 at 11:59pm",
                     "Published March 3, 2019", "Sale ends tonight!"]
    assert spotting.candidates("no dates here\nnone") == []


def test_spot_keeps_only_jev_deadlines_and_fails_quietly():
    lines = spotting.candidates(TEXT)
    assert spotting.spot(lines, judge_lines(["due Oct", "by Monday"]), "page") == lines[:2]
    assert spotting.spot(lines, None) == []

    def broken(s, q):
        raise JevError("down")
    assert spotting.spot(lines, broken) == []


def test_spotted_deadlines_reach_the_prompt():
    page = Page("https://wiki.test", title="Week 3", text=TEXT, deadlines=["Lyric essay reflection due Oct 19"])
    block = page_for_prompt(page)
    assert "<likely_deadlines" in block and "Lyric essay reflection due Oct 19" in block
    assert "<likely_deadlines" not in page_for_prompt(Page("https://x.test", text="hi"))

    seen = []
    m = Memory(":memory:")
    now = datetime(2026, 10, 16, 9, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    respond(m, "what's on https://wiki.test", now, "", runner=lambda s, p: seen.append(p) or {"reply": "ok", "actions": []},
            fetcher=lambda u: Page(u, title="Week 3", text=TEXT), spotter=lambda pg: ["Lyric essay reflection due Oct 19 at 11:59pm"])
    assert "<likely_deadlines" in seen[0]
