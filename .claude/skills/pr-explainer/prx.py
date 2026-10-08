#!/usr/bin/env python3
"""PR Explainer helper, used by the /pr-explainer skill and by the hooks.

Subcommands (run from anywhere inside the repo):
  status                 where this branch stands: mode, artifact URL, freshness
  prepare [--base REF] [--same-pr | --new]
                         collect git facts and the changed-file list into <key>.context.json
  validate [--only P]    check intent/explainer JSON against the contract (P = intent|explainer)
  render [--standalone]  validate, scan for secrets, write <key>.html
  record URL             remember the published artifact URL and the commit it shows
  prune [--dry-run]      remove state for branches that no longer exist

State lives in <git common dir>/pr-explainer/, so it is never committed and is
shared by every worktree of the clone. Standard library only (Python 3.9+).
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

import cmdparse

SKILL_DIR = Path(__file__).resolve().parent
TEMPLATE_PATH = SKILL_DIR / "template.html"
TEMPLATE_VERSION = "1"

MAX_HOTSPOTS = 3
MAX_SNIPPET_LINES = 25
MAX_TLDR = 3
LARGE_PR_FILES = 150
LARGE_PR_LINES = 4000

# Mermaid preview for `render --standalone` only. Published artifacts render
# <pre class="mermaid"> natively, so the real page never loads Mermaid itself.
PREVIEW_MERMAID = "https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.min.js"


class PrxError(Exception):
    """A problem to report to the caller as a one-line message."""


# --------------------------------------------------------------------- git

def git(args, cwd, check=True, input=None, timeout=None):
    proc = subprocess.run(
        ["git", *args], cwd=cwd, input=input, capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=timeout,
    )
    if check and proc.returncode != 0:
        lines = proc.stderr.strip().splitlines()
        reason = next((ln for ln in lines if ln.startswith(("fatal:", "error:"))), lines[0] if lines else "")
        raise PrxError(f"git {' '.join(args)} failed: {reason or f'exit {proc.returncode}'}")
    return proc.stdout


def branch_key(branch: str) -> str:
    """File-name-safe key for a branch. Names that had to change, or that have
    capitals (Windows and macOS file names ignore case), get a short hash, so
    feature/x -> feature__x-1a2b3c never collides with feature__x or Feature__x."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", branch.replace("/", "__"))
    if safe == branch == branch.lower():
        return safe
    return f"{safe}-{hashlib.sha1(branch.encode('utf-8')).hexdigest()[:6]}"


class Repo:
    def __init__(self, cwd=None):
        self.cwd = str(cwd or os.getcwd())
        self.root = git(["rev-parse", "--show-toplevel"], self.cwd).strip()
        common = git(["rev-parse", "--path-format=absolute", "--git-common-dir"], self.cwd).strip()
        self.state_dir = Path(common) / "pr-explainer"
        self.branch = git(["branch", "--show-current"], self.cwd).strip()

    def head(self, branch=None) -> str:
        if not branch or branch == self.branch:
            return git(["rev-parse", "HEAD"], self.cwd).strip()
        for ref in (f"refs/heads/{branch}", f"refs/remotes/origin/{branch}"):
            sha = git(["rev-parse", "--verify", "--quiet", ref], self.cwd, check=False).strip()
            if sha:
                return sha
        raise PrxError(f"Couldn't find branch '{branch}' locally or on origin.")

    def path(self, suffix: str, branch=None) -> Path:
        branch = branch or self.branch
        if not branch:
            raise PrxError("HEAD is detached. Check out the PR branch first.")
        return self.state_dir / f"{branch_key(branch)}.{suffix}"


def read_text(p: Path) -> str:
    return p.read_text(encoding="utf-8-sig").strip() if p.exists() else ""


