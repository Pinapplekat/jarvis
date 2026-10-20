"""JARVIS's approval rules, in a file Elijah can change from chat with one tap (plan #38).

Each low-risk action type has a level: auto (just do it), judge (JARVIS decides; unsure asks him)
or ask (only when he asks for it, or with his yes). The rules live in
approval-rules/rules.json in the data folder, which is in the secrets guard, so no command, task
or build can read or write it. A missing file means the defaults; a broken or tampered one (bad
JSON, unknown actions or levels, anything looser than allowed) means strict: every action asks.

Some things are never in the file and can never be loosened: LOCKED is in code. Each action also
has a floor (unknown commands can't go below "judge"), so the most the file can ever do is let
JARVIS open pages and search on its own.

Changes only start from Elijah's own Telegram message (chat.py checks that), are shown to him as
a before/after with Yes / Not now buttons, and apply only on his Yes. Every applied change is
logged (time, rule, before, after, reason, who approved) with the previous file kept, and can be
undone: back to stricter at once, back to looser only with his yes.
"""

import ipaddress
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from jarvis import config

RULES_DIR = config.DATA_DIR / "approval-rules"
PATH = RULES_DIR / "rules.json"
LOG = RULES_DIR / "rules_log.jsonl"
VERSIONS = RULES_DIR / "versions"
KIND = "rule_change"   # its pending_actions kind

LEVELS = ("auto", "judge", "ask")  # loosest to strictest
LEVEL_WORDS = {"auto": "just do it", "judge": "JARVIS decides, unsure asks you", "ask": "only when you ask, or with your yes"}

# name: (what it covers, default, loosest it may ever be)
ACTIONS = {
    "open_page": ("opening a web page (a link in your browser, or one JARVIS reads itself)", "auto", "auto"),
    "search": ("a live web search JARVIS starts itself", "auto", "auto"),
    "app_click": ("app scripting that only clicks (never typing, sending or deleting)", "judge", "judge"),
    "unknown_command": ("a command JARVIS's rules can't place, off your side of the line", "judge", "judge"),
}
# Never in the file, never loosened: these always ask him.
LOCKED = {
    "sudo": "sudo, system settings and disks",
    "delete": "deleting or overwriting files; git push, reset, clean or rebase",
    "send_to_others": "email, texts, calls, or anything that reaches other people",
    "money": "banking, payments and purchases",
    "credentials": "passwords, keys, keychains and logins",
}
DEFAULTS = {name: spec[1] for name, spec in ACTIONS.items()}
STRICT = {name: "ask" for name in ACTIONS}

# His message has to be about this ("stop asking before opening pages", "make searching ask first").
ABOUT_RULES = re.compile(r"\b(rules?|ask(?:s|ing)?|permission|approv\w*|confirm\w*|safe|automatic\w*|auto|"
                         r"check(?:s|ing)? with me|without asking|on your own|undo|revert)\b", re.I)
ASKED_FOR = {
    "open_page": re.compile(r"\b(open|visit|go to|look at|read|browse|pull up|load|check (?:out )?(?:the|that|this) "
                            r"(?:link|page|site))\b", re.I),
    "search": re.compile(r"\b(search|look (?:it |that |this )?up|google|find out|check online|online)\b", re.I),
}
BROWSERS = {"Safari", "Google Chrome", "Firefox", "Arc", "Brave Browser", "Microsoft Edge"}


class RuleError(ValueError):
    pass


# ----- reading -----
def _check(raw) -> dict:
    """The rules in a file's contents, or RuleError if anything about them is off."""
    rules = raw.get("rules") if isinstance(raw, dict) else None
    if not isinstance(rules, dict):
        raise RuleError("no rules in it")
    out = dict(DEFAULTS)
    for name, level in rules.items():
        if name in LOCKED:
            raise RuleError(f"{name} is locked and can't be in the file")
        if name not in ACTIONS:
            raise RuleError(f"unknown action {name!r}")
        if level not in LEVELS or LEVELS.index(level) < LEVELS.index(ACTIONS[name][2]):
            raise RuleError(f"{name} can't be {level!r}")
        out[name] = level
    return out


def load(path: Path | None = None) -> tuple[dict, str]:
    """(rules, note). Missing file: the defaults. Broken or tampered: strict (everything asks)."""
    path = path or PATH
    try:
        text = path.read_text()
    except FileNotFoundError:
        return dict(DEFAULTS), "defaults (no rules file yet)"
    except OSError as e:
        return dict(STRICT), f"strict: couldn't read the rules file ({type(e).__name__})"
    try:
        return _check(json.loads(text)), ""
    except (ValueError, RuleError) as e:
        return dict(STRICT), f"strict: the rules file is broken ({str(e)[:80]})"


