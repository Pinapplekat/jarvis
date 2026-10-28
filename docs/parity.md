# Can Falak replace the old JARVIS? The everyday-use checklist

What Elijah used the old JARVIS for day to day, one row each, and where Falak stands. Written for M8 from
`legacy/`, the old database (read-only: 1,101 messages from him, 186 command approvals, 39 tasks, 65 self-edits,
6 reminders, 12 journal entries, 13 projects) and `~/Library/Application Support/JARVIS/commands.jsonl`.

**Status:** **works** (built; Elijah ticks the check on Discord), **M9**, **M10**, **M11**, **iPhone app**, **unscheduled**
(no milestone yet), **later** (see `PLAN.md`, "After the rehaul") or **dropped** (with the reason).

A row is done when its Discord check works. Elijah ticks the "works" rows by sending the check on Discord and
changing `[ ]` to `[x]` here.

## Talking and remembering

| Use | How the old one did it | Falak | Discord check |
|---|---|---|---|
| Chat | Telegram, `chat.py`, a short persona, the "Jev" judge vetoing continues | **works** (M3, M4): Discord DMs, the Agent SDK loop, tools from manifests | [ ] "hey, what can you do?" gets a short answer in the persona |
| Reminders and timers | `reminders.py`, checked by the 5-minute tick | **works** (M6, M7): the `reminders` app, delivered by the scheduler | [ ] "remind me in 2 minutes to stretch" arrives on time; "set a timer for 1 minute" rings once |
| Check-ins | the tick's morning and evening prompts | **works** (M6): `checkins`, retried until a message really went out | [ ] `jarvis jobs run checkins.morning` (scratch data) sends a check-in; the next scheduled one arrives at 07:00 |
| Journal | nightly entry written from the day | **works** (M6): `journal` writes at 02:00, read back on request | [ ] "read me yesterday's journal entry" |
| Canvas deadlines and nudges | `canvas.py` feed sync, nudges 24 h and 3 h before; `/weekly` read the Canvas API through Elijah's Chrome | **works** (M6, M7, M10): `school` reads Canvas through Falak's own signed-in browser (submission status, ECON's module dates, announcements that move a date, shown in the next check-in), falls back to the calendar feed when signed out, and says once. `deadline import` still covers what Canvas lacks | [ ] "what's due this week?" and "mark the ECON quiz done". [ ] after "sign in to Canvas": "what's due in ECON?" has module dates and says what's submitted |
| Memory (facts about him) | `memory.py`, facts and observations | **works** (M2): the `memory` app: remember, recall, forget, with undo | [ ] "remember my locker is 214", then "what's my locker number?" |
| Web search and pages | `web.py`, `websearch.py` | **works** (M7): `web` (search and read, no logins). A page can't make Falak send data out | [ ] "search the web for the Seattle Colleges spring break dates" |
| Pages that need JavaScript, signed-in sites | `browser.py` (Playwright, a login profile) and `/weekly` in Chrome | **works** (M10): the `browser` app on the Mac node, Falak's own profile (never his Chrome), headless by default, a window to sign in or to show him a page. Forms only with his yes, the steps in the question. Never passwords, codes, banks or this Mac | [ ] after `falak browser setup`: "read <a JavaScript-only page>" answers from the page, then "show me" opens it, then "send me a screenshot of it". [ ] "fill in the form at <a test page>" asks once with the exact steps; a no runs nothing. [ ] "pull up the week 5 checklist for English" |
| Music | `media.py`, `spotify.py`, `applemusic.py` | **works** (M7): `music` on the Mac node, Spotify first | [ ] "play something by Khruangbin", "pause", "what's playing?" |
| Texts and calls | Twilio, `calls.py`, Messages through osascript | **works** (M7): `messages` through the Mac (iMessage, iPhone calling), asks first. Twilio **dropped** from the rehaul (account suspended) | [ ] "text Mom I'll be late" shows the draft and number and sends only after a yes |
| Sending images | `attachments.py`, ATTACH lines | **works** (M5): `falak__send_image` in a chat turn | [ ] "send me a screenshot of my screen" |

## Seeing

| Use | How the old one did it | Falak | Discord check |
|---|---|---|---|
| Camera | `camera.py`, a signed camera app | **works** (M2, M5): `camera` photo and live view on the Mac node | [ ] "take a photo with the desk camera" |
| Screen and live views | `screen.py`, `spotting.py`, 662 screen peeks | **works** (M5): `screen` look and live view, secrets screened out | [ ] "what's on my screen?", then "pull up homework view" and ask what changed |

## Work

| Use | How the old one did it | Falak | Discord check |
|---|---|---|---|
| Running commands | `terminal.py`: graded by risk, 186 of 192 approval requests were terminal commands, nearly all answered yes | **works** (M8): `terminal__run` grades in a sandbox, so reading and project work runs without asking; `git push`, installs elsewhere and anything unknown ask once with the exact command. `git -C <path>`, `open`, `lsof`, `sed -n` and Trash deletes no longer ask | [ ] "what's the git status of falak?" answers without asking. [ ] "run the tests in m8-sandbox" (add a trivial test first) runs without asking. [ ] "delete notes.txt in m8-sandbox" moves it to the Trash; "undo that" brings it back. [ ] "push m8-sandbox" asks with the exact command and fails plainly (no remote) |
| Background jobs | `jobs.py`, approved commands past 120 s | **works** (M8): background `run`, a watcher that sends one message when a job ends or goes quiet | [ ] "run `python3 -m http.server 8999` in m8-sandbox in the background", then "what jobs are running?", then "stop it": one message when it stops |
| Tasks | `tasks.py` and `agent.py`: Claude Code sessions in a folder, 39 run so far | **works** (M8): `start_task` asks once with the plan; the session runs on the Mac in a sandbox with every tool call graded; one ✅ or ❌ message at the end. Not ported: mid-task approvals, the self-approving judge, the "done" double-check, web tools, ATTACH images | [ ] "in m8-sandbox, add a hello.py that prints hi and a test for it, then commit" asks once with the plan, runs, and sends one ✅. [ ] "how's the task going?" mid-way answers from its steps. [ ] a task told to push lists the push under NEEDS YES instead. [ ] restarting `falak start` mid-task resumes it and still sends one ✅ |
| Projects | `projects.py`, 13 projects with briefs | **works** (M8): `projects` carries them over, with live git state from the Mac | [ ] "what's the state of Vaulted?" answers from the imported project with its branch and uncommitted files |
| Project briefs | a scheduled Claude job rewrote each brief | **later**: the old briefs are imported as text, and Falak updates one when asked (`update_project`) | [ ] "add a note to Vaulted: waiting on the new PCB" |
| Mail | `mailwatch.py`, `mailrelay.py`, `inbox.py`: Mail.app through osascript | **unscheduled**: not in a milestone yet. Graph is closed for Seattle Colleges, so it needs the Mac node | none yet |

## Around him

| Use | How the old one did it | Falak | Discord check |
|---|---|---|---|
| Desk voice | `voice.py`, `ears.py`, `warm.py`, "Hey JARVIS" | **M9**: wake word, Whisper, Kokoro, Discord voice calls | none yet |
| HUD, panel, menubar | `hud.py`, `panel.py`, `menubar.py` | **M11**: old UI reading from the new host. UI overhaul is **later** | none yet |
| Location | Telegram location intake, `location.py`, 8 places | **iPhone app**: location arrives with the app, not through Telegram | none yet |
| Terminal sense | a zsh hook logged every command (86 observations) | **later**: nothing in Falak reads a timeline yet | none yet |
| Git sense | `repos.py` noticed commits and branch switches (31 observations) | **later**: same, no timeline yet. `find_repos` and `repo_state` exist for projects | none yet |
| Self-edit | `selfedit.py`, approved plans built on branches (65 so far) | **later**: tasks never edit Falak's own code or data (`read_only` folders). Comes back with prod mode, see `PLAN.md` | none |
| Prod mode | the nightly log review that built tested fixes on branches, plus weekly feature proposals | **later**: stopped with the old JARVIS on 2026-10-27; returns with self-edit, once there are several nodes (`PLAN.md`, "After the rehaul") | none |
| Watches | `watches.py`, 2 watches | **dropped** for now: an open decision in `PLAN.md` (rebuild as triggers, or drop) | none |
| Phone line | `phone.py`: call JARVIS from his cell through Twilio | **dropped** from the rehaul: the Twilio account is suspended. Revisit with the iPhone app | none |

## What has to be true before the old JARVIS can retire

1. Every **works** row above is ticked on Discord.
2. Every other row is either a named milestone (M9, M10, M11, iPhone app), **unscheduled** with Elijah knowing it, or
   **later** or **dropped** for a reason written here.
3. Mail is the one everyday thing with no milestone. Decide before M11 whether the old JARVIS keeps the mail
   watch until it has one.
