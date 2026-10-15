import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

DATA_DIR = Path(os.getenv("JARVIS_DATA_DIR", "./data")).expanduser()
DB_PATH = DATA_DIR / "jarvis.db"
TOKEN_CACHE_PATH = DATA_DIR / "token_cache.json"

MAIL_ACCOUNT = os.getenv("MAIL_ACCOUNT", "")

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER", "")
MY_PHONE_NUMBER = os.getenv("MY_PHONE_NUMBER", "")
QUIET_HOURS = os.getenv("QUIET_HOURS", "23-7")

GRAPH_CLIENT_ID =os.getenv("GRAPH_CLIENT_ID", "")
GRAPH_TENANT = os.getenv("GRAPH_TENANT", "organizations")
