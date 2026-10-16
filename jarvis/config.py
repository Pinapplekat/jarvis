import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Outside iCloud Drive: syncing a live SQLite file can corrupt it.
DATA_DIR = Path(os.getenv("JARVIS_DATA_DIR", "~/Library/Application Support/JARVIS")).expanduser()
DB_PATH = DATA_DIR / "jarvis.db"
TOKEN_CACHE_PATH = DATA_DIR / "token_cache.json"

MAIL_ACCOUNT = os.getenv("MAIL_ACCOUNT", "")
TIMEZONE = os.getenv("TIMEZONE", "America/Los_Angeles")

CANVAS_BASE_URL = os.getenv("CANVAS_BASE_URL", "https://canvas.seattlecolleges.edu")
CANVAS_TOKEN = os.getenv("CANVAS_TOKEN", "")
CANVAS_ICS_URL = os.getenv("CANVAS_ICS_URL", "")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
# Model for Telegram chat, run through the local Claude Code CLI (uses your Claude subscription)
JARVIS_MODEL = os.getenv("JARVIS_MODEL", "sonnet")

# Twilio is used only for urgent voice calls
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER", "")
MY_PHONE_NUMBER = os.getenv("MY_PHONE_NUMBER", "")
QUIET_HOURS = os.getenv("QUIET_HOURS", "23-7")

GRAPH_CLIENT_ID =os.getenv("GRAPH_CLIENT_ID", "")
GRAPH_TENANT = os.getenv("GRAPH_TENANT", "organizations")