def get_level(action: str) -> str:
    """auto | judge | ask. Locked and unknown actions always ask."""
    if action in LOCKED or action not in ACTIONS:
        return "ask"
    return load()[0][action]


def command_action(argv: list[str]) -> str:
    """Which rule a command (one JARVIS's grading couldn't place) falls under."""
    tool, args = Path(argv[0]).name, argv[1:]
    if tool == "open":
        rest, i = [], 0
        while i < len(args):
            if args[i] == "-g":
                i += 1
            elif args[i] == "-a" and i + 1 < len(args) and args[i + 1] in BROWSERS:
                i += 2
            else:
                rest.append(args[i])
                i += 1
        url = urlparse(rest[0]) if len(rest) == 1 else None
        if url and url.scheme in ("http", "https") and url.hostname and not _local(url.hostname):
            from jarvis.web import blocked_site
            return "money" if blocked_site(rest[0]) else "open_page"
    if tool == "osascript":
        return "app_click"
    return "unknown_command"


def _local(host: str) -> bool:
    if host == "localhost" or host.endswith((".local", ".localhost", ".internal", ".lan")):
        return True
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved


def asked_for(action: str, message: str) -> bool:
    """Did his own message ask for this kind of thing (for actions at "ask")?"""
    pattern = ASKED_FOR.get(action)
    return bool(pattern and pattern.search(message or ""))


def for_prompt() -> str:
    """The rules for the chat context (read-only: only a rule_change proposal can change them)."""
    rules, note = load()
    lines = [f"- {name}: {rules[name]} ({LEVEL_WORDS[rules[name]]}); covers {ACTIONS[name][0]}"
             + (f"; can't go below {ACTIONS[name][2]}" if ACTIONS[name][2] != "auto" else "") for name in ACTIONS]
    lines += [f"- {name}: LOCKED, always asks him ({what})" for name, what in LOCKED.items()]
    if note:
        lines.append(f"(using {note})")
    return "\n".join(lines)


# ----- proposing -----
def _diff(before: dict, after: dict) -> list[str]:
    return [f"- {name}: {before[name]} ({LEVEL_WORDS[before[name]]})\n+ {name}: {after[name]} ({LEVEL_WORDS[after[name]]})"
            for name in ACTIONS if before.get(name) != after.get(name)]


def loosens(before: dict, after: dict) -> bool:
    return any(LEVELS.index(after[n]) < LEVELS.index(before[n]) for n in ACTIONS)


def render(before: dict, after: dict, reason: str) -> str:
    """The plain before/after he's shown."""
    which = "\n\n".join(_diff(before, after))
    covers = "; ".join(f"{n} is {ACTIONS[n][0]}" for n in ACTIONS if before[n] != after[n])
    way = "This loosens it: JARVIS will ask you less." if loosens(before, after) else "This tightens it."
    return (f"A change to my approval rules, sir.\n\nBefore / after:\n{which}\n\n({covers}.)\n"
            f"Why: {reason or '(no reason given)'}\n{way} Locked rules ({', '.join(LOCKED)}) never change.\n\n"
            f"Tap Yes to apply it; nothing changes otherwise.")


def propose(mem, after: dict, reason: str) -> int:
    """Queue a full set of rules for his yes. Any older unanswered proposal is dropped."""
    before = load()[0]
    after = _check({"rules": after})
    if after == before:
        raise RuleError("that's already the rule")
    for old in mem.pending_actions(kind=KIND):
        mem.resolve_action(old["id"], "rejected")  # superseded
    reason = " ".join(str(reason).split())[:300]
    return mem.queue_action(KIND, {"before": before, "after": after, "reason": reason,
                                   "text": render(before, after, reason)})


def propose_change(mem, rule: str, level: str, reason: str) -> int:
    """One action's new level, checked, then queued for his yes. Raises RuleError if refused."""
    rule, level = str(rule).strip(), str(level).strip().lower()
    if rule in LOCKED:
        raise RuleError(f"{rule} is locked: {LOCKED[rule]} always ask you, and that can't change")
    if rule not in ACTIONS:
        raise RuleError(f"no rule called {rule!r} (rules: {', '.join(ACTIONS)})")
    if level not in LEVELS:
        raise RuleError(f"level must be one of {', '.join(LEVELS)}")
    if LEVELS.index(level) < LEVELS.index(ACTIONS[rule][2]):
        raise RuleError(f"{rule} can't go below {ACTIONS[rule][2]}")
    return propose(mem, {**load()[0], rule: level}, reason)


