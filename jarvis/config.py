import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Outside iCloud Drive: syncing a live SQLite file can corrupt it.
DATA_DIR = Path(os.getenv("JARVIS_DATA_DIR", "~/Library/Application Support/JARVIS")).expanduser()
DB_PATH = DATA_DIR / "jarvis.db"
TOKEN_CACHE_PATH = DATA_DIR / "token_cache.json"

MAIL_ACCOUNT = os.getenv("MAIL_ACCOUNT", "")

# Watching (screen text, terminal, git). Text only, kept locally for WATCH_RETENTION_DAYS.
WATCH = os.getenv("WATCH", "1") == "1"
WATCH_INTERVAL = int(os.getenv("WATCH_INTERVAL", "30"))        # seconds between looks
WATCH_RECAPTURE = int(os.getenv("WATCH_RECAPTURE", "60"))      # re-read same window at most this often
WATCH_MAX_CHARS = int(os.getenv("WATCH_MAX_CHARS", "4000"))
WATCH_RETENTION_DAYS = int(os.getenv("WATCH_RETENTION_DAYS", "7"))
WATCH_EXCLUDE_APPS = os.getenv("WATCH_EXCLUDE_APPS", "")       # extra, comma-separated
WATCH_EXCLUDE_TITLES = os.getenv("WATCH_EXCLUDE_TITLES", "")   # extra title words, comma-separated
WATCH_ROOTS = os.getenv("WATCH_ROOTS", "~,~/Library/Mobile Documents/com~apple~CloudDocs")

# Attention: JARVIS deciding on its own to speak up (e.g. when you're stuck)
ATTENTION = os.getenv("ATTENTION", "1") == "1"
JUDGE_ENGINE = os.getenv("JUDGE_ENGINE", "claude")            # claude | jev (later) | local (later)
JUDGE_MODEL = os.getenv("JUDGE_MODEL", "haiku")
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
JARVIS_MODEL = os.getenv("JARVIS_MODEL", "sonnet")

# Self-editing: the git repo JARVIS improves (your project folder), the model that writes
# the code, and the local hour after which JARVIS may suggest one improvement a day.
JARVIS_REPO = os.getenv("JARVIS_REPO", "")
SELF_EDIT_MODEL = os.getenv("SELF_EDIT_MODEL", "opus")
IDEA_HOUR = int(os.getenv("IDEA_HOUR", "18"))

# Twilio is used only for urgent voice calls
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER", "")
MY_PHONE_NUMBER = os.getenv("MY_PHONE_NUMBER", "")
QUIET_HOURS = os.getenv("QUIET_HOURS", "23-7")

GRAPH_CLIENT_ID =os.getenv("GRAPH_CLIENT_ID", "")
GRAPH_TENANT = os.getenv("GRAPH_TENANT", "organizations")
