"""Self-inspection: JARVIS reading its own project (source, README, tests, logs) and running a
few fixed read-only checks, so it can answer questions about its own code and state.

Nothing here writes. Paths are resolved inside the project folder (no `..` or symlink escapes),
secret files are never opened, checks run as fixed argv lists with no shell inside a macOS
sandbox that blocks file writes and network, and every result is redacted and truncated.
Changing code still only happens through an approved plan.
"""

import fnmatch
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from jarvis import config
from jarvis.screen import redact as redact_patterns

MAX_OUTPUT = 6000          # characters of any one result shown to Claude
MAX_FILE_BYTES = 400_000   # bigger files aren't read at all
MAX_SEARCH_HITS = 60
SKIP_DIRS = {".git", ".venv", ".venv.nosync", "__pycache__", ".pytest_cache", "node_modules", "data", "models"}
SECRET_NAMES = [".env", ".env.*", "*token*", "*secret*", "*credential*", "*.pem", "*.key", "*.p12",
                "id_rsa*", "id_ed25519*", "*.keychain*", "jarvis.db*", "*.sqlite*"]
SECRET_OK = {".env.example"}  # the template has no values
LOG_DIR = Path.home() / "Library/Logs/JARVIS"
LOGS = {"tick", "listen", "ears", "watch", "menubar"}

# Read-only checks, each a fixed argv (run in the project folder, sandboxed).
GIT = ["git", "--no-optional-locks", "-c", "core.pager=cat", "-c", "color.ui=false"]
CHECKS = {
    "tests": [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
    "git_status": [*GIT, "status", "--short", "--branch"],
    "git_log": [*GIT, "log", "-n", "20", "--oneline"],
    "git_diff": [*GIT, "diff", "--stat", "--no-ext-diff"],
}
CHECK_TIMEOUTS = {"tests": 120}

# Token shapes worth masking even when they aren't next to a telling name.
TOKEN_PATTERNS = [
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b"),                     # Telegram bot token
    re.compile(r"\b(?:AC|SK)[0-9a-fA-F]{32}\b"),                         # Twilio SID / key
    re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{20,}"),                     # Anthropic / OpenAI keys
    re.compile(r"https?://\S*/feeds/calendars/\S+"),                     # Canvas calendar feed URL
    re.compile(r"\b(?:ghp|gho|github_pat)_[A-Za-z0-9_]{20,}\b"),         # GitHub tokens
]

# A macOS sandbox profile: everything may be read, but nothing written (except temp and
# /dev) and no network. Used for the checks and for auto-approved terminal commands.
SANDBOX = ('(version 1)(allow default)(deny network-outbound (remote ip))(deny file-write*)'
           '(allow file-write* (subpath "/private/tmp") (subpath "/private/var/folders") (subpath "/dev"))')


class Refused(ValueError):
    """The request isn't allowed (outside the project, a secret file, not on the allowlist)."""


@dataclass
class Result:
    code: int | None   # None when it timed out or couldn't start
    output: str        # redacted and truncated
    timed_out: bool = False


def project_dir() -> Path:
    return config.PROJECT_DIR.resolve()


# ----- secrets -----
def _secret_values() -> list[str]:
    """Values JARVIS knows are secret: its settings plus anything in the project's .env."""
    values = [config.TELEGRAM_BOT_TOKEN, config.CANVAS_ICS_URL, config.CANVAS_TOKEN, config.TWILIO_AUTH_TOKEN,
              config.TWILIO_ACCOUNT_SID, config.TYPESAFE_API_KEY, os.getenv("ANTHROPIC_API_KEY", "")]
    try:
        from dotenv import dotenv_values
        for root in {project_dir(), Path(__file__).resolve().parent.parent}:
            env = root / ".env"
            if env.is_file():
                values += [v for k, v in dotenv_values(env).items()
                           if v and any(w in k.upper() for w in ("TOKEN", "KEY", "SECRET", "SID", "ICS", "PASS"))]
    except Exception:
        pass
    return sorted({v for v in values if v and len(v) >= 6}, key=len, reverse=True)


def redact(text: str, limit: int = MAX_OUTPUT) -> str:
    """Mask known secret values and token-like strings, then cap the length."""
    for value in _secret_values():
        text = text.replace(value, "<redacted>")
    for pattern in TOKEN_PATTERNS:
        text = pattern.sub("<redacted>", text)
    text = redact_patterns(text)
    if len(text) > limit:
        text = text[:limit].rstrip() + f"\n… (cut; {len(text) - limit} more characters)"
    return text


def is_secret(path: Path) -> bool:
    return any(part not in SECRET_OK and any(fnmatch.fnmatch(part.lower(), pat) for pat in SECRET_NAMES)
               for part in path.parts)


# ----- paths -----
def resolve(path: str, root: Path | None = None) -> Path:
    """`path` (relative to the project, or absolute) resolved and checked to sit inside it."""
    root = (root or project_dir()).resolve()
    raw = Path(os.path.expanduser(str(path).strip() or "."))
    full = (raw if raw.is_absolute() else root / raw).resolve()  # follows symlinks, collapses ..
    try:
        rel = full.relative_to(root)
    except ValueError:
        raise Refused(f"{path} is outside the JARVIS project") from None
    if is_secret(rel):
        raise Refused(f"{path} may hold secrets; not reading it")
    if ".git" in rel.parts:
        raise Refused("use the git checks instead of reading .git directly")
    return full


def read_project_file(path: str) -> str:
    """A text file with line numbers. "path:120-220" reads just those lines (long files are cut)."""
    first, last = 1, None
    if (m := re.fullmatch(r"(.+?):(\d+)(?:-(\d+))?", path.strip())):
        path, first, last = m[1], int(m[2]), int(m[3]) if m[3] else None
    full = resolve(path)
    if not full.is_file():
        raise Refused(f"no file {path}")
    if full.stat().st_size > MAX_FILE_BYTES:
        raise Refused(f"{path} is too big to read ({full.stat().st_size // 1000} KB)")
    data = full.read_bytes()
    if b"\0" in data[:4096]:
        raise Refused(f"{path} isn't a text file")
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    numbered = "\n".join(f"{i:>4}  {line}" for i, line in enumerate(lines, 1)
                         if first <= i <= (last or len(lines)))
    return redact(numbered + f"\n({len(lines)} lines in all)")


def _walk(base: Path):
    root = project_dir()
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".venv"))
        for name in sorted(filenames):
            full = Path(dirpath) / name
            rel = full.relative_to(root)
            if name == ".DS_Store" or is_secret(rel) or full.is_symlink():
                continue
            yield full, rel


