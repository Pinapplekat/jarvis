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

Anything risky (an email to someone else, a text or call, a high-risk command) arrives in Telegram as a plain question, "is that what you meant?", with the exact recipients and text or the exact command, plus **Approve / Reject** (or **Run / Reject**) buttons as a shortcut. If you're at the Mac, JARVIS asks out loud too.

- **Answer in words:** a clear "yes" (also "yep, go ahead", "do it", "send it", or a voice note saying so; at the desk mic or on a call too) does what the button does; a clear "no" cancels. Something that starts like an answer but isn't clear ("yes but make it shorter") cancels that item, says so, and goes to chat. A bare "yes" means the one thing waiting; with several waiting, JARVIS names them and asks you to tap the one you mean. "ok" with nothing waiting is just conversation.
- **Safeguards:** only your chat is obeyed; a reply only counts for questions sent before it; command requests lapse after `COMMAND_APPROVAL_MINUTES` (30); text in pages, emails or files can never answer for you.
- **Email:** mail only to your own address (`MAIL_ACCOUNT`) goes out on its own (low risk). Anything to anyone else needs your yes, and the yes covers that exact draft: if the recipients, subject or body change afterwards, it isn't sent.
- **No internal numbers:** messages refer to things by what they are ("the email to your professor"), never "command #70"; if the model slips, "Plan #16" is rewritten to "the plan" before it reaches you. The CLI still shows ids.

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
- **Locked down:** Claude runs with no tools, no MCP servers and no personal settings. It can only *propose* actions (remember/forget a fact, log a note, update a project, add or finish a deadline, draft an email). JARVIS checks each one before doing it. Email to anyone but you is only ever drafted and still needs your yes.
- **Context it sees:** saved facts, projects, recent events, the next 14 days of deadlines, drafts awaiting approval, and the last 12 chat messages.
- **Voice:** short and casual, matching how you text. Tweak the "Voice" section of `SYSTEM_PROMPT` in `jarvis/chat.py` to change it.
- **Links:** send a link and JARVIS opens it in its own headless Chromium (JavaScript runs, so wikis and JS-heavy agendas work) before answering. It can also open links it remembers or finds on pages it read, up to 3 rounds and 6 pages per message. It has no access to your browser's logins, so pages behind Canvas or Google sign-in won't load. Local and private network addresses are blocked, and page text is treated as untrusted data. One-time setup: `uv run playwright install chromium`.
- **Character:** Tony Stark's JARVIS: calls you "sir", dry wit, candid pushback, still brief.
- **Speaks out loud:** reminders, alerts and important-mail pings are spoken through the Mac's speakers (macOS `say`, voice `VOICE_NAME`, default British "Daniel") when you've used the keyboard or mouse in the last `VOICE_IDLE_SECONDS` (180) and it isn't quiet hours. Telegram always gets the text too. `jarvis say "..."` tests the voice. `VOICE=0` turns it off.
- **Reminders:** "text me at 9 to finish econ" → JARVIS messages you then (within ~25s; late reminders say so).
- **Improving itself:** ask "add a /status command" (or JARVIS spots a gap; it may suggest one idea a day after `IDEA_HOUR`). JARVIS writes a plan (steps, files, risks) and sends it with **Yes / Not now**. Reply with changes to get a revised plan. A "yes" saves it to `~/Library/Application Support/JARVIS/plans/`. Then either:
  - **self-editing off (default):** in Claude Code, say "build the approved JARVIS plans" (`jarvis plans` lists them, `jarvis plans done <id>` marks one built); or
  - **self-editing on:** JARVIS builds it itself (see below) and sends you the result with **Deploy / Discard**.
  - In short, JARVIS can propose and build changes to its own code once you approve a plan and self-editing is on.
  - Self-edit pipeline last re-tested: 2026-10-17.
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

### Calling JARVIS from your phone

Call the Twilio number from your cell and talk to JARVIS anywhere. It picks up with "Good evening, sir.", you talk, and it answers in the same Daniel voice as at the desk. Keep going as long as you like; say "goodbye" or "that's all" to hang up (or just hang up).

