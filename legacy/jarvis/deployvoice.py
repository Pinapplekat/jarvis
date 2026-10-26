"""Deploying a finished self-edit by voice or text, never without his explicit words.

"Deploy it" first only gets an offer ("Ready to deploy X. Say 'yes, deploy it'."); the deploy runs
only when his very next words within a few minutes are an explicit "yes, deploy it". Anything else
drops the offer. Self-editing must be on (JARVIS_REPO in .env), and only a build that's 'ready'
(tested, waiting for Deploy) can go."""

import json
import re
import time

from jarvis.memory import Memory

OFFER_KEY = "deploy_offer"
OFFER_SECONDS = 300
YES_DEPLOY = re.compile(r"^\s*(?:(?:yes|yeah|yep|ok|okay)[,\s]+(?:please\s+)?(?:deploy|ship)(?:\s+(?:it|that|the build|"
                        r"both|them))?|(?:go ahead and|please)\s+deploy(?:\s+(?:it|that|the build))?)"
                        r"(?:[,\s]+(?:sir|jarvis|please))*\s*[.!]*\s*$", re.I)


def offer(mem: Memory, edit_id: int | None = None) -> str:
    """The deploy_build action: check there's a ready build, then ask for explicit words."""
    from jarvis import selfedit
    if not selfedit.enabled():
        raise ValueError("self-editing is paused (JARVIS_REPO is off in .env), so there's nothing to deploy")
    ready = mem.self_edits("ready")
    if edit_id is not None:
        ready = [r for r in ready if r["id"] == edit_id]
    if not ready:
        raise ValueError("no build is ready to deploy")
    if len(ready) > 1:
        raise ValueError("more than one build is ready (" + "; ".join(r["title"] for r in ready[:4])
                         + "); ask him which")
    job = ready[0]
    mem.set_kv(OFFER_KEY, json.dumps({"id": job["id"], "title": job["title"], "at": time.time()}))
    return f"Ready to deploy \"{job['title']}\", sir. Say \"yes, deploy it\" to go ahead."


def answer(mem: Memory, text: str, spawn=None) -> str | None:
    """If an offer is open: his explicit yes deploys; anything else withdraws it. None = no offer."""
    raw = mem.get_kv(OFFER_KEY)
    if not raw:
        return None
    mem.set_kv(OFFER_KEY, "")  # one answer only
    try:
        offered = json.loads(raw)
    except ValueError:
        return None
    if time.time() - offered.get("at", 0) > OFFER_SECONDS or not YES_DEPLOY.match(text or ""):
        return None  # not a clear yes: the offer lapses and this is ordinary chat
    from jarvis import selfedit
    job = mem.get_self_edit(int(offered["id"]))
    if not selfedit.enabled() or not job or job["status"] != "ready":
        return f"Can't deploy that now, sir ({'self-editing is paused' if not selfedit.enabled() else 'it is no longer ready'})."
    (spawn or selfedit.spawn)("deploy", job["id"])
    mem.log("self_edit", f"deploy started by his spoken/typed yes: {job['title'][:100]}")
    return f"Deploying \"{job['title']}\", sir. I'll tell you when it's live."
