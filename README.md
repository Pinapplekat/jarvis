# JARVIS

Personal assistant: persistent memory, school/work/money awareness, approved email sending, phone alerts.

## Setup

```bash
uv sync --extra dev
cp .env.example .env
```

The virtualenv lives in `.venv.nosync` (symlinked as `.venv`) so iCloud doesn't sync it.

## Memory

```bash
uv run jarvis remember school major "Computer Science"
uv run jarvis project "My Shop" business --summary "Online store" --goals "First 10 sales"
uv run jarvis note "Launched landing page" --project "My Shop"
uv run jarvis context
```

## Email (Apple Mail relay)

The school tenant blocks Graph API apps, so JARVIS sends and reads mail through Apple Mail on this Mac. New Outlook for Mac can't be scripted.

1. Add the school account to Mail: System Settings → Internet Accounts → Microsoft Exchange.
2. Set `MAIL_ACCOUNT` in `.env` to the address to send from.

```bash
uv run jarvis mail-accounts
uv run jarvis inbox --limit 10
uv run jarvis draft --to someone@example.com --subject "Hi" --body "..."
uv run jarvis queue
uv run jarvis approve 1        # or tap Approve in Telegram
uv run jarvis relay            # sends everything approved
```

The first run triggers a macOS prompt to let your terminal control Mail. **The background jobs need the same permission separately:** the first time they touch Mail, macOS asks whether `uv` (or Python) may control Mail. Click OK, or turn it on later in System Settings → Privacy & Security → Automation. Until then, background reading and sending time out (tick logs "Mail didn't answer in time") while everything else keeps working. Approved emails only go out while the Mac is awake.

**Inbox in chat:** JARVIS sees your last 15 inbox headers (sender, subject, time, unread; never bodies), so "what's in my inbox?" works.

**Important-mail pings:** each `tick` checks unread mail and texts you about messages from `SCHOOL_EMAIL_DOMAINS` (default `seattlecolleges.edu`), from email addresses you've told JARVIS about (school facts), or with subjects mentioning deadlines, grades, withdrawals and the like. It pings once per email, waits until quiet hours end, and the first run skips mail that was already unread. Turn it off with `MAIL_WATCH=0`.

## Approvals and alerts (Telegram, free)

Each queued draft arrives in Telegram with **Approve / Reject** buttons. Typing `yes 3` / `no 3` (or just `yes` when one is waiting) works too. Only your chat is obeyed, and a reply only counts for drafts sent before it.

Setup:
1. In Telegram, message **@BotFather** → `/newbot` → pick a name and a username ending in `bot`. It replies with a token.
2. Paste the token into `TELEGRAM_BOT_TOKEN` in `.env` yourself.
3. Open your new bot in Telegram and press **Start**.
4. Run `uv run jarvis telegram-setup`. It finds your chat, saves `TELEGRAM_CHAT_ID`, and sends a hello.

```bash
uv run jarvis digest --send    # week ahead to Telegram
```

### Chatting with JARVIS

Message the bot like a person: "what's due this week?", "remember rent is due on the 1st", "add ECON module 4 due Sunday", "email prof@school.edu asking for a 2-day extension". JARVIS answers in a few seconds. `jarvis ask "..."` does the same from the terminal.

