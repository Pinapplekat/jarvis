"""Thin command line: parse arguments, call core functions."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from jarvis import __version__
from jarvis.contract.loader import DEFAULT_APPS_DIR, manifest_paths
from jarvis.contract.manifest import ManifestError, load_manifest
from jarvis.core import actions, db, outbox
from jarvis.core.apps import Registry, load_apps, settings_for_missing_apps
from jarvis.core.log import setup_logging
from jarvis.contract.envfile import load_secrets
from jarvis.core.settings import Settings, SettingsError, load_settings


def _job_count() -> int:
    return sum(len(load_manifest(p).jobs) for p in manifest_paths(DEFAULT_APPS_DIR))


def _info(settings: Settings) -> int:
    conn = db.connect(settings)
    try:
        versions = db.applied_versions(conn)
        connected = conn.execute("SELECT COUNT(*) FROM nodes WHERE connected_at IS NOT NULL").fetchone()[0]
        unsent = outbox.unsent_count(conn)
    finally:
        conn.close()
    print(f"data folder: {settings.data_dir}")
    print(f"database:    {db.db_path(settings)}")
    print(f"migrations:  {versions}")
    print(f"assistant:   {settings.assistant_name}")
    print(f"log:         {settings.data_dir / 'logs' / 'falak.log'}")
    print(f"secret keys: {sorted(load_secrets(settings.data_dir)) or 'none'}")
    print(f"nodes:       {connected} connected")
    print(f"jobs:        {_job_count()} (unsent outbox messages: {unsent})")
    print(f"settings for missing apps: {settings_for_missing_apps(settings) or 'none'}")
    return 0


def _import_db(settings: Settings, source: str | None, overwrite: bool) -> int:
    try:
        counts = db.import_legacy_db(settings, Path(source) if source else db.LIVE_DB, overwrite=overwrite)
    except FileExistsError:
        print(f"{db.db_path(settings)} already exists. Pass --overwrite to replace it.", file=sys.stderr)
        return 1
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return 1
    for table, n in counts.items():
        print(f"{table}: {n}")
    return 0


def _print_outcome(out: object) -> int:
    """Print what an action did. Returns the exit code."""
    if isinstance(out, actions.Ran):
        print(out.result.text)
        for att in out.result.attachments:
            print(f"{att.kind}: {att.path.stat().st_size} bytes at {att.path}")
        actions.release(out)
        return 0
    if isinstance(out, actions.Asked):
        print(f"Asked #{out.ask_id}: {out.question}")
        return 0
    if isinstance(out, actions.Declined):
        print(f"Declined #{out.ask_id}.")
        return 0
    print(out.message, file=sys.stderr)
    return 1


def _apps_command(settings: Settings, args: argparse.Namespace) -> int:
    reg: Registry = load_apps(settings)
    try:
        origin = actions.Origin(channel="cli", outside_content=False)
        if args.group == "apps":
            for app in reg.apps.values():
                print(f"{app.manifest.name} (runs on {app.manifest.runs_on}): {app.manifest.description}")
                for a in app.manifest.tools:
                    print(f"  {a.name} [{a.effect}] {a.description}")
            return 0
        if args.group == "run":
            app_name, _, action_name = args.target.partition(".")
            try:
                payload = json.loads(args.args)
            except json.JSONDecodeError as e:
                print(f"--args is not valid JSON: {e}", file=sys.stderr)
                return 2
            return _print_outcome(actions.run(reg, f"{app_name}__{action_name}", payload, origin))
        if args.group == "deadline":
            return _import_deadlines(reg, args.file, origin)
        if args.group in ("yes", "no"):
            return _print_outcome(actions.answer_ask(reg, args.id, args.group == "yes"))
        if args.group == "asks":
            for ask in actions.pending_asks(reg):
                print(f"#{ask['id']} {ask['question']}  ({ask['tool']}, {ask['created_at']})")
            return 0
        if args.group == "undo":
            return _print_outcome(actions.undo(reg, args.id))
        for row in actions.recent_log(reg, args.n):  # log
            print(f"#{row['id']} {row['time']} {row['tool']} [{row['effect']}] {row['status']}: {row['result']}")
        return 0
    finally:
        reg.close()


def _import_deadlines(reg: Registry, file: str, origin: actions.Origin) -> int:
    """`jarvis deadline import <file.json>`: the list /weekly writes, [{course, title, due, key?, url?, done?}]."""
    try:
        items = json.loads(Path(file).read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"couldn't read {file}: {e}", file=sys.stderr)
        return 2
    if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
        print(f"{file} must hold a JSON list of deadlines", file=sys.stderr)
        return 2
    items = [{k: v for k, v in i.items() if v is not None} for i in items]  # /weekly writes null for "unknown"
    return _print_outcome(actions.run(reg, "school__import_deadlines", {"items": items}, origin))


def build_parser(name: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jarvis")
    parser.add_argument("--version", action="version", version=f"jarvis {__version__} ({name})")
    sub = parser.add_subparsers(dest="group")
    dev = sub.add_parser("dev", help="developer commands").add_subparsers(dest="command")
    dev.add_parser("info", help="show data folder, database and migrations")
    imp = dev.add_parser("import-db", help="copy the live JARVIS database into the Falak data folder")
    imp.add_argument("--source", help="source database (default: the live JARVIS database)")
    imp.add_argument("--overwrite", action="store_true", help="replace an existing falak.db")
    node = sub.add_parser("node", help="pair, revoke and run device nodes").add_subparsers(dest="node_command")
    for verb, text in (("pair", "pair a new node and print its key once"), ("revoke", "revoke a node")):
        node.add_parser(verb, help=text).add_argument("name")
    node.add_parser("list", help="every paired node")
    node.add_parser("run", help="run this machine's node and connect it to the host")
    node.add_parser("music-login", help="sign the music app in to Spotify (once, on the node's Mac)")
    voice = sub.add_parser("voice", help="the speech models for the desk and calls").add_subparsers(
        dest="voice_command", required=True)
    voice.add_parser("setup", help="copy or download the listening and speaking models into Falak's folder")
    voice.add_parser("status", help="which speech models are in place")
    browser = sub.add_parser("browser", help="Falak's own web browser (on this Mac's node)").add_subparsers(
        dest="browser_command", required=True)
    browser.add_parser("setup", help="install Falak's Chromium into the node's folder (needs 2 GB free)")
    browser.add_parser("status", help="whether Chromium is installed, and which sites are signed in")
    browser.add_parser("reset", help="delete every saved login in Falak's browser")
    sub.add_parser("nodes", help="nodes connected to the running host, with their capabilities")
    sub.add_parser("apps", help="list apps and their actions")
    run = sub.add_parser("run", help="run an action as <app>.<action>")
    run.add_argument("target")
    run.add_argument("--args", default="{}", help="the arguments as JSON")
    deadline = sub.add_parser("deadline", help="school deadlines").add_subparsers(dest="deadline_command", required=True)
    deadline.add_parser("import", help="add deadlines from a JSON list (what /weekly writes)").add_argument("file")
    sub.add_parser("asks", help="list pending asks")
    for name in ("yes", "no"):
        sub.add_parser(name, help=f"answer a pending ask: {name}").add_argument("id", type=int)
    sub.add_parser("undo", help="undo the last undoable action (or a log entry)").add_argument(
        "id", type=int, nargs="?"
    )
    sub.add_parser("log", help="recent actions").add_argument("-n", type=int, default=20)
    sub.add_parser("chat", help="talk to it in the terminal")
    sub.add_parser("start", help="run Falak and this Mac's node together until Ctrl-C (the everyday command)")
    serve = sub.add_parser("serve", help="run Falak and its channels (Discord) until Ctrl-C")
    serve.add_argument("--with-node", action="store_true", help="also start this Mac's node as a child process (dev)")
    serve.add_argument("--with-fixtures", action="store_true", help="also load the test fixture apps (dev only)")
    sub.add_parser("ask", help="send one message and print the reply").add_argument("message")
    sub.add_parser("turns", help="recent turns: timing and tools used").add_argument("-n", type=int, default=20)
    jobs = sub.add_parser("jobs", help="the apps' scheduled jobs: last and next run")
    jobs_sub = jobs.add_subparsers(dest="jobs_command")
    jobs_run = jobs_sub.add_parser("run", help="run one job now, as <app>.<job>")
    jobs_run.add_argument("target")
    jobs_run.add_argument("--force", action="store_true", help="run even in quiet hours")
    sub.add_parser("outbox", help="messages Falak has sent or is waiting to send").add_argument("-n", type=int, default=20)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        settings = load_settings()
    except SettingsError as e:
        print(f"settings error: {e}", file=sys.stderr)
        return 2
    setup_logging(settings)
    parser = build_parser(settings.assistant_name)
    args = parser.parse_args(argv)
    if args.group == "dev" and args.command == "info":
        return _info(settings)
    if args.group == "dev" and args.command == "import-db":
        return _import_db(settings, args.source, args.overwrite)
    if args.group == "voice":
        from jarvis import voice_cli

        return voice_cli.setup(settings) if args.voice_command == "setup" else voice_cli.status(settings)
    if args.group == "browser":
        from jarvis import browser_cli

        return browser_cli.command(args)
    if args.group in ("node", "nodes"):
        from jarvis import node_cli

        return node_cli.command(settings, args)
    if args.group in ("apps", "run", "deadline", "asks", "yes", "no", "undo", "log", "chat", "ask", "turns", "serve",
                      "start", "jobs", "outbox"):
        try:
            if args.group == "outbox" or (args.group == "jobs" and not args.jobs_command):
                from jarvis import jobs_cli

                return jobs_cli.show_outbox(settings, args.n) if args.group == "outbox" else jobs_cli.list_jobs(settings)
            if args.group in ("chat", "ask", "turns", "serve", "start", "jobs"):
                from jarvis import chat_cli  # loads the SDK, which the other commands don't need

                if args.group == "start":
                    return chat_cli.serve(settings, with_node=True)
                if args.group == "serve":
                    return chat_cli.serve(settings, args.with_fixtures, args.with_node)
                if args.group == "jobs":
                    return chat_cli.jobs_run(settings, args.target, args.force)
                if args.group == "chat":
                    return chat_cli.chat(settings)
                if args.group == "ask":
                    return chat_cli.ask(settings, args.message)
                return chat_cli.turns(settings, args.n)
            return _apps_command(settings, args)
        except (ManifestError, SettingsError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
