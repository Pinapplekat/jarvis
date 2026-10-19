"""Was that meant for JARVIS? The desk mic stays open for a follow-up after each answer, so it also
hears Elijah talking to people in the room ("love you, babe"). A cheap check first: naming JARVIS
means yes; clear signs of talking to someone else mean no. Otherwise Jev decides, and only a
confident "not for JARVIS" keeps it silent. Also: is a transcript clearly unfinished ("...and",
"um"), so the turn should wait for the rest?"""

import re

NAMED = re.compile(r"\bjarvis\b", re.I)
OTHER_PERSON = re.compile(
    r"\b(?:babe|baby|honey|sweetheart|sweetie|my love|love you|emily|em|mom|dad|bro|dude|buddy)\b", re.I)
JEV_ASK = ("Elijah has just been talking with JARVIS, his AI assistant, at his desk, and the mic is still "
           "open. Is this new remark addressed to JARVIS (continuing that conversation or asking it "
           "something), rather than said to another person in the room or on the phone?")
SILENT_BELOW = 0.75  # Jev must be at least this sure it's NOT for JARVIS to stay quiet

# Ends on a word that can't end a sentence, or is only filler: he isn't done yet.
# Only words a sentence can't end on ("...where is it at?" is finished; "...and" isn't).
UNFINISHED = re.compile(
    r"(?:^|\s)(?:and|or|but|so|um+|uh+|er+|erm|like|because|cause|the|a|an|to|of|for|with|then|if|"
    r"my|your|our|their)\s*[.,…-]*\s*$|,\s*$", re.I)
FILLER_ONLY = re.compile(r"^\s*(?:um+|uh+|er+|erm|hmm+|so|and|like|okay so|well)[\s.,?!…-]*$", re.I)


def unfinished(text: str) -> bool:
    return bool(FILLER_ONLY.match(text) or UNFINISHED.search(text))


def for_jarvis(text: str, recent: list[str] = (), fast=None) -> tuple[bool, str]:
    """(meant for JARVIS?, why). Unsure means yes."""
    if NAMED.search(text):
        return True, "named JARVIS"
    if OTHER_PERSON.search(text):
        return False, "talking to someone else"
    if fast is None:
        return True, "no Jev; assuming it's for JARVIS"
    from jarvis.judge import Question
    state = "Recent conversation:\n" + ("\n".join(recent[-4:]) or "(none)") + f"\n\nNew remark heard at the desk:\n{text}"
    try:
        a = fast(state, [Question("for_jarvis", JEV_ASK)])["for_jarvis"]
    except Exception as e:
        return True, f"Jev unavailable ({str(e)[:60]})"
    if a.value is False and a.confidence >= SILENT_BELOW:
        return False, f"Jev: not for JARVIS ({a.confidence:.2f})"
    return True, f"Jev: for JARVIS ({a.confidence:.2f})"
