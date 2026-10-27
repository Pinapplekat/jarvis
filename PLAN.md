# Falak rehaul plan

Drafted 2026-10-26. This is the plan only: none of it is built yet. The code is built later by Claude
Sonnet from milestone briefs (see "Handoff").

## Goal

Rebuild JARVIS as **Falak**: an assistant that feels like someone you're talking to, not a program
asking for permission. It's structured so it can't grow back into a tangle.

The rehaul covers **the host and the Mac node only**. The iPhone, Watch, Mac mini, glasses and the UI
come after it.

## Why rebuild instead of patching

Today's JARVIS works, but it grew by patches into a mix of things you don't like:

| Sign | Today |
|---|---|
| Size | 72 modules, ~21,900 lines; [chat.py](jarvis/chat.py) 1,553 lines, [cli.py](jarvis/cli.py) 1,635 |
| System prompt | 316 lines (~28,600 characters) of rules added one patch at a time |
| Settings | 134 environment variables in [config.py](jarvis/config.py), most of them on/off switches for single behaviors |
| One tool | Spread across the prompt text, hand-kept lists, and ~56 `if kind == ...` branches |
| Confirmations | 192 approval requests in 2 weeks; 186 were for terminal commands, including `ls`, `cat`, `grep`, `tail` and `git` marked "risk: none". You said yes to almost all of them. |
| Agent loop | A second model vetoes most of the follow-up steps the first one wants (see "Agent loop") |
| Tick | 17 unrelated steps every 5 minutes, mostly doing nothing |
| Patches on patches | An honesty filter, rewrite rules and retries that clean up problems the structure creates |

What's worth keeping is carried over on purpose, not by copying files: the memory database and its data,
the honesty principle (only claim what happened), parked turns that resume, the persona, Kokoro voice,
local Whisper, the camera app, and the terminal risk grading.

**How:**
- The new code is built on a branch in a clean layout.
- The old code moves to `legacy/` as a reference to port from.
- The JARVIS you use now keeps running from its deployed copy (`~/Library/Application Support/JARVIS/app`)
  and stays frozen (no new features) until the new one reaches parity and takes over.

## How Falak should feel

- **A person, not a program.** Falak talks like someone helping you: it acts, then tells you what it did.
- **It doesn't ask permission for things that are its job.** Looking at the screen or camera, running
  commands that read or build things, opening apps, playing music, setting reminders, saving notes: it
  just does them.
- **It confirms only what you'd want confirmed, and asks like a person:** "Want me to send this to
  Professor Lee?" or "Here's my plan for the refactor, go?" Your normal reply answers it. No "command
  #70", no Run/Reject buttons on every command, no ritual.
- **Persona:** dry wit, brief, candid, calls you "sir" (unless you decide otherwise).

## Confirmations, redesigned

Falak asks before only three kinds of things:

1. **Speaking for you to other people:** sending an email, text, message or post to anyone but you, or
   placing a call. It shows the draft in a sentence and asks.
2. **Things that can't be undone:** deleting outside a scratch area, `git push --force`, spending money,
   uninstalling, changing system settings.
3. **Plans you want to steer:** big multi-step work (a refactor, a new app, a long task). It proposes the
   plan; you say go or change it.

Everything else runs, made safe by **containment and undo** instead of questions:
- **Commands** run in a macOS sandbox that can write only to your project folders and a scratch area
  (`agent.py` already does this for tasks). Reading, searching, building, testing and local `git` all
  just run. A push, an install or `sudo` falls under rule 2 or asks once.
- **Deletes go to the Trash**, edits happen inside git, and Falak keeps an action log, so "undo that"
  works.
- **Content from outside can't speak for you.** If a web page, email or screen text is what's pushing
  Falak toward an outbound action, it asks, even when you've otherwise let that action run freely. This
  is the one security rule worth its friction.
- **Trust is learned in plain words.** "You don't need to ask me before emailing myself" becomes a saved
  rule. Falak says it in one line, you can see it in the rules list, and you can undo it. No before/after
  approval screens.
- **Asking works on any channel.** At the desk it asks out loud and hears "yeah, send it"; on Discord a
  normal reply works. Buttons are a shortcut, never required.

These map straight onto app effects: `read` and `local` run, `outbound` and `irreversible` ask (see
"Tools are apps").

