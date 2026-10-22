import argparse
import json
import re
import sys

from jarvis import config
from jarvis.memory import Memory


def _now_local():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo(config.TIMEZONE))


def _messenger():
    if not (config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID):
        return None
    from jarvis.inbox import Mirror
    from jarvis.telegram import TelegramClient
    return Mirror(TelegramClient())  # everything sent is also on the Mac, in the inbox app


def _caller():
    from jarvis.calls import TwilioCaller, twilio_configured
    return TwilioCaller() if twilio_configured() else None


def _deliver(mem: Memory, messenger, caller) -> tuple[int, int]:
    from jarvis.alerts import deliver_alerts, parse_quiet_hours
    return deliver_alerts(mem, messenger, caller, _now_local().replace(tzinfo=None),
                          parse_quiet_hours(config.QUIET_HOURS))


def _say_if_present(text: str) -> None:
    """Ask out loud too, when he's at the Mac (outside quiet hours)."""
    from datetime import datetime
    from jarvis.voice import should_speak, speak
    if should_speak(datetime.now()):
        speak(text)


HEARTBEAT_TOGGLE = re.compile(
    r"^\s*(?P<verb>pause|stop|mute|silence|resume|unpause|unmute|start)\s+(?:the\s+|your\s+)?heartbeat\s*[.!]*\s*$", re.I)


def _set_env(key: str, value: str) -> None:
    from pathlib import Path
    path = Path(__file__).resolve().parent.parent / ".env"
    lines = path.read_text().splitlines() if path.exists() else []
    for i, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n")


def _spotify_login() -> None:
    """Signs in to Spotify with PKCE (playback, read and like scopes) and keeps the tokens in the Keychain."""
    import secrets
    import webbrowser

    from jarvis import spotify
    if not config.SPOTIFY_CLIENT_ID:
        sys.exit("Set SPOTIFY_CLIENT_ID in .env first (developer.spotify.com, Create app)")
    verifier, state = spotify.new_verifier(), secrets.token_urlsafe(16)
    url = spotify.login_url(verifier, state)
    print(f"In the Spotify app's settings, the Redirect URI must be {config.SPOTIFY_REDIRECT_URI}\n"
          f"Opening the sign-in page (or open it yourself):\n{url}\n")
    webbrowser.open(url)
    redirected = input("After approving, paste the address the browser ended up on: ")
    try:
        stored = spotify.finish_login(redirected, verifier, state)
    except spotify.SpotifyError as e:
        sys.exit(str(e))
    print("Signed in. Tokens are in the Keychain (JARVIS Spotify)." if stored else
          "Signed in, but the Keychain wouldn't take the token, so it only lasts for this run.")
    if config.SPOTIFY_REFRESH_TOKEN:
        print("You can now remove SPOTIFY_REFRESH_TOKEN from .env.")


def _telegram_setup() -> None:
    from jarvis.telegram import TelegramClient
    tg = TelegramClient(chat_id="")
    bot = tg.me()
    # Any message to a brand-new bot is from its owner; the name is shown so you can confirm.
    raw = tg._call("getUpdates", timeout=0, allowed_updates=["message"])
    msgs = [u["message"] for u in raw
            if "message" in u and u["message"].get("chat", {}).get("type") == "private"]
    if not msgs:
        sys.exit(f"Open Telegram, find @{bot['username']}, send it any message, then rerun this.")
    sender = msgs[-1]["from"]
    chat_id = sender["id"]
    _set_env("TELEGRAM_CHAT_ID", str(chat_id))
    TelegramClient(chat_id=str(chat_id)).send("JARVIS is connected. Drafts and alerts will show up here.")
    name = " ".join(filter(None, [sender.get("first_name"), sender.get("last_name")]))
    print(f"Connected @{bot['username']} to {name} (@{sender.get('username', '-')}), "
          f"chat {chat_id}. Saved to .env. Check Telegram for a hello.")


FEED_CACHE_SECONDS = 600


def _feed(max_age: float | None = None) -> bytes:
    """The Canvas calendar feed. tick refreshes it every 5 min; chat passes max_age to reuse
    that copy instead of waiting ~1 s on the network."""
    import time
    from jarvis.canvas import fetch_feed
    cache = config.DATA_DIR / "canvas_feed.ics"
    if max_age is not None and cache.exists() and time.time() - cache.stat().st_mtime < max_age:
        return cache.read_bytes()
    data = fetch_feed(config.CANVAS_ICS_URL)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(data)
    return data


def _deadlines(mem: Memory, now, max_age: float | None = None):
    from jarvis.canvas import CanvasClient, from_feed, from_memory, merge
    if config.CANVAS_TOKEN:
        fetched = CanvasClient().fetch_deadlines(now.tzinfo, now.date())
    elif config.CANVAS_ICS_URL:
        fetched = from_feed(_feed(max_age), now.tzinfo)
    else:
        sys.exit("Set CANVAS_ICS_URL (Canvas → Calendar → Calendar Feed) or CANVAS_TOKEN in .env")
    return merge(fetched, from_memory(mem))


def _sync_canvas(mem: Memory):
    from jarvis.canvas import raise_deadline_alerts
    now = _now_local()
    deadlines = _deadlines(mem, now)
    raised = raise_deadline_alerts(mem, deadlines, now)
    due = [d for d in deadlines if d.kind == "due"]
    todo = sum(1 for d in due if d.due > now and not d.done)
    mem.log("canvas_sync", f"{len(due)} deadlines, {todo} upcoming not done, {raised} alerts")
    return deadlines, raised


def _inbox_text(mem: Memory) -> str:
    """Instant: the snapshot the background tick saves every 5 min (Mail is too slow to ask live)."""
    from jarvis.mailwatch import inbox_for_prompt, snapshot
    messages, as_of = snapshot(mem)
    return inbox_for_prompt(messages[:15], None, as_of)


def _attention(mem: Memory):
    from jarvis import judge
    from jarvis.attention import Attention
    from jarvis.voice import Announcer
    return Attention(mem, judge.engine(on_fallback=lambda e: mem.log("jev_fallback", str(e)[:200])),
                     Announcer(_messenger(), mem), fast=judge.fast_engine())


def _activity(mem: Memory) -> str:
    from jarvis.watch import recent_activity
    return recent_activity(mem, 30)


def _transcribe(audio: bytes) -> str:
    from jarvis.speech import transcribe
    return transcribe(audio)


def _warm(mem: Memory):
    """A persistent Claude session for chat (much faster per reply), or None if turned off."""
    if not config.WARM_CLAUDE:
        return None
    from jarvis.chat import REPLY_SCHEMA, SYSTEM_PROMPT, run_claude
    from jarvis.warm import WarmClaude
    def log(s: str) -> None:
        import sqlite3
        try:
            mem.log("warm_fallback", s[:200])
        except sqlite3.ProgrammingError:  # used from another thread (the HUD's): its own connection
            other = Memory(config.DB_PATH)
            try:
                other.log("warm_fallback", s[:200])
            finally:
                other.db.close()
    return WarmClaude(SYSTEM_PROMPT, REPLY_SCHEMA, fallback=run_claude, log=log)


def latency_summary(lines: list[str]) -> str:
    """Summarize 'answered' log lines: median/slowest total and time to first sentence, per model."""
    import re
    from statistics import median
    rows = []
    for line in lines:
        total = re.search(r"([\d.]+)s total", line)
        first = re.search(r"first sentence ([\d.]+)s", line)
        model = re.search(r"model (\S+) (warm|cold)", line)
        if total:
            rows.append((float(total[1]), float(first[1]) if first else None,
                         f"{model[1]} {model[2]}" if model else "unknown"))
    audio = [int(m[1]) for line in lines if (m := re.search(r"time_to_first_audio=(\d+)ms", line))]
    if not rows and not audio:
        return "No timed replies yet."
    out = [f"{len(rows)} recent replies"]
    if audio:
        out.append(f"- desk voice     n={len(audio):<3} time to first audio median {median(audio) / 1000:.1f}s, "
                   f"slowest {max(audio) / 1000:.1f}s (target 1s)")
    for key in sorted({r[2] for r in rows}):
        group = [r for r in rows if r[2] == key]
        totals = [r[0] for r in group]
        firsts = [r[1] for r in group if r[1] is not None]
        out.append(f"- {key:<14} n={len(group):<3} median {median(totals):.1f}s, slowest {max(totals):.1f}s"
                   + (f", first sentence median {median(firsts):.1f}s" if firsts else ""))
    return "\n".join(out)


def _page_spotter():
    """Jev deadline spotting for pages JARVIS opens, or None without Jev."""
    from jarvis import judge, spotting
    fast = judge.fast_engine()
    if fast is None:
        return None
    return lambda page: spotting.spot(spotting.candidates(page.text), fast, context=f"web page: {page.title}")


def chat_model_choice(mem: Memory) -> str:
    """The chat model he picked by voice or text ("use Sonnet"): haiku | sonnet | opus | auto."""
    try:
        return json.loads(mem.get_kv("chat_model") or "{}").get("model") or "auto"
    except ValueError:
        return "auto"


def set_chat_model(mem: Memory, model: str) -> str:
    """Remember his model choice (a kv setting, never .env). Returns the one-line confirmation."""
    from jarvis.router import chosen_model
    name = chosen_model(model)
    if not name:
        raise ValueError(f"unknown model {model!r} (haiku, sonnet, opus or auto)")
    mem.set_kv("chat_model", json.dumps({"model": name}))
    mem.log("chat_model", name)
    if name == "auto":
        return "Back to choosing the model myself, sir: Haiku for quick replies, Sonnet for the heavy ones."
    if name == config.JARVIS_MODEL:
        return f"Back to {name.title()}, sir."
    return f"Switched to {name.title()}, sir. Replies will be a little slower."