def write_text(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def load_json(p: Path, label: str):
    if not p.exists():
        raise PrxError(f"{label} not found at {p}")
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as e:
        raise PrxError(f"{label} is not valid JSON ({p.name} line {e.lineno} col {e.colno}: {e.msg})")


def is_ancestor(repo: Repo, older: str, newer: str) -> bool:
    return subprocess.run(["git", "merge-base", "--is-ancestor", older, newer],
                          cwd=repo.cwd, capture_output=True).returncode == 0


def explainer_state(repo: Repo, branch=None, base=None) -> dict:
    branch = branch or repo.branch
    url = read_text(repo.path("url", branch))
    sha = read_text(repo.path("sha", branch))
    head = repo.head(branch)
    fresh = bool(url) and sha == head
    stale = bool(url and sha) and not fresh
    if base is None and stale:
        ctx = repo.path("context.json", branch)
        base = (json.loads(ctx.read_text(encoding="utf-8-sig")).get("base_ref") if ctx.exists() else None) or None
    return {
        "branch": branch,
        "mode": "update" if url else "create",
        "artifact_url": url or None,
        "explained_sha": sha or None,
        "head_sha": head,
        "fresh": fresh,
        "base_ref": base,
        # The explained commit is already in the base branch, so that PR was
        # merged (merge commit or fast-forward) and this is new work, even if
        # it continues on the same branch. Squash merges need GitHub: see
        # finished_pr(), which prepare checks.
        "merged": stale and bool(base) and is_ancestor(repo, sha, base),
        # The explained commit isn't in this branch's history: a rebase or
        # force-push, or the branch name reused for a new PR.
        "diverged": stale and not is_ancestor(repo, sha, head),
        "state_dir": str(repo.state_dir),
    }


def finished_pr(repo: Repo, branch: str, sha: str):
    """(pr, problem). pr: a merged or closed GitHub PR for this branch whose head
    included the explained commit, when no PR for the branch is open. problem:
    why GitHub couldn't be asked, so prepare can warn instead of going quiet."""
    if os.environ.get("PRX_NO_GITHUB"):
        return None, None
    try:
        proc = subprocess.run(
            ["gh", "pr", "list", "--head", branch, "--state", "all", "--limit", "20",
             "--json", "number,state,headRefOid,url"],
            cwd=repo.cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
        if proc.returncode != 0:
            lines = proc.stderr.strip().splitlines()
            return None, (lines[-1] if lines else f"gh exited {proc.returncode}")
        prs = json.loads(proc.stdout or "[]")
    except (OSError, subprocess.TimeoutExpired, ValueError) as err:
        return None, f"{type(err).__name__}: {err}"
    if any(p.get("state") == "OPEN" for p in prs):
        return None, None
    for p in prs:
        oid = p.get("headRefOid") or ""
        if p.get("state") in ("MERGED", "CLOSED") and (oid == sha or (oid and is_ancestor(repo, sha, oid))):
            return p, None
    return None, None


# ---------------------------------------------------------- command matching
# Shared by the hooks. cmdparse finds the simple commands a Bash or PowerShell
# line would run (quote-aware, with wrappers, substitutions and heredocs fed to
# a shell unwrapped); these helpers pick out `gh pr create` and `git push`.

# Only for commands that can't be tokenised (an unclosed quote): fail closed.
CRUDE_PR_CREATE = re.compile(r"\bgh(?:\.exe)?\b.*?\bpr\s+(?:create|new)\b", re.I | re.S)
CRUDE_PUSH = re.compile(r"\bgit(?:\.exe)?\b.*?\bpush\b", re.I | re.S)
GH_CREATE_OPTIONS = {"--head": "head", "-H": "head", "--base": "base", "-B": "base", "--title": "title",
                     "-t": "title", "--body": "body", "-b": "body", "--body-file": "body_file", "-F": "body_file"}
GIT_VALUE_OPTIONS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--config-env"}
TITLE_RE = re.compile(r"""(?:--title|(?<![\w-])-t)(?:=|\s+)(?:"(?:[^"\\]|\\.)*"|'[^']*'|[^\s;&|)]+)""")


def parse_commands(cmd: str, tool: str = "Bash"):
    """The commands in a hook's command string, or None if they can't be worked out.
    None makes the callers fall back to a crude regex and fail closed."""
    try:
        return cmdparse.commands(cmd or "", "powershell" if (tool or "").lower() == "powershell" else "bash")
    except Exception:  # noqa: BLE001 - a parser bug must mean "can't tell", never "no PR here"
        return None


def gh_pr_create_args(c):
    """The arguments after `gh pr create` / `gh pr new`, or None for any other command.

    The sequence counts anywhere in the command's words, not only as the
    program. That's the backstop for wrappers the parser doesn't model
    (`find -exec`, `setsid`, `coproc`, function bodies): an unquoted
    `gh pr create` in any argument list is treated as one. Quoted text stays a
    single word, so `rg 'gh pr create'` still doesn't match.
    """
    for j, word in enumerate(c.argv):
        if cmdparse.program(word) == "gh":
            args = _after_gh_pr_create(c.argv[j + 1:])
            if args is not None:
                return args
    return None


def _after_gh_pr_create(rest):
    seen_pr = False
    while rest:
        a = rest[0]
        if a in ("-R", "--repo"):
            rest = rest[2:]
        elif a.startswith("-"):
            rest = rest[1:]
        elif a == "pr" and not seen_pr:
            seen_pr, rest = True, rest[1:]
        elif a in ("create", "new") and seen_pr:
            return rest[1:]
        else:
            return None
    return None


def is_pr_create(cmd: str, tool: str = "Bash") -> bool:
    cmds = parse_commands(cmd, tool)
    if cmds is None:
        return bool(CRUDE_PR_CREATE.search(cmd or ""))
    return any(gh_pr_create_args(c) is not None for c in cmds)


def _sends_commits(c) -> bool:
    if not c.argv or cmdparse.program(c.argv[0]) != "git":
        return False
    argv, i = c.argv, 1
    while i < len(argv) and argv[i].startswith("-"):
        i += 2 if argv[i] in GIT_VALUE_OPTIONS else 1
    if argv[i:i + 1] != ["push"]:
        return False
    for a in argv[i + 1:]:
        if a in ("--dry-run", "--delete") or a.startswith(":") or re.fullmatch(r"-[A-Za-z]*[nd][A-Za-z]*", a):
            return False  # a dry run, or deleting a branch
    return True


def is_refreshing_push(cmd: str, tool: str = "Bash") -> bool:
    """A git push that sends new commits (not --dry-run, not a branch delete)."""
    cmds = parse_commands(cmd, tool)
    if cmds is None:
        return bool(CRUDE_PUSH.search(cmd or ""))
    return any(_sends_commits(c) for c in cmds)


def gh_create_options(args) -> dict:
    opts = {"head": None, "base": None, "title": None, "body": [], "body_file": []}
    i = 0
    while i < len(args):
        a = args[i]
        name, eq, inline = a.partition("=")
        if a in GH_CREATE_OPTIONS:
            key, value, i = GH_CREATE_OPTIONS[a], (args[i + 1] if i + 1 < len(args) else ""), i + 2
        elif eq and name in GH_CREATE_OPTIONS:
            key, value, i = GH_CREATE_OPTIONS[name], inline, i + 1
        else:
            i += 1
            continue
        if key in ("body", "body_file"):
            opts[key].append(value)
        else:
            opts[key] = value
    return opts


def _local_path(name: str, cwd: str) -> Path:
    name = re.sub(r"\$env:(\w+)", lambda m: os.environ.get(m.group(1), m.group(0)), name, flags=re.I)
    name = os.path.expandvars(os.path.expanduser(name))
    m = re.match(r"^/([A-Za-z])/(.*)$", name)  # Git Bash /c/Users/... on Windows
    if m and os.name == "nt":
        name = f"{m.group(1)}:/{m.group(2)}"
    p = Path(name)
    return p if p.is_absolute() else Path(cwd) / p


def short(sha) -> str:
    return (sha or "none")[:7]


def gate_problem(cmd: str, cwd: str, tool: str = "Bash"):
    """Why a `gh pr create` in this command must not run yet, or None when it may."""
    cmds = parse_commands(cmd, tool)
    if cmds is None:
        if CRUDE_PR_CREATE.search(cmd or ""):
            return "couldn't parse this command (check its quotes), so it couldn't check the PR explainer."
        return None
    creates = [(c, args) for c in cmds for args in [gh_pr_create_args(c)] if args is not None]
    if not creates:
        return None
    repo = Repo(cwd)
    for c, args in creates:
        problem = _create_problem(repo, c, gh_create_options(args), cmd, cwd)
        if problem:
            return problem
    return None


def _create_problem(repo: Repo, c, opts: dict, cmd: str, cwd: str):
    branch = opts["head"].split(":")[-1] if opts["head"] else repo.branch
    if not branch:
        return "HEAD is detached. Check out the PR branch, run /pr-explainer, then create the PR."
    st = explainer_state(repo, branch)
    if not st["artifact_url"]:
        return (f"No PR explainer for branch '{branch}' yet. Run the /pr-explainer skill first, "
                "then re-run gh pr create with the explainer link in the PR body.")
    if st["merged"]:
        return (f"The recorded explainer for '{branch}' shows commit {short(st['explained_sha'])}, which is already "
                f"in {st['base_ref']}: that PR was merged, so this is a new one. Run /pr-explainer; prepare will "
                "ask you to start a new artifact (--new) so the merged PR's page keeps showing its own code.")
    if st["diverged"]:
        return (f"The recorded explainer for '{branch}' shows commit {short(st['explained_sha'])}, which isn't "
                "in this branch's history (a rebase, or the branch name reused for a new PR). Run /pr-explainer; "
                "prepare will ask whether to keep the existing artifact URL.")
    if not st["fresh"]:
        return (f"The PR explainer shows commit {short(st['explained_sha'])} but '{branch}' is at "
                f"{short(st['head_sha'])}. Run /pr-explainer in update mode (republish to the same URL "
                f"{st['artifact_url']}, unless prepare reports that PR as finished: then start a new artifact "
                "with --new), then re-run gh pr create.")
    ctx_path = repo.path("context.json", branch)
    if opts["base"] and ctx_path.exists():
        prepared = load_json(ctx_path, "context.json").get("base_ref", "")
        if opts["base"] not in (prepared, prepared.split("/", 1)[-1]):
            return (f"The explainer was prepared against '{prepared}' but this PR targets '{opts['base']}'. "
                    f"Run /pr-explainer with `prepare --base {opts['base']}`, republish, then re-run gh pr create.")
    url = st["artifact_url"]
    bodies, unreadable = list(opts["body"]), False
    for name in opts["body_file"]:
        if name == "-":  # body on stdin: a heredoc we can read, or a pipe we can't
            if c.stdin is None:
                unreadable = True
            else:
                bodies.append(c.stdin)
            continue
        try:
            bodies.append(_local_path(name, cwd).read_text(encoding="utf-8-sig", errors="replace"))
        except OSError:
            unreadable = True
    if any(url in b for b in bodies):
        return None
    # A body held in a variable or piped in can't be read here, so accept the
    # link anywhere in the command except the title.
    if (unreadable or any("$" in b for b in opts["body"])) and url in TITLE_RE.sub("", cmd):
        return None
    return (f"Put the PR explainer link near the top of the PR body: {url}  "
            f"A ready-made body is at {repo.path('pr-body.md', branch)} (pass it with --body-file).")


# ------------------------------------------------------------------ prepare

EXCLUDE_RULES = [
    ("lockfile", re.compile(
        r"(?:^|/)(?:package-lock\.json|npm-shrinkwrap\.json|yarn\.lock|pnpm-lock\.yaml|bun\.lockb?|"
        r"poetry\.lock|Pipfile\.lock|uv\.lock|Cargo\.lock|Gemfile\.lock|composer\.lock|go\.sum|"
        r"gradle\.lockfile|packages\.lock\.json|flake\.lock)$")),
    ("vendored", re.compile(r"(?:^|/)(?:node_modules|vendor|third_party|bower_components)/")),
    ("generated", re.compile(
        r"(?:^|/)(?:dist|build|out|target|coverage|\.next|__snapshots__|__generated__|generated)/"
        r"|\.(?:min\.js|min\.css|map|snap)$|_pb2(?:_grpc)?\.py$|\.pb\.go$|\.g\.dart$|\.generated\.[^/]+$")),
]


def resolve_base(repo: Repo, base, fetch: bool):
    warnings = []
    if base:
        candidates = [f"origin/{base}", base] if not base.startswith("origin/") else [base]
    else:
        head_ref = git(["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"],
                       repo.cwd, check=False).strip()
        candidates = [head_ref] if head_ref else []
        candidates += ["origin/main", "origin/master", "main", "master"]
    for ref in candidates:
        if git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], repo.cwd, check=False).strip():
            break
    else:
        raise PrxError("Couldn't find the base branch. Pass --base <ref>.")
    if fetch and ref.startswith("origin/"):
        try:
            git(["fetch", "--quiet", "origin", ref[len("origin/"):]], repo.cwd, timeout=60)
        except (PrxError, subprocess.TimeoutExpired) as e:
            warnings.append(f"Couldn't fetch {ref}, using the local copy ({e}).")
    return ref, warnings