def list_files(subdir: str = "") -> str:
    base = resolve(subdir or ".")
    if not base.is_dir():
        raise Refused(f"{subdir} isn't a folder")
    lines = [f"{rel}  ({full.stat().st_size} bytes)" for full, rel in _walk(base)]
    return redact("\n".join(lines) or "(empty)")


def search_code(text: str, subdir: str = "") -> str:
    """Case-insensitive plain-text search (not regex, so no runaway patterns)."""
    needle = text.strip().lower()
    if len(needle) < 2:
        raise Refused("search for at least two characters")
    hits = []
    for full, rel in _walk(resolve(subdir or ".")):
        if full.stat().st_size > MAX_FILE_BYTES:
            continue
        try:
            data = full.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:4096]:
            continue
        for i, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
            if needle in line.lower():
                hits.append(f"{rel}:{i}: {line.strip()[:200]}")
                if len(hits) >= MAX_SEARCH_HITS:
                    return redact("\n".join(hits) + f"\n(stopped at {MAX_SEARCH_HITS} matches)")
    return redact("\n".join(hits) or f"no matches for {text!r}")


def tail_log(name: str, lines: int = 60) -> str:
    name = name.strip().removesuffix(".log")
    if name == "commands":
        path = config.DATA_DIR / "commands.jsonl"
    elif name in LOGS:
        path = LOG_DIR / f"{name}.log"
    else:
        raise Refused(f"unknown log {name!r}; known: {', '.join(sorted(LOGS | {'commands'}))}")
    if not path.exists():
        return f"({name} log is empty)"
    lines = max(1, min(int(lines), 300))
    with path.open("rb") as f:
        f.seek(max(0, path.stat().st_size - 200_000))
        tail = f.read().decode("utf-8", errors="replace").splitlines()[-lines:]
    return redact("\n".join(tail))


# ----- running things -----
def scrubbed_env() -> dict:
    """A minimal environment: no tokens or keys leak into child processes."""
    keep = {"HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TMPDIR", "TERM"}
    env = {k: v for k, v in os.environ.items() if k in keep}
    env.update(PATH=os.pathsep.join([str(Path(sys.executable).parent), str(Path.home() / ".local/bin"),
                                     "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]),
               PYTHONDONTWRITEBYTECODE="1", GIT_PAGER="cat", PAGER="cat", GIT_TERMINAL_PROMPT="0",
               GIT_CONFIG_NOSYSTEM="1", NO_COLOR="1")
    return env


def run_argv(argv: list[str], cwd: Path, timeout: float = 30, sandboxed: bool = True) -> Result:
    """Run one command: no shell, fixed cwd, scrubbed env, timeout, and (by default) inside
    the no-writes, no-network sandbox. Output is redacted and truncated."""
    full = (["/usr/bin/sandbox-exec", "-p", SANDBOX] if sandboxed else []) + list(argv)
    try:
        r = subprocess.run(full, cwd=cwd, env=scrubbed_env(), capture_output=True, text=True,
                           timeout=timeout, stdin=subprocess.DEVNULL, errors="replace")
    except subprocess.TimeoutExpired as e:
        partial = (e.stdout or "") if isinstance(e.stdout, str) else (e.stdout or b"").decode(errors="replace")
        return Result(None, redact(partial + f"\n(stopped after {timeout:.0f}s)"), timed_out=True)
    except OSError as e:
        return Result(None, f"couldn't run it: {e}")
    out = (r.stdout or "") + (("\n" + r.stderr) if r.stderr.strip() else "")
    return Result(r.returncode, redact(out.strip() or "(no output)"))


def run_check(name: str) -> Result:
    if name not in CHECKS:
        raise Refused(f"unknown check {name!r}; allowed: {', '.join(CHECKS)}")
    return run_argv(CHECKS[name], project_dir(), timeout=CHECK_TIMEOUTS.get(name, 30))


# ----- the chat action -----
OPS = ("read", "list", "search", "log", "check")


def inspect(op: str, arg: str = "") -> str:
    """Run one inspect action from chat; returns redacted text (or raises Refused)."""
    if op == "read":
        return read_project_file(arg)
    if op == "list":
        return list_files(arg)
    if op == "search":
        return search_code(arg)
    if op == "log":
        name, _, n = arg.partition(" ")
        return tail_log(name, int(n) if n.strip().isdigit() else 60)
    if op == "check":
        r = run_check(arg.strip())
        status = "timed out" if r.timed_out else f"exit {r.code}"
        return f"[{status}]\n{r.output}"
    raise Refused(f"unknown inspect op {op!r}; use one of {', '.join(OPS)}")
