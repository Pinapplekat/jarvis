import os
from pathlib import Path

from dotenv import load_dotenv

try:
    load_dotenv()
except OSError:  # inside a task's sandbox .env can't be read, by design
    pass

# Outside iCloud Drive: syncing a live SQLite file can corrupt it.
DATA_DIR = Path(os.getenv("JARVIS_DATA_DIR", "~/Library/Application Support/JARVIS")).expanduser()
DB_PATH = DATA_DIR / "jarvis.db"
TOKEN_CACHE_PATH = DATA_DIR / "token_cache.json"

MAIL_ACCOUNT = os.getenv("MAIL_ACCOUNT", "")

# Watching (screen text, terminal, git). Text only, kept locally for WATCH_RETENTION_DAYS.
WATCH = os.getenv("WATCH", "1") == "1"
WATCH_INTERVAL = int(os.getenv("WATCH_INTERVAL", "15"))        # seconds between looks
WATCH_RECAPTURE = int(os.getenv("WATCH_RECAPTURE", "20"))      # re-read same window at most this often
WATCH_MAX_CHARS = int(os.getenv("WATCH_MAX_CHARS", "4000"))
WATCH_RETENTION_DAYS = int(os.getenv("WATCH_RETENTION_DAYS", "7"))
WATCH_EXCLUDE_APPS = os.getenv("WATCH_EXCLUDE_APPS", "")       # extra, comma-separated
WATCH_EXCLUDE_TITLES = os.getenv("WATCH_EXCLUDE_TITLES", "")   # extra title words, comma-separated
WATCH_ROOTS = os.getenv("WATCH_ROOTS", "~,~/Library/Mobile Documents/com~apple~CloudDocs")

# Attention: JARVIS deciding on its own to speak up (e.g. when you're stuck)
ATTENTION = os.getenv("ATTENTION", "1") == "1"
JUDGE_ENGINE = os.getenv("JUDGE_ENGINE", "auto")              # auto (Jev if keyed, else Claude) | jev | claude
TYPESAFE_API_KEY = os.getenv("TYPESAFE_API_KEY", "")
JEV_MODEL = os.getenv("JEV_MODEL", "jev-latest")
JUDGE_MODEL = os.getenv("JUDGE_MODEL", "haiku")
ATTENTION_QUICK = os.getenv("ATTENTION_QUICK", "1") == "1"          # Jev glance on each change
ATTENTION_QUICK_SECONDS = int(os.getenv("ATTENTION_QUICK_SECONDS", "20"))
ATTENTION_REVIEW_MINUTES = int(os.getenv("ATTENTION_REVIEW_MINUTES", "10"))
ATTENTION_THRESHOLD = float(os.getenv("ATTENTION_THRESHOLD", "0.75"))  # confidence needed to speak
ATTENTION_STUCK_THRESHOLD = float(os.getenv("ATTENTION_STUCK_THRESHOLD", "0.5"))  # when stuck rules fired
ATTENTION_DAILY_LIMIT = int(os.getenv("ATTENTION_DAILY_LIMIT", "8"))   # unprompted interjections/day
ATTENTION_MIN_GAP_MINUTES = int(os.getenv("ATTENTION_MIN_GAP_MINUTES", "15"))

# Daily rhythm (local hours) and the nightly journal
RHYTHM = os.getenv("RHYTHM", "1") == "1"
MORNING_HOUR = int(os.getenv("MORNING_HOUR", "7"))      # morning brief from here, once you're at the Mac
MORNING_LATEST = int(os.getenv("MORNING_LATEST", "10")) # ...or at this hour regardless
EVENING_HOUR = int(os.getenv("EVENING_HOUR", "21"))     # evening check-in
JOURNAL_HOUR = int(os.getenv("JOURNAL_HOUR", "2"))      # journal yesterday after this hour

# Speaking out loud on the Mac when you're at it (keyboard/mouse used in the last N seconds)
VOICE = os.getenv("VOICE", "1") == "1"
VOICE_NAME = os.getenv("VOICE_NAME", "Daniel")
VOICE_RATE = int(os.getenv("VOICE_RATE", "185"))
VOICE_IDLE_SECONDS = int(os.getenv("VOICE_IDLE_SECONDS", "180"))