def numstat(repo: Repo, a: str, b: str):
    out = git(["-c", "core.quotepath=off", "diff", "--numstat", "-z", "-M", a, b], repo.cwd)
    toks, files, i = out.split("\0"), [], 0
    while i < len(toks):
        tok = toks[i]
        if not tok:
            i += 1
            continue
        added, deleted, path = tok.split("\t", 2)
        if path == "":  # rename: "A\tD\t\0old\0new\0"
            old, path = toks[i + 1], toks[i + 2]
            i += 3
        else:
            old = None
            i += 1
        binary = added == "-"
        files.append({
            "path": path, "old_path": old, "binary": binary,
            "additions": 0 if binary else int(added),
            "deletions": 0 if binary else int(deleted),
        })
    return files


def linguist_flags(repo: Repo, paths):
    """{path: 'generated'|'vendored'} from .gitattributes linguist-* attributes."""
    if not paths:
        return {}
    out = git(["check-attr", "-z", "--stdin", "linguist-generated", "linguist-vendored"],
              repo.cwd, check=False, input="\0".join(paths) + "\0")
    toks, flags = out.split("\0"), {}
    for i in range(0, len(toks) - 2, 3):
        path, attr, value = toks[i], toks[i + 1], toks[i + 2]
        if value in ("set", "true"):
            flags.setdefault(path, attr.replace("linguist-", ""))
    return flags


def classify(files, flags):
    review, excluded = [], []
    for f in files:
        reason = "binary" if f["binary"] else flags.get(f["path"])
        if not reason:
            reason = next((name for name, rx in EXCLUDE_RULES if rx.search(f["path"])), None)
        (excluded if reason else review).append({**f, "reason": reason} if reason else f)
    review.sort(key=lambda f: f["additions"] + f["deletions"], reverse=True)
    return review, excluded


def areas(files, depth=2):
    acc = {}
    for f in files:
        parts = f["path"].split("/")
        key = "/".join(parts[:depth]) if len(parts) > depth else "/".join(parts[:-1]) or "(root)"
        a = acc.setdefault(key, {"area": key, "files": 0, "additions": 0, "deletions": 0})
        a["files"] += 1
        a["additions"] += f["additions"]
        a["deletions"] += f["deletions"]
    return sorted(acc.values(), key=lambda a: a["additions"] + a["deletions"], reverse=True)[:40]


ARCHIVED_SUFFIXES = ("url", "sha", "intent.json", "html", "rendered.json", "pr-body.md")
STATE_FILE_RE = re.compile(
    r"^(?P<key>.+?)\.(?:url|sha|context\.json|intent\.json|html|preview\.html|rendered\.json|reviewer-prompt\.md"
    r"|pr-body\.md|explainer(?:\.[\w-]+)?\.json|archived-\d{8}T\d{6}\..+)$")


def archive_state(repo: Repo) -> str:
    """Move a branch's artifact URL and related files aside, so the next publish starts a new artifact."""
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
    for suffix in ARCHIVED_SUFFIXES:
        p = repo.path(suffix)
        if p.exists():
            os.replace(p, repo.path(f"archived-{stamp}.{suffix}"))
    return stamp


def explainer_path(repo: Repo, ctx: dict) -> Path:
    """Each prepare names its own explainer file, so a reviewer from an earlier run can't land in this one."""
    return repo.state_dir / ctx["explainer_file"] if ctx.get("explainer_file") else repo.path("explainer.json")


def cmd_prepare(args):
    repo = Repo()
    if not repo.branch:
        raise PrxError("HEAD is detached. Check out the PR branch first.")
    # Fetch the base first, so "is the explained commit already merged?" is current.
    base, warnings = resolve_base(repo, args.base, fetch=not args.no_fetch)
    st = explainer_state(repo, base=base)
    finished, gh_problem = (finished_pr(repo, repo.branch, st["explained_sha"])
                            if st["artifact_url"] and not st["fresh"] else (None, None))
    if gh_problem and not args.new:
        warnings.append(
            f"Couldn't ask GitHub whether this branch's previous PR was squash-merged ({gh_problem}). Check "
            f"`gh pr list --head {repo.branch} --state all`: if that PR is merged or closed, run prepare --new "
            "so its page isn't overwritten.")
    if args.new and st["artifact_url"]:
        archive_state(repo)
    elif (st["merged"] or finished) and not args.same_pr:
        how = (f"GitHub shows PR #{finished['number']} for this branch as {finished['state'].lower()}" if finished
               else f"commit {short(st['explained_sha'])} is already in {base}")
        raise PrxError(
            f"The recorded explainer ({st['artifact_url']}) belongs to a PR that's finished: {how}. If this branch "
            f"is a new PR, run prepare --new to start a new artifact, so the old PR's page keeps showing its own "
            f"code. Only if it really is the same PR, run prepare --same-pr.")
    elif st["diverged"] and not args.same_pr:
        raise PrxError(
            f"The recorded explainer ({st['artifact_url']}, commit {short(st['explained_sha'])}) isn't in this "
            f"branch's history. If this is the same PR after a rebase or force-push, run prepare --same-pr to keep "
            f"that URL. If it's a new PR reusing the branch name, run prepare --new to start a new artifact. "
            f"`gh pr list --head {repo.branch} --state all` shows which PRs used this branch.")
    head = repo.head()
    merge_base = git(["merge-base", base, "HEAD"], repo.cwd).strip()
    if merge_base == head:
        raise PrxError(f"No commits on {repo.branch} beyond {base}. Commit the change first.")
    files = numstat(repo, merge_base, head)
    review, excluded = classify(files, linguist_flags(repo, [f["path"] for f in files]))
    adds = sum(f["additions"] for f in files)
    dels = sum(f["deletions"] for f in files)
    st = explainer_state(repo, base=base)
    key = branch_key(repo.branch)
    run = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + os.urandom(2).hex()
    context = {
        "schema": 1,
        "run": run,
        "explainer_file": f"{key}.explainer.{run}.json",
        "branch": repo.branch,
        "base_ref": base,
        "merge_base": merge_base,
        "head_sha": head,
        "stats": {"files_changed": len(files), "additions": adds, "deletions": dels},
        "large": len(files) > LARGE_PR_FILES or adds + dels > LARGE_PR_LINES,
        "diff_command": f"git diff {merge_base} {head} -- <path>",
        "commits": git(["log", "--format=%h %s", "-n", "60", f"{merge_base}..{head}"], repo.cwd).splitlines(),
        "areas": areas(files),
        "review_files": review,
        "excluded_files": excluded,
    }
    paths = {k: str(repo.path(s)) for k, s in [
        ("context", "context.json"), ("intent", "intent.json"), ("html", "html"),
        ("reviewer_prompt", "reviewer-prompt.md")]}
    paths["explainer"] = str(explainer_path(repo, context))
    # A new prepare starts a new review. Earlier runs' explainers go, and this
    # run's file has a new name, so neither an old analysis nor a late reviewer
    # from an earlier run can be rendered under this commit's SHA badge.
    for old in repo.state_dir.glob(f"{key}.explainer*.json") if repo.state_dir.exists() else []:
        if re.fullmatch(re.escape(key) + r"\.explainer(?:\.[\w-]+)?\.json", old.name):
            old.unlink()
    write_text(repo.path("context.json"), json.dumps(context, indent=2, ensure_ascii=False))
    prompt = (SKILL_DIR / "reviewer-prompt.md").read_text(encoding="utf-8")
    for key, value in {"CONTEXT_PATH": paths["context"], "EXPLAINER_PATH": paths["explainer"],
                       "SCHEMA_PATH": str(SKILL_DIR / "schema.md"), "PRX": str(Path(__file__).resolve()),
                       "STATE_DIR": str(repo.state_dir)}.items():
        prompt = prompt.replace("{{" + key + "}}", value.replace("\\", "/"))
    write_text(repo.path("reviewer-prompt.md"), prompt)
    print(json.dumps({
        "mode": st["mode"], "artifact_url": st["artifact_url"], "branch": repo.branch,
        "base_ref": base, "head_sha": head, "stats": context["stats"], "large": context["large"],
        "review_files": len(review), "excluded_files": len(excluded), "paths": paths,
        "warnings": warnings,
    }, indent=2))
    return 0


