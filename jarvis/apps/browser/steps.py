"""The steps of `act`, in words: parsed once, checked against the page, and the same text is what Elijah is asked.

    fill "Email" with "me@example.com"      press "Submit"      select "Country" as "Canada"      check "I agree"

A field or button is found by the label or text you can see on the page, never by a selector. A step that doesn't match
exactly runs nothing; passwords, one-time codes and card details are never typed."""

from __future__ import annotations

import re
from dataclasses import dataclass

from jarvis.contract.api import ActionError

_Q = r'"((?:[^"\\]|\\.)*)"'
GRAMMAR = {
    "fill": re.compile(rf"^fill {_Q} with {_Q}$", re.S),
    "press": re.compile(rf"^press {_Q}$"),
    "select": re.compile(rf"^select {_Q} as {_Q}$"),
    "check": re.compile(rf"^check {_Q}$"),
}
FORMS = 'fill "Field" with "text"; press "Button"; select "Field" as "Option"; check "Box"'
MAX_STEPS = 12
SECRET_KINDS = {"password", "file"}
SECRET_AUTOCOMPLETE = re.compile(r"password|one-time-code|^cc-")
SECRET_WORDS = re.compile(r"pass(word|code|phrase)|one[- ]time|verification code|security code|\bcvv\b|\bcvc\b|"
                          r"card number|\bssn\b|social security|\bpin\b", re.I)


@dataclass(frozen=True)
class Step:
    verb: str  # fill | press | select | check
    target: str  # the visible label or text
    value: str = ""  # the text to type, or the option to choose

    def words(self) -> str:
        return {"fill": f'fill "{self.target}" with "{self.value}"', "press": f'press "{self.target}"',
                "select": f'select "{self.target}" as "{self.value}"', "check": f'check "{self.target}"'}[self.verb]


@dataclass(frozen=True)
class Control:
    """One thing on the page that can be filled or pressed, as the browser describes it."""

    index: int
    kind: str  # text | password | checkbox | radio | select | button | file
    label: str  # what is visible: its label, placeholder or text
    options: tuple[str, ...] = ()
    autocomplete: str = ""
    name: str = ""
    disabled: bool = False


def _unquote(text: str) -> str:
    return re.sub(r"\\(.)", r"\1", text)


def parse(steps: list[str]) -> list[Step]:
    if not steps:
        raise ActionError("There are no steps to run.")
    if len(steps) > MAX_STEPS:
        raise ActionError(f"That's {len(steps)} steps; a form is at most {MAX_STEPS} in one go.")
    out = []
    for text in steps:
        said = str(text).strip()
        for verb, pattern in GRAMMAR.items():
            if said.startswith(verb + " ") and (m := pattern.match(said)):
                out.append(Step(verb, *map(_unquote, m.groups())))
                break
        else:
            raise ActionError(f"I can't read the step {text!r}. Steps look like: {FORMS}.")
    for earlier in out[:-1]:
        if earlier.verb == "press":
            raise ActionError("A press has to be the last step: it can change the page, and the steps after it "
                              "were never checked against what comes next.")
    return out


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[*:]+\s*$", "", text.strip()).lower().split())


def refused_control(c: Control) -> str | None:
    if c.kind in SECRET_KINDS or SECRET_AUTOCOMPLETE.search(c.autocomplete) \
            or SECRET_WORDS.search(f"{c.label} {c.name}"):
        return (f"“{c.label or c.name}” is a password, code, card or file field. Those are always yours to fill in "
                "yourself; I never type them")
    return None


def _describe(controls: list[Control]) -> str:
    seen = [f'{c.kind} "{c.label}"' for c in controls if c.label and not c.disabled and c.kind not in SECRET_KINDS]
    return "; ".join(seen[:25]) or "nothing I can fill or press"


def resolve(steps: list[Step], controls: list[Control]) -> list[tuple[Step, Control]]:
    """Match every step to exactly one control, or raise and say what the page has instead. Nothing runs here."""
    kinds = {"fill": {"text", "password"}, "press": {"button"}, "select": {"select"}, "check": {"checkbox", "radio"}}
    out = []
    for step in steps:
        wanted = _norm(step.target)
        hits = [c for c in controls if c.kind in kinds[step.verb] and wanted and _norm(c.label) == wanted]
        if not hits:
            raise ActionError(f"Nothing ran. The page has no {step.verb} target “{step.target}”. "
                              f"It has: {_describe(controls)}.")
        if len(hits) > 1:
            raise ActionError(f"Nothing ran. “{step.target}” matches {len(hits)} things on the page; "
                              "I won't guess which one.")
        control = hits[0]
        if (why := refused_control(control)):
            raise ActionError(f"Nothing ran. {why}.")
        if control.disabled:
            raise ActionError(f"Nothing ran. “{step.target}” is switched off on the page.")
        if step.verb == "select" and step.value not in control.options:
            raise ActionError(f"Nothing ran. “{step.target}” has no option “{step.value}”. "
                              f"Its options: {', '.join(control.options[:20]) or 'none'}.")
        out.append((step, control))
    return out
