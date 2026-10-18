"""Images from the Mac to Elijah's Telegram: screenshots a helper or a browse step made.

Only image files (png, jpg, jpeg, gif, webp) up to MAX_BYTES, and only from allowed folders:
JARVIS's own screenshot folder, its task state folders, the folders of tasks from the last week,
and anything Elijah adds in ATTACH_DIRS. Paths are fully resolved first (symlinks and .. can't
escape), and secret locations (keys, keychains, the browser profile...) are refused even inside
an allowed folder. Pages, emails or files can't add folders: the list comes only from settings
and the tasks Elijah started. Images go only to his own chat.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

from jarvis import config
from jarvis.memory import Memory

SHOTS_DIR = config.DATA_DIR / "shots"
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
               ".webp": "image/webp"}
MAX_BYTES = 10 * 1024 * 1024  # Telegram's photo limit
TASK_FOLDER_DAYS = 7


def allowed_dirs(mem: Memory | None = None) -> list[Path]:
    from jarvis import tasks
    dirs = [SHOTS_DIR, tasks.TASKS_DIR]
    dirs += [Path(d).expanduser() for d in config.ATTACH_DIRS.split(",") if d.strip()]
    if mem is not None:
        since = (datetime.now(timezone.utc) - timedelta(days=TASK_FOLDER_DAYS)).isoformat()
        for row in mem.db.execute("SELECT DISTINCT folder FROM tasks WHERE folder IS NOT NULL AND created_at >= ?",
                                  (since,)):
            if Path(row["folder"]).resolve() != config.PROJECT_DIR.resolve():
                dirs.append(Path(row["folder"]))
    out = []
    for d in dirs:
        try:
            out.append(d.resolve())
        except OSError:
            continue
    return out


def safe_image_path(path: str | Path, mem: Memory | None = None) -> Path:
    """The resolved image path if it may be sent, else ValueError with the reason."""
    from jarvis.introspect import is_secret_path
    raw = Path(str(path).strip()).expanduser()
    if not raw.is_absolute():
        raise ValueError("give the image's full path")
    try:
        full = raw.resolve(strict=True)  # follows symlinks, collapses ..
    except (OSError, RuntimeError):
        raise ValueError(f"no file at {path}") from None
    if not full.is_file():
        raise ValueError(f"{path} isn't a file")
    if full.suffix.lower() not in IMAGE_TYPES:
        raise ValueError(f"{full.name} isn't an image (png, jpg, gif or webp)")
    if full.stat().st_size > MAX_BYTES:
        raise ValueError(f"{full.name} is too big to send ({full.stat().st_size // 1_000_000} MB; 10 MB max)")
    if is_secret_path(full):
        raise ValueError(f"{full.name} is in a private location")
    if not any(full.is_relative_to(d) for d in allowed_dirs(mem)):
        raise ValueError(f"{full.parent} isn't a folder JARVIS may send images from")
    return full


def send_image(tg, path: str | Path, caption: str = "", mem: Memory | None = None) -> Path:
    """Check and send one image to his chat. Falls back to a file attachment if Telegram won't
    take it as a photo. Raises ValueError if the path isn't allowed."""
    full = safe_image_path(path, mem)
    data = full.read_bytes()
    try:
        tg.send_photo(data, caption)
    except Exception:
        tg.send_document(full.name, data, caption, mime=IMAGE_TYPES[full.suffix.lower()])
    return full


def save_shot(png: bytes, name: str = "page") -> Path:
    """Keep a screenshot (from the browser) where it can be attached later."""
    SHOTS_DIR.mkdir(parents=True, exist_ok=True)
    path = SHOTS_DIR / f"{name}-{datetime.now():%Y%m%d-%H%M%S}.png"
    path.write_bytes(png)
    for old in sorted(SHOTS_DIR.glob("*.png"))[:-50]:  # keep the newest 50
        old.unlink(missing_ok=True)
    return path
