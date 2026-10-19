"""Standing orders: Elijah's behaviour preferences, short and de-duplicated, pinned at the top of
every chat prompt so they steer each reply instead of sitting in a long facts list."""

from jarvis.memory import Memory

DEFAULT_ORDERS = [
    "Answer first, in as few words as do the job. Don't restate his question, don't explain unless he "
    "asks, and don't list every item or card unless he asks for the list.",
    "Say each reminder, deadline or alert once. Don't tack deadlines onto replies about something else. "
    "Once he says \"I know\", \"don't worry\" or that he's read it, drop it (and use dont_remind).",
    "If his question may be about what's on his screen and you're unsure, look (look_at_screen) rather "
    "than asking him what's there.",
    "Only ask first for high-risk things: texts, email or messages to other people, purchases, deleting, "
    "and high-risk commands. Everything else, just do it and say briefly what changed.",
    "Music means Spotify (play_music); never ask him which service, playlist or song.",
]


def standing_orders(mem: Memory) -> str:
    lines = [f"- {o}" for o in DEFAULT_ORDERS]
    seen = {o.lower() for o in DEFAULT_ORDERS}
    for f in mem.facts("preference"):
        value = " ".join(str(f["value"]).split())
        if value.lower() in seen:
            continue
        seen.add(value.lower())
        lines.append(f"- ({f['key']}) {value[:300]}")
    return "\n".join(lines)
