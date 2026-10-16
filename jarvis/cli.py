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


def _inbox_text() -> str:
    from jarvis.mailrelay import fetch_inbox_headers
    from jarvis.mailwatch import inbox_for_prompt
    return inbox_for_prompt(*fetch_inbox_headers(config.MAIL_ACCOUNT, limit=15, timeout=10))


def _listen(mem: Memory) -> None:
    """Stay connected to Telegram: handle Approve/Reject at once and chat with the user."""
    import time

    from jarvis import plans, selfedit
    from jarvis.approvals import handle_updates, notify_pending
    from jarvis.canvas import format_digest, from_memory
    from jarvis.chat import respond

    tg = _messenger()
    if not tg:
        sys.exit("Telegram isn't set up: add TELEGRAM_BOT_TOKEN to .env, then run jarvis telegram-setup")

    def on_text(update):
        tg.typing()
        now = _now_local()
        try:
            deadlines = _deadlines(mem, now)
        except Exception:  # feed down: answer from stored deadlines rather than not at all
            deadlines = from_memory(mem)
        reply = respond(mem, update.text, now, format_digest(deadlines, now, days=14),
                        on_progress=tg.typing, inbox_text=_inbox_text())
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
            handle_updates(mem, tg, int(config.TELEGRAM_CHAT_ID), on_text, wait=25, on_button=on_button)
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

    if config.CANVAS_TOKEN or config.CANVAS_ICS_URL:
        step("canvas", lambda: f"{_sync_canvas(mem)[1]} new alerts")
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
    sub.add_parser("listen", help="Stay connected to Telegram: chat and approval buttons (runs forever)")
    ask = sub.add_parser("ask", help="Ask JARVIS something from the terminal (same brain as Telegram)")
    ask.add_argument("message")
    sub.add_parser("call-test", help="Call yourself once to check Twilio voice")
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
            schedule.install(args.minutes * 60, listen=listen)
            print(f"tick: every {args.minutes} min. listen: {'always on' if listen else 'skipped (Telegram not set up)'}."
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
                      inbox_text=_inbox_text()))
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
