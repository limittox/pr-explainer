"""Stands in for gh in tests (prx.py runs it when PRX_GH names this file).

Answers `gh pr list --head <branch> --state all ... --json <fields>` with $PRX_GH_ANSWER (a
JSON list of PRs), or with no PRs when $PRX_GH_HEAD names another branch than --head. Answers
`gh pr view <n> --json <fields>` with PR n from $PRX_GH_VIEW (a JSON list), or fails as gh
does for a PR it can't find. Fails like an unauthenticated gh when $PRX_GH_FAIL is set.
"""
import json
import os
import sys

args = sys.argv[1:]
if os.environ.get("PRX_GH_FAIL"):
    sys.stderr.write(os.environ["PRX_GH_FAIL"] + "\n")
    sys.exit(1)


def arg(flag):
    return args[args.index(flag) + 1] if flag in args else None


if "baseRefName" not in (arg("--json") or ""):
    sys.stderr.write(f"fake gh doesn't answer: {args}\n")
    sys.exit(2)
if args[:2] == ["pr", "view"]:
    found = [p for p in json.loads(os.environ.get("PRX_GH_VIEW", "[]")) if str(p.get("number")) == args[2]]
    if not found:
        sys.stderr.write(f"GraphQL: Could not resolve to a PullRequest with the number of {args[2]}.\n")
        sys.exit(1)
    sys.stdout.write(json.dumps(found[0]))
elif args[:2] == ["pr", "list"] and arg("--head") and arg("--state") == "all" and arg("--limit"):
    wrong_head = os.environ.get("PRX_GH_HEAD") and arg("--head") != os.environ["PRX_GH_HEAD"]
    sys.stdout.write("[]" if wrong_head else os.environ.get("PRX_GH_ANSWER", "[]"))
else:
    sys.stderr.write(f"fake gh doesn't answer: {args}\n")
    sys.exit(2)
