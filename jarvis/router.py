"""Decide which context a chat message needs, so JARVIS doesn't gather (or send Claude)
everything every time. One fast Jev call with yes/no questions; it leans toward including
a block, since a missing block hurts more than an extra one. Without a fast engine, or if
it fails, every block is included (the old behavior)."""

from jarvis.judge import Engine, Question

BLOCKS = {
    "deadlines": "Would answering this need his school deadlines, due dates, classes or schedule?",
    "inbox": "Would answering this need his recent email (inbox, messages from professors, mail)?",
    "activity": "Is this about what he's doing right now on his Mac (this error, this screen, his "
                "terminal, the code or page in front of him, 'this'/'that')?",
    "projects": "Is this about one of his coding/business/personal projects or repos?",
    "journal": "Does this refer to earlier days, his week so far, or things that happened before "
               "this conversation?",
}
INCLUDE_AT = 0.3   # probability of "yes" above which a block is included
ALL = frozenset(BLOCKS)


def route(message: str, recent: list[str], fast: Engine | None, on_error=None) -> frozenset[str]:
    """Return the names of the context blocks to include for this message."""
    if fast is None:
        return ALL
    state = "Recent conversation:\n" + ("\n".join(recent[-4:]) or "(none)") + f"\n\nNew message from Elijah:\n{message}"
    try:
        answers = fast(state, [Question(k, ask) for k, ask in BLOCKS.items()])
    except Exception as e:
        if on_error:
            on_error(e)
        return ALL
    picked = set()
    for key, answer in answers.items():
        p_yes = answer.confidence if answer.value else 1 - answer.confidence
        if p_yes >= INCLUDE_AT:
            picked.add(key)
    return frozenset(picked)