- **How it works:** Twilio answers and transcribes what you say (its speech recognition, a few cents per call). The text goes through the normal chat pipeline with full context, marked as a phone call, so replies are one or two short sentences. JARVIS renders the reply with macOS `say` and Twilio plays it down the line. You can talk over it to interrupt.
- **Long answers:** anything that doesn't fit in about 20 seconds aloud, or has links, is sent to Telegram in full ("The rest is in Telegram, sir."). If an answer takes over 9 seconds you hear "One moment, sir."; after 2 minutes JARVIS stops waiting and sends the answer to Telegram.
- **Actions work as in chat:** "remind me at 9", "add ECON module 4 due Sunday" and "start a task to..." all work. Email drafts and commands still only happen after you tap Approve / Run in Telegram, and their buttons are sent during the call. Nothing on the phone approves anything.
- **Only you:** calls from any number other than `MY_PHONE_NUMBER` are rejected before they're answered, so they cost nothing. A call whose caller ID the carrier marked as forged (STIR/SHAKEN) is rejected too. Caller ID can still be faked, so **set `PHONE_PIN`**: JARVIS then asks for a passcode before saying anything, and hangs up after two wrong tries.
- **Only Twilio:** every webhook request must carry a valid `X-Twilio-Signature` (signed with your auth token), so nobody can talk to the webhook directly. The server listens on `127.0.0.1` only and answers just its `/voice/...` paths. The reply audio lives at random addresses that expire after 10 minutes.
- **Cost:** about 1¢ a minute for incoming calls, plus Twilio's speech recognition. On a trial account, Twilio plays its trial notice first and you press a key to continue.
- **Needs the Mac awake and online,** like the other background jobs. Each call gets its own warm Claude session (about 170 MB), closed when you hang up.

Setup:
1. **Expose the webhook through a tunnel.** Twilio has to reach port `PHONE_PORT` (8787) on this Mac over HTTPS, without opening your router. Pick one:
   - **Tailscale Funnel** (free, stable address, recommended): install Tailscale, then `tailscale funnel --bg 8787`. Your address is `https://<this-mac>.<tailnet>.ts.net`. Funnel publishes only that one port.
   - **Cloudflare named tunnel** (free, needs a domain on Cloudflare): `cloudflared tunnel create jarvis`, route a hostname such as `jarvis.yourdomain.com` to `http://localhost:8787`, and run it as a service (`cloudflared service install`).
   - A quick tunnel (`cloudflared tunnel --url http://localhost:8787`, or ngrok) works for a test, but its address changes every time it restarts, so you'd redo steps 2–3 each time.

   Only this webhook goes through the tunnel. Don't point the tunnel at anything else on the Mac.
2. Add to `.env`: `PHONE_PUBLIC_URL=https://...` (the tunnel address, no trailing slash) and `PHONE_PIN=` with a few digits. The `TWILIO_*` and `MY_PHONE_NUMBER` settings from above are needed too.
3. `uv run jarvis phone setup` points your Twilio number's "A call comes in" webhook at `PHONE_PUBLIC_URL/voice/incoming` and its status callback at `/voice/status`. You can also set these yourself in the Twilio console (Phone Numbers → your number → Voice, HTTP POST). The address must match `PHONE_PUBLIC_URL` exactly, or every signature check fails (the log says "no valid Twilio signature").
4. `uv run jarvis schedule install` starts the `phone` background job (log: `~/Library/Logs/JARVIS/phone.log`). `uv run jarvis phone` runs it in the foreground instead, for testing.
5. Optional: in the Twilio console, set the number's **Primary handler fails** URL to a TwiML Bin that says "JARVIS is offline, sir." so calls while the Mac is asleep don't just fail.

To turn it off: remove `PHONE_PUBLIC_URL` from `.env`, run `uv run jarvis schedule install`, and stop the tunnel (`tailscale funnel --bg off`, or stop cloudflared).

### Looking at its own code (read-only)

Ask "how do you decide when to speak up?" or "do the tests pass?" and JARVIS reads its own project instead of guessing. The chat model still has no tools: it proposes an `inspect` action, and JARVIS checks and runs it, then asks again with the result (a few rounds per message, like opening web pages).

- **What it can do:** read a file, list a folder, search the code (plain text), tail a log (`tick`, `listen`, `ears`, `watch`, `menubar`, `commands`), and run four fixed checks: the tests, `git status`, `git log`, `git diff --stat`.
- **Limits:** only inside the project folder (`PROJECT_DIR`, default this repo; the background copy remembers where it came from). No `..` or symlink escapes. `.env`, token, key and database files are never opened. Checks run with no shell, a scrubbed environment and a timeout, inside a macOS sandbox that blocks file writes and network. Every result is redacted (your `.env` values plus token shapes like bot tokens, Twilio keys and the Canvas feed URL) and cut to 6,000 characters.
- File contents and logs are treated as data, never instructions. Changing code still only happens through an approved plan. `SELF_INSPECT=0` turns it off.