def _runner_for(route, warm, choice: str = "auto"):
    """Quick replies: the warm Haiku session. Heavy ones: a streamed one-off Sonnet call (not
    kept warm, to save RAM). Either falls back to a plain call if streaming fails. `choice` is the
    model he picked himself (chat_model_choice), which wins over routing."""
    from jarvis.chat import REPLY_SCHEMA, run_claude
    from jarvis.warm import SessionDied, one_off
    if choice and choice not in ("auto", config.JARVIS_MODEL):
        model = choice
    elif (choice == config.JARVIS_MODEL or not route.heavy) and warm:
        return warm.run, config.JARVIS_MODEL, "warm"
    else:
        model = config.JARVIS_HEAVY_MODEL if route.heavy and choice == "auto" else config.JARVIS_MODEL

    def run(system, prompt, on_text=None, on_actions=None):
        try:
            return one_off(model, system, REPLY_SCHEMA, prompt, on_text, on_actions)
        except SessionDied:
            return run_claude(system, prompt, model=model)
    return run, model, "cold"


def _answer(mem: Memory, text: str, on_progress=lambda: None, spoken: bool = False, warm=None,
            on_first=None, on_text=None, phone: bool = False, send_photo=None, on_draft=None,
            channel: str | None = None) -> str:
    """One chat turn with full context. Shared by Telegram (typed or voice), the desk mic and
    phone calls. `channel`: where he typed or said it, set here by each entry point ("telegram",
    "mac", "mic", "call"; rules.TRUSTED_CHANNELS), never read from the text: only those can start a rule change.
    `on_first(sentence)` fires as soon as the reply's first sentence has streamed in;
    `on_text(so_far)` gets all of the streaming reply text (the desk mic speaks it as it comes);
    `on_draft(so_far)` gets it word by word, every round, for a screen (the HUD, the inbox)."""
    from jarvis import timing
    own = timing.ensure("phone" if phone else "voice_note" if spoken else "telegram")
    try:
        return _answer_timed(mem, text, on_progress, spoken, warm, on_first, on_text, phone, send_photo, on_draft,
                             channel)
    finally:
        if own:
            timing.finish(mem)


def _answer_timed(mem: Memory, text: str, on_progress, spoken: bool, warm, on_first, on_text, phone: bool,
                  send_photo, on_draft=None, channel: str | None = None) -> str:
    import time

    from jarvis import judge, router, timing
    from jarvis.canvas import format_digest, from_memory
    from jarvis.chat import FirstSentence, respond
    from jarvis import panel
    if config.PANEL and (op := router.panel_command(text, panel.visible())):  # "close that", "bigger", "show me the screenshot"
        timing.mark("instant", 0)
        return _instant(mem, text, ("panel", op), spoken=spoken, phone=phone)
    hit = router.media_command(text) if config.SIRI else None
    if hit:  # "pause", "skip", "volume 40": done at once, no model round trip
        timing.mark("instant", 0)
        return _media_fast(mem, text, hit, spoken=spoken, phone=phone)
    from jarvis import deployvoice
    if (deployed := deployvoice.answer(mem, text)):  # his explicit "yes, deploy it" to an open offer
        mem.add_chat("user", text)
        mem.add_chat("assistant", deployed)
        return deployed
    inst = router.instant_command(text)
    if inst and (config.SIRI or inst[0] in ("time", "date", "model")):  # "play X", timers, alarms, the time: no model
        timing.mark("instant", 0)
        return _instant(mem, text, inst, spoken=spoken, phone=phone)
    from jarvis.chat import EarlyPeek, warm_vision
    now = _now_local()
    started = time.time()
    early_peek = EarlyPeek()  # the screen capture runs while Jev routes
    recent = [f"{'Elijah' if h['role'] == 'user' else 'JARVIS'}: {h['text'][:300]}" for h in mem.chat_history(4)]
    route = router.route(text, recent, judge.fast_engine(),
                         on_error=lambda e: mem.log("route_failed", str(e)[:200]))
    blocks = route.blocks
    routed = time.time() - started
    timing.mark("route", routed)
    message = (f"(on the phone, transcribed) {text}" if phone
               else f"(said out loud, transcribed) {text}" if spoken else text)
    if route.look:  # the look runs in the background while the rest of the context is gathered
        peek_wait = early_peek.look(message, (route.look, route.look_why), vision=warm_vision())
    else:
        early_peek.discard()
        mem.log("screen_peek", f"skipped: {route.look_why}")
        peek_wait = None
    runner, model, temp = _runner_for(route, warm, chat_model_choice(mem))
    first = FirstSentence(on_first) if on_first else None
    feeds = [f for f in (first.feed if first else None, on_text) if f]
    stream = (lambda so_far: [f(so_far) for f in feeds]) if feeds else None
    from jarvis import status
    deadlines_text = ""
    if "deadlines" in blocks:
        with status.doing("deadlines"):
            try:  # reuse tick's copy of the feed (≤10 min old) instead of fetching live
                deadlines = _deadlines(mem, now, max_age=FEED_CACHE_SECONDS)
            except Exception:  # feed down: answer from stored deadlines rather than not at all
                deadlines = from_memory(mem)
            deadlines_text = format_digest(deadlines, now, days=14)
    inbox = ""
    if "inbox" in blocks:
        with status.doing("inbox"):
            inbox = _inbox_text(mem)
    if ASKS_WHERE.search(text):  # his location is always in the prompt; show it being used when it matters
        status.show_action("location")
    activity = _activity(mem) if "activity" in blocks else ""
    asked = time.time()
    timing.mark("context", asked - started - routed)
    reply = respond(mem, message, now, deadlines_text, runner=runner, on_progress=on_progress,
                    inbox_text=inbox, activity_text=activity, blocks=blocks,
                    on_text=stream, on_draft=on_draft, spotter=_page_spotter(), send_photo=send_photo, idle=_idle_reader(),
                    peek_wait=peek_wait, vision=warm_vision(), judge=judge.engine(on_fallback=lambda e: mem.log("jev_fallback", str(e)[:200])),
                    channel=channel)
    total = time.time() - started
    first_at = f"{first.at - started:.1f}s" if first and first.at else "-"
    # Timings and choices only; never the message text.
    mem.log("answered", f"{total:.1f}s total, first sentence {first_at}, route {routed:.2f}s ({route.how}), "
                        f"context {asked - started - routed:.2f}s, model {model} {temp}, "
                        f"blocks: {', '.join(sorted(blocks)) or 'minimal'}")
    return reply


ASKS_WHERE = re.compile(r"\b(where (am i|i am|i'm|was i)|my location|location|near(by| me| here)|"
                        r"how far|get (home|there)|directions)\b", re.IGNORECASE)


def _addressed_check(mem: Memory):
    """For the desk mic: was a follow-up (no wake word) meant for JARVIS? Heuristics, then Jev."""
    from jarvis import judge
    from jarvis.addressee import for_jarvis

    def check(text: str) -> tuple[bool, str]:
        recent = [f"{'Elijah' if h['role'] == 'user' else 'JARVIS'}: {h['text'][:200]}" for h in mem.chat_history(4)]
        meant, why = for_jarvis(text, recent, judge.fast_engine())
        if not meant:
            mem.log("not_for_me", why)  # the reason only, never what he said
        return meant, why
    return check


def _idle_reader():
    from jarvis.voice import idle_seconds
    return idle_seconds


def _instant(mem: Memory, text: str, cmd: tuple, spoken: bool = False, phone: bool = False) -> str:
    """A whole-message command that needs no model: done now, answered from the real result."""
    from jarvis import siri
    mem.add_chat("user", f"(on the phone, transcribed) {text}" if phone
                 else f"(said out loud, transcribed) {text}" if spoken else text)
    now = _now_local()
    kind = cmd[0]
    from jarvis import status
    status.show_action({"timer": "set_timer", "alarm": "set_alarm", "panel": "panel", "play": "play_music",
                        "model": "switch_model"}.get(kind, ""))
    try:
        if kind == "time":
            reply = f"It's {now:%-I:%M %p}, sir."
        elif kind == "date":
            reply = f"It's {now:%A, %B %-d}, sir."
        elif kind == "timer":
            reply = siri.set_timer(mem, cmd[1])
        elif kind == "alarm":
            reply = siri.set_alarm(mem, siri.parse_alarm(cmd[1], now))
        elif kind == "model":
            reply = set_chat_model(mem, cmd[1])
        elif kind == "panel":
            from jarvis import panel
            op = cmd[1]
            reply = (panel.show(mem, {"kind": "image", "path": "latest"}) if op == "screenshot"
                     else panel.PANEL_OPS[op](mem))
        else:
            reply = siri.music(mem, cmd[1], "any")
    except Exception as e:  # never leave him without an answer, and never claim it worked
        what = {"timer": "set the timer", "alarm": "set the alarm", "play": "play that",
                "panel": "show that"}.get(kind, "do that")
        reply = f"Sorry, sir, I couldn't {what}: {str(e)[:150]}"
    status.end_action()
    mem.log("instant", f"{kind}: {reply[:150]}")
    mem.add_chat("assistant", reply)
    return reply