# ---------------------------------------------------------------- validation

RISKS = ("low", "medium", "high")
CHANGE_TYPES = ("added", "modified", "removed", "renamed")
NODE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
COMPONENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
LINES_RE = re.compile(r"^(\d+)(?:\s*[-\u2013]\s*(\d+))?$")
GIT_FACT_KEYS = ("commit_sha", "base_ref", "stats")
INTENT_KEYS = {"title", "short_title", "ticket", "goal", "approach", "alternatives_rejected",
               "deliberately_not_done", "author_flagged_risks", "how_to_verify"}
EXPLAINER_KEYS = {"tldr", "overall_risk", "components", "diagram_before", "diagram_after",
                  "changed_node_ids", "removed_node_ids", "hotspots", "risk_heatmap", "intent_check",
                  "test_evidence", "reviewer_questions", "low_risk_collapsed", *GIT_FACT_KEYS}


class Report:
    def __init__(self):
        self.errors, self.warnings = [], []

    def err(self, where, msg):
        self.errors.append(f"{where}: {msg}")

    def warn(self, where, msg):
        self.warnings.append(f"{where}: {msg}")


def _obj(rep, value, where):
    if value is None:
        return {}
    if not isinstance(value, dict):
        rep.err(where, "must be an object")
        return {}
    return value


def _str(rep, obj, key, where, required=True):
    v = obj.get(key)
    if v is not None and not isinstance(v, str):
        rep.err(f"{where}.{key}", "must be a string")
        return ""
    v = (v or "").strip()  # before the emptiness check, so "   " counts as missing
    if not v and required:
        rep.err(f"{where}.{key}", "is required")
    return v


def _list(rep, obj, key, where, required=False):
    """The value as a list, or [] after reporting an error, so callers can always loop over it."""
    v = obj.get(key)
    if v is None:
        if required:
            rep.err(f"{where}.{key}", "is required (a list)")
        return []
    if not isinstance(v, list):
        rep.err(f"{where}.{key}", "must be a list")
        return []
    return v


def _str_list(rep, obj, key, where):
    v = obj.get(key)
    if v is None:
        return []
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        rep.err(f"{where}.{key}", "must be a list of strings")
        return []
    return [x.strip() for x in v if x.strip()]


def _unknown_keys(rep, obj, allowed, where):
    for k in obj:
        if k not in allowed:
            rep.warn(where, f"unknown key '{k}' ignored")


def validate_intent(raw, rep: Report) -> dict:
    o = _obj(rep, raw, "intent")
    _unknown_keys(rep, o, INTENT_KEYS, "intent")
    short_title = _str(rep, o, "short_title", "intent")
    if short_title and (len(short_title.split()) > 5 or len(short_title) > 40):
        rep.err("intent.short_title", "must be a 2-4 word name (it becomes the page title)")
    alts = []
    for i, a in enumerate(_list(rep, o, "alternatives_rejected", "intent")):
        a = _obj(rep, a, f"intent.alternatives_rejected[{i}]")
        opt = _str(rep, a, "option", f"intent.alternatives_rejected[{i}]")
        if opt:
            alts.append({"option": opt, "why_not": _str(rep, a, "why_not", f"intent.alternatives_rejected[{i}]", False)})
    return {
        "title": _str(rep, o, "title", "intent"),
        "short_title": short_title,
        "ticket": _str(rep, o, "ticket", "intent", False),
        "goal": _str(rep, o, "goal", "intent"),
        "approach": _str(rep, o, "approach", "intent"),
        "alternatives_rejected": alts,
        "deliberately_not_done": _str_list(rep, o, "deliberately_not_done", "intent"),
        "author_flagged_risks": _str_list(rep, o, "author_flagged_risks", "intent"),
        "how_to_verify": _str_list(rep, o, "how_to_verify", "intent"),
    }


def clean_diagram(text: str, where: str, rep: Report) -> str:
    """Strip directives the page must not honour; insist on a flowchart."""
    kept = []
    for line in text.replace("\r\n", "\n").split("\n"):
        s = line.strip()
        if s.startswith("%%{"):
            rep.warn(where, "removed a Mermaid init directive")
            continue
        no_click = re.sub(r"(^|;)\s*click\s[^;\n]*", r"\1", line)  # statements can be ;-separated
        if no_click != line:
            rep.warn(where, "removed a click directive (the page links nodes to components itself)")
            if not no_click.strip(" \t;"):
                continue
            line = no_click
        kept.append(line)
    first = next((ln.strip() for ln in kept if ln.strip() and not ln.strip().startswith("%%")), "")
    if not re.match(r"(?:flowchart|graph)\b", first):
        rep.err(where, "must be a Mermaid flowchart (start with 'flowchart LR' or 'flowchart TD')")
    joined = "\n".join(kept).strip()
    if re.search(r"javascript:|<(?!br\s*/?>)[A-Za-z/!]", joined, re.I):
        rep.err(where, "contains HTML-like tags or a script URL; use plain-text labels, "
                       "write < and > as #lt; and #gt; (<br> is allowed)")
    return joined


MERMAID_RESERVED = {"end", "graph", "flowchart", "subgraph", "direction", "style", "linkstyle",
                    "classdef", "class", "click", "call", "href", "default"}


def _node_ids(rep, o, key, diagram, where):
    ids = _str_list(rep, o, key, where)
    for nid in ids:
        if not NODE_ID_RE.match(nid):
            rep.err(f"{where}.{key}", f"'{nid}' must be a simple Mermaid id (letters, digits, _)")
        elif nid.lower() in MERMAID_RESERVED:
            rep.err(f"{where}.{key}", f"'{nid}' is a Mermaid keyword and breaks the diagram; rename the node")
        elif not re.search(rf"(?<![A-Za-z0-9_]){re.escape(nid)}(?![A-Za-z0-9_])", diagram):
            rep.err(f"{where}.{key}", f"'{nid}' does not appear in the diagram")
    return [n for n in ids if NODE_ID_RE.match(n)]


