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

### Self-editing (off unless you turn it on)

With it on, approving a plan makes JARVIS build it: Claude Code edits a private clone (`~/Library/Application Support/JARVIS/src`) on its own branch, allowed only to edit files and run the tests. The build must pass all tests, can't reduce the test count, and can't touch `.env`. You get a summary, the patch file, and **Deploy / Discard**. Changes to safety-critical files (approvals, chat lockdown, Telegram, web, mail, scheduling, self-edit) are flagged ⚠️. Deploy merges, redeploys the background jobs, and fast-forwards this repo; if that sync fails, JARVIS sends the `git pull` command to run.

To turn it on yourself (from this folder):
1. `uv run --extra dev pytest -q`
2. Add to `.env`: `JARVIS_REPO=<absolute path to this folder>` (optional: `SELF_EDIT_MODEL=opus`, `IDEA_HOUR=18`).
3. Commit everything (self-edits build from git).
4. `uv run jarvis schedule install`

To turn it off: remove `JARVIS_REPO` from `.env` and run `uv run jarvis schedule install`.

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
