"""What a command can see and touch: secrets that are never read, the macOS sandboxes, a scrubbed environment."""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

MAX_OUTPUT = 6000  # characters of any one result shown to the model
SECRET_NAMES = [".env", ".env.*", "*token*", "*secret*", "*credential*", "*.pem", "*.key", "*.p12",
                "id_rsa*", "id_ed25519*", "*.keychain*", "jarvis.db*", "falak.db*", "*.sqlite*"]
SECRET_OK = {".env.example"}  # the template has no values
TOKEN_PATTERNS = [
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b"),                     # a bot token
    re.compile(r"\b(?:AC|SK)[0-9a-fA-F]{32}\b"),                         # Twilio SID / key
    re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{20,}"),                     # Anthropic / OpenAI keys
    re.compile(r"https?://\S*/feeds/calendars/\S+"),                     # a Canvas calendar feed URL
    re.compile(r"\b(?:ghp|gho|github_pat)_[A-Za-z0-9_]{20,}\b"),         # GitHub tokens
]
SECRET_ASSIGN = re.compile(
    r"(?i)\b(?P<name>[\w.-]*(?:token|secret|passw(?:or)?d|passwd|api[_-]?key|apikey|auth|credential"
    r"|private[_-]?key|_key|_pat)[\w.-]*)(?:\s*[=:]\s*|\s+)(?P<q>['\"]?)[^\s'\"]{4,}(?P=q)")
LONG_RANDOM = re.compile(r"\b(?=[A-Za-z0-9_\-+/=:]*\d)(?=[A-Za-z0-9_\-+/=:]*[A-Za-z])[A-Za-z0-9_\-+/=:]{28,}\b")
_TEMP = '(subpath "/private/tmp") (subpath "/private/var/folders") (subpath "/dev")'
CLAUDE_PROTECTED = ["settings.json", "settings.local.json", "CLAUDE.md", "hooks", "agents", "commands", "skills",
                    "plugins"]  # Claude Code's own config, which a task must not change for itself


def secret_dirs() -> list[Path]:
    """Folders that hold keys, sessions or private messages: never read by a command, anywhere."""
    home = Path.home()
    support = home / "Library/Application Support"
    return [home / ".ssh", home / ".gnupg", home / ".aws", home / ".config/gh", home / ".docker",
            home / "Library/Keychains", home / "Library/Cookies", home / "Library/Messages",
            support / "Google/Chrome", home / "Library/Safari", support / "Firefox",
            support / "JARVIS/browser-profile"]


def _sb(path: object) -> str:
    return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'


def deny_secrets(keychain: bool = True) -> str:
    """The OS-level secrets guard, in every sandbox. `keychain=False` is only for the sandbox around Claude Code
    itself, which signs in from the (encrypted) keychain; commands still can't name keychain files."""
    dirs = [d for d in secret_dirs() if keychain or d.name != "Keychains"]
    return ("(deny file-read* " + " ".join(f"(subpath {_sb(d)})" for d in dirs)
            + r' (regex #"/\.env(\.[^/]*)?$") (regex #"/(jarvis|falak)\.db") (regex #"/token_cache\.json$"))'
            + r'(allow file-read* (regex #"/\.env\.example$"))')


def read_sandbox() -> str:
    """Everything may be read (but secrets), nothing written (but temp), no network."""
    return ('(version 1)(allow default)(deny network-outbound (remote ip))(deny file-write*)'
            f'(allow file-write* {_TEMP})' + deny_secrets())


def write_sandbox(root: Path, git: bool = False) -> str:
    """Writes only inside `root` (plus temp and package caches), network allowed (installs). Its .git can't be
    written (except by git itself, `git=True`), and never its hooks or config, which would run code later."""
    root, home = Path(root).resolve(), Path.home()
    caches = " ".join(f"(subpath {_sb(home / p)})" for p in (".cache", ".npm", "Library/Caches", ".local/share/uv"))
    no_git = "" if git else f"(deny file-write* (subpath {_sb(root / '.git')}))"
    return ("(version 1)(allow default)(deny file-write*)"
            f"(allow file-write* (subpath {_sb(root)}) {_TEMP} {caches})" + no_git
            + f"(deny file-write* (subpath {_sb(root / '.git/hooks')}) (literal {_sb(root / '.git/config')})"
            + r' (regex #"/\.env(\.[^/]*)?$"))' + deny_secrets())