## Keeping it from getting messy again

Rules written into `ARCHITECTURE.md` and the repo's `CLAUDE.md`, so every builder, Sonnet included,
follows them:

1. **One home per tool.** Everything a tool does lives in its app folder. The core never names a
   specific app (no `if kind == "camera"` anywhere in the core).
2. **One mechanism per concern:** one permission model (effects), one agent loop, one scheduler, one
   channel interface, one settings file. No "backup" copies of the same logic in two processes.
3. **Short prompt, generated tool docs.** The system prompt holds persona and principles only, about one
   screen. Tool descriptions come from manifests. A new behavior goes into an app or a principle, never
   into a growing list of prompt rules.
4. **Fix the structure, not the symptom.** Before adding a filter, retry or special case, ask whether the
   design can make the problem impossible. For example, replies are written after results, so there's
   nothing to filter.
5. **Few settings.** Each app has an on/off switch and the settings its manifest declares. No env flag for
   every behavior. Defaults should be right.
6. **Size alarms.** A core module over ~400 lines or a function over ~60 lines gets split before more is
   added.
7. **Every app ships with tests**, and the core has contract tests that any new app must pass.
8. **Changes start from a brief.** Each milestone has a written brief with done criteria. A review pass
   checks the result against these rules before merging.
9. **Self-edit stays off** until the rehaul is stable. When it comes back it may only add or change apps,
   never the core.

## Architecture

| Piece | Package | Runs on | Holds |
|---|---|---|---|
| **Host** | `jarvis.core` | MacBook Neo now, Mac mini later | Memory DB, agent loop, confirmations, scheduler, channels (Discord, Telegram), Claude |
| **Mac node** | `jarvis.node` | Every Mac | The runtime for device apps: screen, camera, mic, voice, terminal, Mail, music |
| **Protocol** | `jarvis.protocol` | Shared | Message types, capability list, pairing |
| **Apps** | `jarvis/apps/<name>` | Host or a node | Every tool, self-contained |
| *Later:* iPhone / Watch | `clients/apple` (Swift) | Phone, Watch | The same app manifests in Swift |