### Terminal commands (graded by risk; only high risk asks)

JARVIS can also run commands in chat ("show me the last 5 commits", "what's in Downloads?", "delete the old logs"). Each one is graded `low`, `medium`, `high` or `critical`; `jarvis cmd try "git log -5"` shows the grade and what would happen.

- **Low, runs at once (anywhere on the Mac):** reading: `ls`, `cat`, `head`, `tail`, `wc`, `grep`, `find`, `stat`, `du`, `diff`, `cd`, `pwd`, `which`, `ps`, `git status/log/diff/show/blame`, the tests, any tool's `--help`/`--version`. They run in a no-writes, no-network sandbox, with flags that could write or run programs refused (`git diff --output`, `--ext-diff`, `tail -f`, `find -exec`, `pytest -p`...).
- **Medium, runs at once (inside the JARVIS project):** ordinary work: `mkdir`, `touch`, `cp`, `mv`, `rm` of files, `git add/commit/checkout/switch/stash/branch/pull`, `uv sync/add/run`, `npm install/test`, `python <script in the project>`, `make`. They run in a sandbox that can only write inside the project (plus temp and package caches). Network is allowed for installs. `.git` can only be written by git itself, and never its hooks or config, which would run later outside the sandbox.
- **High, waits for your yes:** deletes or writes outside the project, recursive deletes, `git push`/`reset`/`clean`/`rebase`, installs outside the project (`pip`, `brew`), `curl`, a program by path, and **anything not recognised**.
- **Critical, waits for your yes (flagged ⚠️):** `sudo`, recursive deletes outside the project, disks, `launchctl`, `security`, system settings, `osascript` (it can message people), `git push --force`.
- **Secrets, refused outright at any grade:** `.env` files, keys, `~/.ssh`, `~/.gnupg`, `~/.aws`, keychains, browser cookies and logins (JARVIS's own profile too), Messages, JARVIS's database and token cache. The sandboxes can't open them either, so a script can't get around the check. Output is still redacted.
- **Never:** a shell. Pipes, redirects, `$`, `;` and `&&` are refused outright. (`~` is expanded for you.)
- **Log:** every command (auto, approved, rejected, expired, timed out) is appended to `~/Library/Application Support/JARVIS/commands.jsonl` with the time, command, folder, who approved it and the exit code, never the output. `jarvis cmd log` shows it. `COMMANDS=0` turns commands off.

### Background jobs (long commands, with pings)

Commands that take longer than two minutes or keep running (long builds, installs, servers, launching Claude Code) run as background jobs instead of hitting the 120 s cutoff.

- **Still your call:** JARVIS asks the usual "is that what you meant?", noting it runs in the background. This always needs your yes, even for a read-only command, because a job runs outside the sandbox with no time limit.
- **How it runs:** detached in its own session, with no shell and the same scrubbed environment. Output goes to `~/Library/Application Support/JARVIS/jobs/<id>/job.log`, and a small wrapper records the exit code, so JARVIS can tell whether a job finished, failed or died.
- **"How's it going?":** JARVIS can read a job's state, runtime and last log lines without asking (redacted, treated as untrusted data). Chat always sees its five newest jobs.
- **Reports:** each tick (every 5 min), a job that finishes, fails or is lost, or prints nothing for `JOB_STALL_MINUTES` (10), files one report to JARVIS, which tells you in its own words.
- **CLI:** `jarvis jobs` lists them, `jarvis jobs show 3` shows one's log, `jarvis jobs kill 3` stops one. Finished jobs are removed after 14 days.

### JARVIS and its helpers (you talk to one person)

You tell JARVIS what you want; JARVIS hands the work to helpers (tasks, background jobs, self-edit builds) and keeps an eye on them. **The helpers report to JARVIS, never to you.** JARVIS tells you when something is done or failed, or when a helper has something you'd want to hear, in its own words, with whatever else came in at the same time bundled into one message (and said out loud if you're at the Mac). Progress is kept, not sent: ask "how's the site thing going?" or "what did you do today?" any time and JARVIS answers like someone who's been watching; "show me the steps" gets the full list.