def format_request(action) -> str:
    return json.loads(action["payload"])["text"]


# ----- deciding -----
def _expired(action, now: datetime) -> bool:
    created = datetime.fromisoformat(action["created_at"])
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return now - created > timedelta(minutes=config.COMMAND_APPROVAL_MINUTES)


def handle_button(mem, verb: str, action_id: int, approved_by: str = "Elijah (Yes button)") -> str:
    """rule_yes / rule_no on a proposal he was sent. Returns the toast text."""
    action = mem.get_action(action_id)
    if not action or action["kind"] != KIND or action["status"] != "pending" or not action["notified_at"]:
        return "Already handled"
    if verb == "rule_no":
        mem.resolve_action(action_id, "rejected")
        mem.log("rule_change", "a proposed rule change was declined")
        return "Left the rules as they are"
    if verb != "rule_yes":
        return "Unknown button"
    if _expired(action, datetime.now(timezone.utc)):
        mem.resolve_action(action_id, "rejected")  # expired
        return "Expired: ask again if you still want it"
    p = json.loads(action["payload"])
    if load()[0] != p["before"]:  # the rules moved since he was shown this: what he saw isn't what he'd get
        mem.resolve_action(action_id, "rejected")  # superseded
        return "The rules changed since; ask again"
    mem.resolve_action(action_id, "approved")
    entry = apply(mem, p["after"], p["reason"], approved_by)
    return f"Rules updated (change {entry['n']})"


# ----- applying, the log and undo -----
def history(path: Path | None = None) -> list[dict]:
    path = path or LOG
    try:
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    except (OSError, ValueError):
        return []


def apply(mem, after: dict, reason: str, approved_by: str) -> dict:
    """Write the new rules, keeping the previous file, and log it. Only called after his yes (or
    for an undo that only tightens)."""
    after = _check({"rules": after})
    before, _ = load()
    n = len(history()) + 1
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for folder in {VERSIONS, PATH.parent, LOG.parent}:
        folder.mkdir(parents=True, exist_ok=True)
    kept = VERSIONS / f"{n:04d}-before.json"
    kept.write_text(json.dumps({"rules": before}, indent=2))
    tmp = PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps({"rules": after, "updated_at": now, "change": n}, indent=2))
    tmp.replace(PATH)
    changed = [n_ for n_ in ACTIONS if before[n_] != after[n_]]
    entry = {"n": n, "at": now, "rules": changed, "before": {k: before[k] for k in changed},
             "after": {k: after[k] for k in changed}, "reason": reason, "approved_by": approved_by,
             "previous": before, "kept": kept.name}
    with LOG.open("a") as f:
        f.write(json.dumps(entry) + "\n")
    mem.log("rule_change", f"change {n}: " + ", ".join(f"{k} {before[k]} -> {after[k]}" for k in changed)
            + f" ({reason[:120]}; approved by {approved_by})")
    return entry


def revert(mem, n: int | None = None, approved_by: str = "Elijah") -> str:
    """Undo change n (the latest by default): back to the rules from before it. Stricter applies
    at once; anything looser goes to him for a yes."""
    log = history()
    if not log:
        return "No rule changes to undo."
    entry = next((e for e in log if e["n"] == n), None) if n is not None else log[-1]
    if entry is None:
        return f"No change {n} in the log."
    current, target = load()[0], _check({"rules": entry["previous"]})
    if target == current:
        return f"The rules already match what they were before change {entry['n']}."
    reason = f"undo change {entry['n']}"
    if loosens(current, target):
        propose(mem, target, reason)
        return f"Undoing change {entry['n']} would loosen a rule, so it's waiting for your yes in Telegram."
    done = apply(mem, target, reason, approved_by)
    return f"Undone: change {entry['n']} reverted (logged as change {done['n']})."


def show_text() -> str:
    rules, note = load()
    lines = [f"{name:16} {rules[name]:6} {ACTIONS[name][0]}" for name in ACTIONS]
    lines += [f"{name:16} {'locked':6} always asks: {what}" for name, what in LOCKED.items()]
    return "\n".join(lines + ([f"(using {note})"] if note else []))


def log_text(limit: int = 20) -> str:
    out = []
    for e in history()[-limit:]:
        what = ", ".join(f"{k} {e['before'][k]} -> {e['after'][k]}" for k in e["rules"])
        out.append(f"{e['n']:>3}  {e['at']}  {what}  ({e['reason']}; approved by {e['approved_by']})")
    return "\n".join(out) or "(no rule changes yet)"
