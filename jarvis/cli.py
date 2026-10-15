import argparse
import json
import sys

from jarvis import config
from jarvis.memory import Memory


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

    args = parser.parse_args()

    if args.cmd == "graph-test":
        from jarvis.graph import run_access_test
        sys.exit(0 if run_access_test(send=args.send) else 1)

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
        print(f"Queued email #{aid}. Approve with: jarvis approve {aid}")
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