- **Brain:** the Claude Code CLI on this Mac (`claude -p`), so it uses your Claude subscription, not a separate API bill. Model: `JARVIS_MODEL` (default `sonnet`; `haiku` is faster and lighter on usage limits).
- **Locked down:** Claude runs with no tools, no MCP servers and no personal settings. It can only *propose* actions (remember/forget a fact, log a note, update a project, add or finish a deadline, draft an email). JARVIS checks each one before doing it. Email is only ever drafted, and still needs your Approve tap.
- **Context it sees:** saved facts, projects, recent events, the next 14 days of deadlines, drafts awaiting approval, and the last 12 chat messages.
- **Voice:** short and casual, matching how you text. Tweak the "Voice" section of `SYSTEM_PROMPT` in `jarvis/chat.py` to change it.
- **Links:** send a link and JARVIS opens it in its own headless Chromium (JavaScript runs, so wikis and JS-heavy agendas work) before answering. It can also open links it remembers or finds on pages it read, up to 3 rounds and 6 pages per message. It has no access to your browser's logins, so pages behind Canvas or Google sign-in won't load. Local and private network addresses are blocked, and page text is treated as untrusted data. One-time setup: `uv run playwright install chromium`.
- **Character:** Tony Stark's JARVIS: calls you "sir", dry wit, candid pushback, still brief.
- **Speaks out loud:** reminders, alerts and important-mail pings are spoken through the Mac's speakers (macOS `say`, voice `VOICE_NAME`, default British "Daniel") when you've used the keyboard or mouse in the last `VOICE_IDLE_SECONDS` (180) and it isn't quiet hours. Telegram always gets the text too. `jarvis say "..."` tests the voice. `VOICE=0` turns it off.
- **Reminders:** "text me at 9 to finish econ" → JARVIS messages you then (within ~25s; late reminders say so).
- **Improving itself:** ask "add a /status command" (or JARVIS spots a gap; it may suggest one idea a day after `IDEA_HOUR`). JARVIS writes a plan (steps, files, risks) and sends it with **Yes / Not now**. Reply with changes to get a revised plan. A "yes" saves it to `~/Library/Application Support/JARVIS/plans/`. Then either:
  - **self-editing off (default):** in Claude Code, say "build the approved JARVIS plans" (`jarvis plans` lists them, `jarvis plans done <id>` marks one built); or
  - **self-editing on:** JARVIS builds it itself (see below) and sends you the result with **Deploy / Discard**.
- **Always on:** `jarvis listen` keeps a connection open to Telegram (it also handles the Approve/Reject buttons). `jarvis schedule install` runs it in the background.

## Canvas deadlines and weekly digest

Seattle Colleges blocks student access tokens, so JARVIS reads your **Calendar Feed** instead:

1. In Canvas: Calendar → **Calendar Feed** (bottom right) → copy the link.
2. Paste it into `CANVAS_ICS_URL` in `.env` yourself. Treat it like a password: anyone with the link can see your Canvas calendar.

The feed has dates but not submission status, so feed deadlines only get the 24h "check it's done" text, never the 3h call. If IT ever issues a token, set `CANVAS_TOKEN` and JARVIS switches to the API (with status, ECON module dates, and calls).

```bash
uv run jarvis canvas           # fetch deadlines, raise alerts
uv run jarvis digest           # print the week ahead
uv run jarvis digest --send    # ...and send it to Telegram
```

Alerts: a message 24h before anything not done, and a call 3h before work that Canvas shows as unsubmitted (API only). Feed items, off-Canvas work (`submission_types: none`) and module-title deadlines only get the 24h message, marked "check it's done". `tick` syncs Canvas automatically.

### Deadlines the feed misses

The feed lacks ECON's module-title dates, CMST's Sunday homework, and ENGL work posted mid-week. The `/weekly` skill pushes every deadline it finds into JARVIS at the end of each run. You can also add them yourself:

```bash
uv run jarvis deadline add --course "ECON 100" --title "Module 3" --due 2026-10-18
uv run jarvis deadline import deadlines.json   # [{"course","title","due","key"?,"url"?,"done"?}]
uv run jarvis deadline list
uv run jarvis deadline done <key>              # stop reminders
```

Use `--key canvas:<assignment id>` to replace the feed's copy of an assignment. These deadlines get the 24h message but never the 3h call, since their done status is only as fresh as the last update.

## Alerts and phone calls

- `high`: a Telegram message.
- `urgent`: a Telegram message plus a phone call that reads the message twice. During `QUIET_HOURS` (default `23-7`, empty to disable), the message goes out right away and the call waits until morning.

Calls use Twilio **voice only**, which needs no A2P 10DLC registration: about $1.15/mo for the number plus 1–2¢ per minute. Setup: create a Twilio account (trial credit works, since JARVIS only calls your own verified number), buy a local number, then fill `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER` and `MY_PHONE_NUMBER` (E.164, e.g. `+12065550100`) in `.env`.

```bash
uv run jarvis call-test
uv run jarvis alert "Rent due tomorrow" --level high
uv run jarvis alert "Exam moved to today" --level urgent --key exam-moved --now
```

`--key` makes an alert one-time, so a source that checks every few minutes can't call you twice about the same thing.

### Looking at its own code (read-only)

Ask "how do you decide when to speak up?" or "do the tests pass?" and JARVIS reads its own project instead of guessing. The chat model still has no tools: it proposes an `inspect` action, and JARVIS checks and runs it, then asks again with the result (a few rounds per message, like opening web pages).

