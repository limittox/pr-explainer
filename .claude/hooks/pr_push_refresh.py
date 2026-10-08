#!/usr/bin/env python3
"""PostToolUse hook (Bash|PowerShell): after a successful `git push`, ask Claude
to refresh the PR explainer at the same artifact URL.

Only fires when this branch already has an explainer and it shows an older
commit than HEAD. Failed pushes never reach PostToolUse (they fire
PostToolUseFailure), and dry runs and branch deletes are ignored.
Fails open: this hook only nudges, so any error means "say nothing".
"""
import json
import os
import re
import sys
from pathlib import Path

sys.dont_write_bytecode = True
SKILL_DIR = Path(__file__).resolve().parents[1] / "skills" / "pr-explainer"


def main() -> int:
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8-sig", errors="replace"))
        cmd = (data.get("tool_input") or {}).get("command") or ""
        cwd = data.get("cwd") or os.getcwd()
        if not re.search(r"\bpush\b", cmd):  # fast path
            return 0
        sys.path.insert(0, str(SKILL_DIR))
        import prx

        if not prx.is_refreshing_push(cmd):
            return 0
        st = prx.explainer_state(prx.Repo(cwd))
    except Exception:  # noqa: BLE001
        return 0
    if not st["artifact_url"] or st["fresh"]:
        return 0
    print(json.dumps({
        "decision": "block",
        "reason": (f"New commits pushed (HEAD {st['head_sha'][:7]}); the PR explainer still shows "
                   f"{prx.short(st['explained_sha'])}. Run /pr-explainer in update mode and republish to the "
                   f"SAME artifact URL {st['artifact_url']}. Do not create a new artifact."),
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
