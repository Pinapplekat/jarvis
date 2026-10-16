import argparse
import json
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
    from jarvis.telegram import TelegramClient
    return TelegramClient()


def _caller():
    from jarvis.calls import TwilioCaller, twilio_configured
    return TwilioCaller() if twilio_configured() else None


def _deliver(mem: Memory, messenger, caller) -> tuple[int, int]:
    from jarvis.alerts import deliver_alerts, parse_quiet_hours
    return deliver_alerts(mem, messenger, caller, _now_local().replace(tzinfo=None),
                          parse_quiet_hours(config.QUIET_HOURS))


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


def _deadlines(mem: Memory, now):
    from jarvis.canvas import CanvasClient, fetch_feed, from_feed, from_memory, merge
    if config.CANVAS_TOKEN:
        fetched = CanvasClient().fetch_deadlines(now.tzinfo, now.date())
    elif config.CANVAS_ICS_URL:
        fetched = from_feed(fetch_feed(config.CANVAS_ICS_URL), now.tzinfo)
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
                     Announcer(_messenger(), mem))


def _activity(mem: Memory) -> str:
    from jarvis.watch import recent_activity
    return recent_activity(mem, 30)


def _transcribe(audio: bytes) -> str:
    from jarvis.speech import transcribe
    return transcribe(audio)


def _answer(mem: Memory, text: str, on_progress=lambda: None, spoken: bool = False) -> str:
    """One chat turn with full context. Shared by Telegram (typed or voice) and the desk mic."""
    import time

    from jarvis import judge, router
    from jarvis.canvas import format_digest, from_memory
    from jarvis.chat import respond
    now = _now_local()
    started = time.time()
    recent = [f"{'Elijah' if h['role'] == 'user' else 'JARVIS'}: {h['text'][:300]}" for h in mem.chat_history(4)]
    blocks = router.route(text, recent, judge.fast_engine(),
                          on_error=lambda e: mem.log("route_failed", str(e)[:200]))
    routed = time.time() - started
    deadlines_text = ""
    if "deadlines" in blocks:  # the Canvas fetch is the slow part; skip it when not needed
        try:
            deadlines = _deadlines(mem, now)
        except Exception:  # feed down: answer from stored deadlines rather than not at all
            deadlines = from_memory(mem)
        deadlines_text = format_digest(deadlines, now, days=14)
    message = f"(said out loud, transcribed) {text}" if spoken else text
    reply = respond(mem, message, now, deadlines_text, on_progress=on_progress,
                    inbox_text=_inbox_text(mem) if "inbox" in blocks else "",
                    activity_text=_activity(mem) if "activity" in blocks else "", blocks=blocks)
    mem.log("answered", f"{time.time() - started:.1f}s total, route {routed:.2f}s, "
                        f"context: {', '.join(sorted(blocks)) or 'minimal'}")
    return reply


