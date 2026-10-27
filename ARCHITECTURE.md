# Falak architecture rules

These rules apply to every change in the rehaul, whoever writes it. [PLAN.md](PLAN.md) says *what* we're
building; this file says *how the code must be shaped*. If a change needs to break a rule, stop and ask
instead of working around it.

Falak is the public name. The code name is `jarvis`: the package, CLI, paths and labels keep that name.

## Layout

```
jarvis/
  core/       # the brain side: agent loop, memory, confirmations, scheduler, channel interface, settings
  channels/   # one adapter per way of talking to Falak (discord.py, terminal.py)
  host.py     # composition root: loads apps and channels, routes each message to the agent loop
  contract/   # what apps, host and node share: handler API, manifest parser, schema subset
  node/       # the device runtime: runs device apps, talks to the host
  protocol/   # host-node message types, shared by core and node
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
   - `contract` (the handler API, manifests and the schema subset) imports nothing else from `jarvis`.
   - `apps` may import `jarvis.contract.api`, `jarvis.protocol` and their own folder. They never import
     another app, `jarvis.node` or `jarvis.core`.
   - `core`, `node` and `channels` may import `contract` and `protocol`.
   - `channels` may import `jarvis.core.channel` and nothing else from the core. Only `channels/discord.py`
     imports `discord`.
   - `core` never imports `channels` or `host`. `host.py` is the one place that wires them together.
   - `core` and `node` never import each other.
   - `protocol` imports nothing else from `jarvis`. Both sides depend on it, so it depends on neither.
   - Nothing imports `legacy`.
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
    a turn was driven by outside content, outbound actions ask even if they'd normally run, and so do
    actions marked `sends_data` (a search query or a URL leaves the Mac, so a page can't make Falak send out
    what it read). A `sends_data` action carries a `confirm` question for that case.
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
- `Falak-dev` is Elijah's: he runs `jarvis serve` on it and tests against it. Builders never write to it. Any
  `jarvis` command opens the database, migrates it and can queue messages, so run them with `FALAK_DATA_DIR` set to
  a scratch folder (copy the data in first with `sqlite3 <Falak-dev>/falak.db ".backup '<scratch>/falak.db'"` if
  you need real data). Look at Falak-dev only read-only (`sqlite3 -readonly`). If a run against Falak-dev
  happens anyway, say so in the report back with what it changed; never clean up after it by hand.

## Devices

- Apps with `runs_on = "node:<cap>"` run in the **node** process, a separate program that connects to the host's
  WebSocket (`node_listen`, `127.0.0.1:8765` for now). Validation, confirmations, the action log, asks and undo
  stay on the host; `core/dispatch.py` is the one place a handler is run, in-process or on a node.
- The host never imports a node app's code, and the node never opens the host database or imports `core`.
- Nodes pair with `jarvis node pair <name>`; the host stores only the SHA-256 of the key. The node keeps its own
  folder, `FALAK_NODE_DIR` (default `~/Library/Application Support/Falak-node/`).
- Pictures a tool returns reach the model and are then deleted. One goes to Elijah only when the model calls
  `falak__send_image` in a chat turn; it is held in memory and attached to the reply, never written to disk. The
  brain runs Claude Code with `--no-session-persistence`, so it keeps no copies either.
- A manifest `[[sessions]]` entry (a live view) gives the model `_start`, `_look` and `_stop` tools. The host owns
  the `sessions` table; the node owns the running capture and closes it when the host link drops.

## Apps calling apps

- An app reads only its own tables (`<app>_*`). To read another app's data it calls that app's **shared**
  action with `ctx.call(app, action, **args)`, which goes through the normal dispatch (host or node), on the
  caller's connection, and isn't written to the action log.
- The caller lists the app in `[app] uses`; the action has `share = true` and must be a `read`. An action
  only other apps need sets `tool = false` so the model never sees it.
- A missing or turned-off app, or a node that isn't connected, raises `AppUnavailable`: the caller shows
  "not available" instead of failing.
- `tests/test_share.py` fails if an app's code names another app's tables or the core's.

## Jobs and the outbox

- An app's `[[jobs]]` are scheduled work that runs inside `jarvis serve` (host apps only). `core/scheduler.py` is the
  one scheduler: it replaces the old 5-minute tick, and nothing else should loop on a timer.
- A handler is `handler(ctx, run: JobRun) -> JobResult`. It runs in a worker thread with its own SQLite connection,
  never the shared one. It can ask to run sooner (`JobResult.wake_at`, `ctx.wake("<job>")`) and can ask for a
  **thinking turn** (`JobResult.think`): the same agent loop as a chat turn, with the origin `job:<app>.<job>`, so
  `answer_ask` refuses there. Thinking turns wait behind chat and count against `thinking_runs_per_day`.
  `Think.on_done` names a handler the scheduler calls after a turn that didn't fail or get skipped, so a job
  can record "done for today" only once its message really went out.
- Everything Falak says first is a row in the `outbox` table (`Host.notify`). One sender task delivers it through
  `notify_channel`, keeps failures and retries, and adds each sent message to the conversation. Nothing is dropped.
- The scheduler's state lives in the `jobs` and `job_runs` tables, so a restart neither repeats nor forgets a run.
  Quiet hours, the timezone and the catch-up window are core settings (`quiet_hours`, `timezone`, `catch_up_minutes`).

## Workflow

- Work happens on the `rehaul` branch in its own git worktree, outside the folder the live JARVIS deploys
  from.
- Each milestone is built from its brief in `docs/briefs/`. It ends with tests passing, a review against
  this file, and a tag `rehaul-m<n>`.
- Commits are small and say why.