def validate_explainer(raw, ctx: dict, rep: Report) -> dict:
    o = _obj(rep, raw, "explainer")
    _unknown_keys(rep, o, EXPLAINER_KEYS, "explainer")
    for k in GIT_FACT_KEYS:
        if k in o:
            rep.warn(f"explainer.{k}", "ignored; the renderer takes this from git")
    changed_files = {f["path"] for f in ctx.get("review_files", []) + ctx.get("excluded_files", [])}

    tldr = o.get("tldr")
    tldr = [tldr] if isinstance(tldr, str) else tldr
    if not tldr or not isinstance(tldr, list) or not all(isinstance(t, str) for t in tldr):
        rep.err("explainer.tldr", "is required (a list of up to 3 short sentences)")
        tldr = []
    elif len(tldr) > MAX_TLDR:
        rep.err("explainer.tldr", f"has {len(tldr)} items; keep it to {MAX_TLDR}")

    risk = o.get("overall_risk")
    if risk not in RISKS:
        rep.err("explainer.overall_risk", f"must be one of {', '.join(RISKS)}")
        risk = "medium"

    after_raw = _str(rep, o, "diagram_after", "explainer")
    after = clean_diagram(after_raw, "explainer.diagram_after", rep) if after_raw else ""
    before_raw = _str(rep, o, "diagram_before", "explainer", False)
    before = clean_diagram(before_raw, "explainer.diagram_before", rep) if before_raw else ""
    changed = _node_ids(rep, o, "changed_node_ids", after, "explainer")
    removed = _node_ids(rep, o, "removed_node_ids", before, "explainer") if before else []
    if o.get("removed_node_ids") and not before:
        rep.err("explainer.removed_node_ids", "needs a diagram_before to point into")

    components, seen = [], set()
    for i, c in enumerate(_list(rep, o, "components", "explainer", required=True)):
        where = f"explainer.components[{i}]"
        c = _obj(rep, c, where)
        cid = _str(rep, c, "id", where)
        if cid and not COMPONENT_ID_RE.match(cid):
            rep.err(f"{where}.id", "use letters, digits, - and _ only")
        if cid in seen:
            rep.err(f"{where}.id", f"duplicate id '{cid}'")
        seen.add(cid)
        ctype = c.get("change_type")
        if ctype not in CHANGE_TYPES:
            rep.err(f"{where}.change_type", f"must be one of {', '.join(CHANGE_TYPES)}")
        node_ids = _str_list(rep, c, "node_ids", where)
        for nid in node_ids:
            if not re.search(rf"(?<![A-Za-z0-9_]){re.escape(nid)}(?![A-Za-z0-9_])", f"{after}\n{before}"):
                rep.warn(f"{where}.node_ids", f"'{nid}' is not in either diagram")
        components.append({
            "id": cid, "name": _str(rep, c, "name", where), "change_type": ctype,
            "summary": _str(rep, c, "summary", where), "files": _str_list(rep, c, "files", where),
            "node_ids": [n for n in node_ids if NODE_ID_RE.match(n)],
        })

    hotspots = []
    raw_hotspots = _list(rep, o, "hotspots", "explainer")
    if len(raw_hotspots) > MAX_HOTSPOTS:
        rep.err("explainer.hotspots", f"has {len(raw_hotspots)}; keep the top {MAX_HOTSPOTS} and move the rest to low_risk_collapsed")
    for i, h in enumerate(raw_hotspots):
        where = f"explainer.hotspots[{i}]"
        h = _obj(rep, h, where)
        hrisk = h.get("risk")
        if hrisk not in ("high", "medium"):
            rep.err(f"{where}.risk", "must be high or medium (low-risk items go in low_risk_collapsed)")
        file = _str(rep, h, "file", where)
        if file and changed_files and file not in changed_files:
            rep.warn(f"{where}.file", f"'{file}' is not in the changed files")
        lines = _str(rep, h, "lines", where, False)
        if lines and not LINES_RE.match(lines):
            rep.warn(f"{where}.lines", "use '40-78' or '40'; the link will not jump to a line")
        snippet = h.get("snippet") or ""
        if not isinstance(snippet, str):
            rep.err(f"{where}.snippet", "must be a string")
            snippet = ""
        snippet = snippet.replace("\r\n", "\n").strip("\n")
        if snippet.count("\n") + 1 > MAX_SNIPPET_LINES:
            rep.err(f"{where}.snippet", f"has {snippet.count(chr(10)) + 1} lines; keep it to {MAX_SNIPPET_LINES}")
        fmt = h.get("snippet_format", "code")
        if fmt not in ("code", "diff"):
            rep.err(f"{where}.snippet_format", "must be code or diff")
        rank = h.get("rank", i + 1)
        hotspots.append({
            "rank": rank if isinstance(rank, int) else i + 1, "file": file, "lines": lines,
            "risk": hrisk, "category": _str(rep, h, "category", where, False),
            "why_it_matters": _str(rep, h, "why_it_matters", where), "snippet": snippet,
            "snippet_format": fmt,
        })
    hotspots.sort(key=lambda h: h["rank"])

    ic = _obj(rep, o.get("intent_check"), "explainer.intent_check")
    matches = ic.get("matches_ticket")
    if matches is not None and not isinstance(matches, bool):
        rep.err("explainer.intent_check.matches_ticket", "must be true, false or null")
        matches = None
    te = _obj(rep, o.get("test_evidence"), "explainer.test_evidence")
    questions = _str_list(rep, o, "reviewer_questions", "explainer")
    if len(questions) > 5:
        rep.warn("explainer.reviewer_questions", "more than 5; the page shows them all, but fewer land better")
    return {
        "tldr": [t.strip() for t in tldr if t.strip()][:MAX_TLDR],
        "overall_risk": risk,
        "components": components,
        "diagram_before": before,
        "diagram_after": after,
        "changed_node_ids": changed,
        "removed_node_ids": removed,
        "hotspots": hotspots,
        "intent_check": {
            "matches_ticket": matches,
            "extras_not_requested": _str_list(rep, ic, "extras_not_requested", "explainer.intent_check"),
            "possibly_missing": _str_list(rep, ic, "possibly_missing", "explainer.intent_check"),
        },
        "test_evidence": {
            "tests_added": _str_list(rep, te, "tests_added", "explainer.test_evidence"),
            "weak_spots": _str_list(rep, te, "weak_spots", "explainer.test_evidence"),
        },
        "reviewer_questions": questions,
        "low_risk_collapsed": _str_list(rep, o, "low_risk_collapsed", "explainer"),
    }


# ------------------------------------------------------------- secret scan
# A backstop before code snippets leave the machine (claude.ai) and the TL;DR
# goes into the PR body (GitHub). It's pattern-based and errs towards flagging:
# redacting a false positive is cheap, leaking a real secret isn't.

# Names containing a credential word, so DB_PASSWORD, GITHUB_TOKEN and
# stripeApiKey count. token(?!i[sz]) keeps "tokenizer" out.
_SECRET_NAME = (r"[A-Za-z0-9_.-]*(?:password|passwd|passphrase|pwd|secret|credentials?|token(?!i[sz])"
                r"|api[_-]?key|access[_-]?key|private[_-]?key)[A-Za-z0-9_.-]*")


def _looks_random(val: str) -> bool:
    """Key material rather than an identifier: letters and digits, varied, not snake_case words."""
    return (bool(re.search(r"[A-Za-z]", val)) and bool(re.search(r"\d", val)) and len(set(val)) >= 8
            and not re.fullmatch(r"[A-Za-z]+(?:[_-][A-Za-z0-9]+)+", val))


