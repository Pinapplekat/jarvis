# Falak (code name jarvis)

Falak is a personal assistant: a host with an agent loop, tool apps, channels and device nodes.

- Read `ARCHITECTURE.md` before changing code, and the milestone brief in `docs/briefs/` before starting work.
- `PLAN.md` is the big picture.

## Commands

- `uv sync --extra dev`
- `uv run pytest`
- `uv run jarvis dev info`

## Live-system rules

- The live JARVIS runs from `~/Library/Application Support/JARVIS/app` with its own database. Never write
  under `~/Library/Application Support/JARVIS/`.
- Never run `jarvis schedule install` or any old `jarvis` command, and never touch
  `~/Library/LaunchAgents/com.jarvis.*`.
- Never work in the iCloud repo folder (the old JARVIS self-edits `main` there) and never commit to `main`.
  Work in this worktree on `rehaul`.
- Falak's own data lives in `~/Library/Application Support/Falak-dev/` (`FALAK_DATA_DIR`).
- No new dependencies without a reason in the brief or the commit.

Don't import from `legacy/`; read it to port behavior on purpose.
