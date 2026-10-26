"""Thin command line: parse arguments, call core functions."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from jarvis import __version__
from jarvis.core import db
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


def build_parser(name: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jarvis")
    parser.add_argument("--version", action="version", version=f"jarvis {__version__} ({name})")
    sub = parser.add_subparsers(dest="group")
    dev = sub.add_parser("dev", help="developer commands").add_subparsers(dest="command")
    dev.add_parser("info", help="show data folder, database and migrations")
    imp = dev.add_parser("import-db", help="copy the live JARVIS database into the Falak data folder")
    imp.add_argument("--source", help="source database (default: the live JARVIS database)")
    imp.add_argument("--overwrite", action="store_true", help="replace an existing falak.db")
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
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