def claude_sandbox(folder: Path, state: Path, edits: bool) -> str:
    """The sandbox around the whole `claude` process of a task: writes only to its folder (not when read-only), its
    state folder, Claude Code's own files, package caches and temp."""
    folder, home = Path(folder).resolve(), Path.home()
    writable = [*([folder] if edits else []), Path(state).resolve(), home / ".claude", home / ".cache",
                home / ".npm", home / "Library/Caches", home / ".local/share/uv", home / ".local/state/claude"]
    protected = " ".join(f"(subpath {_sb(home / '.claude' / p)})" for p in CLAUDE_PROTECTED)
    return ("(version 1)(allow default)(deny file-write*)"
            f"(allow file-write* {' '.join(f'(subpath {_sb(p)})' for p in writable)} {_TEMP}"
            f' (regex #"^{re.escape(str(home))}/\\.claude\\.json"))'
            f"(deny file-write* {protected} (subpath {_sb(folder / '.git/hooks')}) "
            f"(literal {_sb(folder / '.git/config')})" + r' (regex #"/\.env(\.[^/]*)?$"))'
            + deny_secrets(keychain=False))


def redact(text: str, limit: int = MAX_OUTPUT) -> str:
    """Mask token-like strings and secret assignments, then cap the length."""
    for pattern in TOKEN_PATTERNS:
        text = pattern.sub("<redacted>", text)
    text = SECRET_ASSIGN.sub(lambda m: f"{m['name']}=<redacted>", text)
    text = LONG_RANDOM.sub("<redacted>", text)
    if len(text) > limit:
        text = text[:limit].rstrip() + f"\n… (cut; {len(text) - limit} more characters)"
    return text


def is_secret(path: Path) -> bool:
    return any(part not in SECRET_OK and any(fnmatch.fnmatch(part.lower(), pat) for pat in SECRET_NAMES)
               for part in path.parts)


def is_secret_path(path: Path) -> bool:
    """An absolute path that is, or is inside, something secret (by name or by folder)."""
    path = Path(path)
    for d in secret_dirs():
        for form in {d, d.resolve()}:  # as written, and with symlinks followed
            try:
                path.relative_to(form)
                return True
            except ValueError:
                continue
    return is_secret(path)


def scrubbed_env() -> dict[str, str]:
    """A minimal environment: no tokens or keys leak into child processes."""
    keep = {"HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TMPDIR", "TERM"}
    env = {k: v for k, v in os.environ.items() if k in keep}
    home = Path.home()
    env.update(PATH=os.pathsep.join([str(Path(sys.executable).parent), str(home / ".local/bin"),
                                     "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]),
               PYTHONDONTWRITEBYTECODE="1", GIT_PAGER="cat", PAGER="cat", GIT_TERMINAL_PROMPT="0",
               GIT_CONFIG_NOSYSTEM="1", NO_COLOR="1")
    return env


@dataclass
class Result:
    code: int | None  # None when it timed out or couldn't start
    output: str  # redacted and capped
    timed_out: bool = False


def run_argv(argv: list[str], cwd: Path, timeout: float, profile: str | None = None) -> Result:
    """Run one command: no shell, fixed cwd, scrubbed env, a timeout, and (with `profile`) inside that sandbox."""
    full = (["/usr/bin/sandbox-exec", "-p", profile] if profile else []) + list(argv)
    try:
        r = subprocess.run(full, cwd=cwd, env=scrubbed_env(), capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL, errors="replace")
    except subprocess.TimeoutExpired as e:
        partial = e.stdout if isinstance(e.stdout, str) else (e.stdout or b"").decode(errors="replace")
        return Result(None, redact(partial + f"\n(stopped after {timeout:.0f}s)"), timed_out=True)
    except OSError as e:
        return Result(None, f"couldn't run it: {e}")
    out = (r.stdout or "") + (("\n" + r.stderr) if r.stderr.strip() else "")
    return Result(r.returncode, redact(out.strip() or "(no output)"))
