"""Stands in for gh in tests (prx.py runs it when PRX_GH names this file).

Answers `gh pr list ... --json <fields>` with $PRX_GH_ANSWER (a JSON list of PRs),
or fails like an unauthenticated gh when $PRX_GH_FAIL is set.
"""
import os
import sys

args = sys.argv[1:]
if os.environ.get("PRX_GH_FAIL"):
    sys.stderr.write(os.environ["PRX_GH_FAIL"] + "\n")
    sys.exit(1)
if args[:2] != ["pr", "list"] or "--head" not in args or "baseRefName" not in args[args.index("--json") + 1]:
    sys.stderr.write(f"fake gh doesn't answer: {args}\n")
    sys.exit(2)
sys.stdout.write(os.environ.get("PRX_GH_ANSWER", "[]"))