def _listen(mem: Memory) -> None:
    """Stay connected to Telegram: handle Approve/Reject at once and chat with the user."""
    import time

    from jarvis import plans, selfedit
    from jarvis.approvals import handle_updates, notify_pending

    tg = _messenger()
    if not tg:
        sys.exit("Telegram isn't set up: add TELEGRAM_BOT_TOKEN to .env, then run jarvis telegram-setup")

    def on_text(update):
        tg.typing()
        reply = _answer(mem, update.text, on_progress=tg.typing, spoken=update.via_voice)
        if update.via_voice:  # answer a voice note with a voice note (walks, AirPods)
            from jarvis.speech import voice_note
            tg.action("record_voice")
            try:
                tg.send_voice(voice_note(reply), caption=reply if len(reply) <= 1000 else "")
                if len(reply) > 1000:
                    tg.send(reply)
            except Exception as e:  # never lose the answer over audio trouble
                mem.log("voice_note_failed", str(e)[:200])
                tg.send(reply)
        else:
            tg.send(reply)
        notify_pending(mem, tg)  # drafts made in this chat get their buttons right away
        if mem.self_edits("drafting"):
            tg.typing()
            plans.draft_plans(mem, tg)

    def on_button(verb, edit_id):
        if verb.startswith("plan_"):
            on_approved = (lambda i: selfedit.queue_approved(mem, tg, i)) if selfedit.enabled() else None
            return plans.handle_button(mem, verb, edit_id, on_approved)
        return selfedit.handle_button(mem, tg, verb, edit_id)

    from jarvis.reminders import deliver_reminders
    from jarvis.voice import Announcer
    announcer = Announcer(tg, mem)

    print(f"Listening as chat {config.TELEGRAM_CHAT_ID}", flush=True)
    while True:
        try:
            deliver_reminders(mem, announcer, _now_local())
            # Short long-poll so reminders go out within ~25s of their time.
            handle_updates(mem, tg, int(config.TELEGRAM_CHAT_ID), on_text, wait=25, on_button=on_button,
                           transcribe=_transcribe)
        except Exception as e:  # network blips, Telegram hiccups: wait and reconnect
            print(f"listen error: {e}", flush=True)
            mem.log("listen_error", str(e)[:300])
            time.sleep(10)


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
    if config.MAIL_WATCH and config.MAIL_ACCOUNT:
        from jarvis.mailwatch import check_new_mail
        step("mail-watch", lambda: check_new_mail(mem, _now_local()))  # before alerts: same-tick ping
    if tg or caller:
        step("alerts", lambda: "messaged %d, called %d" % _deliver(mem, announcer, caller))
    if tg:
        # Incoming messages and button presses are handled by `jarvis listen`, not here:
        # Telegram allows only one reader of updates at a time.
        step("drafts", lambda: f"sent {notify_pending(mem, tg)}")
        step("reminders", lambda: f"sent {deliver_reminders(mem, announcer, _now_local())}")  # backup for listener
        from jarvis import plans

        def ideas():
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
    se = sub.add_parser("self-edit", help="Background worker for applying approved plans (off unless JARVIS_REPO is set)")
    se.add_argument("action", choices=["build", "deploy"])
    se.add_argument("id", nargs="?")
    sub.add_parser("ears", help="Listen for 'Hey Jarvis' on the Mac's mic and answer out loud (runs forever)")
    sub.add_parser("listen", help="Stay connected to Telegram: chat and approval buttons (runs forever)")
    ask = sub.add_parser("ask", help="Ask JARVIS something from the terminal (same brain as Telegram)")
    ask.add_argument("message")
    sub.add_parser("call-test", help="Call yourself once to check Twilio voice")
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
    sy = sub.add_parser("say", help="Speak text in JARVIS's voice (ignores presence and quiet hours)")
    sy.add_argument("text")

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
            schedule.install(args.minutes * 60, listen=listen, watch=config.WATCH, ears=config.EARS)
            print(f"tick: every {args.minutes} min. listen: {'always on' if listen else 'skipped (Telegram not set up)'}."
                  f" watch: {'on' if config.WATCH else 'off'}. ears: {'on' if config.EARS else 'off'}."
                  f" Logs: {schedule.LOG_DIR}")
        elif args.action == "uninstall":
            print("Removed" if schedule.uninstall() else "Wasn't scheduled")
        elif args.action == "run-now":
            schedule.run_now()
            print(f"Started. Check: jarvis schedule status")
        else:
            print(schedule.status())
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
        aid = mem.queue_action("send_email", {
            "from": args.sender, "to": args.to, "subject": args.subject, "body": args.body,
        })
        print(f"Queued email #{aid}. It goes to Telegram on the next tick, or: jarvis approve {aid}")
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
        mem.resolve_action(args.id, "approved" if args.cmd == "approve" else "rejected")
        print(f"#{args.id} {args.cmd}d")
        return
    if args.cmd == "relay":
        from jarvis.mailrelay import process_approved
        sent, failed = process_approved(mem)
        print(f"Sent {sent}, failed {failed}")
        sys.exit(1 if failed else 0)
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
    if args.cmd == "ears":
        from jarvis import speech
        from jarvis.ears import Ears
        speech._whisper()  # load the model up front so the first answer isn't slow
        log = lambda s: print(s, flush=True)  # noqa: E731
        Ears(answer=lambda text: _answer(mem, text, spoken=True), transcribe=speech.transcribe,
             log=log).run_forever()
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
            watch.Watcher(mem, log=lambda s: print(s, flush=True), attention=_attention(mem)).run_forever()
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
    if args.cmd == "self-edit":
        from jarvis import selfedit
        if not selfedit.enabled():
            sys.exit("Self-editing is off. To turn it on, set JARVIS_REPO in .env to this project folder.")
        selfedit.main([args.action, *([args.id] if args.id else [])])
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
