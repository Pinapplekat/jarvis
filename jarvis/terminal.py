"""Terminal sense: a zsh hook appends one line per command (time, exit code, folder,
duration, command) to a local log; JARVIS reads new lines into the timeline, redacted."""

from datetime import datetime, timezone
from pathlib import Path

from jarvis import config
from jarvis.memory import Memory
from jarvis.repos import known_repos, project_for
from jarvis.screen import redact

LOG = config.DATA_DIR / "terminal.log"
HOOK = config.DATA_DIR / "jarvis-hook.zsh"
OFFSET_KEY = "terminal_offset"
SOURCE_LINE = f'[[ -f "{HOOK}" ]] && source "{HOOK}"  # JARVIS terminal sense'

HOOK_SCRIPT = f"""# JARVIS terminal sense: logs each command's time, exit code, folder, duration.
# Remove the matching 'source' line from ~/.zshrc to turn it off.
zmodload zsh/datetime
_jarvis_log="{LOG}"
_jarvis_preexec() {{ _jarvis_cmd="$1"; _jarvis_start=$EPOCHREALTIME }}
_jarvis_precmd() {{
  local code=$?
  [[ -z "$_jarvis_cmd" ]] && return
  local dur=$(( EPOCHREALTIME - _jarvis_start ))
  printf '%s\\t%s\\t%s\\t%.1f\\t%s\\n' "$EPOCHSECONDS" "$code" "$PWD" "$dur" "${{_jarvis_cmd//$'\\n'/ }}" >> "$_jarvis_log"
  unset _jarvis_cmd
}}
autoload -Uz add-zsh-hook
add-zsh-hook preexec _jarvis_preexec
add-zsh-hook precmd _jarvis_precmd
"""


def install_hook(zshrc: Path = Path.home() / ".zshrc") -> str:
    """Write the hook script and add one 'source' line to ~/.zshrc (idempotent)."""
    HOOK.parent.mkdir(parents=True, exist_ok=True)
    HOOK.write_text(HOOK_SCRIPT)
    existing = zshrc.read_text() if zshrc.exists() else ""
    if SOURCE_LINE in existing:
        return "already installed"
    with zshrc.open("a") as f:
        f.write(("\n" if existing and not existing.endswith("\n") else "") + SOURCE_LINE + "\n")
    return f"added to {zshrc}; open a new terminal tab to start logging"


def hook_installed(zshrc: Path = Path.home() / ".zshrc") -> bool:
    return zshrc.exists() and SOURCE_LINE in zshrc.read_text()


def ingest(mem: Memory, log: Path = LOG) -> int:
    """Read new terminal log lines into the timeline. Returns how many were added."""
    if not log.exists():
        return 0
    offset = int(mem.get_kv(OFFSET_KEY) or 0)
    size = log.stat().st_size
    if size < offset:
        offset = 0  # log was rotated/cleared
    with log.open("rb") as f:
        f.seek(offset)
        data = f.read()
    complete = data[: data.rfind(b"\n") + 1]  # leave a half-written last line for next time
    repos = known_repos(mem)
    added = 0
    for raw in complete.decode(errors="replace").splitlines():
        parts = raw.split("\t", 4)
        if len(parts) != 5:
            continue
        epoch, code, cwd, dur, cmd = parts
        try:
            at = datetime.fromtimestamp(int(epoch), timezone.utc)
        except ValueError:
            continue
        status = "ok" if code == "0" else f"exit {code}"
        mem.observe("terminal", f"$ {redact(cmd)[:300]}  [{status}, {dur}s]", app="terminal",
                    title=cwd, project=project_for(repos, cwd=cwd), at=at)
        added += 1
    mem.set_kv(OFFSET_KEY, str(offset + len(complete)))
    return added