# Talking to JARVIS: on-device Whisper model, and the "Hey Jarvis" desk listener
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base.en")         # small.en is more accurate, slower
EARS = os.getenv("EARS", "1") == "1"
MENUBAR = os.getenv("MENUBAR", "1") == "1"                    # status indicator in the menu bar
WAKE_THRESHOLD = float(os.getenv("WAKE_THRESHOLD", "0.6"))    # raise if it wakes by mistake
FOLLOW_UP_SECONDS = int(os.getenv("FOLLOW_UP_SECONDS", "6"))  # listen for a follow-up without the wake word
EAR_END_SILENCE_SECONDS = float(os.getenv("EAR_END_SILENCE_SECONDS", "1.2"))  # quiet that ends a turn
EAR_MAX_SILENCE_SECONDS = float(os.getenv("EAR_MAX_SILENCE_SECONDS", "1.5"))  # ...once he's paused mid-thought
BARGE_IN = os.getenv("BARGE_IN", "1") == "1"                  # talk over JARVIS to stop it and be heard
BARGE_IN_MIN_MS = int(os.getenv("BARGE_IN_MIN_MS", "300"))    # sustained speech that counts as interrupting
BARGE_IN_LEVEL = float(os.getenv("BARGE_IN_LEVEL", "3.0"))    # x the speech level; raise if it interrupts itself
# Ping on Telegram for important new mail (school domains, saved professor addresses, keywords)
MAIL_WATCH = os.getenv("MAIL_WATCH", "1") == "1"
SCHOOL_EMAIL_DOMAINS = os.getenv("SCHOOL_EMAIL_DOMAINS", "seattlecolleges.edu")
TIMEZONE = os.getenv("TIMEZONE", "America/Los_Angeles")

CANVAS_BASE_URL = os.getenv("CANVAS_BASE_URL", "https://canvas.seattlecolleges.edu")
CANVAS_TOKEN = os.getenv("CANVAS_TOKEN", "")
CANVAS_ICS_URL = os.getenv("CANVAS_ICS_URL", "")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
# Model for Telegram chat, run through the local Claude Code CLI (uses your Claude subscription)
JARVIS_MODEL = os.getenv("JARVIS_MODEL", "haiku")              # quick chat replies (kept warm)
JARVIS_HEAVY_MODEL = os.getenv("JARVIS_HEAVY_MODEL", "sonnet")  # drafting, planning, debugging; background writing
JARVIS_MODEL_ROUTING = os.getenv("JARVIS_MODEL_ROUTING", "auto")  # auto | fast | heavy
WARM_CLAUDE = os.getenv("WARM_CLAUDE", "1") == "1"  # keep a persistent chat session (≈3x faster replies)
# Screen looks (private peeks and "what's on my screen?"): a warm session, reused for a few looks.
VISION_MODEL = os.getenv("VISION_MODEL", "")  # empty = JARVIS_MODEL (the fast one); "sonnet" for sharper reading

# Self-editing: the git repo JARVIS improves (your project folder), the model that writes
# the code, and the local hour after which JARVIS may suggest one improvement a day.
JARVIS_REPO = os.getenv("JARVIS_REPO", "")
SELF_EDIT_MODEL = os.getenv("SELF_EDIT_MODEL", "opus")
IDEA_HOUR = int(os.getenv("IDEA_HOUR", "18"))
BUILD_HANG_MINUTES = int(os.getenv("BUILD_HANG_MINUTES", "10"))  # no output this long: restart the build session

# Task mode: a Claude Code session confined to the task's folder; risky steps wait for Elijah's yes.
TASKS = os.getenv("TASKS", "1") == "1"                       # kill switch
TASK_MODEL = os.getenv("TASK_MODEL", "sonnet")
TASK_MAX_TURNS = int(os.getenv("TASK_MAX_TURNS", "40"))      # tool calls per session run
TASK_MAX_MINUTES = int(os.getenv("TASK_MAX_MINUTES", "20"))  # working time (waiting for Elijah doesn't count)
# JARVIS approving high-risk steps itself when they're off Elijah's side of the line (never anything
# irreversible, money/credentials, sudo/system changes, or reaching other people). 0 = always ask him.
SELF_APPROVE = os.getenv("SELF_APPROVE", "1") == "1"
SELF_APPROVE_CONFIDENCE = float(os.getenv("SELF_APPROVE_CONFIDENCE", "0.6"))  # Jev's confidence in a low/medium grade

# Thinking heartbeat: each tick, a cheap look at deadlines, mail and open loops; texts only when worth it.
HEARTBEAT = os.getenv("HEARTBEAT", "1") == "1"
HEARTBEAT_MIN_INTERVAL_MIN = int(os.getenv("HEARTBEAT_MIN_INTERVAL_MIN", "30"))
HEARTBEAT_JEV_INTERVAL_MIN = int(os.getenv("HEARTBEAT_JEV_INTERVAL_MIN", "10"))  # Jev's first look is cheap
HEARTBEAT_MAX_RUNS_PER_DAY = int(os.getenv("HEARTBEAT_MAX_RUNS_PER_DAY", "24"))  # cost cap
# Not a quota: JARVIS judges how much it has said. This only stops a broken loop.
HEARTBEAT_RUNAWAY_PER_DAY = int(os.getenv("HEARTBEAT_RUNAWAY_PER_DAY", "10"))
HEARTBEAT_MODEL = os.getenv("HEARTBEAT_MODEL", "haiku")