- **One repo.** The Apple clients go in `clients/apple` so they're never confused with tool apps.
- **Protocol:**
  - Nodes connect to the host over a WebSocket on Tailscale, with one key per device.
  - Message types: **events** (node to host), **commands** (host to node), **presence** (which device
    you're at, so replies go there).
  - In the rehaul the host and the Mac node run on the same MacBook over localhost, but as separate
    processes from day one.
- **Name:** the public name **Falak** (فلك, orbit or celestial sphere) is one setting, `ASSISTANT_NAME`.
  - `jarvis` stays the internal code name: package, CLI, launchd labels, paths.
  - "Hey Falak" needs its own openWakeWord model, tested against sound-alikes like "fall back."
  - Pronunciation is settled once (FA-lak) for Kokoro and given to Whisper as a spelling hint.
- **Apple apps (later):** signed under the **Vaulted LLC** developer account.

## Tools are apps

Each tool is a small, self-contained app in the repo. They aren't apps you open; they're structured like
apps so each tool lives in one place.

```
jarvis/apps/
  camera/
    manifest.toml     # what it does: actions, input schemas, effects, where it runs, sessions, jobs
    actions.py        # the handlers
    tests/
  calculator/
  recorder/
  ...
```

**Manifest** (TOML, so the Swift clients can later read the same file):
- **Actions:** name, a description written for the model, and a JSON Schema for the input. These become
  the native tools the agent loop offers.
- **Effect of each action:**
  - `read`: looks at something. Runs.
  - `local`: changes something on your own devices. Runs, and can be undone.
  - `outbound`: speaks for you to others. Asks.
  - `irreversible`: can't be undone. Asks.
- **Where it runs:** `host`, or a node capability such as `camera`. The host routes the call to a device
  that has it.
- **Sessions:** start, sample and stop for long-running actions. "Pull up homework view" is the camera
  app's `live_view` session: Falak watches the feed, follows along and points things out. The webcam
  works, and so does the iPhone as an overhead desk camera (Continuity Camera's Desk View).
- **Jobs and triggers:** apps register their own scheduled jobs and event triggers (mail owns mail watch,
  school owns the Canvas sync).
- **Views:** reserved for the UI overhaul. Nothing is built for them now.

**The core gives every app:** its own storage namespace, only the secrets its manifest names, logging,
notifications through the channel interface, a way to ask you something, and events.

**Picking tools per turn:** the router (Jev) picks the apps that look relevant, plus an always-on set
(memory, calculator, camera, screen). The Agent SDK's tool search covers the rest.

**Senses are ordinary tools.** Falak uses the camera, screen and mic whenever they help. The safeguards:
- macOS shows when they're in use, and Falak says when it looked.
- Nothing is stored unless you ask.
- Banking and password-manager apps are skipped by default.

**Apps in the rehaul**, mostly ported from today's modules:

| App | Ported from | Runs on |
|---|---|---|
| camera (photo, live view) | camera.py | Mac node |
| screen (look, screenshot, live view, click) | screen.py, clicker.py, attachments.py | Mac node |
| recorder / mic (record, transcribe, listen sessions) | ears.py, speech.py | Mac node |
| voice (speak, wake word) | voice.py, deployvoice.py | Mac node |
| calculator | new | host |
| memory and notes | memory.py, journal.py | host |
| terminal (sandboxed) and jobs | terminal.py, jobs.py, agent.py sandbox | Mac node |
| mail (read, draft, send, watch) | mailrelay.py, mailwatch.py | Mac node + host |
| reminders, timers, alarms | reminders.py, siri.py | host + Mac node |
| music | spotify.py, applemusic.py, media.py | Mac node |
| web (search, open, browse) | websearch.py, web.py, browser.py | host |
| school (Canvas, deadlines) | canvas.py | host |
| location and places | location.py | iPhone app (after the rehaul) |
| projects and repos | projects.py, repos.py | host |
| tasks (long-running Claude Code work) | tasks.py, agent.py | host |

## Agent loop

What's kept as principles: replies claim only what happened, turns that wait (on your answer, a job or a
device) park and resume, there are step and time limits, and every step is logged.

What changes, based on 333 real turns (Oct 22-26):
- About 9 in 10 replies were one model call.
- The Jev judge vetoed 85 of the 116 times the loop leaned toward continuing.
- 106 extra "write the answer, don't act" calls.
- 30 replies had promises cut out by the honesty filter.

New loop:
- **Native tool calls through the Claude Agent SDK.** It runs on the same Claude login you use now with
  `claude -p`. Anthropic's rule against claude.ai login applies only to products offered to other people.
- Tools come from the app manifests. The model decides when it's done. Confirmations come from effects.
- The model writes its reply **after** it has seen results, so there's nothing to filter.
- One loop for chat and long tasks (today these are [loop.py](jarvis/loop.py) and
  [agent.py](jarvis/agent.py)).
- Jev only routes: which apps and context a turn needs, and which model.

## Message channels

- **Channel interface:** send, ask, edit, show typing, incoming messages. Each channel is a plug-in.
- **Discord** is the main way to message Falak: the bot's DMs, voice messages, photos, and buttons as
  optional shortcuts.
- **Telegram** is dropped. Location comes with the iPhone app (decided 2026-10-27).
- **The desk** (mic and speakers) is a channel too.
- **Calls:**
  - Bots can't ring you. A call means joining a voice channel in a private server, and Falak joins too.
  - Python can't hear under Discord's end-to-end encryption (required since March 2026), so listening
    uses a small Node add-on (`@discordjs/voice`, Node 22). Falak answers in the Kokoro voice.
- **Urgent alerts:** optional Pushover emergency priority, since Discord can't ring.
- **Twilio** is off: the account reports "not active."

## Scheduler (replaces the tick)

- Apps register jobs with a trigger (interval, time of day or event), needs (network, Mail, a channel), a
  timeout, backoff and a quiet-hours rule.
- Jobs run independently inside the host process, so a slow Claude job never blocks a reminder.
- **Delivery** (reminders, alerts, approved sends) happens exactly when due or as soon as something is
  queued, not on a 5-minute poll.
- **Sync jobs** skip quietly when offline and report a broken integration once.
- **Thinking jobs** (check-ins, journal, heartbeat) share one Claude budget and react to changes instead of
  polling.
- Each job keeps its last run, next run, last error and duration.
- Watches never triggered in the old tick: rebuild them as a trigger feature, or leave them out.

## Milestones (host + Mac node rehaul)

Each milestone becomes one brief for Sonnet, with goal, files, interfaces, tests and done criteria.

| # | Milestone | Done when |
|---|---|---|
| M1 | **Skeleton:** branch, new layout, old code to `legacy/`, settings, logging, DB access to the existing memory data, `ARCHITECTURE.md` + `CLAUDE.md` rules, test setup | `jarvis --version` runs; contract tests run; old JARVIS untouched |
| M2 | **App contract:** manifest format, registry, effects → confirmation policy, action log + undo; apps: calculator, memory, camera (photo) | Apps load from manifests; an outbound test action asks, a read action doesn't |
| M3 | **Agent loop** on the Agent SDK: persona prompt, tools from manifests, conversational asking, parking/resume, honesty by construction | A chat turn uses two apps in sequence and replies after the results; "send it" answers a pending ask |
| M4 | **Channels:** interface, Discord text, desk text input, Telegram location intake | Talk to Falak in Discord DMs; location still arrives |
| M5 | **Mac node:** separate process, protocol over localhost, capability routing; screen, camera live view, recorder apps with sessions | "Pull up homework view" opens a live feed Falak can describe as it changes |
| M6 | **Scheduler:** job registry; port reminders, mail watch, Canvas, check-ins, journal into their apps | Reminders fire on time; the launchd tick isn't needed |
| M7 | **Apps that share, everyday Mac apps:** `ctx.call` between apps, check-in retry, school `deadline import`, web, music, messages, timers | Check-ins read through `ctx.call`; search, music, texts and timers work on Discord |
| M8 | **Work apps:** terminal (sandboxed), background jobs, tasks (Claude Code sessions), projects and repos | Parity checklist for everyday use passes |
| M9 | **Voice:** "Hey Falak" wake word, Whisper, Kokoro at the desk; Discord voice calls | Talk at the desk and on a Discord call |
| M10 | **Cutover:** data migration check, old UI (HUD, Panel, Inbox) reading from the new host, launchd jobs switched, old code retired | You use only Falak for a week without falling back |

Mail (read, draft, send, mail watch) isn't scheduled yet. It needs the Mac node, since Graph is closed for
Seattle Colleges and the old JARVIS reads Mail.app.

## After the rehaul

1. iPhone app (Vaulted LLC account): pairing, location (places, "did he make it to class"), HealthKit, notifications, voice,
   camera, using the same app manifests in Swift.
2. Watch app: quick answers, dictation, heart rate, haptics.
3. Mac mini: move the host, add local models. The MacBook becomes a node.
4. UI overhaul: HUD, panel, live-view display ("homework view"), app views, phone and watch screens.
5. Glasses: another node.
6. Canvas through Falak's own signed-in browser, since personal tokens are off and the feed misses ECON and
   ENGL work: [docs/ideas/canvas-browser.md](docs/ideas/canvas-browser.md).

## Handoff

- When you're ready, each milestone gets a brief in `docs/briefs/M<n>.md`, written from this plan, with:
  - what to build and what not to touch
  - the interfaces it must match
  - tests and done criteria
  - the rules from "Keeping it from getting messy again"
- Sonnet builds one milestone per session on the rehaul branch.
- After each one, a review pass checks it against the done criteria and `ARCHITECTURE.md`, then it's
  tagged.

## Decided

- Name **Falak**; internal code name `jarvis`.
- Apple developer account: **Vaulted LLC**.
- Tools are apps in `jarvis/apps/`.
- Confirmations only for speaking for you, irreversible actions and plans you want to steer.
- Rebuild host + Mac node first; mobile and UI later.
- Old JARVIS frozen until cutover.
- Built by Sonnet from milestone briefs.

## Open decisions

- **Twilio:** reactivate the account or drop phone calls.
- **Watches:** rebuild as triggers, or drop.
- **Voice:** which Kokoro voice, and whether "sir" stays.
- **Branch vs. new repo:** the plan assumes a branch in this repo.

## Constraints

- Hosting stays under $10/mo, not counting AI. Claude runs on your subscription.
- The Mac has 8 GB of RAM: the node stays light, and local models wait for the mini.
- Until the mini exists the host sleeps with the laptop.