**Who approves what.** Commands the rules know are graded at once: reading anywhere is low, ordinary project work is medium, both run. For anything the rules can't place (an unfamiliar tool, a write outside the project, a simple app-scripting click), Jev grades the risk tier in one fast call, and also checks it's clearly something you asked for rather than something a page or file suggested. Low or medium runs immediately and you hear afterwards what it did (a task's final report lists what JARVIS ran on its own). High, critical, unsure, or any Jev failure asks you. **Your side of the line, which is never graded and always asks you:** anything critical (sudo, system settings, disks, credentials, `security`), anything irreversible (deleting or overwriting files, `git push`/`reset`/`clean`/`rebase`, uninstalling), anything that reaches other people or sends your data out (email, `ssh`/`scp`/`gh`, publishing, `curl` with data), and app scripts that type, send, delete, run shell commands or touch Mail, Messages, Finder or settings. `SELF_APPROVE_CONFIDENCE` (0.6) is how sure Jev must be of a low/medium grade. Every command JARVIS runs on its own is logged (`jarvis cmd log` shows `jarvis` as the approver). `SELF_APPROVE=0` makes JARVIS ask you about everything high-risk again. The same line applies to commands JARVIS proposes in chat: off your side and clearly what you asked for, it runs them itself and tells you the result; your side waits for your yes.

**Seeing its helpers.** JARVIS always knows the state of its own helpers, and it can also see every Claude Code session on this Mac (one in a screen window like `jarvis-build`, one in the desktop app) from the live transcript Claude Code writes under `~/.claude/projects`: what each was asked, its last steps and words, and whether it's working or has gone quiet. Ask "is Claude still working?" or "what's the build doing?" and it answers from that; it never tries screen dumps. It also knows when self-editing is off, so it won't claim an approved plan is building. `jarvis helpers` shows what it sees; `jarvis helpers <title words>` shows one session in detail. Read-only, redacted, treated as data.

### Task mode (working like Claude Code)

For anything that takes several steps ("get the tests passing in ~/Code/site", "figure out why the ears log is noisy", "find out how X works on my Mac"), JARVIS starts a task instead of answering in one go. A task is a real **Claude Code session** (`TASK_MODEL`, default `sonnet`) working in one folder: it reads and edits code, runs commands and tests, checks the results, and keeps going until the goal is met and verified.

- **Its folder:** "start a task in ~/Code/site to …" gives it that folder to edit. With no folder it works in the JARVIS project **read-only**: changing JARVIS's own code still takes an approved plan. Your home folder or `/` are refused as too broad.
- **Fenced in three ways:** the whole `claude` process runs in a macOS sandbox that can only write to its folder (plus Claude Code's own files, caches and temp), can't read secrets, and can't change Claude Code's settings or hooks. A hook grades every tool call before it runs: reads anywhere (secrets excepted) and edits inside the folder go ahead; each command gets the same grade as above, so low and medium run and high or critical pause the task. Anything else is refused, and the hook fails closed. Approved high-risk commands are run by JARVIS for the session (as you, no shell), and the output goes back to it.
- **Asks only when it matters:** a high-risk step goes to JARVIS first (see above); only your side of the line, or a step JARVIS isn't sure about, comes to you. Your yes runs it and the session carries on; a no (or an expiry) tells it to find another way.
- **Reports to JARVIS:** no progress messages in your chat. JARVIS passes on the result, a failure, or something the session flagged for you (a note is a step, not the end). Jev (or Haiku) double-checks a "done" once before it's reported. While JARVIS is answering you out loud at the desk, reports wait and go to Telegram as soon as it's done (or you cut in), two minutes at most.
- **Stop and budgets:** say "stop" to end it at once (the session is killed and a command it was waiting on is cancelled). `TASK_MAX_TURNS` (40) tool calls per run, 8 runs, `TASK_MAX_MINUTES` (20) of working time (waiting for you doesn't count). `TASKS=0` turns task mode off.
- **Survives restarts:** the session id is saved, so a task cut off by a restart (or a session that goes quiet for 5 minutes) resumes where it was.
- **CLI:** `jarvis tasks` lists them, `jarvis tasks show 3` shows the steps and the session log path, `jarvis tasks stop 3`.
- JARVIS's chat sees its recent tasks and commands (waiting, ran, rejected, expired), so "did that go through?" gets a real answer.

### Self-editing (off unless you turn it on)

With it on, approving a plan makes JARVIS build it: Claude Code edits a private clone of this repo (`~/Library/Application Support/JARVIS/src`) on its own branch. The build must pass all tests, can't reduce the test count, and can't touch `.env`. You get a summary, the patch file, and **Deploy / Discard**. Changes to safety-critical files (approvals, chat lockdown, Telegram, web, mail, scheduling, self-edit) are flagged ⚠️. Deploy merges, redeploys the background jobs, and fast-forwards this repo; if that sync fails, JARVIS sends the `git pull` command to run.

- **The build session (`jarvis-build`):** a detached process, so you never open Claude Code yourself. It runs in the same sandbox and with the same hook as task mode (folder: the clone), and reports to JARVIS like any helper. A high-risk step (Claude Code's permission prompt) is decided by JARVIS when it's off your side of the line, otherwise forwarded to you as a plain-words question; it runs on the yes and the session resumes. A session that prints nothing for `BUILD_HANG_MINUTES` (10) is restarted from where it was (twice at most), and a builder that dies is restarted by the tick (twice at most, then the plan is marked failed and you're told).
- **Control:** `jarvis build status` (running?, which plan, last step, last output, session id, log path), `jarvis build start` (starts a builder for queued plans unless one is already running), `jarvis build stop`.

To turn it on yourself (from this folder):
1. `uv run --extra dev pytest -q`
2. Add to `.env`: `JARVIS_REPO=<absolute path to this folder>` (optional: `SELF_EDIT_MODEL=opus`, `IDEA_HOUR=18`).
3. Commit everything (self-edits build from git).
4. `uv run jarvis schedule install`

To turn it off: remove `JARVIS_REPO` from `.env` and run `uv run jarvis schedule install`.

## Texts, calls, alarms, timers and music

Ask like you'd ask Siri: "text Mom I'm running late", "call Dana", "set a timer for 10 minutes", "wake me at 7:30", "play some Daft Punk".

- **Texts and calls wait for your yes.** They arrive in Telegram like an email draft, with the exact name, number and text plus **Approve / Reject**; a typed or spoken yes works too. The yes covers exactly that number and text: any change afterwards voids it. Unanswered requests lapse after `COMMAND_APPROVAL_MINUTES` (30). An approved text goes out at once through Messages (iMessage); a call opens FaceTime with the number (`tel:` link, through your iPhone if Continuity calling is on), where macOS may ask you to click Call.
- **Names to numbers:** a number in your saved facts ("remember Mom's number is 206-555-0100") or your Contacts. No match, or more than one, and JARVIS asks you which instead of guessing.
- **Alarms, timers and music just happen,** with a one-line confirmation. Timers and alarms become a JARVIS reminder (spoken at the Mac, sent to Telegram). For a real Clock timer or alarm, make a Shortcut that starts a timer from its input (seconds) or sets an alarm from its input (`HH:MM`), and set `SIRI_TIMER_SHORTCUT` / `SIRI_ALARM_SHORTCUT` to its name; if it fails, the reminder is used.
- **Music is Spotify first.** JARVIS turns the request into a Spotify search ("nice study lo-fi" becomes a playlist search for "study lo-fi"), picks the best song, artist or playlist with the Spotify Web API and starts it on your active Spotify device (or the first one that's open) through the player API, with no clicking in the app. It only says "Playing ..." once Spotify accepted the play command; otherwise it tells you why not (no device open, Premium required, token problems). Setup: a Spotify developer app (developer.spotify.com → Create app) and a refresh token for your account granted the scopes `user-modify-playback-state` and `user-read-playback-state`. Put `SPOTIFY_CLIENT_ID` and `SPOTIFY_REFRESH_TOKEN` in `.env`, plus `SPOTIFY_CLIENT_SECRET` only if your app isn't PKCE (`SPOTIFY_MARKET`, default `US`, sets the region). The refresh token is a secret: JARVIS never logs it or sends it anywhere but Spotify, and if Spotify rotates it the new one is kept in memory only. Playback control needs Premium and Spotify already open on some device; without that (or without the keys) the backup takes over. `SPOTIFY_APP_FALLBACK=1` adds the old desktop-app driving as a last resort before the backup: JARVIS asks the app to play the search, then clicks the top result's Play button through System Events (only inside Spotify, only while it's in front; needs Accessibility, and a Spotify redesign can break it).
- **Apple Music is only the backup:** if Spotify isn't available, fails or has no match, JARVIS plays the same request in the Music app: a library playlist with that name, otherwise the top matching song in your library, otherwise the top song in the Apple Music catalog (found with Apple's public search, no key; Music opens it and System Events presses Play, which needs an Apple Music subscription). It says which service played ("Spotify had nothing for it, so I played ... on Apple Music."). If both fail it says so. It never touches Apple Music when Spotify plays.
- **Safety:** the chat model only proposes these; text in pages, emails or files can't trigger or approve one. Each one is logged and ends up in the nightly journal.
- **Permissions:** the first time, macOS asks whether the background job may control Messages, Spotify, Music, System Events and Contacts (Privacy & Security → Automation; Contacts under Contacts). Apple Music's catalog backup (and `SPOTIFY_APP_FALLBACK`) also needs Accessibility for the background job (Privacy & Security → Accessibility) so System Events can press Play; without it JARVIS says that's what's missing. Your terminal needs the same for the test command.
- **Try it:** `jarvis siri-test timer 5m`, `jarvis siri-test alarm 07:30`, `jarvis siri-test music "Get Lucky" --kind song`, `jarvis siri-test contact Mom` (shows the number it would use). Texts and calls have no test command: they only go through an approval. `SIRI=0` turns all of this off.

## Pictures to your phone

JARVIS can send images from the Mac to your Telegram: a screenshot a helper took, a chart it made, a page it browsed. A task attaches one by writing `ATTACH: /path/to/image.png` and JARVIS sends it with its report; in chat, ask to see it ("send me the screenshot") and JARVIS attaches it.

**Your screen, on request:** "send me a screenshot" captures your whole screen and sends it in the same step: no permission prompt, no second request. It's only taken when you ask, kept in the screenshot folder (newest 50), and goes only to your chat.

**Looking at your screen, privately:** "what's on my screen?" or "what does this error say?" makes JARVIS capture the screen and look at the picture itself (a second, tool-less Claude call with the image attached as input, `JARVIS_HEAVY_MODEL`), then answer in text. Nothing is sent to Telegram or shown, and the picture is deleted right after. It works from the desk mic and phone calls too, where pictures can't be sent. Screen text is treated as untrusted data and the look can only answer, never act. Needs Screen Recording permission for the background job's Python; it's slower than a normal reply and uses more of your Claude limits.

**Peeking on its own:** JARVIS also decides for itself whether a glance would help. The same Jev routing call asks "would a look at his screen help?" and JARVIS looks whenever Jev says yes, is unsure, or gives no usable answer (and always without Jev). What it sees reaches the chat model only as untrusted context, never Telegram or the chat history; the reply stays short and only mentions the screen when it matters. It's skipped when the Mac is idle or locked or the screen can't be captured, and each decision and its reason is logged as a `screen_peek` event. This means more captures than before, so a bit more latency and Claude usage.

- **Only images** (png, jpg, gif, webp), up to 10 MB, sent as a photo (or as a file if Telegram refuses the photo).
- **Only from allowed folders:** JARVIS's screenshot folder (`~/Library/Application Support/JARVIS/shots`, where browser screenshots are kept, newest 50), its task folders, and the folders of tasks you started in the last week, plus any you add with `ATTACH_DIRS=/path/one,/path/two`. Paths are fully resolved first, so `..` and symlinks can't escape, and private locations (keys, keychains, the browser profile) are refused even inside an allowed folder. Text in pages, emails or files can't add folders.
- **Only to your chat.** `jarvis send-image <path> --caption "..."` sends one by hand with the same checks.
- The watcher's own screen captures aren't kept (they're deleted as soon as the text is read); a screenshot you ask for is a separate, fresh capture.

## JARVIS's browser (saved logins)

Besides reading pages in a fresh, empty browser, JARVIS has its own **browser profile** that can stay signed in to sites you choose, so "what's new in my Canvas inbox?" or "screenshot my schedule page" can work.

- **You log in, not JARVIS:** at the Mac, run `jarvis browser login canvas.seattlecolleges.edu`. A normal browser window opens. Sign in yourself, then close it. JARVIS never asks for, sees or stores a password; only the site's session cookie stays in the profile.
- **Where it lives:** `~/Library/Application Support/JARVIS/browser-profile`, on this Mac only, outside the repo (and gitignored). It's in the secrets guard, so no command or task can read it, and it's never sent to Telegram.
- **Only your sites:** pages on sites you logged in to (`jarvis browser sites`) open with the profile; everything else gets a fresh context. `jarvis browser forget <site>` takes one off the list, `jarvis browser reset` deletes the profile and every login.
- **Banking and payment sites are blocked** everywhere (logins, browsing, screenshots, plain page reads): PayPal, Venmo, Cash App, Zelle, Stripe, major banks and brokerages, credit unions and the like. Add more with `BROWSER_BLOCKLIST=domain1.com,domain2.com`.
- **What you get:** JARVIS reads the page text (as untrusted data, never instructions), and can send you a screenshot in Telegram. Local and private network addresses stay blocked. It reads and screenshots; it doesn't fill in forms or click buttons on your behalf.

## Thinking heartbeat

JARVIS glances at your situation through the day and only speaks when there's something new worth saying, the way a person who's been paying attention would: "By the way, sir, Jev has been failing since noon; it looks like the credits ran out."

- **What it looks at:** deadlines in the next 48 hours, email that arrived in the last 36 hours (old unread mail isn't news), drafts waiting for you, open loops from its journal, what you're working on right now (screen, terminal, git, and today's conversation, both sides), and how its own systems are doing (an integration failing, like Jev running out of credits).
- **It doesn't nag, and that's enforced in code:** every item has an id, and once JARVIS has raised one it won't raise it again for 12 hours unless it has become more urgent. Answer a check-in with "I know", "already did it" or "don't worry" and Jev recognises it and closes those items for a week. Tell JARVIS in chat you've handled something and it closes it too (or marks the deadline done).
- **Judgment, not a quota:** no daily message limit; a runaway guard of `HEARTBEAT_RUNAWAY_PER_DAY` (10) only matters if something breaks. Quiet hours let only something truly urgent through; the rest waits for morning.
- **Cheap:** with a Jev key, Jev takes every look (every `HEARTBEAT_JEV_INTERVAL_MIN`, 10, minutes) and a sure "nothing to say" ends it there; only then does `HEARTBEAT_MODEL` (`haiku`) write the message. Nothing changed means no call at all. `HEARTBEAT_MAX_RUNS_PER_DAY` (24) caps Claude calls.
- **It can only suggest a message**: no tools, no email, no commands. What it says joins the chat history, so you can reply naturally.
- **Off switch:** "pause heartbeat" / "resume heartbeat" in chat, `jarvis heartbeat off` / `on`, or `HEARTBEAT=0`. `jarvis heartbeat` shows today's looks, messages, what you've closed, and its last decisions.

## Location (from Telegram)

Share your location with the JARVIS bot in Telegram (📎 → Location). A **live** share updates on its own, so JARVIS always roughly knows where you are, the same way it knows what's on your screen.

- **What it keeps:** points on this Mac only (latitude, longitude, accuracy, time). If you haven't moved, an update extends the last point instead of adding one. It keeps at most `LOCATION_KEEP_POINTS` (300) points from the last `LOCATION_KEEP_DAYS` (14) days. A location counts as stale after `LOCATION_STALE_MINUTES` (30). Locations from anyone but you are ignored.
- **Place names:** OpenStreetMap Nominatim turns a point into a street or building name. It gets coordinates rounded to about 100 m, at most one lookup a second, and the answers are cached. `LOCATION_GEOCODE=0` turns it off.
- **Street address:** so JARVIS can say "you're at 1701 Broadway" rather than just the area, it also looks up the street address of your latest point, also with Nominatim (`nominatim.openstreetmap.org/reverse`, free, no key). **What's sent:** only the latitude and longitude rounded to 5 decimals (about 1 m), plus JARVIS's User-Agent; no name, email, chat text or anything else. **When:** only for a fresh point when chat needs your location, only after you've moved about 30 m since the last lookup, and at most once a minute (Nominatim's usage policy), with a 5 s timeout. **What's kept:** only the latest address, replaced each time, in JARVIS's location state on this Mac; never in the journal, logs or facts, and no history. `jarvis location forget` wipes it. If a lookup fails, JARVIS falls back to the street area. **Off switch:** `REVERSE_GEOCODE=0` (no request at all; everything else works as before). This does send your precise location to a third party (OpenStreetMap); a self-hosted geocoder would avoid that if you'd rather.
- **These are the only places your coordinates are sent.**
- **Places and routine:** after 20 minutes in one spot, JARVIS asks once what the place is ("school", "work"). Reply in chat, or run `jarvis location label 3 school`. It then learns which weekdays and hours you're usually at each place, so chat and the morning brief can make a guess like "not at school; usually is by now on Mondays". These are guesses from your own history; class times in facts aren't read yet.
- **What chat sees:** your last known place and how old it is, the street address (when fresh), whether live sharing is on, the last few hours of movement, and the routine guess. **Never coordinates:** an unnamed spot is just "a spot with no name yet", the chat model is told never to state coordinates, and any latitude/longitude pair that still ends up in a reply is stripped before it reaches you.
- **Live sharing lapses:** Telegram ends a live share after at most 8 hours, unless you pick "until I turn it off". JARVIS reminds you once when a share ends, outside quiet hours. A background reporter on the phone (OwnTracks, or a Shortcuts automation posting to an authenticated endpoint) would avoid the gaps. It isn't built; it would be a later plan.
- **CLI:** `jarvis location` (what JARVIS sees), `jarvis location history --hours 24`, `jarvis location places`, and `jarvis location forget` (wipes all points, places and visits). `LOCATION=0` turns it off.

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
- **At the desk ("Hey Jarvis"):** `jarvis ears` (a background job under the tiny **JARVIS Ears** app, which holds Microphone permission) listens for the wake word with openWakeWord. After a chime, speak; it waits out natural pauses and stops after `EAR_END_SILENCE_SECONDS` (1.2) of quiet, or `EAR_MAX_SILENCE_SECONDS` (1.5) once you've paused mid-thought and carried on, then JARVIS answers out loud. For `FOLLOW_UP_SECONDS` (6) afterwards you can just keep talking. A "pop" means it didn't catch you. If it wakes by mistake, raise `WAKE_THRESHOLD` (0.6). `EARS=0` turns it off. The startup log line "mic check" shows whether macOS is allowing the mic.
- **Talking over JARVIS (barge-in):** while JARVIS answers, the mic stays open. Speak for `BARGE_IN_MIN_MS` (300) and it stops mid-sentence, drops the rest, and listens; what you said, including the words that cut in, is your next turn, with the same pause rules (barging in never shortens them). To keep its own voice from interrupting it, the bar is `BARGE_IN_LEVEL` (3) times normal speech and twice how loud JARVIS sounds at the mic. If it cuts itself off, raise `BARGE_IN_LEVEL` or use headphones. `BARGE_IN=0` closes the mic while it talks, as before. Spoken approvals still go through the same checks.

### Fast replies (models, warm session, streaming)

- **Two models:** quick replies use `JARVIS_MODEL` (default `haiku`), kept warm in a persistent session, typically **~2 s**. Heavier requests use `JARVIS_HEAVY_MODEL` (default `sonnet`) as a streamed one-off call, ~5–6 s: drafting email, planning, debugging, multi-step reasoning, long answers. Jev decides which, in the same call as context routing (or a keyword check without Jev). `JARVIS_MODEL_ROUTING=fast|heavy` forces one. Background writing (journal, briefs, plans, check-ins, interjections) stays on the heavy model.
- **Speaking as it writes:** replies stream. At the desk, each sentence is spoken as soon as it's finished, and the next one is rendered while the current one plays, so JARVIS starts talking after the first sentence instead of the whole reply. If the final reply doesn't match what streamed (an error, or a "checking" round), what's queued is stopped and the real answer is said instead. Telegram voice notes are still one recording of the full reply. In Telegram the first sentence is sent right away as its own message and the rest follows as a second new message once it's written (more if it's past Telegram's 4096-character limit); a message already sent is never edited. If the reply changed after the first sentence went out (an error, or a "checking" round), the full reply is sent as a new message instead. Actions are only checked and carried out once the full reply has arrived.
- **Warm session:** saves the 2–3.5 s it takes to start `claude -p`. It retires after 12 messages or 2 hours idle, and a spare warms up just before. One per listener (about 170 MB each) to keep RAM in check. On failure a message falls back to a one-off call (`warm_fallback`). `WARM_CLAUDE=0` turns it off.
- **Cached inputs:** chat uses tick's copies of the inbox snapshot and the Canvas feed (up to 10 min old), so no reply waits on Mail or the network.
- **Measure it:** `jarvis latency` shows median and slowest reply times and time to first sentence, per model, plus desk time to first audio (from when you stop talking; target 1 s, also in the ears log as `time_to_first_audio=NNNms`). Claude's startup and Telegram network timeouts can still add delay. Only timings and choices are logged, never your message text.

## Jev (fast decisions)

With `TYPESAFE_API_KEY` set (from console.typesafe.ai → API Keys), JARVIS uses TypeSafe's Jev (`JEV_MODEL`, default `jev-latest`) for its quick typed decisions:
- **Context routing:** before each chat message, five yes/no questions decide which context to load (deadlines, inbox, current screen and terminal, project briefs, journal), plus one on whether to peek at your screen privately. It leans toward including and looking, and skips the Canvas fetch when deadlines aren't needed. Each answer's timing and chosen context are logged as `answered` events.
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

The main launchd jobs (`watch`, `ears`, `menubar` and `phone` are covered in their own sections):
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
