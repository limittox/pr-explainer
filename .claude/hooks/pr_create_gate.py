#!/usr/bin/env python3
"""PreToolUse hook (Bash|PowerShell): gate `gh pr create` on a fresh PR explainer.

Blocks (exit 2, reason on stderr) unless this branch has a published explainer,
the explainer shows the current HEAD, and its link is in the PR body.
Fails closed for PR-creation commands: if it can't check, it blocks.
"""
import json
import os
import re
import sys
from pathlib import Path

sys.dont_write_bytecode = True
SKILL_DIR = Path(__file__).resolve().parents[1] / "skills" / "pr-explainer"


def block(reason: str) -> int:
    print(f"PR explainer gate: {reason}", file=sys.stderr)
    return 2


def main() -> int:
    raw = sys.stdin.buffer.read().decode("utf-8-sig", errors="replace")
    try:
        data = json.loads(raw)
        cmd = (data.get("tool_input") or {}).get("command") or ""
        cwd = data.get("cwd") or os.getcwd()
        tool = data.get("tool_name") or "Bash"
    except (ValueError, AttributeError):
        if re.search(r"\bgh(?:\.exe)?\s+pr\s+(?:create|new)\b", raw):
            return block("couldn't read the hook input, so the PR explainer check couldn't run.")
        return 0

    # Fast path: no "pr" anywhere and no PowerShell -EncodedCommand to decode.
    if not re.search(r"pr|-e", cmd, re.I):
        return 0
    try:
        sys.path.insert(0, str(SKILL_DIR))
        import prx
    except Exception as err:  # noqa: BLE001 - any import failure must block, not crash open
        return block(f"couldn't load {SKILL_DIR / 'prx.py'} ({err}).")
    if not prx.is_pr_create(cmd, tool):
        return 0
    try:
        problem = prx.gate_problem(cmd, cwd, tool)
    except Exception as err:  # noqa: BLE001
        return block(f"couldn't check the explainer for this branch ({err}).")
    return block(problem) if problem else 0


if __name__ == "__main__":
    sys.exit(main())
