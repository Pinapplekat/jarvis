# Falak (code name jarvis)

Falak is a personal assistant: a host with an agent loop, tool apps, channels and device nodes.

- Read `ARCHITECTURE.md` before changing code, and the milestone brief in `docs/briefs/` before starting work.
- `PLAN.md` is the big picture.

## Commands

- `uv sync --extra dev`
- `uv run pytest`
- `uv run jarvis dev info`
- `bin/falak` runs this checkout's CLI from anywhere (Elijah links it into `~/.local/bin`). `falak start` runs
  Falak and this Mac's node together. It uses Falak-dev, so builders don't run it (see the Falak-dev rule).

## Live-system rules

- The live JARVIS runs from `~/Library/Application Support/JARVIS/app` with its own database. Never write
  under `~/Library/Application Support/JARVIS/`.
- Never run `jarvis schedule install` or any old `jarvis` command, and never touch
  `~/Library/LaunchAgents/com.jarvis.*`.
- Never work in the iCloud repo folder (the old JARVIS self-edits `main` there) and never commit to `main`.
  Work in this worktree on `rehaul`.
- Falak's own data lives in `~/Library/Application Support/Falak-dev/` (`FALAK_DATA_DIR`). That folder is
  Elijah's test system: never write to it. Run every `jarvis` command with `FALAK_DATA_DIR` set to a scratch
  folder, read Falak-dev only with `sqlite3 -readonly`, and report any run that touched it (see "Data" in
  `ARCHITECTURE.md`).
- No new dependencies without a reason in the brief or the commit.

Don't import from `legacy/`; read it to port behavior on purpose.
