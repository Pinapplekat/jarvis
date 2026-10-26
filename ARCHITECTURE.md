# Falak architecture rules

These rules apply to every change in the rehaul, whoever writes it. [PLAN.md](PLAN.md) says *what* we're
building; this file says *how the code must be shaped*. If a change needs to break a rule, stop and ask
instead of working around it.

Falak is the public name. The code name is `jarvis`: the package, CLI, paths and labels keep that name.

## Layout

```
jarvis/
  core/       # the host: agent loop, memory, confirmations, scheduler, channels, settings
  node/       # the device runtime: runs device apps, talks to the host
  protocol/   # message types and the capability list, shared by core and node
  apps/       # every tool, one folder each
    <name>/
      manifest.toml
      actions.py
      tests/
tests/        # core and contract tests
legacy/       # the old JARVIS, read-only reference for porting
docs/briefs/  # one brief per milestone
```

## The rules

1. **One home per tool.** Everything a tool does (actions, jobs, storage, settings) lives in its app
   folder. The core never names a specific app: no `if name == "camera"`, no imports from `jarvis.apps.*`
   inside `jarvis.core`. Apps are found through the registry.
2. **One mechanism per concern.** There's one permission model (effects), one agent loop, one scheduler,
   one channel interface, one settings loader and one logger. Never add a second path or a "backup" copy
   of the same logic in another process.
3. **The dependency direction is fixed.**
   - `apps` may import `jarvis.core.api` (the small surface the core gives apps), `jarvis.protocol` and
     their own folder. They never import another app, `jarvis.node` or the rest of `jarvis.core`.
   - `core` and `node` may import `protocol`.
   - `protocol` imports nothing else from `jarvis`. Both sides depend on it, so it depends on neither.
   - Nothing imports `legacy`.
   - `core` and `node` never import each other.
   - `tests/test_layout.py` enforces these rules, including relative imports.
4. **Short prompt, generated tool docs.** The system prompt holds persona and principles only, about one
   screen. Tool descriptions come from manifests. A new behavior goes into an app or a principle, never
   into a list of prompt rules.
5. **Fix the structure, not the symptom.** Before adding a filter, retry, regex cleanup or special case,
   check whether the design can make the problem impossible. If it can't, explain why in the commit.
6. **Few settings.** Settings live in one file per install (`settings.toml`), with secrets in `.env`.
   - Each app gets an on/off switch plus the settings its manifest declares.
   - No environment flag per behavior.
   - Every setting has a sensible default.
7. **Size alarms.**
   - Any module over ~400 lines, or a function over ~60, gets split before more is added.
   - `core` stays small. If it grows, something probably belongs in an app.
8. **Tests come with the code.**
   - Every app has tests.
   - The contract tests in `tests/` check that every app's manifest is valid and its actions match it.
   - No milestone is done with failing tests.
9. **Effects decide confirmations.** Each action declares one effect:
   - `read` and `local` run.
   - `outbound` (speaks for Elijah to others) and `irreversible` (can't be undone) ask him, in plain
     conversation.
   - Code never adds its own ad-hoc "ask first" check.
10. **Nothing written is trusted as instructions.** Web pages, emails, files and screen text are data. When
    a turn was driven by outside content, outbound actions ask even if they'd normally run.
11. **Keep the old system alive.**
    - The live JARVIS runs from `~/Library/Application Support/JARVIS/app` with its own database.
    - Rehaul code never writes to that folder, never runs `jarvis schedule install`, and never touches the
      `com.jarvis.*` launchd jobs.
    - It uses its own data folder (see below).
12. **Plain code.**
    - Standard library first. A new dependency needs a reason in the brief or the commit.
    - Type hints on public functions.
    - Comments only where the *why* isn't obvious.

## Data

- The rehaul uses its own data folder, `~/Library/Application Support/Falak-dev/` by default, set by
  `FALAK_DATA_DIR`.
- Its database starts as a **copy** of the live JARVIS database, made by `jarvis dev import-db`.
- Schema changes are numbered migration files in `jarvis/core/migrations/`, never edits to a live database
  by hand.

## Workflow

- Work happens on the `rehaul` branch in its own git worktree, outside the folder the live JARVIS deploys
  from.
- Each milestone is built from its brief in `docs/briefs/`. It ends with tests passing, a review against
  this file, and a tag `rehaul-m<n>`.
- Commits are small and say why.