def _media_fast(mem: Memory, text: str, hit: tuple, spoken: bool = False, phone: bool = False) -> str:
    """A whole-message playback command from his chat or mic, done straight away with a one-line answer."""
    from jarvis import media
    op, value = hit
    from jarvis import status
    status.show_action("media_control")
    mem.add_chat("user", f"(on the phone, transcribed) {text}" if phone
                 else f"(said out loud, transcribed) {text}" if spoken else text)
    try:
        reply = media.media_control(op, value)
    except Exception as e:  # never leave him without an answer
        reply = f"Couldn't do that just now ({str(e)[:120]})."
    status.end_action()
    mem.log("media_control", f"{op} (fast path): {reply}")
    mem.add_chat("assistant", reply)
    return reply


def _send_rest(tg, reply: str, first: str | None = None) -> None:
    """Deliver a chat reply as new messages, never editing one already sent: after an early
    first sentence, just the rest (nothing if that was all); the whole reply if it changed
    since (a failure or a rethink); anything over Telegram's limit in several messages."""
    from jarvis.chat import remainder
    from jarvis.telegram import split_text
    rest = remainder(reply, first)
    for piece in split_text(reply if rest is None else rest):
        tg.send(piece)


def _task_worker(mem: Memory, tg):
    """Task mode: a sandboxed Claude Code session per task; Jev (or Haiku) double-checks a 'done'."""
    from jarvis import judge
    from jarvis.tasks import Worker
    return Worker(tg, judge=judge.engine(on_fallback=lambda e: mem.log("jev_fallback", str(e)[:200])),
                  log=lambda s: print(s, flush=True), say=_say_if_present)


def _carry_on(worker, tg=None):
    """After a command Elijah answered: a task waiting on it resumes; a build gets the output; a
    chat turn parked on it (jarvis.loop) carries on, right away when Telegram is at hand."""
    from jarvis import loop, tasks

    def carry_on(db, payload, action_id, outcome):
        if payload.get("build_id"):
            db.set_kv(f"agent_outcome:{action_id}", outcome[:6000])
        if tasks.requeue(db, payload, action_id, outcome) and worker:
            tasks.start_queued(db, worker)
        if not payload.get("task_id") and not payload.get("build_id"):
            loop.save_outcome(db, action_id, outcome)
            if tg:
                try:
                    loop.check_parked(db, loop.sender_for(tg))
                except Exception as e:  # the listener's next pass tries again
                    db.log("loop_resume_failed", str(e)[:200])
    return carry_on


def _spoken_confirmation(mem: Memory, text: str) -> str | None:
    """A yes/no said at the desk mic or on the phone, to whatever is waiting for his OK. Returns
    what to say back, or None if it isn't one (then it's ordinary chat)."""
    from datetime import datetime, timezone
    from jarvis import terminal
    from jarvis.approvals import confirm_by_words
    tg = _messenger()

    def on_command(verb, action_id):
        return terminal.handle_button(mem, tg, verb, action_id, background=False, on_done=_carry_on(None)) \
            if tg else "Telegram isn't set up"
    done = confirm_by_words(mem, text, datetime.now(timezone.utc), on_command)
    return done.note if done else None


def _listen(mem: Memory) -> None:
    """Stay connected to Telegram: handle Approve/Reject at once and chat with the user."""
    import time

    from jarvis import plans, selfedit
    from jarvis.approvals import handle_updates, notify_pending

    tg = _messenger()
    if not tg:
        sys.exit("Telegram isn't set up: add TELEGRAM_BOT_TOKEN to .env, then run jarvis telegram-setup")
    warm = _warm(mem)
    from jarvis import camera, tasks
    camera.sweep()  # camera stills a crash left behind
    worker = _task_worker(mem, tg)
    if (n := tasks.recover(mem)):
        print(f"requeued {n} task(s) cut off by a restart", flush=True)

    import threading
    turn = threading.Lock()  # Telegram and the HUD share the warm session: one answer at a time

    def handlers(mem, tg, source="telegram"):
        """Chat and buttons, bound to one database connection and one way of replying: Telegram's,
        or the inbox app's (its own thread, so its own connection)."""
        def on_text(update):
            from jarvis.status import set_state
            from jarvis import timing
            with turn:
                set_state(source, "working", detail="answering a voice note" if update.via_voice else "answering")
                timing.start("voice_note" if update.via_voice else "telegram")
                try:
                    _on_text(update)
                finally:
                    timing.finish(mem)
                    set_state(source, "idle")

        def _on_text(update):
            if (toggle := HEARTBEAT_TOGGLE.match(update.text or "")):
                from jarvis import heartbeat
                off = toggle["verb"].lower() in ("pause", "stop", "mute", "silence")
                heartbeat.set_paused(mem, off)
                tg.send("Heartbeat paused, sir; I'll only speak when spoken to (reminders and alerts still come)."
                        if off else "Heartbeat back on, sir.")
                return
            is_stop, stop_id = tasks.parse_stop(update.text)
            if is_stop and mem.db.execute("SELECT 1 FROM tasks WHERE status IN ('queued','running','waiting')").fetchone():
                tg.send(tasks.stop(mem, stop_id))
                for t in tasks.recent(mem, 3):  # show it as stopped in its progress message too
                    if t["status"] == "stopped" and t["message_id"]:
                        try:
                            tg.edit(t["message_id"], tasks.progress_text(t))
                        except Exception:
                            pass
                        break
                return
            tg.typing()
            sent = {}
            streams = getattr(tg, "stream", None)  # the inbox app: one message that fills in as it's written

            def first_out(sentence):  # typed chat: send the first sentence now, the rest as its own message
                if not update.via_voice:
                    sent["id"], sent["text"] = tg.send(sentence), sentence

            reply = _answer(mem, update.text, on_progress=tg.typing, spoken=update.via_voice, warm=warm,
                            on_first=None if streams else first_out, on_draft=streams, send_photo=tg.send_photo,
                            channel="telegram" if source == "telegram" else "mac" if source == "inbox" else None)
            if streams:
                tg.finish(reply)
            elif sent:  # buttons and plans below must still go out, so no early return
                _send_rest(tg, reply, sent["text"])
            elif update.via_voice:  # answer a voice note with a voice note (walks, AirPods)
                from jarvis.speech import voice_note
                tg.action("record_voice")
                try:
                    tg.send_voice(voice_note(reply), caption=reply if len(reply) <= 1000 else "")
                    if len(reply) > 1000:
                        _send_rest(tg, reply)
                except Exception as e:  # never lose the answer over audio trouble
                    mem.log("voice_note_failed", str(e)[:200])
                    _send_rest(tg, reply)
            else:
                _send_rest(tg, reply)
            notify_pending(mem, tg, say=_say_if_present)  # drafts and risky commands: the question, right away
            tasks.start_queued(mem, worker)  # a task started in this chat begins at once
            if mem.self_edits("drafting"):
                tg.typing()
                plans.draft_plans(mem, tg)

        def on_button(verb, edit_id):
            if verb.startswith("cmd_"):
                from jarvis import terminal
                return terminal.handle_button(mem, tg, verb, edit_id, on_done=_carry_on(worker, tg))
            if verb.startswith("plan_"):
                on_approved = (lambda i: selfedit.queue_approved(mem, tg, i)) if selfedit.enabled() else None
                return plans.handle_button(mem, verb, edit_id, on_approved)
            return selfedit.handle_button(mem, tg, verb, edit_id)
        return on_text, on_button

    on_text, on_button = handlers(mem, tg)
    if config.HUD:
        threading.Thread(target=_serve_hud, args=(tg, warm, turn), daemon=True).start()
    if config.INBOX:  # the Telegram chat on the Mac, buttons and all (jarvis/inbox.py)
        threading.Thread(target=_serve_inbox, args=(tg, handlers), daemon=True).start()
    if config.PANEL:  # "Show Helpers" from the menu bar, and the helpers view kept live while it's up
        from jarvis import panel
        threading.Thread(target=panel.serve_live, args=(lambda: Memory(config.DB_PATH),),
                         kwargs={"log": lambda s: print(s, flush=True)}, daemon=True).start()


    def on_location(update):
        """Stored quietly; only a fresh share (not each live update) gets a short reply."""
        if not config.LOCATION:
            return
        from jarvis import location, status
        status.show_action("location", ttl=status.MIN_SHOW)
        p = update.location
        if location.record_point(mem, p["lat"], p["lon"], p["at"], p["accuracy"], p["source"],
                                 p["live_until"]) is None:
            return
        location.place_name(mem, p["lat"], p["lon"])  # name it now, while it's fresh (cached)
        if p["first"]:
            where = location.describe_spot(mem, p["lat"], p["lon"])
            until = (f" until {p['live_until'].astimezone(_now_local().tzinfo):%-I:%M %p}"
                     if p["live_until"] else " until you turn it off" if p["source"] == "live" else "")
            tg.send(f"Following along{until}, sir ({where})." if p["source"] == "live"
                    else f"Noted, sir: {where}.")

    from jarvis import attachments, reports
    from jarvis.reminders import deliver_reminders
    from jarvis.voice import Announcer
    announcer = Announcer(tg, mem)
    from jarvis import inbox, loop
    park_send = loop.sender_for(tg, announcer, inbox.MacChat(tg) if config.INBOX else None)

    print(f"Listening as chat {config.TELEGRAM_CHAT_ID}", flush=True)
    outage = Outage()
    while True:
        try:
            from jarvis import outbox
            outbox.flush(mem, tg.send, tz=_now_local().tzinfo)  # anything held while Telegram was unreachable
            deliver_reminders(mem, announcer, _now_local())
            tasks.start_queued(mem, worker)  # tasks from the desk mic, or resumed after an expiry
            with turn:  # chat turns parked on his yes, a task or a job: carried on (or timed out) first,
                loop.check_parked(mem, park_send)  # so their reply covers the helper's report
            reports.deliver(mem, announcer.send, send_image=lambda path, caption: attachments.send_image(
                tg, path, caption, mem))  # what tasks, jobs and builds told JARVIS, in one message, with pictures
            # Short long-poll so reminders go out within ~25s of their time.
            handle_updates(mem, tg, int(config.TELEGRAM_CHAT_ID), on_text, wait=25, on_button=on_button,
                           transcribe=_transcribe, on_location=on_location)
            if (note := outage.ok()):
                print(note, flush=True)
                mem.log("listen_error", note)
        except Exception as e:  # network blips, Telegram hiccups: wait (backing off) and reconnect
            wait, log = outage.failed(str(e))
            if log:
                print(f"listen error: {e}", flush=True)
                mem.log("listen_error", str(e)[:300])
            time.sleep(wait)


