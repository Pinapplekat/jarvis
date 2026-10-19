"""Before each chat message, decide (1) which context it needs, so JARVIS doesn't gather or
send everything every time, (2) whether it needs the heavy model (Sonnet) or the quick
one (Haiku), and (3) whether to peek at his screen first. One fast Jev call answers all three;
it leans toward including context and looking, since a missing block hurts more than an extra
one. Without Jev, every block is included, JARVIS looks, and a keyword check picks the model."""

import re
from dataclasses import dataclass

from jarvis import config
from jarvis.judge import SCREEN_ASK, Engine, Question, screen_look

BLOCKS = {
    "deadlines": "Would answering this need his school deadlines, due dates, classes or schedule?",
    "inbox": "Would answering this need his recent email (inbox, messages from professors, mail)?",
    "activity": "Is this about what he's doing right now on his Mac (this error, this screen, his "
                "terminal, the code or page in front of him, 'this'/'that')?",
    "projects": "Is this about one of his coding/business/personal projects or repos?",
    "journal": "Does this refer to earlier days, his week so far, or things that happened before "
               "this conversation?",
}
HEAVY_ASK = ("Does this need careful, substantial work: drafting or rewriting an email or document, "
             "planning, debugging code or an error, multi-step reasoning, or a long detailed answer? "
             "(Small talk, quick facts, confirmations, reminders and short questions do not.)")
HEAVY_WORDS = re.compile(
    r"\b(email|e-mail|draft|write|rewrite|plan|planning|debug|error|traceback|exception|fix|why|explain|"
    r"essay|outline|compare|research|analy[sz]e|code|bug|stack ?trace)\b|https?://", re.IGNORECASE)
INCLUDE_AT = 0.3   # probability of "yes" above which a block is included
HEAVY_AT = 0.5
ALL = frozenset(BLOCKS)


@dataclass(frozen=True)
class Route:
    blocks: frozenset[str]
    heavy: bool
    how: str  # "jev" | "keywords" (no Jev, or it failed)
    look: bool = True    # peek at his screen privately before answering (Jev's call; unsure = yes)
    look_why: str = ""


def _p_yes(answer) -> float:
    return answer.confidence if answer.value else 1 - answer.confidence


def heavy_by_keywords(message: str) -> bool:
    return len(message) > 280 or bool(HEAVY_WORDS.search(message))


def route(message: str, recent: list[str], fast: Engine | None, on_error=None) -> Route:
    forced = config.JARVIS_MODEL_ROUTING
    def finish(blocks, heavy, how, look=(True, "no judgment from Jev; looking to be safe")):
        if forced in ("fast", "heavy"):
            heavy = forced == "heavy"
        return Route(frozenset(blocks), heavy, how, *look)

    if fast is None:
        return finish(ALL, heavy_by_keywords(message), "keywords")
    state = "Recent conversation:\n" + ("\n".join(recent[-4:]) or "(none)") + f"\n\nNew message from Elijah:\n{message}"
    questions = ([Question(k, ask) for k, ask in BLOCKS.items()] + [Question("heavy", HEAVY_ASK)]
                 + [Question("look_at_screen", SCREEN_ASK)])
    try:
        answers = fast(state, questions)
    except Exception as e:
        if on_error:
            on_error(e)
        return finish(ALL, heavy_by_keywords(message), "keywords")
    blocks = {k for k in BLOCKS if _p_yes(answers[k]) >= INCLUDE_AT}
    return finish(blocks, _p_yes(answers["heavy"]) >= HEAVY_AT, "jev", screen_look(answers))
