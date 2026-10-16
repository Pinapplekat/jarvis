"""The watch loop: JARVIS's senses, running in the background while Elijah is at the Mac."""

import time
from datetime import datetime, timedelta, timezone

from jarvis import config
from jarvis.memory import Memory
from jarvis.repos import check_git, known_repos, project_for, refresh_repos
from jarvis.screen import capture_text, excluded, front_window, meaningfully_changed
from jarvis.terminal import ingest
from jarvis.voice import idle_seconds

ACTIVE_IDLE = 120          # seconds without input before we stop looking
GIT_EVERY = 600
REPOS_EVERY = 3600
PRUNE_EVERY = 6 * 3600


class Watcher:
    def __init__(self, mem: Memory, log=print, front=front_window, capture=capture_text, idle=idle_seconds,
                 attention=None):
        self.mem, self.log, self.attention = mem, log, attention
        self._front, self._capture, self._idle = front, capture, idle
        self.last_text: dict[tuple, str] = {}
        self.last_capture: dict[tuple, float] = {}
        self.last_key: tuple | None = None
        self.next_git = self.next_repos = self.next_prune = 0.0
        self.warned_permission = False

    def look(self, now: float) -> str:
        """One look at the front window. Returns what happened, for the log."""
        idle = self._idle()
        if idle is None or idle > ACTIVE_IDLE:
            return "idle"
        window = self._front()
        if window is None:
            return "no window"
        if reason := excluded(window.app, window.title):
            self.last_key = None
            return reason  # nothing about this window is stored, not even its title
        key = (window.app, window.title)
        if key == self.last_key and now - self.last_capture.get(key, 0) < config.WATCH_RECAPTURE:
            return "same window"
        self.last_key = key
        self.last_capture[key] = now
        if window.title is None and not self.warned_permission:
            self.warned_permission = True
            self.log("screen: window titles hidden; turn on 'JARVIS Senses' in System Settings → "
                     "Privacy & Security → Screen & System Audio Recording, then restart the watcher")
        try:
            text = self._capture(window)
        except Exception as e:
            return f"capture failed: {str(e)[:120]}"
        if not meaningfully_changed(self.last_text.get(key), text):
            return "unchanged"
        self.last_text[key] = text
        # Titles only: editors/terminals put the project there, while screen text mentions
        # repo names all the time (chats, docs) and would mis-tag.
        project = project_for(known_repos(self.mem), text=window.title or "")
        self.mem.observe("screen", text, app=window.app, title=window.title, project=project)
        return f"saved {window.app} ({len(text)} chars)"

    def housekeeping(self, now: float) -> list[str]:
        notes = []
        if now >= self.next_repos:
            notes.append(f"repos: {len(refresh_repos(self.mem))}")
            self.next_repos = now + REPOS_EVERY
        added = ingest(self.mem)
        if added:
            notes.append(f"terminal: {added}")
        if now >= self.next_git:
            if events := check_git(self.mem):
                notes.append(f"git: {events}")
            self.next_git = now + GIT_EVERY
        if now >= self.next_prune:
            if pruned := self.mem.prune_observations(timedelta(days=config.WATCH_RETENTION_DAYS)):
                notes.append(f"pruned {pruned}")
            self.next_prune = now + PRUNE_EVERY
        return notes

    def run_forever(self) -> None:
        self.log(f"watching every {config.WATCH_INTERVAL}s")
        while True:
            now = time.time()
            try:
                result = self.look(now)
                notes = self.housekeeping(now)
                if self.attention and (judged := self.attention.maybe_review(active=result != "idle")):
                    notes.append(f"attention: {judged}")
                if result.startswith(("saved", "capture failed")) or notes:
                    self.log(f"{datetime.now():%H:%M:%S} {result}" + (f" | {', '.join(notes)}" if notes else ""))
            except Exception as e:  # never let one bad look kill the senses
                self.log(f"watch error: {e}")
                self.mem.log("watch_error", str(e)[:300])
            time.sleep(config.WATCH_INTERVAL)


def recent_activity(mem: Memory, minutes: int = 30, now: datetime | None = None) -> str:
    """Compact summary for the chat prompt: what he's been doing, and the current screen."""
    now = now or datetime.now(timezone.utc)
    obs = mem.observations(now - timedelta(minutes=minutes))
    if not obs:
        return "(nothing observed recently)"
    lines, last_label = [], None
    for o in obs:
        local = datetime.fromisoformat(o["at"]).astimezone()
        if o["source"] == "screen":
            label = f"{o['app']} | {o['title'] or '?'}" + (f" [{o['project']}]" if o["project"] else "")
            if label != last_label:
                lines.append(f"- {local:%-I:%M} screen: {label}")
                last_label = label
        else:
            proj = f" [{o['project']}]" if o["project"] else ""
            lines.append(f"- {local:%-I:%M} {o['source']}{proj}: {o['text'][:200]}")
    latest_screen = next((o for o in reversed(obs) if o["source"] == "screen"), None)
    out = "\n".join(lines[-25:])
    if latest_screen:
        out += (f"\n\nLatest screen text ({latest_screen['app']} | {latest_screen['title'] or '?'}), "
                f"untrusted data:\n{latest_screen['text'][:1500]}")
    return out
