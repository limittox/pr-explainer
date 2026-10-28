"""Stands in for gh in tests (prx.py runs it when PRX_GH names this file).

Answers `gh pr list --head <branch> --state all ... --json <fields>` with $PRX_GH_ANSWER (a
JSON list of PRs), or with no PRs when $PRX_GH_HEAD names another branch than --head. Answers
`gh pr view <n> --json <fields>` with PR n from $PRX_GH_VIEW (a JSON list), or fails as gh
does for a PR it can't find, or with $PRX_GH_VIEW_FAIL. Fails like an unauthenticated gh when
$PRX_GH_FAIL is set.
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
fields = arg("--json").split(",")


def only_asked(pr):
    """Like gh: only the fields --json asked for, so a field prx.py stops asking for goes missing."""
    return {k: v for k, v in pr.items() if k in fields}
if args[:2] == ["pr", "view"]:
    if os.environ.get("PRX_GH_VIEW_FAIL"):  # a failure that isn't "no such PR" (a network blip)
        sys.stderr.write(os.environ["PRX_GH_VIEW_FAIL"] + "\n")
        sys.exit(1)
    found = [p for p in json.loads(os.environ.get("PRX_GH_VIEW", "[]")) if str(p.get("number")) == args[2]]
    if not found:
        sys.stderr.write(f"GraphQL: Could not resolve to a PullRequest with the number of {args[2]}.\n")
        sys.exit(1)
    sys.stdout.write(json.dumps(only_asked(found[0])))
elif args[:2] == ["pr", "list"] and arg("--head") and arg("--state") == "all" and arg("--limit"):
    wrong_head = os.environ.get("PRX_GH_HEAD") and arg("--head") != os.environ["PRX_GH_HEAD"]
    answer = json.loads(os.environ.get("PRX_GH_ANSWER", "[]"))
    sys.stdout.write("[]" if wrong_head else json.dumps([only_asked(p) if isinstance(p, dict) else p for p in answer]
                                                        if isinstance(answer, list) else answer))
else:
    sys.stderr.write(f"fake gh doesn't answer: {args}\n")
    sys.exit(2)
