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
uv run jarvis approve 1        # later: approved by SMS reply
uv run jarvis relay            # sends everything approved
```

The first run triggers a macOS prompt to let your terminal control Mail. Approved emails only go out while the Mac is awake and running `relay`.

## SMS approvals (Twilio)

JARVIS texts you each queued draft; reply `YES 3` / `NO 3` (or just `yes` when only one is waiting). Only texts from `MY_PHONE_NUMBER`, sent after the draft was announced, count.

Setup:
1. Create a Twilio account and buy a local US number (~$1–2/mo).
2. US texting requires **A2P 10DLC** registration. Without a business tax ID, register as a **Sole Proprietor** (Console → Messaging → Regulatory Compliance). One-time brand fee ≈ $4.50 plus a small monthly campaign fee; approval can take days. Use case: "account notifications to myself".
3. Fill `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER` and `MY_PHONE_NUMBER` (E.164, e.g. `+12065550100`) in `.env`.

```bash
uv run jarvis sms-test         # one text to yourself
uv run jarvis tick             # text new drafts, apply replies, send approved
```

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