- **What it can do:** read a file, list a folder, search the code (plain text), tail a log (`tick`, `listen`, `ears`, `watch`, `menubar`, `commands`), and run four fixed checks: the tests, `git status`, `git log`, `git diff --stat`.
- **Limits:** only inside the project folder (`PROJECT_DIR`, default this repo; the background copy remembers where it came from). No `..` or symlink escapes. `.env`, token, key and database files are never opened. Checks run with no shell, a scrubbed environment and a timeout, inside a macOS sandbox that blocks file writes and network. Every result is redacted (your `.env` values plus token shapes like bot tokens, Twilio keys and the Canvas feed URL) and cut to 6,000 characters.
- File contents and logs are treated as data, never instructions. Changing code still only happens through an approved plan. `SELF_INSPECT=0` turns it off.

### Terminal commands (risky ones wait for your OK)

JARVIS can also run commands in chat ("show me the last 5 commits", "delete the old logs").

- **Run at once:** read-only commands on a strict allowlist, inside the project: `ls`, `cat`, `head`, `tail`, `wc`, `grep`, `git status/log/diff/show`, and the tests. They run in the same no-writes, no-network sandbox, with flags that could write or run programs refused (`git diff --output`, `--ext-diff`, `tail -f`, `pytest -p`...), and secret files blocked. `jarvis cmd try "git log -5"` shows how a command would be treated.
- **Wait for you:** anything else (writes, deletes, installs, other folders) arrives in Telegram with the exact command and folder and **Run / Reject** buttons. It runs only when you tap Run, once, then JARVIS sends the output. Requests lapse after `COMMAND_APPROVAL_MINUTES` (30), and a typed "yes" never runs a command.
- **Never:** a shell. Pipes, redirects, `$`, `;` and `&&` are refused outright.
- **Log:** every command (auto, approved, rejected, expired, timed out) is appended to `~/Library/Application Support/JARVIS/commands.jsonl` with the time, command, folder, who approved it and the exit code, never the output. `jarvis cmd log` shows it. `COMMANDS=0` turns commands off.

### Task mode (working a problem step by step)

