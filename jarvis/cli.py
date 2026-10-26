"""Thin command line: parse arguments, call core functions."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from jarvis import __version__
from jarvis.core import actions, db
from jarvis.core.apps import Registry, load_apps, settings_for_missing_apps
from jarvis.core.manifest import ManifestError
from jarvis.core.log import setup_logging
from jarvis.core.settings import Settings, SettingsError, load_secrets, load_settings


def _info(settings: Settings) -> int:
    conn = db.connect(settings)
    try:
        versions = db.applied_versions(conn)
    finally:
        conn.close()
    print(f"data folder: {settings.data_dir}")
    print(f"database:    {db.db_path(settings)}")
    print(f"migrations:  {versions}")
    print(f"assistant:   {settings.assistant_name}")
    print(f"log:         {settings.data_dir / 'logs' / 'falak.log'}")
    print(f"secret keys: {sorted(load_secrets(settings.data_dir)) or 'none'}")
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
                for a in app.manifest.actions:
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


def build_parser(name: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jarvis")
    parser.add_argument("--version", action="version", version=f"jarvis {__version__} ({name})")
    sub = parser.add_subparsers(dest="group")
    dev = sub.add_parser("dev", help="developer commands").add_subparsers(dest="command")
    dev.add_parser("info", help="show data folder, database and migrations")
    imp = dev.add_parser("import-db", help="copy the live JARVIS database into the Falak data folder")
    imp.add_argument("--source", help="source database (default: the live JARVIS database)")
    imp.add_argument("--overwrite", action="store_true", help="replace an existing falak.db")
    sub.add_parser("apps", help="list apps and their actions")
    run = sub.add_parser("run", help="run an action as <app>.<action>")
    run.add_argument("target")
    run.add_argument("--args", default="{}", help="the arguments as JSON")
    sub.add_parser("asks", help="list pending asks")
    for name in ("yes", "no"):
        sub.add_parser(name, help=f"answer a pending ask: {name}").add_argument("id", type=int)
    sub.add_parser("undo", help="undo the last undoable action (or a log entry)").add_argument(
        "id", type=int, nargs="?"
    )
    sub.add_parser("log", help="recent actions").add_argument("-n", type=int, default=20)
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
    if args.group in ("apps", "run", "asks", "yes", "no", "undo", "log"):
        try:
            return _apps_command(settings, args)
        except (ManifestError, SettingsError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