SECRET_PATTERNS = [  # (what it looks like, pattern, extra check on the value)
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), None),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})"), None),
    ("Slack token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"), None),
    ("webhook URL", re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9]+/[A-Za-z0-9]+/[A-Za-z0-9]+"
                               r"|https://(?:canary\.|ptb\.)?discord(?:app)?\.com/api/webhooks/\d+/[\w-]+"), None),
    ("npm token", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b|(?i:_auth(?:Token)?)\s*=\s*(?P<val>[^\s'\"]{8,})"), None),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}"), None),
    ("Anthropic API key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}"), None),
    ("OpenAI-style key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{32,}"), None),
    ("Stripe live key", re.compile(r"\b[rs]k_live_[A-Za-z0-9]{20,}"), None),
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), None),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"), None),
    ("URL with password", re.compile(r"\b[a-z][a-z0-9+.\-]*://[^\s:/@]+:(?P<val>[^\s@/]{6,})@"), None),
    ("password in a connection string", re.compile(
        r"(?im)(?:^|;)\s*(?:password|pwd)\s*=\s*(?P<val>[^;'\"\s]{4,})\s*(?:;|$)"), None),
    ("bearer token", re.compile(
        r"(?i)\bbearer\s+(?P<val>(?=[A-Za-z0-9\-._~+/]*[\d._~+/-])[A-Za-z0-9\-._~+/]{16,}=*)"), None),
    ("basic auth credentials", re.compile(  # base64 has capitals or digits; "a basic understanding" doesn't
        r"\b(?i:basic)\s+(?P<val>(?=[A-Za-z0-9+/]*[A-Z\d+/=])[A-Za-z0-9+/]{8,}={0,2})(?![\w+/])"), None),
    ("credential assignment", re.compile(
        rf"(?i)\b{_SECRET_NAME}[\"']?\s*[:=]\s*[\"'](?P<val>[^\"'\s]{{6,}})[\"']"), None),
    ("passphrase", re.compile(  # the one credential that's normally several words
        r"(?i)\b[A-Za-z0-9_.-]*passphrase[\"']?\s*[:=]\s*[\"'](?P<val>[^\"'\n]{6,})[\"']"), None),
    # Unquoted .env / YAML / shell values. Must contain a digit, so references
    # like `password: settings.db_password` don't count.
    ("credential in config", re.compile(
        rf"(?im)^\s*(?:export\s+|-\s+)?[\"']?{_SECRET_NAME}[\"']?\s*[:=]\s*"
        r"(?P<val>(?=[A-Za-z0-9+/=_\-]*\d)[A-Za-z0-9+/=_\-]{8,})\s*(?:#.*)?$"), None),
    # ENCRYPTION_KEY, jwtSigningKey, client_key: "key" names are also used for
    # cache keys and sort keys, so the value has to look like key material.
    ("key-like value", re.compile(
        r"(?i)\b[A-Za-z0-9_.-]*key[\"']?\s*[:=]\s*[\"']?(?P<val>[A-Za-z0-9+/=_\-]{16,})"), _looks_random),
]
PLACEHOLDER_RE = re.compile(
    r"^(?:<[^>]*>|\*+|x+|\.{3}|\$\{[^}]*\}|\{\{[^}]*\}\}|\{[^}]*\}|%\(?[A-Za-z_]+\)?s?|\$[A-Za-z_]\w*"
    r"|redacted|changeme"
    # example-token-value, your_api_key_here, test-only: a prefix followed only by these words.
    # No digits: test1234 and dummy2024 are exactly what real throwaway passwords look like.
    r"|(?:your|example|dummy|test|fake|placeholder|sample|my)(?:[_-]?(?:api|access|auth|secret|private|key"
    r"|token|password|pass|passphrase|value|here|only|user|data|string))*"
    r"|string|password|secret|token|bearer|basic|none|null|undefined|required|optional"  # schema/type words
    r"|(?:/|\./|\.\./|~/)\S*|[\w.-]*[A-Za-z][\w-]*\.(?:json|ya?ml|toml|ini|cfg|conf|txt|env|pem|key|crt))$",
    re.I)


def iter_strings(obj, where):
    if isinstance(obj, str):
        yield where, obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from iter_strings(v, f"{where}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from iter_strings(v, f"{where}[{i}]")


def scan_secrets(obj, where, rep: Report):
    for path, text in iter_strings(obj, where):
        for name, rx, check in SECRET_PATTERNS:
            for m in rx.finditer(text):
                val = m.groupdict().get("val")
                if val and PLACEHOLDER_RE.match(val):
                    continue
                if check and not check(val or m.group(0)):
                    continue
                rep.err(path, f"looks like a {name}; remove it or replace the value with <redacted>")
                break


# ------------------------------------------------------------------- render

LANG_BY_EXT = {
    "py": "python", "js": "javascript", "mjs": "javascript", "cjs": "javascript", "jsx": "javascript",
    "ts": "typescript", "tsx": "typescript", "kt": "kotlin", "kts": "kotlin", "java": "java",
    "go": "go", "rs": "rust", "rb": "ruby", "php": "php", "cs": "csharp", "c": "c", "h": "c",
    "cc": "cpp", "cpp": "cpp", "hpp": "cpp", "swift": "swift", "m": "objectivec", "sql": "sql",
    "sh": "bash", "bash": "bash", "zsh": "bash", "yml": "yaml", "yaml": "yaml", "json": "json",
    "toml": "ini", "ini": "ini", "xml": "xml", "html": "xml", "vue": "xml", "svg": "xml",
    "css": "css", "scss": "scss", "less": "less", "md": "markdown", "graphql": "graphql",
    "gql": "graphql", "lua": "lua", "r": "r", "pl": "perl", "vb": "vbnet",
}
RISK_LABEL = {"low": "Low risk", "medium": "Medium risk", "high": "High risk"}
CHANGE_LABEL = {"added": "Added", "modified": "Modified", "removed": "Removed", "renamed": "Renamed"}
HIGHLIGHT = {  # literal colours: Mermaid classDef can't read CSS variables
    "prxChanged": "stroke:#f08c00,stroke-width:3px",
    "prxRemoved": "stroke:#e03131,stroke-width:2px,stroke-dasharray:5 4",
}


def e(value) -> str:
    return html.escape(str(value), quote=True)


def json_for_script(obj) -> str:
    return (json.dumps(obj, ensure_ascii=False)
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))


def with_highlight(diagram: str, ids, cls: str) -> str:
    if not ids:
        return diagram
    return f"{diagram}\n  classDef {cls} {HIGHLIGHT[cls]}\n  class {','.join(ids)} {cls}"


def code_lang(path: str, fmt: str):
    if fmt == "diff":
        return "diff"
    name = path.rsplit("/", 1)[-1].lower()
    if name in ("makefile", "gnumakefile"):
        return "makefile"
    return LANG_BY_EXT.get(name.rsplit(".", 1)[-1]) if "." in name else None


def github_base(repo: Repo):
    remote = git(["remote", "get-url", "origin"], repo.cwd, check=False).strip()
    m = re.match(r"^(?:https?://(?:[^@/]+@)?|ssh://git@|git@)([^/:]+)[:/]([^/]+/[^/]+?)(?:\.git)?/?$", remote)
    if not m or "github" not in m.group(1).lower():
        return None
    return f"https://{m.group(1)}/{m.group(2)}"


def blob_link(base, sha, path, lines=""):
    if not base or not path:
        return None
    url = f"{base}/blob/{sha}/{quote(path)}"
    m = LINES_RE.match(lines or "")
    if m:
        url += f"#L{m.group(1)}" + (f"-L{m.group(2)}" if m.group(2) else "")
    return url


def fmt_lines(lines: str) -> str:
    m = LINES_RE.match(lines or "")
    if not m:
        return e(lines)
    return f"L{m.group(1)}" + (f"\u2013{m.group(2)}" if m.group(2) else "")


def path_html(path, url, extra=""):
    inner = f'{e(path)}{extra}'
    if url:
        return f'<a class="path" href="{e(url)}" target="_blank" rel="noopener">{inner}</a>'
    return f'<span class="path">{inner}</span>'


def items(values, empty="None reported"):
    if not values:
        return f'<p class="none">{e(empty)}</p>'
    return "<ul>" + "".join(f"<li>{e(v)}</li>" for v in values) + "</ul>"


def section(sid, title, body, aside=""):
    return (f'<section id="{sid}" aria-labelledby="{sid}-h"><div class="section-head">'
            f'<h2 id="{sid}-h">{e(title)}</h2>{aside}</div>{body}</section>')


def diffstat_blocks(adds, dels):
    total = adds + dels
    green = round(5 * adds / total) if total else 0
    red = 5 - green if total else 0
    kinds = ["add"] * green + ["del"] * red + ["none"] * (5 - green - red)
    return "".join(f'<i class="b-{k}"></i>' for k in kinds)