def _serve_inbox(tg, handlers) -> None:
    """Buttons pressed and messages typed in the inbox app (jarvis/inbox.py), on their own thread."""
    from datetime import datetime, timezone
    from jarvis import inbox
    from jarvis.approvals import _handle, notify_pending
    from jarvis.telegram import Update
    mem = Memory(config.DB_PATH)  # SQLite connections stay on the thread that opened them
    inbox.seed(mem)

    def handle(req: dict) -> None:
        mac = inbox.MacChat(tg)
        on_text, on_button = handlers(mem, mac, source="inbox")
        now = datetime.now(timezone.utc)
        if req.get("type") == "button":
            local_id, data = int(req.get("id") or 0), str(req.get("data") or "")
            msg = inbox.open_button(local_id, data)
            if not msg:
                return  # already answered, or not a real button: nothing to do
            mac.local_id = local_id
            u = Update(update_id=0, sender_id=0, sent_at=now, callback_id="inbox", callback_data=data,
                       message_id=msg.get("tg_id"), message_text=msg["text"])
            _handle(mem, mac, u, on_text, on_button)
            inbox.resolve(local_id)  # whatever the result, its buttons are spent
        elif req.get("type") == "text" and (text := str(req.get("text") or "").strip()[:inbox.MAX_TEXT]):
            inbox.record("you", text, via="Mac")
            try:
                _handle(mem, mac, Update(update_id=0, sender_id=0, sent_at=now, text=text), on_text, on_button)
            finally:
                if mac.draft:  # cut off mid-reply: keep what streamed, but stop showing it as being written
                    inbox.update(mac.draft, inbox.read_text(mac.draft), streaming=False)
        notify_pending(mem, tg, say=None)  # anything new that needs his OK: in Telegram and here
    inbox.serve(handle, log=lambda s: print(s, flush=True))


def _serve_hud(tg, warm, turn) -> None:
    """Answer what he types into the HUD on his Mac (jarvis/hud.py), on its own thread."""
    from jarvis import hud, timing
    from jarvis.approvals import notify_pending
    from jarvis.status import set_state
    mem = Memory(config.DB_PATH)  # SQLite connections stay on the thread that opened them

    def answer(text: str, on_text) -> str:
        with turn:
            set_state("hud", "working", detail="answering")
            timing.start("hud")
            try:
                from jarvis import inbox
                inbox.record("you", text, via="HUD")
                reply = _answer(mem, f"(typed in the HUD on his Mac) {text}", warm=warm, on_draft=on_text,
                                send_photo=tg.send_photo, channel="mac")
                inbox.record("jarvis", reply, via="HUD")
            finally:
                timing.finish(mem)
                set_state("hud", "idle")
        try:
            notify_pending(mem, tg, say=_say_if_present)  # drafts and risky commands still ask in Telegram
        except Exception as e:
            mem.log("hud", f"couldn't send approvals: {str(e)[:150]}")
        return reply
    hud.serve(answer, log=lambda s: print(s, flush=True))


class Outage:
    """Backoff for the Telegram loop while the network is down: 5 s, doubling to 2 min, and only the
    first error of an outage (and every 20th) is logged, plus one line when it's back."""

    def __init__(self, first: float = 5.0, most: float = 120.0, clock=None):
        import time
        self.first, self.most, self.clock = first, most, clock or time.time
        self.failures, self.since = 0, None

    def failed(self, error: str = "") -> tuple[float, bool]:
        if self.failures == 0:
            self.since = self.clock()
        self.failures += 1
        wait = min(self.most, self.first * 2 ** (self.failures - 1))
        return wait, self.failures == 1 or self.failures % 20 == 0

    def ok(self) -> str | None:
        if not self.failures:
            return None
        note = f"Telegram back after {self.failures} failed polls over {self.clock() - self.since:.0f}s"
        self.failures, self.since = 0, None
        return note


def _phone_ready() -> bool:
    from jarvis.calls import twilio_configured
    return bool(config.PHONE_PUBLIC_URL) and twilio_configured()


def _phone() -> None:
    """Answer Elijah's calls to the Twilio number: the webhook the tunnel forwards to."""
    from jarvis import phone
    from jarvis.approvals import notify_pending
    from jarvis.speech import phone_audio

    if not _phone_ready():
        sys.exit("Set PHONE_PUBLIC_URL and the TWILIO_* / MY_PHONE_NUMBER settings in .env (README: Calling JARVIS)")
    if not config.PHONE_PUBLIC_URL.startswith("https://"):
        sys.exit("PHONE_PUBLIC_URL must be the tunnel's https:// address")
    tg = _messenger()
    log = lambda s: print(f"phone: {s}", flush=True)  # noqa: E731

    def answer(text, warm):
        mem = Memory(config.DB_PATH)  # SQLite connections stay on the thread that opened them
        try:
            if (said := _spoken_confirmation(mem, text)):
                return said
            reply = _answer(mem, text, spoken=True, phone=True, warm=warm, channel="call")
            if tg:
                notify_pending(mem, tg)  # drafts and commands asked for on the call get their buttons
            return reply
        finally:
            mem.db.close()

    def start_call():  # one warm Claude session per call, closed when it ends (RAM is tight)
        if not config.WARM_CLAUDE:
            return None
        from jarvis.chat import REPLY_SCHEMA, SYSTEM_PROMPT, run_claude
        from jarvis.warm import WarmClaude
        return WarmClaude(SYSTEM_PROMPT, REPLY_SCHEMA, fallback=run_claude, log=log)

    line = phone.PhoneLine(answer, phone_audio, config.PHONE_PUBLIC_URL, config.TWILIO_AUTH_TOKEN,
                           config.MY_PHONE_NUMBER, pin=config.PHONE_PIN,
                           send_text=tg.send if tg else None, start_call=start_call,
                           end_call=lambda warm: warm.close(), log=log)
    print(f"Answering calls from {phone.masked(config.MY_PHONE_NUMBER)} on 127.0.0.1:{config.PHONE_PORT} "
          f"(public: {config.PHONE_PUBLIC_URL}/voice/incoming, PIN {'on' if config.PHONE_PIN else 'off'})", flush=True)
    phone.serve(line, config.PHONE_PORT)


def _run_watches(mem: Memory, deadlines: list | None = None) -> str:
    """The due watches, checked cheaply; the judge only sees a watch's question after its filter matched."""
    from jarvis import judge, watches
    from jarvis.canvas import CanvasClient
    src = watches.Sources(deadlines=lambda: list(deadlines or []),  # this tick's sync; none if it failed
                          announcements=lambda: CanvasClient().fetch_announcements() if config.CANVAS_TOKEN else [])
    return watches.run_due(mem, _now_local(), src, judge=judge.fast_engine())


