#!/usr/bin/env python3
"""PostToolUse hook (Bash|PowerShell): after a successful `git push`, ask Claude
to refresh the PR explainer at the same artifact URL.

Only fires when this branch already has an explainer, it shows an older
commit than HEAD, and the branch's upstream is now at HEAD. Failed pushes
normally fire PostToolUseFailure instead, but a piped `git push | tail` hides
the exit code, so the upstream check catches those. Dry runs and branch
deletes are ignored.
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
        # Fast path, after removing what shells drop when they build words
        # (`git pu''sh` runs `git push`); -e is PowerShell's -EncodedCommand.
        if "$'" not in cmd and not re.search(r"push|-e", re.sub(r"[\"'`\\]", "", cmd), re.I):
            return 0
        sys.path.insert(0, str(SKILL_DIR))
        import prx

        if not prx.is_refreshing_push(cmd, data.get("tool_name") or "Bash"):
            return 0
        st = prx.explainer_state(prx.Repo(cwd))
        if not st["artifact_url"] or st["fresh"]:
            return 0
        # A rejected push can still reach PostToolUse when its exit code is
        # hidden (`git push | tail`). Only nudge once the upstream is at HEAD.
        upstream = prx.git(["rev-parse", "--verify", "--quiet", "@{u}"], cwd, check=False).strip()
        if upstream and upstream != st["head_sha"]:
            return 0
    except Exception:  # noqa: BLE001
        return 0
    head, shown, url = st["head_sha"][:7], prx.short(st["explained_sha"]), st["artifact_url"]
    if st["merged"]:
        reason = (f"New commits pushed (HEAD {head}). The PR explainer at {url} shows {shown}, which is already "
                  f"in {st['base_ref']}: that PR was merged. Run /pr-explainer; prepare will ask you to start a "
                  "new artifact (--new) so the merged PR's page keeps showing its own code.")
    elif st["diverged"]:
        reason = (f"New commits pushed (HEAD {head}). The PR explainer shows {shown}, which isn't in this "
                  "branch's history: a rebase or force-push of the same PR, or the branch name reused for a "
                  f"new PR. Run /pr-explainer; prepare will ask whether to keep the URL {url} (same PR) or "
                  "start a new artifact (new PR).")
    else:
        reason = (f"New commits pushed (HEAD {head}); the PR explainer still shows {shown}. Run /pr-explainer "
                  f"in update mode and republish to the SAME artifact URL {url}. Do not create a new artifact.")
    print(json.dumps({"decision": "block", "reason": reason}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
