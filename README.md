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

The first run triggers a macOS prompt to let your terminal control Mail. Approved emails only go out while the Mac is awake and running `relay`.

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

## Running automatically

```bash
uv run jarvis schedule install     # run tick every 5 min (--minutes N to change)
uv run jarvis schedule status      # last exit code + recent log lines
uv run jarvis schedule run-now
uv run jarvis schedule uninstall
```

Each `tick` syncs Canvas, delivers alerts, sends new drafts to Telegram, applies your answers and sends approved email. Steps without config (Telegram, Twilio) are skipped, and a failing step doesn't stop the others. It runs via launchd, so only while the Mac is awake; missed runs catch up on wake. Log: `~/Library/Logs/JARVIS/tick.log`.

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