# JARVIS's own browser profile (logins Elijah made himself). Outside the repo; never sent anywhere.
BROWSER_PROFILE = DATA_DIR / "browser-profile"
BROWSER_BLOCKLIST = os.getenv("BROWSER_BLOCKLIST", "")  # extra domains, comma-separated
# Extra folders JARVIS may send images from (besides its screenshots, task folders); comma-separated.
ATTACH_DIRS = os.getenv("ATTACH_DIRS", "")

# Self-inspection and terminal commands. PROJECT_DIR is the JARVIS repo (the background jobs
# run from a copy, which records where it came from); commands stay inside it unless approved.
_HERE = Path(__file__).resolve().parent.parent
_SOURCE_MARK = _HERE / ".jarvis-source"
PROJECT_DIR = Path(os.getenv("JARVIS_PROJECT_DIR") or (
    _SOURCE_MARK.read_text().strip() if _SOURCE_MARK.is_file() else _HERE)).expanduser()
SELF_INSPECT = os.getenv("SELF_INSPECT", "1") == "1"        # read own files, run read-only checks
COMMANDS = os.getenv("COMMANDS", "1") == "1"                # terminal commands (risky ones need Approve)
COMMAND_APPROVAL_MINUTES = int(os.getenv("COMMAND_APPROVAL_MINUTES", "30"))  # unanswered requests expire
JOB_STALL_MINUTES = int(os.getenv("JOB_STALL_MINUTES", "10"))  # ping once if a background job prints nothing this long

# Siri-style actions: texts and calls wait for Elijah's yes; alarms, timers and music just run.
SIRI = os.getenv("SIRI", "1") == "1"
SIRI_TIMER_SHORTCUT = os.getenv("SIRI_TIMER_SHORTCUT", "")  # a Shortcut that starts a Clock timer (input: seconds)
SIRI_ALARM_SHORTCUT = os.getenv("SIRI_ALARM_SHORTCUT", "")  # a Shortcut that sets a Clock alarm (input: HH:MM)
# Music plays through the Spotify Web API on his active device: a Spotify developer app's client ID and a
# refresh token for his account (scopes user-modify-playback-state, user-read-playback-state,
# user-library-modify). `jarvis media login` gets one into the Keychain; one in .env is copied there once.
# Secrets: never logged. The secret is optional (only for apps that aren't PKCE).
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "")
SPOTIFY_REFRESH_TOKEN = os.getenv("SPOTIFY_REFRESH_TOKEN", "")
SPOTIFY_REDIRECT_URI = os.getenv("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback")  # as set in the Spotify app
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "")
SPOTIFY_MARKET = os.getenv("SPOTIFY_MARKET", "US")
SPOTIFY_APP_FALLBACK = os.getenv("SPOTIFY_APP_FALLBACK", "0") == "1"  # last resort: drive the desktop app's UI

# Location: points from locations you share in Telegram, kept on this Mac; place names come
# from OpenStreetMap Nominatim (rounded, cached coordinates are the only thing sent).
LOCATION = os.getenv("LOCATION", "1") == "1"
LOCATION_GEOCODE = os.getenv("LOCATION_GEOCODE", "1") == "1"
LOCATION_STALE_MINUTES = int(os.getenv("LOCATION_STALE_MINUTES", "30"))
LOCATION_KEEP_POINTS = int(os.getenv("LOCATION_KEEP_POINTS", "300"))
LOCATION_KEEP_DAYS = int(os.getenv("LOCATION_KEEP_DAYS", "14"))
# Street address of the latest point (Nominatim, at most once a minute, only the latest kept).
REVERSE_GEOCODE = os.getenv("REVERSE_GEOCODE", "1") == "1"

# Twilio is used only for voice: urgent calls to you, and you calling JARVIS
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER", "")
MY_PHONE_NUMBER = os.getenv("MY_PHONE_NUMBER", "")
QUIET_HOURS = os.getenv("QUIET_HOURS", "23-7")
# Calling JARVIS from your cell: a Twilio voice webhook on this Mac, reached through a tunnel.
PHONE_PUBLIC_URL = os.getenv("PHONE_PUBLIC_URL", "").rstrip("/")  # the tunnel's https address
PHONE_PORT = int(os.getenv("PHONE_PORT", "8787"))                 # local port (localhost only)
PHONE_PIN = os.getenv("PHONE_PIN", "")  # digits keyed in after pickup; guards against caller-ID spoofing

GRAPH_CLIENT_ID =os.getenv("GRAPH_CLIENT_ID", "")
GRAPH_TENANT = os.getenv("GRAPH_TENANT", "organizations")