def _tick(mem: Memory) -> int:
    """One scheduled pass. Each step runs even if an earlier one fails."""
    from datetime import datetime

    from jarvis.approvals import notify_pending
    from jarvis.mailrelay import process_approved
    from jarvis.reminders import deliver_reminders

    from jarvis.voice import Announcer

    tg, caller = _messenger(), _caller()
    announcer = Announcer(tg, mem) if tg else None  # alerts and reminders are also spoken
    report, errors = [], 0

    def step(name, fn):
        nonlocal errors
        try:
            report.append(f"{name}: {fn()}")
        except Exception as e:  # keep going; one broken integration shouldn't stop the rest
            errors += 1
            report.append(f"{name}: ERROR {e}")
            mem.log("tick_error", f"{name}: {e}")

    deadlines = []
    if config.CANVAS_TOKEN or config.CANVAS_ICS_URL:
        def canvas():
            found, raised = _sync_canvas(mem)
            deadlines.extend(found)
            return f"{raised} new alerts"
        step("canvas", canvas)
    from jarvis import jobs

    def watch_jobs():
        raised, pruned = jobs.check_jobs(mem), jobs.prune()
        return f"{raised} pings" + (f", pruned {pruned}" if pruned else "")
    step("jobs", watch_jobs)  # before reporting, so a finished job is mentioned this tick
    if tg:
        from jarvis import reports
        from jarvis import attachments
        step("reports", lambda: f"passed on {reports.deliver(mem, announcer.send, send_image=lambda p, c: attachments.send_image(tg, p, c, mem))}")  # backup for the listener
    if config.LOCATION:
        from jarvis import location
        from jarvis.alerts import in_quiet_hours, parse_quiet_hours

        def where():
            now = _now_local()
            quiet = in_quiet_hours(now.time().replace(tzinfo=None), parse_quiet_hours(config.QUIET_HOURS))
            ask = (lambda text: (tg.send(text), mem.add_chat("assistant", text))) if tg and not quiet else None
            learned = location.learn(mem, now, ask=ask)
            lapse = location.check_sharing(mem, now, quiet)
            if lapse and tg:
                tg.send(lapse)
                mem.add_chat("assistant", lapse)
            pruned = location.prune(mem)
            return learned + (", sharing lapsed" if lapse else "") + (f", pruned {pruned}" if pruned else "")
        step("location", where)
    if config.MAIL_WATCH and config.MAIL_ACCOUNT:
        from jarvis.mailwatch import check_new_mail
        from jarvis import judge as _judge
        step("mail-watch", lambda: check_new_mail(mem, _now_local(), fast=_judge.fast_engine()))  # before alerts
    if config.WATCHES:
        step("watches", lambda: _run_watches(mem, deadlines))  # after mail and location, before alerts and drafts
    if config.HEARTBEAT:
        from jarvis import heartbeat
        from jarvis.alerts import in_quiet_hours as _quiet, parse_quiet_hours as _hours

        def think():
            now = _now_local()
            quiet = _quiet(now.time().replace(tzinfo=None), _hours(config.QUIET_HOURS))
            from jarvis.canvas import from_memory
            from jarvis import judge as _judge_mod
            return heartbeat.run_heartbeat(mem, now, deadlines or from_memory(mem), quiet=quiet,
                                           fast=_judge_mod.fast_engine())
        step("heartbeat", think)  # after Canvas and mail, before alerts: a message goes out this tick
    if tg or caller:
        step("alerts", lambda: "messaged %d, called %d" % _deliver(mem, announcer, caller))
    from jarvis.tasks import requeue
    from jarvis.terminal import expire_commands

    def lapse(action_id, payload):  # a task waiting on it carries on (the listener picks it up)
        requeue(mem, payload, action_id, "(the approval request expired before Elijah answered)")
    step("commands", lambda: f"expired {expire_commands(mem, on_expired=lapse)}")  # before any new buttons go out
    from jarvis import siri
    step("texts-calls", lambda: f"expired {siri.expire(mem)}, sent {siri.process_approved(mem)}")  # backstop: a yes runs at once
    if tg:
        # Incoming messages and button presses are handled by `jarvis listen`, not here:
        # Telegram allows only one reader of updates at a time.
        step("drafts", lambda: f"sent {notify_pending(mem, tg, say=_say_if_present)}")
        from jarvis import selfedit
        if selfedit.enabled():
            step("build", lambda: selfedit.check_health(mem, tg))
        step("reminders", lambda: f"sent {deliver_reminders(mem, announcer, _now_local())}")  # backup for listener
        from jarvis import plans

        def ideas():
            plans.settle_combined(mem)
            new = plans.maybe_propose_idea(mem, _now_local())
            plans.draft_plans(mem, tg)
            return f"new idea #{new}" if new else "none"
        step("ideas", ideas)
    else:
        report.append("telegram: skipped (not set up)")

    def relay():
        sent, failed = process_approved(mem)
        if failed and tg:
            tg.send(f"⚠️ {failed} approved email(s) failed to send. Run: jarvis schedule status")
        if failed:
            raise RuntimeError(f"sent {sent}, {failed} failed")
        return f"sent {sent}"
    step("mail", relay)

    from jarvis import journal, rhythm
    from jarvis.canvas import format_digest, from_memory
    from jarvis.voice import user_present

    def check_in():
        now = _now_local()
        upcoming = format_digest(deadlines or from_memory(mem), now, days=2)
        return rhythm.maybe_check_in(mem, now, announcer or Announcer(None, mem), user_present(),
                                     upcoming) or "none due"
    step("rhythm", check_in)
    step("journal", lambda: journal.maybe_write(mem, _now_local()) or "not due")

    print(f"{datetime.now():%Y-%m-%d %H:%M} " + " | ".join(report), flush=True)
    return 1 if errors else 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="jarvis")
    sub = parser.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("remember", help="Store a fact")
    r.add_argument("category", choices=["school", "work", "money", "personal", "preference"])
    r.add_argument("key")
    r.add_argument("value")

    f = sub.add_parser("forget", help="Delete a fact")
    f.add_argument("category")
    f.add_argument("key")

    p = sub.add_parser("project", help="Add or update a project")
    p.add_argument("name")
    p.add_argument("kind", choices=["business", "personal", "school"])
    p.add_argument("--status")
    p.add_argument("--summary")
    p.add_argument("--goals")

    n = sub.add_parser("note", help="Log something that happened")
    n.add_argument("content")
    n.add_argument("--project")

    sub.add_parser("context", help="Print everything JARVIS knows")

    g = sub.add_parser("graph-test", help="Check what the school Outlook account allows")
    g.add_argument("--send", action="store_true", help="Also send a test email to yourself")

    sub.add_parser("mail-accounts", help="List accounts signed in to Apple Mail")

    i = sub.add_parser("inbox", help="Show recent messages from Apple Mail")
    i.add_argument("--account", default=config.MAIL_ACCOUNT)
    i.add_argument("--limit", type=int, default=10)

    d = sub.add_parser("draft", help="Queue an email for approval")
    d.add_argument("--to", required=True, action="append", help="Repeat for multiple recipients")
    d.add_argument("--subject", required=True)
    d.add_argument("--body", required=True)
    d.add_argument("--from", dest="sender", default=config.MAIL_ACCOUNT)

    sub.add_parser("queue", help="Show emails waiting for approval")
    for name in ("approve", "reject"):
        a = sub.add_parser(name, help=f"{name.capitalize()} a queued email")
        a.add_argument("id", type=int)

    sub.add_parser("relay", help="Send all approved emails through Apple Mail")
    sub.add_parser("telegram-setup", help="Link your Telegram chat (after pressing Start on the bot)")
    pl = sub.add_parser("plans", help="Improvement plans JARVIS proposed (approved ones are built in Claude Code)")
    pl.add_argument("action", nargs="?", choices=["list", "show", "done"], default="list")
    pl.add_argument("id", nargs="?")
    ru = sub.add_parser("rules", help="JARVIS's approval rules: show them, the change log, or undo a change")
    ru.add_argument("action", nargs="?", choices=["show", "log", "revert"], default="show")
    ru.add_argument("n", nargs="?", help="for revert: the change number (default: the latest)")
    se = sub.add_parser("self-edit", help="Background worker for applying approved plans (off unless JARVIS_REPO is set)")
    se.add_argument("action", choices=["build", "deploy"])
    se.add_argument("id", nargs="?")
    cm = sub.add_parser("cmd", help="Terminal commands JARVIS runs: audit log, or how a command would be treated")
    cm.add_argument("action", choices=["log", "try"])
    cm.add_argument("command", nargs="?", default="", help='for try: the command, quoted ("git log -5")')
    cm.add_argument("-n", type=int, default=20)
    jb = sub.add_parser("jobs", help="Background jobs (approved long commands): list, show one's log, or kill one")
    jb.add_argument("action", nargs="?", default="list", choices=["list", "show", "kill"])
    jb.add_argument("id", nargs="?", type=int)
    jb.add_argument("-n", type=int, default=40, help="for show: log lines")
    hb = sub.add_parser("heartbeat", help="JARVIS's background check-ins: run one now, status, or turn off/on")
    hb.add_argument("action", nargs="?", default="status", choices=["run", "status", "off", "on"])
    bd = sub.add_parser("build", help="The self-edit build session (jarvis-build): start, status, or stop")
    bd.add_argument("action", nargs="?", default="status", choices=["start", "status", "stop"])
    br = sub.add_parser("browser", help="JARVIS's browser profile: log in to a site yourself, list sites, forget one, reset")
    br.add_argument("action", nargs="?", default="sites", choices=["login", "sites", "forget", "reset"])
    br.add_argument("site", nargs="?", help="for login/forget: e.g. canvas.seattlecolleges.edu")
    hp = sub.add_parser("helpers", help="What JARVIS sees of its helpers: build, Claude Code sessions on this Mac")
    hp.add_argument("which", nargs="?", default="", help="words from a session's title for detail")
    hp.add_argument("--panel", action="store_true", help="put every helper on the panel, kept live")
    hp.add_argument("--only", default="all", choices=["all", "code", "commands", "tasks"], help="with --panel: which tab")
    si = sub.add_parser("send-image", help="Send an image to your Telegram (same folder checks as JARVIS uses)")
    si.add_argument("path")
    si.add_argument("--caption", default="")
    tk = sub.add_parser("tasks", help="Task mode: list tasks, show one's steps, or stop one")
    tk.add_argument("action", nargs="?", default="list", choices=["list", "show", "stop"])
    tk.add_argument("id", nargs="?", type=int)
    lo = sub.add_parser("location", help="Where your phone was last seen (Telegram location shares)")
    lo.add_argument("action", nargs="?", default="show", choices=["show", "history", "places", "label", "forget"])
    lo.add_argument("id", nargs="?", type=int, help="for label: the place number")
    lo.add_argument("name", nargs="?", help="for label: what it is (school, work, home...)")
    lo.add_argument("--hours", type=int, default=24, help="for history")
    lt = sub.add_parser("latency", help="How fast recent chat replies were, by model")
    lt.add_argument("--last", type=int, default=30)
    sub.add_parser("ears", help="Listen for 'Hey Jarvis' on the Mac's mic and answer out loud (runs forever)")
    sub.add_parser("listen", help="Stay connected to Telegram: chat and approval buttons (runs forever)")
    ask = sub.add_parser("ask", help="Ask JARVIS something from the terminal (same brain as Telegram)")
    ask.add_argument("message")
    se = sub.add_parser("search", help="Search the web through the search API (counts toward the daily cap)")
    se.add_argument("query")
    se.add_argument("-n", type=int, default=5)
    sub.add_parser("call-test", help="Call yourself once to check Twilio voice")
    ph = sub.add_parser("phone", help="Answer your calls to the Twilio number (runs forever), or point the number here")
    ph.add_argument("action", nargs="?", default="serve", choices=["serve", "setup"])
    pj = sub.add_parser("projects", help="Project briefs built from your repos and GitHub")
    pj.add_argument("action", nargs="?", default="list", choices=["list", "show", "refresh", "sync"])
    pj.add_argument("name", nargs="?")
    jn = sub.add_parser("journal", help="JARVIS's nightly journal of your days")
    jn.add_argument("action", nargs="?", default="show", choices=["show", "write"])
    jn.add_argument("day", nargs="?", help="YYYY-MM-DD (default: latest / yesterday)")
    rh = sub.add_parser("rhythm", help="Send the morning brief or evening check-in now (for testing)")
    rh.add_argument("kind", choices=["morning", "evening"])
    at = sub.add_parser("attention", help="JARVIS deciding when to speak up: decision log, or review now")
    at.add_argument("action", nargs="?", default="log", choices=["log", "review-now"])
    wa = sub.add_parser("watch", help="JARVIS's senses: screen text, terminal, git (runs in the background)")
    wa.add_argument("action", nargs="?", default="run",
                    choices=["run", "once", "timeline", "install-shell-hook", "status"])
    wa.add_argument("--minutes", type=int, default=30, help="for timeline")
    ws = sub.add_parser("watches", help="Things JARVIS keeps an eye on for you: list them, or check the due ones now")
    ws.add_argument("action", nargs="?", default="list", choices=["list", "check"])
    wc = sub.add_parser("watch-cancel", help="Stop a watch (its number from 'jarvis watches', words from its name, or all)")
    wc.add_argument("which")
    sy = sub.add_parser("say", help="Speak text in JARVIS's voice (ignores presence and quiet hours)")
    sy.add_argument("text")
    st = sub.add_parser("siri-test", help="Try a Siri-style action: timer 5m, alarm 07:30, music QUERY, contact NAME")
    st.add_argument("action", choices=["timer", "alarm", "music", "contact"])
    st.add_argument("arg")
    st.add_argument("--kind", default="any", choices=["song", "artist", "playlist", "any"], help="for music")
    md = sub.add_parser("media", help="Control playback (pause, next, volume 40, queue 'Get Lucky', now_playing...) "
                                      "or 'login' to sign in to Spotify")
    md.add_argument("op", help="login, pause, play, next, previous, seek SECONDS, volume N, volume_up, "
                               "volume_down, mute, unmute, shuffle on|off, repeat off|track|context, "
                               "queue SONG, like, now_playing")
    md.add_argument("value", nargs="?", default=None)

    al = sub.add_parser("alert", help="Raise an alert (delivered on next tick)")
    al.add_argument("message")
    al.add_argument("--level", choices=["high", "urgent"], default="high")
    al.add_argument("--key", help="Dedupe key: an alert with the same key is never repeated")
    al.add_argument("--now", action="store_true", help="Deliver immediately instead of waiting for tick")
    sub.add_parser("tick", help="Sync Canvas, deliver alerts, handle drafts and replies, send approved")
    sub.add_parser("canvas", help="Fetch Canvas deadlines and raise deadline alerts")
    sc = sub.add_parser("schedule", help="Run tick automatically in the background (launchd)")
    sc.add_argument("action", choices=["install", "uninstall", "status", "run-now"])
    sc.add_argument("--minutes", type=int, default=5)
    dl = sub.add_parser("deadline", help="Deadlines Canvas doesn't know about (fed by /weekly)")
    dls = dl.add_subparsers(dest="dcmd", required=True)
    da = dls.add_parser("add", help="Add or update one deadline")
    da.add_argument("--course", required=True)
    da.add_argument("--title", required=True)
    da.add_argument("--due", required=True, help="'2026-10-18 23:59' or '2026-10-18' (= 11:59 PM)")
    da.add_argument("--key", help="Use canvas:<assignment id> to replace the feed's copy")
    da.add_argument("--url")
    da.add_argument("--done", action="store_true")
    di = dls.add_parser("import", help="Add/update deadlines from a JSON list (file or - for stdin)")
    di.add_argument("file")
    di.add_argument("--source", default="weekly")
    dd = dls.add_parser("done", help="Mark a deadline done so it stops alerting")
    dd.add_argument("key")
    dd.add_argument("--undo", action="store_true")
    dls.add_parser("list", help="Show stored deadlines with their keys")

    dg = sub.add_parser("digest", help="Show the week ahead from Canvas")
    dg.add_argument("--days", type=int, default=7)
    dg.add_argument("--send", action="store_true", help="Also send it to you on Telegram")

    args = parser.parse_args()

    if args.cmd == "say":
        from jarvis.voice import speak, speakable
        speak(args.text)
        print(f"Saying: {speakable(args.text)}")
        return

    if args.cmd == "telegram-setup":
        _telegram_setup()
        return

    if args.cmd == "graph-test":
        from jarvis.graph import run_access_test
        sys.exit(0 if run_access_test(send=args.send) else 1)

    if args.cmd == "schedule":
        from jarvis import schedule
        if args.action == "install":
            listen = bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID)
            phone = _phone_ready()
            schedule.install(args.minutes * 60, listen=listen, watch=config.WATCH, ears=config.EARS,
                             menubar=config.MENUBAR, phone=phone, panel=config.PANEL, hud=config.HUD and listen,
                             inbox=config.INBOX and listen)
            print(f"tick: every {args.minutes} min. listen: {'always on' if listen else 'skipped (Telegram not set up)'}."
                  f" watch: {'on' if config.WATCH else 'off'}. ears: {'on' if config.EARS else 'off'}."
                  f" phone: {'on' if phone else 'off'}. panel: {'on' if config.PANEL else 'off'}."
                  f" hud: {'on (⌥Space)' if config.HUD and listen else 'off'}."
                  f" inbox: {'on' if config.INBOX and listen else 'off'}."
                  f" Logs: {schedule.LOG_DIR}")
        elif args.action == "uninstall":
            print("Removed" if schedule.uninstall() else "Wasn't scheduled")
        elif args.action == "run-now":
            schedule.run_now()
            print(f"Started. Check: jarvis schedule status")
        else:
            print(schedule.status())
        return

    if args.cmd == "phone":
        if args.action == "setup":
            from jarvis.calls import TwilioCaller
            if not _phone_ready() or not config.PHONE_PUBLIC_URL.startswith("https://"):
                sys.exit("Set PHONE_PUBLIC_URL (the tunnel's https:// address) and the TWILIO_* settings first")
            TwilioCaller().point_number_at(config.PHONE_PUBLIC_URL)
            print(f"Calls to {config.TWILIO_FROM_NUMBER} now go to {config.PHONE_PUBLIC_URL}/voice/incoming")
            return
        _phone()
        return

    if args.cmd == "mail-accounts":
        from jarvis.mailrelay import list_accounts
        print("\n".join(list_accounts()))
        return

    if args.cmd == "inbox":
        from jarvis.mailrelay import read_inbox
        if not args.account:
            sys.exit("Set MAIL_ACCOUNT in .env or pass --account")
        for m in read_inbox(args.account, args.limit):
            flag = " " if m.read else "*"
            print(f"{flag} {m.received:%a %b %d %H:%M}  {m.sender[:40]:40}  {m.subject}")
        return

    mem = Memory(config.DB_PATH)
    if args.cmd == "draft":
        if not args.sender:
            sys.exit("Set MAIL_ACCOUNT in .env or pass --from")
        from jarvis.mailrelay import queue_email
        aid, risk = queue_email(mem, {"from": args.sender, "to": args.to, "subject": args.subject, "body": args.body})
        print(f"Queued email #{aid}." + (" It's only to you, so it goes out on the next tick." if risk == "low" else
              f" It goes to Telegram on the next tick, or: jarvis approve {aid}"))
        return
    if args.cmd == "queue":
        for a in mem.pending_actions(kind="send_email"):
            p = json.loads(a["payload"])
            print(f"#{a['id']}  {p['from']} -> {', '.join(p['to'])}  {p['subject']}\n    {p['body'][:200]}")
        return
    if args.cmd in ("approve", "reject"):
        action = mem.get_action(args.id)
        if not action or action["status"] != "pending":
            sys.exit(f"No pending action #{args.id}")
        if args.cmd == "approve" and action["kind"] == "send_email":
            from jarvis.mailrelay import approve
            approve(mem, args.id)
        else:
            mem.resolve_action(args.id, "approved" if args.cmd == "approve" else "rejected")
        print(f"#{args.id} {args.cmd}d")
        return
    if args.cmd == "relay":
        from jarvis.mailrelay import process_approved
        sent, failed = process_approved(mem)
        print(f"Sent {sent}, failed {failed}")
        sys.exit(1 if failed else 0)
    if args.cmd == "siri-test":  # texts and calls aren't here: they only go out through an approval
        from jarvis import siri
        try:
            if args.action == "timer":
                print(siri.set_timer(mem, siri.parse_duration(args.arg)))
            elif args.action == "alarm":
                print(siri.set_alarm(mem, siri.parse_alarm(args.arg, _now_local())))
            elif args.action == "music":
                print(siri.music(mem, args.arg, args.kind))
            else:
                print("%s: %s" % siri.resolve_contact(mem, args.arg))
        except ValueError as e:
            sys.exit(str(e))
        return
    if args.cmd == "media":
        from jarvis import media
        if args.op == "login":
            _spotify_login()
            return
        try:
            print(media.media_control(args.op, args.value))
        except ValueError as e:
            sys.exit(str(e))
        return
    if args.cmd == "call-test":
        from jarvis.calls import TwilioCaller
        TwilioCaller().call("Voice calls are working.")
        print(f"Calling {config.MY_PHONE_NUMBER}")
        return
    if args.cmd == "alert":
        aid = mem.raise_alert(args.level, args.message, args.key)
        if aid is None:
            print(f"Already alerted for key {args.key!r}; skipping")
            return
        print(f"Alert #{aid} ({args.level}) queued")
        if args.now:
            messages, calls = _deliver(mem, _messenger(), _caller())
            print(f"Messaged {messages}, called {calls}")
        return
    if args.cmd == "canvas":
        deadlines, raised = _sync_canvas(mem)
        due = sum(1 for d in deadlines if d.kind == "due")
        print(f"{due} deadlines, {len(deadlines) - due} calendar events, {raised} new alert(s)")
        return
    if args.cmd == "deadline":
        from jarvis.canvas import import_deadlines
        tz = _now_local().tzinfo
        if args.dcmd == "add":
            item = {"course": args.course, "title": args.title, "due": args.due,
                    "key": args.key, "url": args.url, "done": True if args.done else None}
            import_deadlines(mem, [item], tz, source="manual")
            print("Saved")
        elif args.dcmd == "import":
            raw = sys.stdin.read() if args.file == "-" else open(args.file).read()
            print(f"Saved {import_deadlines(mem, json.loads(raw), tz, args.source)} deadline(s)")
        elif args.dcmd == "done":
            if not mem.set_deadline_done(args.key, not args.undo):
                sys.exit(f"No stored deadline {args.key!r} (see: jarvis deadline list)")
            print("Updated")
        elif args.dcmd == "list":
            for r in mem.deadlines():
                print(f"{'x' if r['done'] else ' '} {r['due'][:16]}  {r['course']}: {r['title']}  [{r['key']}]")
        return
    if args.cmd == "digest":
        from jarvis.canvas import format_digest
        deadlines, _ = _sync_canvas(mem)
        text = format_digest(deadlines, _now_local(), args.days)
        print(text)
        if args.send:
            tg = _messenger()
            if not tg:
                sys.exit("Telegram isn't set up: add TELEGRAM_BOT_TOKEN to .env, then run jarvis telegram-setup")
            tg.send(text)
        return
    if args.cmd == "listen":
        _listen(mem)
        return
    if args.cmd == "cmd":
        from jarvis import terminal
        if args.action == "try":
            v = terminal.classify(args.command)
            how = {"auto": f"runs on its own ({v.sandbox} sandbox)", "approval": "asks you first",
                   "reject": "refused"}[v.kind]
            print(f"{v.risk if v.kind != 'reject' else 'refused'}: {how}. {v.reason}"
                  + (f"\nwould run: {' '.join(v.argv)}" if v.argv else ""))
            return
        for e in terminal.recent_audit(args.n):
            what = e.get("outcome") or (f"exit {e.get('exit')}" if "exit" in e else "waiting for OK")
            print(f"{e['at'][5:16]} {e.get('class', ''):<8} {str(e.get('approved_by') or '-'):<9} "
                  f"{what:<14} $ {' '.join(e.get('argv') or [])}")
        return
    if args.cmd == "jobs":
        from jarvis import jobs
        if args.action == "list":
            for s in jobs.list_jobs(20):
                print(f"#{s['id']:<4} {jobs.describe_state(s):<34} $ {' '.join(s['argv'])[:100]}")
            print(f"\nJob folders: {jobs.JOBS_DIR}")
        elif args.action == "show":
            s = jobs.job_status(args.id or 0, lines=args.n)
            if not s:
                sys.exit(f"No job #{args.id} (see: jarvis jobs)")
            print(f"#{s['id']} {jobs.describe_state(s)}\n$ {' '.join(s['argv'])}\nin {s['cwd']}\n\n{s['tail']}")
        else:
            print(f"Stopped #{args.id}" if jobs.kill_job(args.id or 0) else f"Job #{args.id} isn't running")
        return
    if args.cmd == "heartbeat":
        from jarvis import heartbeat
        now = _now_local()
        if args.action == "run":
            from jarvis.canvas import from_memory
            try:
                deadlines = _deadlines(mem, now, max_age=FEED_CACHE_SECONDS)
            except (Exception, SystemExit):
                deadlines = from_memory(mem)
            from jarvis import judge as _judge_mod
            print(heartbeat.run_heartbeat(mem, now, deadlines, force=True, fast=_judge_mod.fast_engine()))
            print("(a message, if any, goes out with the next tick)")
        elif args.action in ("off", "on"):
            heartbeat.set_paused(mem, args.action == "off")
            print(f"Heartbeat {'paused' if args.action == 'off' else 'on'}. (HEARTBEAT=0 in .env turns it off for good.)")
        else:
            print(heartbeat.status(mem, now))
            for e in mem.db.execute("SELECT at, content FROM events WHERE kind = 'heartbeat' ORDER BY id DESC LIMIT 10"):
                print(f"  {e['at'][5:16]} {e['content'][:140]}")
        return
    if args.cmd == "build":
        from jarvis import selfedit
        if args.action == "status":
            print(selfedit.status_text())
            if not selfedit.enabled():
                print("(self-editing is off: JARVIS_REPO isn't set)")
            return
        if not selfedit.enabled():
            sys.exit("Self-editing is off. To turn it on, set JARVIS_REPO in .env to this project folder.")
        tg = _messenger()
        print(selfedit.start(mem, tg) if args.action == "start" else selfedit.stop_build(mem))
        return
    if args.cmd == "browser":
        from jarvis import browser
        if args.action == "login":
            if not args.site:
                sys.exit("Usage: jarvis browser login <site>   (e.g. canvas.seattlecolleges.edu)")
            print("A browser window is opening. Sign in yourself, then close the window. "
                  "JARVIS never sees your password; only the session cookie stays in its profile.")
            print(f"Saved: JARVIS can now open {browser.login(args.site)} as you.")
        elif args.action == "forget":
            print("Removed" if args.site and browser.forget_site(args.site) else f"{args.site!r} isn't on the list")
        elif args.action == "reset":
            browser.reset()
            print("Deleted the browser profile and every saved login.")
        else:
            print("\n".join(browser.sites()) or "(no sites yet: jarvis browser login <site>)")
            print(f"\nProfile (local only, never shared): {config.BROWSER_PROFILE}")
        return
    if args.cmd == "helpers":
        from jarvis import helpers
        if args.panel:
            from jarvis import panel
            print(panel.show(mem, {"type": "show", "kind": "helpers", "show": args.only}))
            return
        print(helpers.detail(mem, args.which) if args.which else helpers.for_prompt(mem, max_age=0))
        return
    if args.cmd == "send-image":
        from jarvis.attachments import send_image
        tg = _messenger()
        if not tg:
            sys.exit("Telegram isn't set up")
        try:
            print(f"Sent {send_image(tg, args.path, args.caption, mem)}")
        except ValueError as e:
            sys.exit(f"Not sent: {e}")
        return
    if args.cmd == "tasks":
        from jarvis import tasks
        if args.action == "show":
            print(tasks.show(mem, args.id or (tasks.recent(mem, 1)[0]["id"] if tasks.recent(mem, 1) else 0)))
        elif args.action == "stop":
            print(tasks.stop(mem, args.id))
        else:
            for t in tasks.recent(mem, 15):
                print(f"#{t['id']:<4} {t['status']:<8} {len(tasks.steps_of(t)):>3} steps  {t['goal'][:90]}")
        return
    if args.cmd == "location":
        from datetime import timedelta
        from jarvis import location
        now = _now_local()
        if args.action == "show":
            print(location.for_prompt(mem, now))
        elif args.action == "history":
            for p in location.recent(mem, now - timedelta(hours=args.hours)):
                start = location._dt(p["at"]).astimezone(now.tzinfo)
                end = location._dt(p["last_seen"]).astimezone(now.tzinfo)
                print(f"{start:%a %-I:%M %p}-{end:%-I:%M %p}  {p['source']:<6} "
                      f"{location.describe_spot(mem, p['lat'], p['lon'])}")
        elif args.action == "places":
            for p in location.places(mem):
                near = location.place_name(mem, p["lat"], p["lon"], lookup=False) or "(no name looked up)"
                print(f"#{p['id']:<3} {p['name'] or '(unlabelled)':<14} {near}")
        elif args.action == "label":
            if not args.id or not args.name or not location.label_place(mem, args.id, args.name):
                sys.exit("Usage: jarvis location label <place #> <name>  (see: jarvis location places)")
            print(f"Place #{args.id} is now {args.name.lower()}")
        else:
            location.forget_all(mem)
            print("Forgot all location points, places and visits")
        return
    if args.cmd == "latency":
        print(latency_summary([e["content"] for e in mem.db.execute(
            "SELECT content FROM events WHERE kind IN ('answered', 'first_audio') ORDER BY id DESC LIMIT ?", (args.last,))]))
        from jarvis.timing import summary
        rows = []
        for (content,) in mem.db.execute("SELECT content FROM events WHERE kind = 'timing' ORDER BY id DESC LIMIT ?",
                                         (args.last,)):
            try:
                rows.append(json.loads(content))
            except ValueError:
                pass
        print("\n" + summary(rows))
        return
    if args.cmd == "ears":
        from jarvis import speech
        config.DESK_PROCESS = True  # runs inside JARVIS Ears.app: no new permissions from here
        from jarvis.ears import Ears
        speech._whisper()  # load the model up front so the first answer isn't slow
        log = lambda s: print(s, flush=True)  # noqa: E731
        warm = _warm(mem)
        tg = _messenger()
        from jarvis import camera
        camera.sweep()  # camera stills a crash left behind

        def after_reply():  # helper reports that waited while JARVIS was talking: text only, since
            if tg:          # the mic is open for his follow-up and mustn't hear JARVIS's own voice
                from jarvis import attachments, reports
                reports.flush_pending(mem, tg.send, send_image=lambda p, c: attachments.send_image(tg, p, c, mem))
        ears = Ears(answer=lambda text, on_text=None: _spoken_confirmation(mem, text)
             or _answer(mem, text, spoken=True, warm=warm, on_text=on_text, channel="mic"),
             transcribe=speech.transcribe, log=log, after_reply=after_reply,
             timing=lambda ms: mem.log("first_audio", f"time_to_first_audio={ms}ms"),
             on_turn=lambda steps: mem.log("timing", json.dumps(steps)), addressed=_addressed_check(mem))
        from jarvis.chat import warm_vision
        ears.prewarm = warm_vision().prewarm
        ears.run_forever()
        return
    if args.cmd == "projects":
        from jarvis import projects
        if args.action == "sync":
            print(f"added {projects.sync_projects(mem)} project(s)")
        elif args.action == "list":
            for p in mem.projects():
                print(f"{p['name']:<34} {p['kind']:<8} {p['status']:<8} {p['summary'] or '(no brief yet)'}")
        elif args.action == "show":
            p = mem.project(args.name or "")
            if not p:
                sys.exit(f"No project {args.name!r} (see: jarvis projects list)")
            print(f"{p['name']} [{p['kind']}, {p['status']}] {p['repo']}\n\n{p['brief'] or '(no brief yet)'}")
        elif args.action == "refresh":
            projects.sync_projects(mem)
            names = [args.name] if args.name else [p["name"] for p in mem.projects() if p["repo"]]
            for name in names:
                try:
                    print(f"{name}: {projects.write_brief(mem, name)}", flush=True)
                except Exception as e:
                    print(f"{name}: failed ({str(e)[:150]})", flush=True)
        return
    if args.cmd == "journal":
        from datetime import date, timedelta
        from jarvis import journal
        now = _now_local()
        if args.action == "write":
            day = date.fromisoformat(args.day) if args.day else now.date() - timedelta(days=1)
            print(journal.write(mem, day, now.tzinfo))
        else:
            rows = [mem.journal(args.day)] if args.day else mem.recent_journal(1)
            rows = [r for r in rows if r]
            if not rows:
                sys.exit("No journal entry yet (it's written after 2am for the previous day)")
            r = rows[0]
            print(f"{r['day']}\n\n{r['entry']}\n\nOpen loops: {json.loads(r['open_loops']) or 'none'}")
        return
    if args.cmd == "rhythm":
        from jarvis import rhythm
        from jarvis.canvas import format_digest
        from jarvis.voice import Announcer, user_present
        now = _now_local()
        mem.set_kv(f"rhythm_{args.kind}", "")  # allow re-sending for testing
        mem.set_kv(f"rhythm_{args.kind}_attempt", "")
        hour = config.MORNING_HOUR if args.kind == "morning" else config.EVENING_HOUR
        fake_now = now.replace(hour=hour, minute=5)
        sent = rhythm.maybe_check_in(mem, fake_now, Announcer(_messenger(), mem), True,
                                     format_digest(_deadlines(mem, now), now, days=2))
        print(f"sent {sent}" if sent else "nothing sent (quiet hours or RHYTHM=0)")
        return
    if args.cmd == "attention":
        if args.action == "log":
            now = _now_local()
            quick = mem.get_kv(f"attention_quick_{now.date().isoformat()}") or "0"
            spoke = mem.judgments_spoken_since(now.replace(hour=0, minute=0, second=0, microsecond=0))
            print(f"today: {quick} quick Jev checks, spoke {spoke}/{config.ATTENTION_DAILY_LIMIT} times\n")
            for j in mem.recent_judgments(limit=15):
                answers = json.loads(j["answers"])
                verdict = "SPOKE" if j["spoke"] else f"quiet ({j['blocked']})"
                print(f"{j['at'][5:16]} {j['trigger']:<8} {verdict:<32} speak={answers['speak']} "
                      f"kind={answers['kind']['value']} signals={json.loads(j['signals'])}")
                if j["message"]:
                    print(f"    \"{j['message'][:160]}\"")
        else:
            from datetime import datetime, timezone
            from jarvis.attention import stuck_signals
            a = _attention(mem)
            now = datetime.now(timezone.utc)
            print(a.review(now, "manual", stuck_signals(mem, now)))
        return
    if args.cmd == "watch":
        from jarvis import terminal, watch
        if args.action == "run":
            from jarvis import judge, tags
            fast = judge.fast_engine()
            watch.Watcher(mem, log=lambda s: print(s, flush=True), attention=_attention(mem),
                          tagger=lambda app, title, text: tags.tag_window(app, title, text, fast)).run_forever()
        elif args.action == "once":
            w = watch.Watcher(mem)
            print("look:", w.look(__import__("time").time()))
            print("housekeeping:", w.housekeeping(__import__("time").time()) or "nothing new")
        elif args.action == "timeline":
            print(watch.recent_activity(mem, args.minutes))
        elif args.action == "install-shell-hook":
            print(terminal.install_hook())
        elif args.action == "status":
            from datetime import datetime, timedelta, timezone
            from jarvis.repos import known_repos
            day = mem.observations(datetime.now(timezone.utc) - timedelta(hours=24))
            counts = {s: sum(o["source"] == s for o in day) for s in ("screen", "terminal", "git")}
            print(f"last 24h: {counts}; repos known: {len(known_repos(mem))}; "
                  f"terminal hook: {'installed' if terminal.hook_installed() else 'not installed'}")
        return
    if args.cmd in ("watches", "watch-cancel"):
        from jarvis import watches
        if args.cmd == "watch-cancel":
            try:
                print("Cancelled: " + ", ".join(watches.cancel(mem, args.which)))
            except ValueError as e:
                sys.exit(str(e))
        elif args.action == "check":
            print(_run_watches(mem))
            print("(an alert, if any, goes out with the next tick)")
        else:
            print(watches.list_text(mem, _now_local(), ids=True))
        return
    if args.cmd == "plans":
        from jarvis.plans import PLANS_DIR
        if args.action == "list":
            for s in mem.self_edits():
                print(f"#{s['id']:<3} {s['status']:<10} {s['origin']:<8} {s['title']}")
            print(f"\nApproved plan files: {PLANS_DIR}")
        elif args.action == "show":
            job = mem.get_self_edit(int(args.id or 0))
            if not job:
                sys.exit(f"No plan #{args.id}")
            print(f"#{job['id']} {job['title']} [{job['status']}]\n\nRequest:\n{job['request']}\n\n"
                  f"Plan:\n{job['plan'] or '(not drafted yet)'}")
        elif args.action == "done":
            if not mem.set_self_edit(int(args.id or 0), expect=("approved",), status="deployed",
                                     summary="built in a Claude Code session"):
                sys.exit(f"No approved plan #{args.id}")
            print(f"Marked #{args.id} as built")
        return
    if args.cmd == "rules":
        from jarvis import rules
        if args.action == "show":
            print(rules.show_text() + f"\n\nFile: {rules.PATH}")
        elif args.action == "log":
            print(rules.log_text())
        else:  # stricter applies now; looser waits for his tap in Telegram
            print(rules.revert(mem, int(args.n) if args.n else None, approved_by="Elijah (CLI)"))
        return
    if args.cmd == "self-edit":
        from jarvis import selfedit
        if not selfedit.enabled():
            sys.exit("Self-editing is off. To turn it on, set JARVIS_REPO in .env to this project folder.")
        selfedit.main([args.action, *([args.id] if args.id else [])])
        return
    if args.cmd == "search":
        from jarvis import websearch
        found = websearch.run(mem, args.query, n=args.n)
        print(f"Query sent: {found.query}")
        print(found.error or websearch.for_prompt(found))
        print(f"({websearch.used_today(mem)} of {config.SEARCH_DAILY_LIMIT} searches used today)")
        return

    if args.cmd == "ask":
        from jarvis.canvas import format_digest
        from jarvis.chat import respond
        now = _now_local()
        print(respond(mem, args.message, now, format_digest(_deadlines(mem, now), now, days=14),
                      inbox_text=_inbox_text(mem), activity_text=_activity(mem)))
        return
    if args.cmd == "tick":
        import fcntl
        # One tick at a time, or the scheduled and a manual run could both send an approved email.
        with open(config.DATA_DIR / "tick.lock", "w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                sys.exit("Another tick is running; skipped.")
            sys.exit(_tick(mem))

    if args.cmd == "remember":
        mem.remember(args.category, args.key, args.value, source="cli")
    elif args.cmd == "forget":
        if not mem.forget(args.category, args.key):
            sys.exit(f"No fact {args.category}/{args.key}")
    elif args.cmd == "project":
        fields = {k: v for k in ("status", "summary", "goals") if (v := getattr(args, k))}
        mem.upsert_project(args.name, args.kind, **fields)
    elif args.cmd == "note":
        project_id = None
        if args.project:
            match = [p for p in mem.projects() if p["name"] == args.project]
            if not match:
                sys.exit(f"No project named {args.project!r}")
            project_id = match[0]["id"]
        mem.log("note", args.content, project_id)
    elif args.cmd == "context":
        print(mem.context_snapshot())


if __name__ == "__main__":
    main()