For anything that takes several steps ("figure out why the ears log is noisy", "get the tests passing", "find out how X works on my Mac"), JARVIS starts a task instead of answering in one go, or saying it doesn't know how. It works like a Claude Code session: Claude picks one action (inspect the project, run a command, open a page, or finish), sees the result, and decides the next step. It's told to investigate (search the code, read docs, run `--help`, check what's running) and to change approach when something fails.

- **Checks between steps:** Jev (Haiku without a Jev key) answers three quick questions after each step: did it work, is the goal met, is it going in circles. Notes go back to Claude. Two "stuck" answers in a row get a nudge to try something different; four end the task with what it tried.
- **Approval pauses, doesn't end:** a command that needs your OK shows Run / Reject (with Jev's risk label, e.g. "⚠️ risk: high") and the task waits. Run feeds the output back and the task carries on; Reject tells Claude to find another way. An expired request does the same. Jev can label risk but never approves anything.
- **Progress:** one Telegram message per task, updated each step (✓ / ✗), then a short summary. Say "stop" (or "stop task 3") to end it; a command it was waiting on is cancelled.
- **Budget:** 15 steps or 10 minutes of work (time waiting for you doesn't count). Tasks run inside `jarvis listen`; one cut off by a restart is picked up again with a note.
- **Extra lookups that run at once** (sandboxed, so they can't write or reach the network): any installed tool's `--help`/`--version`, `which`, `ps`, `pgrep`, `pwd`, `date`, `file`, `stat`, `du`, and `find` inside the project (not `-exec`/`-delete`).
- JARVIS's chat sees its recent tasks and commands (waiting, ran, rejected, expired), so "did that go through?" gets a real answer.

### Self-editing (off unless you turn it on)

With it on, approving a plan makes JARVIS build it: Claude Code edits a private clone (`~/Library/Application Support/JARVIS/src`) on its own branch, allowed only to edit files and run the tests. The build must pass all tests, can't reduce the test count, and can't touch `.env`. You get a summary, the patch file, and **Deploy / Discard**. Changes to safety-critical files (approvals, chat lockdown, Telegram, web, mail, scheduling, self-edit) are flagged ⚠️. Deploy merges, redeploys the background jobs, and fast-forwards this repo; if that sync fails, JARVIS sends the `git pull` command to run.

To turn it on yourself (from this folder):
1. `uv run --extra dev pytest -q`
2. Add to `.env`: `JARVIS_REPO=<absolute path to this folder>` (optional: `SELF_EDIT_MODEL=opus`, `IDEA_HOUR=18`).
3. Commit everything (self-edits build from git).
4. `uv run jarvis schedule install`

To turn it off: remove `JARVIS_REPO` from `.env` and run `uv run jarvis schedule install`.

## Watching (senses)

`jarvis watch` (a background job) builds a local timeline while you're at the Mac:
- **Screen:** about every 15–20 s while you're active, the *front window only* is captured, its text read on-device with Apple Vision, and the image deleted at once. Only changed text is stored (max 4,000 characters).
- **Never read:** Messages (iMessage), password managers, banking apps, and windows whose title looks like a bank, payment or sign-in page. Add more with `WATCH_EXCLUDE_APPS` / `WATCH_EXCLUDE_TITLES`.
- **Terminal:** with the zsh hook installed (`jarvis watch install-shell-hook`, which adds one `source` line to `~/.zshrc`), each command's folder, exit code and duration is logged.
- **Git:** repos are found under `WATCH_ROOTS` (home and iCloud Drive); new commits and branch switches are noted.
- **Redaction:** things that look like secrets (`*_KEY=`, `password:`, long tokens) are scrubbed before storing.
- **Storage:** observations live in the local database for `WATCH_RETENTION_DAYS` (7). Chat sees the last 30 minutes plus your current screen's text, so "what's this error?" works. That part goes to Claude when you chat.

Needs **Screen Recording** permission for **JARVIS Senses** (System Settings → Privacy & Security → Screen & System Audio Recording → + → `~/Applications/JARVIS Senses.app`). That's a tiny signed launcher app that the watch job runs under, because macOS won't reliably list a bare Python. It's only rebuilt when its source changes, since a rebuild would reset the permission. `jarvis watch status` shows what's being collected, `jarvis watch timeline` shows the last 30 minutes, and `WATCH=0` plus `jarvis schedule install` turns it off.

## Attention (speaking up on its own)

The watch loop also decides when JARVIS should speak up unprompted:
- **Stuck triggers** (cheap rules, no AI): the same terminal command failing 3+ times in 10 min, 4+ failing commands, or error output on screen 3+ times in 10 min. These cause an immediate review (at most every 10 min).
- **Quick checks (with Jev):** whenever new screen, terminal or git activity was observed, at most every `ATTENTION_QUICK_SECONDS` (20), Jev answers the speak/kind/urgency questions about the last 10 minutes in about 0.2 s. Quiet answers are only counted, not logged; a yes goes through the full rules below. They're skipped during quiet hours or once the daily limit is reached. Cost is a few dollars a month at most. `ATTENTION_QUICK=0` turns them off.
- **Periodic reviews** (without Jev): every `ATTENTION_REVIEW_MINUTES` (10) of new activity.
- **Decision layer** (`jarvis/judge.py`): a situation plus typed questions (speak? stuck_help / heads_up / none? urgency 0–1), answered with confidence. The engine is `JUDGE_ENGINE` (`claude` with `JUDGE_MODEL=haiku` now; Jev or a local Mac mini model later, same interface).
- **Rules:** speak if confidence ≥ `ATTENTION_THRESHOLD` (0.75), or ≥ `ATTENTION_STUCK_THRESHOLD` (0.5) when stuck rules fired. At most `ATTENTION_DAILY_LIMIT` (8) per day, `ATTENTION_MIN_GAP_MINUTES` (15) apart unless urgency ≥ 0.9, never in quiet hours, only while you're active.
- **What it says:** Claude writes one short spoken line (said aloud) and a Telegram version with the specific fix.
- **Accountability:** every decision is logged. `jarvis attention log` shows the log, `jarvis attention review-now` forces a review, and asking JARVIS "why did you say that?" works because chat sees its recent interjections and reasons.

## Daily rhythm, journal and project briefs

- **Morning brief:** after `MORNING_HOUR` (7), the first time you're at the Mac, or at `MORNING_LATEST` (10) regardless. It covers today's deadlines, reminders, important mail, yesterday's open loops, and where to start.
- **Evening check-in:** at `EVENING_HOUR` (21). It asks how the day went and notes what you got done and what's tomorrow. Just reply; it's normal chat, so what you share is remembered.
- Both are spoken when you're at the Mac, always sent to Telegram, never in quiet hours, and once a day. A failed attempt retries after 30 minutes rather than being lost. `jarvis rhythm morning|evening` sends one now for testing.
- **Nightly journal:** after `JOURNAL_HOUR` (2), JARVIS writes up the previous day from chat, screen, terminal, git and events. It saves lasting facts and open loops. This is the long-term memory, since the raw timeline is pruned after 7 days. Chat sees the last 3 entries. `jarvis journal [show|write] [YYYY-MM-DD]`.
- **Project briefs:** every repo becomes a project (`jarvis projects sync`) with a living brief built from its README, the last 15 commits, GitHub (`gh`: description, open issues and PRs) and your past week of activity in it. Briefs of projects you touched are refreshed nightly; chat gets the briefs of your recently active ones. `jarvis projects [list|show NAME|refresh [NAME]]`.

## Talking to JARVIS

Speech is handled on-device: Whisper (`faster-whisper`, model `WHISPER_MODEL`, default `base.en`, stored in `~/Library/Application Support/JARVIS/models`) for listening, and macOS `say` plus ffmpeg for JARVIS's voice. Audio isn't stored, and only the transcribed text goes to Claude.

- **On walks (Telegram voice notes):** hold the mic in the bot chat and talk. JARVIS transcribes the note, answers with full context, and replies with a voice note in Daniel's voice, with the text as the caption. Spoken "yes 3" approvals work too. Voice notes from anyone else are never downloaded.
- **At the desk ("Hey Jarvis"):** `jarvis ears` (a background job under the tiny **JARVIS Ears** app, which holds Microphone permission) listens for the wake word with openWakeWord. After a chime, speak; it stops when you pause, then JARVIS answers out loud. For `FOLLOW_UP_SECONDS` (6) afterwards you can just keep talking. A "pop" means it didn't catch you. The mic is ignored while JARVIS speaks. If it wakes by mistake, raise `WAKE_THRESHOLD` (0.6). `EARS=0` turns it off. The startup log line "mic check" shows whether macOS is allowing the mic.

### Fast replies (models, warm session, streaming)

- **Two models:** quick replies use `JARVIS_MODEL` (default `haiku`), kept warm in a persistent session, typically **~2 s**. Heavier requests use `JARVIS_HEAVY_MODEL` (default `sonnet`) as a streamed one-off call, ~5–6 s: drafting email, planning, debugging, multi-step reasoning, long answers. Jev decides which, in the same call as context routing (or a keyword check without Jev). `JARVIS_MODEL_ROUTING=fast|heavy` forces one. Background writing (journal, briefs, plans, check-ins, interjections) stays on the heavy model.
- **Speaking as it writes:** replies stream. At the desk, each sentence is spoken as soon as it's finished, and the next one is rendered while the current one plays, so JARVIS starts talking after the first sentence instead of the whole reply. If the final reply doesn't match what streamed (an error, or a "checking" round), what's queued is stopped and the real answer is said instead. Telegram voice notes are still one recording of the full reply. In Telegram the first sentence is sent right away and the message is then edited into the full reply. Actions are only checked and carried out once the full reply has arrived.
- **Warm session:** saves the 2–3.5 s it takes to start `claude -p`. It retires after 12 messages or 2 hours idle, and a spare warms up just before. One per listener (about 170 MB each) to keep RAM in check. On failure a message falls back to a one-off call (`warm_fallback`). `WARM_CLAUDE=0` turns it off.
- **Cached inputs:** chat uses tick's copies of the inbox snapshot and the Canvas feed (up to 10 min old), so no reply waits on Mail or the network.
- **Measure it:** `jarvis latency` shows median and slowest reply times and time to first sentence, per model, plus desk time to first audio (from when you stop talking; target 1 s, also in the ears log as `time_to_first_audio=NNNms`). Claude's startup and Telegram network timeouts can still add delay. Only timings and choices are logged, never your message text.

## Jev (fast decisions)

With `TYPESAFE_API_KEY` set (from console.typesafe.ai → API Keys), JARVIS uses TypeSafe's Jev (`JEV_MODEL`, default `jev-latest`) for its quick typed decisions:
- **Context routing:** before each chat message, five yes/no questions decide which context to load (deadlines, inbox, current screen and terminal, project briefs, journal). It leans toward including, and skips the Canvas fetch when deadlines aren't needed. Each answer's timing and chosen context are logged as `answered` events.
- **Attention:** the "should I speak up?" judgment (Jev instead of Claude Haiku, so no Claude usage).

- **What you're doing:** each saved screen is tagged (coding, homework, reading, writing, chatting, email, video, gaming, browsing, planning, other). Without Jev, an app-name guess does it. The journal and evening check-in get a time breakdown ("coding 2h10m, homework 45m…").
- **Important mail:** Jev judges each new email from sender and subject only. Mail from your school's domain or a saved professor address always counts. Without Jev the keyword rules apply.
- **Deadline spotting:** in pages JARVIS opens and in important email subjects, date-mentioning lines go to Jev, which keeps the real deadlines. Pages pass them to Claude as `likely_deadlines`. Mail pings that look like deadlines say so and accept a plain "add it" reply.

`JUDGE_ENGINE=auto` (the default) means Jev when a key is set, otherwise Claude. Any Jev error falls back to Claude for that call (logged as `jev_fallback`), and without a key everything works as before.

## Menu bar indicator

**JARVIS Status** (a tiny Swift app in `~/Applications`, run as the `menubar` background job) shows what JARVIS is doing:
- 👂 waiting for "Hey Jarvis"
- 🎙 listening (red)
- ⋯ working on a reply (orange)
- 🔊 speaking (blue)
- an "off" icon when the desk listener isn't running

Click it for details. The parts of JARVIS write their state to `status.json` in the data folder. Busy states expire on their own, so a crash can't leave the icon stuck, and the desk listener sends a heartbeat every 10 s. "Hide Indicator" keeps it closed until the next `jarvis schedule install` or login. `MENUBAR=0` turns it off. (The orange microphone dot in the macOS menu bar is separate: that's macOS showing the desk listener has the mic.)

## Running automatically

```bash
uv run jarvis schedule install     # run tick every 5 min (--minutes N to change)
uv run jarvis schedule status      # last exit code + recent log lines
uv run jarvis schedule run-now
uv run jarvis schedule uninstall
```

Two launchd jobs:
- `tick` every 5 minutes: syncs Canvas, delivers alerts, sends new drafts to Telegram, sends approved email. Steps without config (Telegram, Twilio) are skipped, and a failing step doesn't stop the others. One tick at a time (lock file).
- `listen`, always on: Telegram chat and Approve/Reject buttons. Restarted automatically if it stops.

Both run only while the Mac is awake; missed ticks catch up on wake. Logs: `~/Library/Logs/JARVIS/`.

**The jobs run from a copy** in `~/Library/Application Support/JARVIS/app`, because iCloud offloads files from this folder and background reads of offloaded files fail intermittently. **After changing code or `.env`, run `uv run jarvis schedule install` again** to refresh the copy and restart the jobs.

The memory database lives in `~/Library/Application Support/JARVIS`, not in the iCloud project folder, because syncing a live SQLite file can corrupt it.

## Outlook access test (Microsoft Graph) — blocked by school tenant

1. Sign in to https://entra.microsoft.com with a **personal** Microsoft account (not school). A free account gets you a tenant to register apps in.
2. App registrations → New registration
   - Name: `JARVIS`
   - Supported account types: **Accounts in any organizational directory and personal Microsoft accounts**
   - Redirect URI: leave blank
3. On the new app: Authentication → **Allow public client flows = Yes** → Save.
4. API permissions → Add → Microsoft Graph → Delegated: `User.Read`, `Mail.ReadWrite`, `Mail.Send`, `Calendars.Read`.
5. Copy the **Application (client) ID** into `GRAPH_CLIENT_ID` in `.env`.
6. Run:

```bash
uv run jarvis graph-test          # sign in, check read/calendar/draft
uv run jarvis graph-test --send   # also email yourself a test message
```

Sign in with your **school** account when prompted. If you see "Selected user account does not exist in tenant 'Microsoft Services'", the app is single-tenant (fix step 2 under Authentication → Supported account types) or `GRAPH_TENANT` is set to the app's own tenant ID instead of `organizations`. Each check prints PASS/FAIL. If sign-in fails with an admin-consent error, the school tenant blocks third-party apps and we switch to a fallback.
