"""The PreToolUse hook for JARVIS's Claude Code sessions (see jarvis.agent).

Claude Code runs this before every tool call, with the call as JSON on stdin; it prints the
decision. It runs inside the session's sandbox, started with `python -I` so nothing in the
task's folder can be imported in place of JARVIS's own code. Any error means "deny".
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> None:
    try:
        from jarvis.agent import run_hook
        root = Path(os.environ["JARVIS_AGENT_ROOT"])
        state = Path(os.environ["JARVIS_AGENT_STATE"])
        out = run_hook(sys.stdin.read(), root, state, edits=os.environ.get("JARVIS_AGENT_EDITS") != "0")
    except Exception as e:  # fail closed
        out = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                      "permissionDecisionReason": f"JARVIS's safety check failed ({str(e)[:100]})"}}
    print(json.dumps(out))


if __name__ == "__main__":
    main()
