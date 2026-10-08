#!/usr/bin/env python3
"""PreToolUse hook (Bash|PowerShell): gate `gh pr create` on a fresh PR explainer.

Blocks (exit 2, reason on stderr) unless this branch has a published explainer,
the explainer shows the current HEAD, and its link is in the PR body.
Fails closed for anything that looks like PR creation: unreadable input, a
command the parser can't follow, a parser crash, or a git error all block.
"""
import json
import os
import re
import sys
from pathlib import Path

sys.dont_write_bytecode = True
SKILL_DIR = Path(__file__).resolve().parents[1] / "skills" / "pr-explainer"
CRUDE_PR_CREATE = re.compile(r"\bgh(?:\.exe)?\b.*\bpr\s+(?:create|new)\b", re.I | re.S)


def block(reason: str) -> int:
    print(f"PR explainer gate: {reason}", file=sys.stderr)
    return 2


def plainly_unrelated(cmd: str) -> bool:
    """True when cmd can't be a PR creation, so the parser (about 60 ms to load) can be skipped.

    Shells drop quotes and escapes when they build words, so `gh p''r create`,
    `gh "p"r create`, `gh p\\r create` and PowerShell's `gh p`r create` all run
    `gh pr create`. Look for "pr" only after removing those characters, and
    never skip ANSI-C quoting ($'\\x70r') or a PowerShell -EncodedCommand.
    """
    if "$'" in cmd:
        return False
    return not re.search(r"pr|-e", re.sub(r"[\"'`\\]", "", cmd), re.I)


def main() -> int:
    raw = sys.stdin.buffer.read().decode("utf-8-sig", errors="replace")
    try:
        data = json.loads(raw)
        cmd = (data.get("tool_input") or {}).get("command") or ""
        cwd = data.get("cwd") or os.getcwd()
        tool = data.get("tool_name") or "Bash"
    except (ValueError, AttributeError):
        if CRUDE_PR_CREATE.search(raw):
            return block("couldn't read the hook input, so the PR explainer check couldn't run.")
        return 0

    if plainly_unrelated(cmd):
        return 0
    try:
        sys.path.insert(0, str(SKILL_DIR))
        import prx
    except Exception as err:  # noqa: BLE001 - any import failure must block, not crash open
        return block(f"couldn't load {SKILL_DIR / 'prx.py'} ({err}).")
    try:
        creating = prx.is_pr_create(cmd, tool)
    except Exception as err:  # noqa: BLE001 - never let a crash wave a PR through
        if CRUDE_PR_CREATE.search(cmd):
            return block(f"couldn't parse this command ({err}), so it couldn't check the PR explainer.")
        return 0
    if not creating:
        return 0
    try:
        problem = prx.gate_problem(cmd, cwd, tool)
    except Exception as err:  # noqa: BLE001
        return block(f"couldn't check the explainer for this branch ({err}).")
    return block(problem) if problem else 0


if __name__ == "__main__":
    sys.exit(main())