def build_page(repo: Repo, ctx: dict, it: dict, ex: dict) -> str:
    head = ctx["head_sha"]
    gh = github_base(repo)
    stats = ctx["stats"]
    base_name = ctx["base_ref"].split("/", 1)[1] if ctx["base_ref"].startswith("origin/") else ctx["base_ref"]
    author = git(["config", "user.name"], repo.cwd, check=False).strip()
    rendered = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    risk = ex["overall_risk"]

    ticket = ""
    if it["ticket"]:
        t = it["ticket"]
        ticket = (f'<a class="ticket" href="{e(t)}" target="_blank" rel="noopener">{e(t)}</a>'
                  if re.match(r"^https?://", t) else f'<span class="ticket">{e(t)}</span>')
    mast = f"""<header class="mast">
  <p class="eyebrow">Pull request explainer</p>
  <h1>{e(it["title"])}</h1>
  <div class="facts">
    <span class="risk risk-{risk}">{RISK_LABEL[risk]}</span>
    <span class="sha" title="{e(head)}"><span class="k">commit</span> {e(head[:7])}</span>
    <span class="branch">{e(ctx["branch"])} <span aria-hidden="true">&rarr;</span><span class="sr"> into </span> {e(base_name)}</span>
    <span class="diffstat"><span class="add">+{stats["additions"]:,}</span> <span class="del">&minus;{stats["deletions"]:,}</span> <span class="n">{stats["files_changed"]:,} files</span> <span class="blocks" aria-hidden="true">{diffstat_blocks(stats["additions"], stats["deletions"])}</span></span>
    {ticket}
  </div>
  <p class="provenance">Sections marked <em>Independent read</em> come from a separate agent that saw only the diff and the ticket, not the session that wrote the code. The author&rsquo;s own account is labelled as a claim.</p>
</header>"""

    tldr = section("tldr", "TL;DR", '<ul class="tldr">' + "".join(f"<li>{e(t)}</li>" for t in ex["tldr"]) + "</ul>")

    node_map = {n: c["id"] for c in ex["components"] for n in c["node_ids"]}
    after_src = with_highlight(ex["diagram_after"], ex["changed_node_ids"], "prxChanged")
    if ex["diagram_before"]:
        before_src = with_highlight(ex["diagram_before"], ex["removed_node_ids"], "prxRemoved")
        tabs = ('<div class="seg" role="tablist" aria-label="Diagram version">'
                '<button type="button" role="tab" id="tab-before" aria-controls="panel-before" aria-selected="false" tabindex="-1">Before</button>'
                '<button type="button" role="tab" id="tab-after" aria-controls="panel-after" aria-selected="true">After</button></div>')
        panels = (f'<figure class="panel" id="panel-before" role="tabpanel" aria-labelledby="tab-before" aria-hidden="true"><pre class="mermaid">{e(before_src)}</pre></figure>'
                  f'<figure class="panel is-active" id="panel-after" role="tabpanel" aria-labelledby="tab-after"><pre class="mermaid">{e(after_src)}</pre></figure>')
    else:
        tabs = ""
        panels = f'<figure class="panel is-active" id="panel-after"><pre class="mermaid">{e(after_src)}</pre></figure>'
    legend = []
    if ex["changed_node_ids"]:
        legend.append('<span><i class="sw sw-changed"></i>Added or changed</span>')
    if ex["removed_node_ids"]:
        legend.append('<span><i class="sw sw-removed"></i>Removed (before view)</span>')
    if node_map:
        legend.append("<span>Select a box to jump to its component</span>")
    diagram = section("diagram", "Architecture", f'<div class="stage">{panels}</div>'
                      + (f'<p class="legend">{"".join(legend)}</p>' if legend else ""), tabs)

    cards = []
    for n, h in enumerate(ex["hotspots"], 1):
        lang = code_lang(h["file"], h["snippet_format"])
        code = ""
        if h["snippet"]:
            cls = f"language-{lang}" if lang else "nohighlight"
            code = f'<pre class="code"><code class="{cls}">{e(h["snippet"])}</code></pre>'
        where = f' <span class="lines">{fmt_lines(h["lines"])}</span>' if h["lines"] else ""
        cat = f'<span class="cat">{e(h["category"])}</span>' if h["category"] else ""
        cards.append(f"""<article class="hotspot" id="hotspot-{n}">
  <div class="hs-head"><span class="rank" aria-label="Hotspot {n}">{n}</span>
    <div class="hs-title">{path_html(h["file"], blob_link(gh, head, h["file"], h["lines"]), where)}
      <div class="tags"><span class="risk risk-{h["risk"]}">{RISK_LABEL[h["risk"]]}</span>{cat}</div></div></div>
  <p class="why">{e(h["why_it_matters"])}</p>
  {code}
</article>""")
    hotspots = section("hotspots", "Where to look first", "".join(cards) if cards
                       else '<p class="none">No high or medium risk hotspots found.</p>')

    comps = []
    for c in ex["components"]:
        files = "".join(f"<li>{path_html(f, blob_link(gh, head, f))}</li>" for f in c["files"])
        count = len(c["files"])
        comps.append(f"""<details class="component" id="component-{e(c["id"])}">
  <summary><span class="ctype ctype-{c["change_type"]}">{CHANGE_LABEL.get(c["change_type"], "")}</span><span class="cname">{e(c["name"])}</span><span class="ccount">{count} file{"" if count == 1 else "s"}</span></summary>
  <div class="cbody"><p>{e(c["summary"])}</p>{f'<ul class="files">{files}</ul>' if files else ""}</div>
</details>""")
    components = section("components", "Components changed", f'<div class="components">{"".join(comps)}</div>' if comps
                         else '<p class="none">No components listed.</p>')

    alts = "".join(f'<li><strong>{e(a["option"])}</strong>{": " + e(a["why_not"]) if a["why_not"] else ""}</li>'
                   for a in it["alternatives_rejected"])
    ic = ex["intent_check"]
    verdict = {True: ("yes", "Matches the ticket"), False: ("no", "Does not match the ticket"),
               None: ("unclear", "Couldn\u2019t tell from the ticket")}[ic["matches_ticket"]]
    intent = section("intent", "Intent check", f"""<div class="pair">
  <div class="claim"><h3>Author&rsquo;s intent <span class="tag">claim</span></h3>
    <dl>
      <dt>Goal</dt><dd>{e(it["goal"])}</dd>
      <dt>Approach</dt><dd>{e(it["approach"])}</dd>
      <dt>Rejected alternatives</dt><dd>{f"<ul>{alts}</ul>" if alts else '<p class="none">None given</p>'}</dd>
      <dt>Deliberately not done</dt><dd>{items(it["deliberately_not_done"], "None given")}</dd>
      <dt>Risks the author flagged</dt><dd>{items(it["author_flagged_risks"], "None given")}</dd>
      <dt>How to verify</dt><dd>{items(it["how_to_verify"], "None given")}</dd>
    </dl></div>
  <div class="independent"><h3>Independent read</h3>
    <p class="verdict verdict-{verdict[0]}">{e(verdict[1])}</p>
    <dl>
      <dt>Extras nobody asked for</dt><dd>{items(ic["extras_not_requested"], "None found")}</dd>
      <dt>Possibly missing</dt><dd>{items(ic["possibly_missing"], "None found")}</dd>
    </dl></div>
</div>""")

    te = ex["test_evidence"]
    tests = section("tests", "Test evidence", f"""<div class="pair plain">
  <div><h3>Tests added</h3>{items(te["tests_added"], "No tests added")}</div>
  <div><h3>Weak spots</h3>{items(te["weak_spots"], "None found")}</div>
</div>""")

    questions = section("questions", "Before you approve",
                        '<ul class="questions">' + "".join(f"<li>{e(q)}</li>" for q in ex["reviewer_questions"]) + "</ul>"
                        ) if ex["reviewer_questions"] else ""

    excluded = ctx.get("excluded_files", [])
    low = ""
    if ex["low_risk_collapsed"] or excluded:
        counts = {}
        for f in excluded:
            counts[f["reason"]] = counts.get(f["reason"], 0) + 1
        summary = ", ".join(f"{n} {r}" for r, n in sorted(counts.items(), key=lambda kv: -kv[1]))
        shown = excluded[:200]
        more = f"<li>and {len(excluded) - len(shown)} more</li>" if len(excluded) > len(shown) else ""
        skipped = (f'<details class="skipped"><summary>Not analysed: {e(summary)}</summary><ul class="files">'
                   + "".join(f'<li>{path_html(f["path"], None)} <span class="reason">{e(f["reason"])}</span></li>' for f in shown)
                   + more + "</ul></details>") if excluded else ""
        low = section("low-risk", "Low risk", (items(ex["low_risk_collapsed"]) if ex["low_risk_collapsed"] else "") + skipped)

    footer = (f'<footer class="foot"><span>Template v{TEMPLATE_VERSION}</span>'
              f'<span>Base {e(ctx["base_ref"])} at {e(ctx["merge_base"][:7])}</span>'
              f'<span>Head {e(head)}</span>'
              f'<span>Rendered {e(rendered)}{" for " + e(author) if author else ""}</span></footer>')
    node_json = f'<script type="application/json" id="prx-node-map">{json_for_script(node_map)}</script>'

    body = "\n".join(p for p in [mast, tldr, diagram, hotspots, components, intent, tests, questions, low, footer, node_json] if p)
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    values = {"TITLE": e(it["short_title"]), "BODY": body}
    return re.sub(r"\{\{([A-Z_]+)\}\}", lambda m: values.get(m.group(1), m.group(0)), template)


def standalone(page: str) -> str:
    """Wrap the fragment the way the artifact host does, plus Mermaid, for local preview."""
    return ('<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
            '<style>body{margin:0}img{max-width:100%}[hidden]{display:none!important}</style></head><body>\n'
            + page
            + f'\n<script src="{PREVIEW_MERMAID}"></script>\n<script>mermaid.initialize({{startOnLoad:true,'
            'securityLevel:"strict",theme:matchMedia("(prefers-color-scheme: dark)").matches?"dark":"default"});</script>'
            '\n</body></html>\n')


def _check(repo: Repo, only=None):
    ctx = load_json(repo.path("context.json"), "context.json (run `prx.py prepare` first)")
    rep = Report()
    it = ex = None
    if only in (None, "intent"):
        raw = load_json(repo.path("intent.json"), "intent.json")
        it = validate_intent(raw, rep)
        scan_secrets(raw, "intent", rep)
    if only in (None, "explainer"):
        raw = load_json(explainer_path(repo, ctx), "This prepare's explainer.json (run the reviewer)")
        ex = validate_explainer(raw, ctx, rep)
        scan_secrets(raw, "explainer", rep)
    return ctx, it, ex, rep


def _print_report(rep: Report):
    for w in rep.warnings:
        print(f"warning  {w}")
    for err in rep.errors:
        print(f"ERROR    {err}")
    if rep.errors:
        print(f"{len(rep.errors)} error(s). Fix them and run again.")


def cmd_validate(args):
    repo = Repo()
    _, _, _, rep = _check(repo, args.only)
    _print_report(rep)
    if not rep.errors:
        print("OK")
    return 1 if rep.errors else 0


def inputs_digest(repo: Repo, ctx: dict) -> str:
    """Fingerprint of the JSON a page was rendered from, so record can tell if it changed."""
    h = hashlib.sha256()
    for p in (repo.path("intent.json"), explainer_path(repo, ctx)):
        h.update(p.read_bytes())
    h.update(ctx.get("run", "").encode())
    return h.hexdigest()


def cmd_render(args):
    repo = Repo()
    ctx, it, ex, rep = _check(repo)
    head = repo.head()
    if ctx["head_sha"] != head:
        raise PrxError(f"HEAD moved from {short(ctx['head_sha'])} to {short(head)} since prepare. "
                       "Run prepare again and refresh explainer.json.")
    _print_report(rep)
    if rep.errors:
        return 1
    page = build_page(repo, ctx, it, ex)
    if args.standalone:
        out = Path(args.out) if args.out else repo.path("preview.html")
        write_text(out, standalone(page))
    else:
        out = Path(args.out) if args.out else repo.path("html")
        write_text(out, page)
        write_text(repo.path("rendered.json"), json.dumps(
            {"head_sha": head, "html": str(out), "inputs": inputs_digest(repo, ctx)}, indent=2))
    print(json.dumps({"html": str(out), "head_sha": head, "short_sha": head[:7],
                      "bytes": out.stat().st_size, "warnings": len(rep.warnings)}, indent=2))
    return 0


# ------------------------------------------------------------------- record

ARTIFACT_URL_RE = re.compile(r"^https://claude\.ai/(?:code/)?artifacts?/[A-Za-z0-9_-]+/?$")


def pr_body(url: str, ex: dict) -> str:
    lines = [f"> **Interactive explainer:** [open the review guide]({url}). Before/after diagram, "
             "top hotspots, risks and an independent intent check. Opens on claude.ai with an org account.",
             "", "**TL;DR**"]
    lines += [f"- {t}" for t in ex["tldr"]]
    return "\n".join(lines) + "\n"


def cmd_record(args):
    repo = Repo()
    url = args.url.strip()
    if not ARTIFACT_URL_RE.match(url):
        raise PrxError("Expected a claude.ai artifact URL like https://claude.ai/code/artifact/<id>")
    existing = read_text(repo.path("url"))
    if existing and existing != url and not args.replace:
        raise PrxError(f"Branch {repo.branch} already has an explainer at {existing}. Republish to that URL "
                       "instead of creating a second artifact (pass --replace only if the old one is gone).")
    rendered = load_json(repo.path("rendered.json"), "rendered.json (run `prx.py render` first)")
    head = repo.head()
    if rendered["head_sha"] != head:
        raise PrxError(f"The rendered page shows {short(rendered['head_sha'])} but HEAD is {short(head)}. "
                       "Run prepare and render again, then republish.")
    # Check before writing anything, so a bad file can't mark HEAD fresh or put a
    # secret in pr-body.md (which goes to GitHub, outside the artifact's org boundary).
    ctx, _, ex, rep = _check(repo)
    if rep.errors:
        _print_report(rep)
        raise PrxError("intent.json or explainer.json has errors. Fix them, render and republish.")
    if rendered.get("inputs") != inputs_digest(repo, ctx):
        raise PrxError("intent.json or explainer.json changed after the last render. Render and republish, then record.")
    write_text(repo.path("pr-body.md"), pr_body(url, ex))
    write_text(repo.path("url"), url + "\n")
    write_text(repo.path("sha"), head + "\n")
    print(json.dumps({"artifact_url": url, "head_sha": head, "pr_body": str(repo.path("pr-body.md"))}, indent=2))
    return 0


def cmd_status(args):
    print(json.dumps(explainer_state(Repo()), indent=2))
    return 0


def cmd_prune(args):
    """Remove state for branches that no longer exist locally or on origin."""
    repo = Repo()
    refs = git(["for-each-ref", "--format=%(refname:short)", "refs/heads", "refs/remotes/origin"], repo.cwd)
    live = {branch_key(r[len("origin/"):] if r.startswith("origin/") else r) for r in refs.split()}
    removed = []
    for p in sorted(repo.state_dir.glob("*")) if repo.state_dir.exists() else []:
        m = STATE_FILE_RE.match(p.name)
        if m and m.group("key") not in live:
            removed.append(p.name)
            if not args.dry_run:
                p.unlink()
    print(json.dumps({"removed" if not args.dry_run else "would_remove": removed}, indent=2))
    return 0


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    p = argparse.ArgumentParser(prog="prx.py", description="PR Explainer helper")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status", help="show mode, artifact URL and freshness for this branch")
    s.set_defaults(func=cmd_status)
    s = sub.add_parser("prepare", help="collect git facts and the file list")
    s.add_argument("--base", help="base branch (default: origin's default branch)")
    s.add_argument("--no-fetch", action="store_true", help="don't fetch the base branch first")
    which = s.add_mutually_exclusive_group()
    which.add_argument("--same-pr", action="store_true",
                       help="the branch was rebased or force-pushed: keep its artifact URL")
    which.add_argument("--new", action="store_true",
                       help="a new PR (e.g. a reused branch name): archive the old URL and start a new artifact")
    s.set_defaults(func=cmd_prepare)
    s = sub.add_parser("validate", help="check intent.json / explainer.json")
    s.add_argument("--only", choices=("intent", "explainer"))
    s.set_defaults(func=cmd_validate)
    s = sub.add_parser("render", help="write the explainer page")
    s.add_argument("--standalone", action="store_true", help="full HTML document with Mermaid, for local preview")
    s.add_argument("--out", help="output path (default: the state dir)")
    s.set_defaults(func=cmd_render)
    s = sub.add_parser("record", help="remember the published artifact URL")
    s.add_argument("url")
    s.add_argument("--replace", action="store_true", help="replace a different URL already on record")
    s.set_defaults(func=cmd_record)
    s = sub.add_parser("prune", help="remove state for branches that no longer exist")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=cmd_prune)
    args = p.parse_args(argv)
    try:
        return args.func(args)
    except PrxError as err:
        print(f"prx: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
