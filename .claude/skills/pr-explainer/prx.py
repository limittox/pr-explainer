#!/usr/bin/env python3
"""PR Explainer helper, used by the /pr-explainer skill and by the hooks.

Subcommands (run from anywhere inside the repo):
  status                 where this branch stands: mode, artifact URL, freshness
  prepare [--base REF] [--same-pr | --new]
                         collect git facts and the changed-file list into <key>.context.json
  validate [--only P]    check intent/explainer JSON against the contract (P = intent|explainer)
  render [--standalone]  validate, scan for secrets, write <key>.page.html (the PR's page),
                         index.html (the repo artifact's main page) and <key>.html
  record URL             remember the published artifact, the PR's link and the commit it shows
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
INDEX_TEMPLATE_PATH = SKILL_DIR / "index.html"
TEMPLATE_VERSION = "3"

MAX_HOTSPOTS = 3
MAX_SNIPPET_LINES = 25
MAX_TLDR = 3
MAX_QUIZ = 5
MAX_TOUR = 6
MAX_STEPS = 8
# Past these lengths text stops being scanned and starts being skipped: a warning asks for less.
LONG = {"tldr": 200, "headline": 90, "why": 420, "summary": 280, "item": 220, "setup": 280,
        "explain": 320, "tour": 180, "approach": 700}
LARGE_PR_FILES = 150
LARGE_PR_LINES = 4000

# A PR's page in the repo artifact is a file beside the artifact's main page. The viewer
# draws <pre class="mermaid"> only on the main page, so the PR page loads Mermaid itself.
# validate's diagram checks are this version's verdicts (tests/mermaid_cases.json and the
# DiagramCleaning tests); before changing the pin, re-record them with tests/mermaid_cases.html.
PAGE_MERMAID = "https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.min.js"
# A PR page's id is the #anchor in its link. Only a plain token (letters, digits, . _ ~ -)
# reaches the artifact's page from a link, and branch keys are made of those already.
PAGE_ID_RE = re.compile(r"^[A-Za-z0-9_~-][A-Za-z0-9._~-]{0,119}$")
# Repo-level files (the repo artifact's URL and main page) live in a folder of their own:
# a flat name like index.html is also what a branch called "index" would use.
REPO_DIR = "repo-artifact"


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


def default_base_name(repo: Repo, branch=None):
    """The branch gh pr create targets without --base: the branch's gh-merge-base setting
    (which gh reads first), else origin's default branch, or None if this clone doesn't
    record it (no refs/remotes/origin/HEAD). gh asks GitHub, so guessing main here could
    approve a page rendered against the wrong base."""
    if branch:
        configured = git(["config", f"branch.{branch}.gh-merge-base"], repo.cwd, check=False).strip()
        if configured:
            return configured
    ref = origin_head(repo)
    return branch_name(ref) if ref else None


def branch_name(ref: str) -> str:
    """origin/release/1.0 -> release/1.0; a local ref stays as it is."""
    return ref[len("origin/"):] if ref.startswith("origin/") else ref


def published_base(repo: Repo, branch: str):
    """The base the recorded page was rendered against. Older state without
    published.json falls back to the last prepare's base."""
    for suffix in ("published.json", "context.json"):
        p = repo.path(suffix, branch)
        if p.exists():
            base = json.loads(p.read_text(encoding="utf-8-sig")).get("base_ref")
            if base:
                return base
    return None


def repo_file(repo: Repo, name: str) -> Path:
    return repo.state_dir / REPO_DIR / name


def repo_record(repo: Repo) -> dict:
    p = repo_file(repo, "repo.json")
    return json.loads(p.read_text(encoding="utf-8-sig")) if p.exists() else {}


def repo_artifact(repo: Repo):
    """The artifact that new PR pages go to, once the first one has been recorded."""
    return (repo_record(repo).get("artifact_url") or "").rstrip("/") or None  # older records may end in /


def new_page_id(repo: Repo, key: str, head: str) -> str:
    """branch key + the commit the page is first made for. If an archived link of this branch
    already used that id (a new PR started at the same commit), count up instead of reusing it."""
    # A long key is cut, but keeps a hash of the whole key: two long branch names that
    # share their first 100 characters must not share a page.
    short_key = key if len(key) <= 100 else f"{key[:93]}-{hashlib.sha1(key.encode('utf-8')).hexdigest()[:6]}"
    base = f"{short_key}-{head[:7]}"
    if not PAGE_ID_RE.match(base):
        base = f"pr-{head[:12]}"
    used = {split_link(read_text(p))[1] for p in repo.state_dir.glob(f"{key}.archived-*.url")} \
        if repo.state_dir.exists() else set()
    page_id, n = base, 1
    while page_id in used:
        n += 1
        page_id = f"{base}-{n}"
    return page_id


def split_link(link):
    """https://claude.ai/artifact/X#page -> (https://claude.ai/artifact/X, page). A PR recorded
    before the repo artifact has an artifact of its own, and no page."""
    base, _, page = (link or "").partition("#")
    return base, page or None


def explainer_state(repo: Repo, branch=None, base=None) -> dict:
    branch = branch or repo.branch
    url = read_text(repo.path("url", branch))
    sha = read_text(repo.path("sha", branch))
    head = repo.head(branch)
    fresh = bool(url) and sha == head
    stale = bool(url and sha) and not fresh
    published = published_base(repo, branch) if url else None
    if base is None and stale:
        base = published
    return {
        "branch": branch,
        "mode": "update" if url else "create",
        "artifact_url": url or None,
        "explained_sha": sha or None,
        "head_sha": head,
        "fresh": fresh,
        "base_ref": base,
        "published_base": published,
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
# gh pr create's flags that take a value, by shorthand and long name.
GH_SHORT_VALUE = {"a": "assignee", "B": "base", "b": "body", "F": "body-file", "H": "head", "l": "label",
                  "m": "milestone", "p": "project", "r": "reviewer", "t": "title", "T": "template", "R": "repo"}
GH_LONG_VALUE = set(GH_SHORT_VALUE.values()) | {"recover"}
GIT_VALUE_OPTIONS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--config-env"}
TITLE_RE = re.compile(r"""(?:--title|(?<![\w-])-t)(?:=|\s+)(?:"(?:[^"\\]|\\.)*"|'[^']*'|[^\s;&|)]+)""")


def squash(cmd: str) -> str:
    """cmd with line continuations joined and quote and escape characters removed: shells
    drop these while building words, so `gh p''r create` runs `gh pr create`. (The hooks'
    fast paths use the same rule.)"""
    return re.sub(r"[\"'`\\]", "", re.sub(r"[`\\]\r?\n", "", cmd))


def crude_pr_create(cmd: str) -> bool:
    """The fallback when the parser can't follow a command: fail closed on anything that
    looks like PR creation, spelled plainly or split by quotes and escapes."""
    cmd = cmd or ""
    return bool(CRUDE_PR_CREATE.search(cmd) or CRUDE_PR_CREATE.search(squash(cmd)))


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
        return crude_pr_create(cmd)
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
        return bool(CRUDE_PUSH.search(cmd or "") or CRUDE_PUSH.search(squash(cmd or "")))
    return any(_sends_commits(c) for c in cmds)


def gh_create_options(args) -> dict:
    """gh pr create's options as gh itself reads them (pflag rules): a short flag
    can carry its value attached (-Bdevelop, -b=x) or end a cluster of boolean
    flags (-dB develop), and a repeated flag keeps its last value. Returns
    {long name: value} for the flags that take a value."""
    opts = {}
    i = 0
    while i < len(args):
        a, i = args[i], i + 1
        if a == "--":
            break
        if a.startswith("--"):
            name, eq, value = a[2:].partition("=")
            if name in GH_LONG_VALUE:
                if not eq:
                    value, i = (args[i] if i < len(args) else ""), i + 1
                opts[name] = value
        elif a.startswith("-") and len(a) > 1:
            for j in range(1, len(a)):
                name = GH_SHORT_VALUE.get(a[j])
                if not name:
                    if a[j + 1:j + 2] == "=":
                        break  # -d=false: the rest is this boolean flag's value
                    continue  # a boolean flag in the cluster
                rest = a[j + 1:]
                if rest.startswith("="):
                    value = rest[1:]
                elif rest:
                    value = rest
                else:
                    value, i = (args[i] if i < len(args) else ""), i + 1
                opts[name] = value
                break  # the rest of the cluster was the value
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
        if crude_pr_create(cmd):
            return "couldn't parse this command (check its quotes), so it couldn't check the PR explainer."
        return None
    if not any(gh_pr_create_args(c) is not None for c in cmds):
        return None
    # Follow directory changes before each gh pr create, so `cd OTHER && gh pr
    # create` is checked against OTHER's explainer, not this repo's. Only cds in
    # the main shell are followed: one in a child shell (a Bash subshell, $(...)
    # or pipeline stage, bash -c, a background job) never reaches a gh pr create
    # in the main shell.
    changes = [c for c in cmds if _changes_dir(c)]
    nested_change = any(c.nested for c in changes)
    here, stack, repos = cwd, [], {}
    for c in cmds:
        if _changes_dir(c):
            if c.nested:
                continue
            if c.early:  # a cd in a PowerShell $(...): listed before the commands it runs after
                here = None
            else:
                here = _next_dir(c, cmdparse.program(c.argv[0]), here, stack)
            continue
        args = gh_pr_create_args(c)
        if args is None:
            continue
        if cmdparse.program(c.argv[0]) != "gh":  # found by the backstop, inside a program the parser doesn't model
            return (f"gh pr create runs inside another program here ({c.argv[0]}), so the gate can't tell which "
                    "repository or arguments it gets. Run gh pr create as its own command.")
        # A gh pr create from a $(...) is listed before the commands it runs after,
        # and a nested one may share a child shell with a nested cd: can't tell.
        where = None if (c.early and changes) or (c.nested and nested_change) else here
        for step in c.chdir:  # env -C DIR, sudo -D DIR, wsl --cd DIR, pwsh -WorkingDirectory DIR
            where = _resolve_dir(step, where)
        if where is None:
            return CANT_TELL_DIR
        if where not in repos:
            repos[where] = Repo(where)
        problem = _create_problem(repos[where], c, gh_create_options(args), cmd, where)
        if problem:
            return problem
    return None


CD_COMMANDS = {"cd", "chdir", "set-location", "sl", "pushd", "push-location"}
POP_COMMANDS = {"popd", "pop-location"}
CANT_TELL_DIR = ("this command changes directory in a way the gate can't follow before gh pr create (a variable, "
                 "`cd -`, popd, a directory that doesn't exist here, or a cd in the same subshell, `$(...)` or child "
                 "shell as gh pr create), so it can't tell which repository's explainer to check. Run gh pr create "
                 "from the repository, as its own command.")


def _changes_dir(c) -> bool:
    return bool(c.argv) and cmdparse.program(c.argv[0]) in CD_COMMANDS | POP_COMMANDS


def _next_dir(c, name: str, here, stack: list):
    """The working directory after a cd / pushd / popd / Set-Location, or None if it can't be known."""
    if name in POP_COMMANDS:
        return stack.pop() if stack else None
    if name in ("pushd", "push-location"):
        stack.append(here)
    args, target, i = c.argv[1:], None, 0
    while i < len(args):
        a = args[i]
        option, colon, _ = a.lower().partition(":")  # -Path:DIR
        if option in ("-path", "-literalpath", "-lp"):
            target = a[len(option) + 1:] if colon else (args[i + 1] if i + 1 < len(args) else None)
            break
        if a.startswith("-") and a != "-":
            i += 1
            continue
        target = a
        break
    if target is None:
        return str(Path.home())  # a bare cd goes home
    return _resolve_dir(target, here)


def _resolve_dir(target: str, here):
    """target as a directory, relative to here, or None if it can't be known without running anything."""
    if here is None:
        return None
    if re.fullmatch(r"\$\(\s*git rev-parse --show-toplevel\s*\)", target):
        try:  # the common `cd "$(git rev-parse --show-toplevel)"`
            return Repo(here).root
        except PrxError:
            return None
    if not target or target == "-" or re.search(r"[$`%]", target):
        return None  # cd -, variables and substitutions: can't tell
    path = _local_path(target, here)
    return str(path) if path.is_dir() else None  # e.g. a wsl --cd path that only exists in Linux


def has_link(text: str, link: str) -> bool:
    """The link appears whole: not as the start of a longer page id (#x-1234567-2) or
    artifact id. A sentence's full stop after it is fine; page ids never end in one."""
    return re.search(re.escape(link) + r"(?![A-Za-z0-9_~-]|\.[A-Za-z0-9_~-])", text) is not None


def _create_problem(repo: Repo, c, opts: dict, cmd: str, cwd: str):
    branch = opts["head"].split(":")[-1] if opts.get("head") else repo.branch
    if not branch:
        return "HEAD is detached. Check out the PR branch, run /pr-explainer, then create the PR."
    st = explainer_state(repo, branch)
    if not st["artifact_url"]:
        return (f"No PR explainer for branch '{branch}' yet. Run the /pr-explainer skill first, "
                "then re-run gh pr create with the explainer link in the PR body.")
    if st["merged"]:
        return (f"The recorded explainer for '{branch}' shows commit {short(st['explained_sha'])}, which is already "
                f"in {st['base_ref']}: that PR was merged, so this is a new one. Run /pr-explainer; prepare will "
                "ask you to start a new page (--new) so the merged PR's page keeps showing its own code.")
    if st["diverged"]:
        return (f"The recorded explainer for '{branch}' shows commit {short(st['explained_sha'])}, which isn't "
                "in this branch's history (a rebase, or the branch name reused for a new PR). Run /pr-explainer; "
                "prepare will ask whether to keep the existing artifact URL.")
    if not st["fresh"]:
        return (f"The PR explainer shows commit {short(st['explained_sha'])} but '{branch}' is at "
                f"{short(st['head_sha'])}. Run /pr-explainer in update mode (republish to the same artifact "
                f"{split_link(st['artifact_url'])[0]}, unless prepare reports that PR as finished: then start a "
                "new page with --new), then re-run gh pr create.")
    # The page shows the diff against the base it was rendered for, so the PR
    # must target that base: gh's --base, or the repo's default branch without it.
    published = st["published_base"]
    target = opts.get("base") or default_base_name(repo, branch)
    if published and not target and git(["remote"], repo.cwd, check=False).strip():
        return (f"Without --base, the gate can't tell which branch gh pr create targets: this clone doesn't record "
                f"origin's default branch (`git remote set-head origin --auto` records it). So it can't check that "
                f"the explainer page, rendered against '{published}', shows the right diff. "
                f"Pass --base {branch_name(published)} to gh pr create.")
    if published and target and target not in (published, branch_name(published)):
        how = "this PR targets" if opts.get("base") else "without --base, gh targets the default branch"
        return (f"The explainer page was rendered against '{published}', but {how} '{target}', so it shows the "
                f"wrong diff. Run /pr-explainer with `prepare --base {target}` and republish, then re-run gh pr create.")
    url = st["artifact_url"]
    # gh uses the last --body and the last --body-file; check what it will actually send.
    sources, unreadable = [], False
    if "body" in opts:
        sources.append(opts["body"])
    if "body-file" in opts:
        name = opts["body-file"]
        if name == "-":  # body on stdin: a heredoc we can read, or a pipe we can't
            if c.stdin is None:
                unreadable = True
            else:
                sources.append(c.stdin)
        else:
            try:
                sources.append(_local_path(name, cwd).read_text(encoding="utf-8-sig", errors="replace"))
            except OSError:
                unreadable = True
    if sources and not unreadable and all(has_link(s, url) for s in sources):
        return None
    # A body held in a variable or piped in can't be read here, so accept the
    # link anywhere in the command except the title.
    if (unreadable or "$" in opts.get("body", "")) and has_link(TITLE_RE.sub("", cmd), url):
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


def origin_head(repo: Repo) -> str:
    """origin's default branch as this clone records it (origin/main), or ''."""
    return git(["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"], repo.cwd, check=False).strip()


def resolve_base(repo: Repo, base, fetch: bool, ask_origin: bool = True):
    warnings = []
    has_origin = "origin" in git(["remote"], repo.cwd, check=False).split()
    if fetch and ask_origin and has_origin and not origin_head(repo):
        # Ask origin for its default branch before choosing one, so a clone that never
        # recorded it doesn't fall back to main when the default is something else.
        # The gate reads it too, for a gh pr create run without --base. Skipped when
        # the user named the base: the answer wouldn't change anything here.
        try:
            git(["remote", "set-head", "origin", "--auto"], repo.cwd, check=False, timeout=30)
        except subprocess.TimeoutExpired:
            pass
    if base:
        candidates = [f"origin/{base}", base] if not base.startswith("origin/") else [base]
    else:
        head_ref = origin_head(repo)
        candidates = [head_ref] if head_ref else []
        candidates += ["origin/main", "origin/master", "main", "master"]
    for ref in candidates:
        if git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], repo.cwd, check=False).strip():
            break
    else:
        raise PrxError("Couldn't find the base branch. Pass --base <ref>.")
    if not base and has_origin and ref != origin_head(repo):
        warnings.append(f"This clone doesn't record origin's default branch, so {ref} is a guess. If the PR "
                        "should target another branch, run prepare again with --base <branch>.")
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


ARCHIVED_SUFFIXES = ("url", "sha", "published.json", "intent.json", "html", "page.html", "rendered.json", "pr-body.md")
STATE_FILE_RE = re.compile(
    r"^(?P<key>.+?)\.(?:url|sha|published\.json|context\.json|intent\.json|page\.html|html|preview\.html|rendered\.json"
    r"|reviewer-prompt\.md"
    r"|pr-body\.md|explainer(?:\.[\w-]+)?\.json|archived-\d{8}T\d{6}\..+)$")


def archive_state(repo: Repo) -> str:
    """Move a branch's artifact URL and related files aside, so the next publish starts a new page."""
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
    # An update keeps the base the page was published against: the open PR targets
    # it, even if origin's default branch has been recorded or changed since.
    wanted = args.base
    kept = not wanted and not args.new and read_text(repo.path("url", repo.branch))
    if kept:
        wanted = published_base(repo, repo.branch)
    # Fetch the base first, so "is the explained commit already merged?" is current.
    base, warnings = resolve_base(repo, wanted, fetch=not args.no_fetch, ask_origin=not args.base)
    default = origin_head(repo)
    if kept and default and base != default:
        # The published base may itself have been a guess made before origin's default was known.
        warnings.append(
            f"Kept {base}, the base this branch's page was published against, but origin's default branch is "
            f"{default}. If the PR should target {branch_name(default)}, run prepare --base {branch_name(default)}, "
            f"republish, and retarget the open PR with `gh pr edit --base {branch_name(default)}`.")
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
            f"is a new PR, run prepare --new to start a new page, so the old PR's page keeps showing its own "
            f"code. Only if it really is the same PR, run prepare --same-pr.")
    elif st["diverged"] and not args.same_pr:
        raise PrxError(
            f"The recorded explainer ({st['artifact_url']}, commit {short(st['explained_sha'])}) isn't in this "
            f"branch's history. If this is the same PR after a rebase or force-push, run prepare --same-pr to keep "
            f"that URL. If it's a new PR reusing the branch name, run prepare --new to start a new page. "
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
    # Where this PR's page goes. A branch recorded before the repo artifact keeps its own
    # artifact. Every other PR gets a page named by its #anchor, kept across updates, in
    # the artifact its link already points at, or for a new PR the repo's current one. A
    # new page is named after the commit it was first made for, so a branch name reused
    # for a later PR never overwrites the earlier PR's page.
    recorded = st["artifact_url"]
    home, page_id = split_link(recorded)
    legacy = bool(recorded) and page_id is None
    if not recorded:
        page_id = new_page_id(repo, key, head)
        home = repo_artifact(repo)
    if legacy:
        publish = {"artifact_url": recorded, "page": str(repo.path("html")), "files": {}, "link": recorded}
    else:
        publish = {"artifact_url": home, "page": str(repo_file(repo, "index.html")),
                   "files": {f"prs/{page_id}.html": str(repo.path("page.html"))},
                   "link": f"{home}#{page_id}" if home else None}
    run = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + os.urandom(2).hex()
    context = {
        "schema": 1,
        "run": run,
        "page_id": None if legacy else page_id,
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
        ("context", "context.json"), ("intent", "intent.json"), ("html", "html"), ("page", "page.html"),
        ("reviewer_prompt", "reviewer-prompt.md")]}
    paths["index"] = str(repo_file(repo, "index.html"))
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
        "publish": publish, "warnings": warnings,
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
                  "test_evidence", "reviewer_questions", "low_risk_collapsed", "quiz", "tour", *GIT_FACT_KEYS}


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


def _long(rep, text, kind, where, hint="shorten it"):
    if len(text) > LONG[kind]:
        rep.warn(where, f"is {len(text)} characters; over {LONG[kind]} it gets skimmed, so {hint}")


def _long_items(rep, values, where):
    for i, v in enumerate(values):
        _long(rep, v, "item", f"{where}[{i}]", "keep each item to one sentence")
    return values


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
    # The approach reads best as a few steps; one string still works for a small change.
    if isinstance(o.get("approach"), list):
        approach = _str_list(rep, o, "approach", "intent")
        if not approach:
            rep.err("intent.approach", "is required (a string, or a list of steps)")
        elif len(approach) > MAX_STEPS:
            rep.warn("intent.approach", f"has {len(approach)} steps; {MAX_STEPS} or fewer read better")
        for i, step in enumerate(approach):
            _long(rep, step, "summary", f"intent.approach[{i}]", "keep each step to a sentence or two")
    else:
        approach = _str(rep, o, "approach", "intent")
        _long(rep, approach, "approach", "intent.approach", "split it into a list of short steps")
    goal = _str(rep, o, "goal", "intent")
    _long(rep, goal, "summary", "intent.goal", "say it in a sentence or two")
    for i, a in enumerate(alts):
        _long(rep, a["why_not"], "item", f"intent.alternatives_rejected[{i}].why_not", "keep it to one sentence")
    return {
        "title": _str(rep, o, "title", "intent"),
        "short_title": short_title,
        "ticket": _str(rep, o, "ticket", "intent", False),
        "goal": goal,
        "approach": approach,
        "alternatives_rejected": alts,
        "deliberately_not_done": _long_items(rep, _str_list(rep, o, "deliberately_not_done", "intent"),
                                             "intent.deliberately_not_done"),
        "author_flagged_risks": _long_items(rep, _str_list(rep, o, "author_flagged_risks", "intent"),
                                            "intent.author_flagged_risks"),
        "how_to_verify": _long_items(rep, _str_list(rep, o, "how_to_verify", "intent"), "intent.how_to_verify"),
    }


def clean_diagram(text: str, where: str, rep: Report) -> str:
    """Strip directives the page must not honour; insist on a flowchart."""
    # Mermaid finds %%{...}%% directives anywhere in the source, not only at the
    # start of a line; they can span lines, and the closing }%% is optional.
    # Repeat until none is left: removing one can join the leftovers into another.
    text = text.replace("\r\n", "\n")
    no_directives = text
    while True:
        stripped = re.sub(r"%%\{.*?\}%%", "", no_directives, flags=re.S)
        if stripped == no_directives:
            break
        no_directives = stripped
    if no_directives != text:
        rep.warn(where, "removed a Mermaid init directive")
        text = no_directives
    kept = []
    for line in text.split("\n"):
        if "%%{" in line:  # an unclosed directive: drop it and the rest of its line
            rep.warn(where, "removed a Mermaid init directive")
            line = line[:line.index("%%{")]
            if not line.strip():
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
    line = trailing_comment(joined)
    if line:
        rep.err(where, f"has a %% comment after a statement ('{line}'); Mermaid only reads comments on a line "
                       f"of their own")
    for label in unquoted_bad_labels(joined):
        rep.err(where, f"the label '{label}' has ( ) [ ] {{ }} or a quote, which Mermaid won't parse unless the "
                       f"label is quoted: A[\"...\"], B{{\"...\"}} or -->|\"...\"| (write a quote inside as #quot;)")
    for text, form, _ in diagram_labels(joined):
        if form == "unclosed":
            rep.err(where, f"the box '{text}' is never closed, which Mermaid won't parse")
    for problem in bad_edge_text(joined) + arrow_problems(joined):
        rep.err(where, problem)
    for label, kind, fix in markdown_labels(joined):
        label = label.replace("\n", "\\n")
        if kind:
            shows = "nothing (a link definition)" if kind == "definition" else f"\"Unsupported markdown: {kind}\""
            rep.err(where, f"the label '{label}' draws as {shows} (Mermaid reads labels as Markdown); {fix}")
        else:
            rep.warn(where, f"Mermaid draws the label '{label}' in bold or italics and drops the marks; {fix}")
    for group in subgraph_cycles(joined):
        rep.err(where, f"the subgraph '{group}' holds a node of its own id, which Mermaid can't lay out "
                       f"(\"would create a cycle\"); give the group or the box another id")
    return joined


def statement_skeletons(diagram: str) -> list:
    """Each statement line as (line, skeleton, spans): the skeleton is the line without its quoted
    text, |edge text|, box labels and -- edge text -->, which leaves the ids and arrows, and spans
    are those labels' matches (label_spans). Comments and the lines whose value runs to the end of
    the line (style, accTitle, a subgraph line without a [title]...), which Mermaid reads as text,
    are left out."""
    lines = diagram.split("\n")
    # Quoted text goes first, over lines too, keeping its line breaks so each line stays in place
    unquoted = re.sub(r'"[^"]*"', lambda m: "\n" * m.group().count("\n"), diagram).split("\n")
    out = []
    for line, rest in zip(lines, unquoted):
        if re.match(r"\s*(?:%%|(?:style|classDef|linkStyle|direction|accTitle|accDescr)\b|subgraph\b[^\[]*$)", line):
            continue
        rest = re.sub(r"^(\s*subgraph\s+[A-Za-z0-9_-]+\s*)\[[^\]\n]*\]", r"\1", rest)
        kept, pos, spans = [], 0, []
        for m, _, start, end in label_spans(rest):
            kept.append(rest[pos:start])
            pos = end
            spans.append(m)
        skeleton = "".join(kept) + rest[pos:]
        for _ in range(3):                    # brackets not on a box id, nested too: A [x], [(..)]
            skeleton = re.sub(r"\[[^\[\]\n]*\]|\([^()\n]*\)|\{[^{}\n]*\}", "", skeleton)
        out.append((line, skeleton, spans))
    return out


def trailing_comment(diagram: str):
    """The first line with a %% comment after a statement, which breaks the diagram: Mermaid reads
    %% as a comment only on a line of its own. A %% in a label or edge text is text (A[90%% done])."""
    return next((line.strip() for line, skeleton, _ in statement_skeletons(diagram) if "%%" in skeleton), None)


def arrow_problems(diagram: str) -> list:
    """Arrows Mermaid misreads, as messages: edge text with no head of its own line after it on the
    line (-- a ==> B doesn't parse), an edge with both -- text --> and |text|, and a --o or --x
    head glued to a word, which Mermaid draws as an arrowhead on a stray box (Client--overrides-->Store
    gets a box 'verrides'), or to another arrow (A--x-->B doesn't parse)."""
    found = []
    for line, skeleton, spans in statement_skeletons(diagram):
        unended = re.search(r"(?<![-=.<])<?(--|==|-\.)(?![-=.>xo])", skeleton)
        if unended:
            found.append(f"'{line.strip()}' starts edge text with {unended.group(1)} but doesn't end it with "
                         f"{EDGE_HEADS[unended.group(1)]} on the same line, which Mermaid won't parse")
        if any(a.group("dash") is not None and b.group("pipe") is not None
               and b.start() < a.end() + len(a.group("dclose")) for a, b in zip(spans, spans[1:])):
            found.append(f"'{line.strip()}' gives one edge both -- text --> and |text|, which Mermaid won't "
                         f"parse; keep one")
        dotted = re.search(r"(?<![-.])\.-([A-Za-z0-9_]+)", skeleton)
        if dotted:
            found.append(f"in '{line.strip()}', Mermaid ends the dotted edge's text at .- and reads "
                         f"'{dotted.group(1)}' as a box: put the text in pipes (-.->|...|)")
        glued = re.search(r"(?<![-=.<])(?:--|==)[ox](?:([A-Za-z0-9_]+)|(?=[-=.]))", skeleton)
        if glued:
            what = f"'{glued.group(1)}' as a box" if glued.group(1) else "the arrow after it as another arrow"
            found.append(f"in '{line.strip()}', Mermaid reads {glued.group()[:3]} as an arrowhead and {what}: "
                         f"for edge text use pipes (-->|...|), and for the arrowhead put a space after it")
    return found


# A node's shape: its opening delimiter and the ones that can close it (the longest openers first).
NODE_SHAPES = [("(((", (")))",)), ("((", ("))",)), ("([", ("])",)), ("[(", (")]",)), ("[[", ("]]",)), ("{{", ("}}",)),
               ("[/", ("/]", "\\]")), ("[\\", ("\\]", "/]")), ("[", ("]",)), ("(", (")",)), ("{", ("}",)),
               (">", ("]",))]


# A -- edge text --> B (or A <-- text --> B): an arrow's opening half on its own (not part of
# ---, and not --o or --x, which are heads), then the text up to a head of the same line, as
# Mermaid ends it: -- text at -->, --o, --x or ---; == text at ==>, ==o, ==x or ===; -. text
# at .-> or .-. So == or a > in -- text is text (-- status==ok -->, -- n>1 -->).
DASHED_EDGE_TEXT = re.compile(r"(?<![-=.<])(?P<dopen><?(?:(?P<dd>--)|(?P<de>==)|-\.))(?![-=.>xo])(?P<dash>[^\n]*?)"
                              r"(?=(?P<dclose>(?(dd)(?:-->|--[ox]|---)|(?(de)(?:==>|==[ox]|===)|\.->?))))")
EDGE_HEADS = {"--": "-->, --o, --x or ---", "==": "==>, ==o, ==x or ===", "-.": ".-> or .-"}
LABEL_TOKEN = re.compile(r"[A-Za-z0-9_](?P<open>" + "|".join(re.escape(o) for o, _ in NODE_SHAPES) + ")"
                         r"|(?:[-=.]>?|(?<=--|==)[ox])[ \t]*\|(?P<pipe>[^|\n]*)\||" + DASHED_EDGE_TEXT.pattern)


def label_spans(body: str):
    """Each label left to right, as Mermaid reads a line, as (match, text, start, end), where
    body[start:end] is what to cut to leave the ids and arrows: a box's label from its opener to
    its close (so --base or == in it is text, and a <br> isn't a shape; it can run over lines, as
    Mermaid allows), |edge text| after an arrow (--o|..| too), or -- edge text --> up to its head
    (so the > of -- n>1 --> isn't a flag shape, and the head stays). A box never closed has text
    None, and its span runs to the end of its line."""
    pos = 0
    while (m := LABEL_TOKEN.search(body, pos)):
        pos = m.end()
        if m.group("open"):
            rest = body[m.end():]
            ends = [(i, c) for c in dict(NODE_SHAPES)[m.group("open")] if (i := rest.find(c)) >= 0]
            if ends:
                end, close = min(ends)
                pos = m.end() + end + len(close)
                yield m, rest[:end], m.start() + 1, pos
            else:
                line_end = body.find("\n", m.end())
                pos = line_end if line_end >= 0 else len(body)
                yield m, None, m.start() + 1, pos
        elif m.group("pipe") is not None:
            yield m, m.group("pipe"), m.start("pipe") - 1, m.end()
        else:
            yield m, m.group("dash"), m.start(), m.end()


def diagram_labels(diagram: str) -> list:
    """Every label in a flowchart as (text, form, arrow): node labels of any shape, |edge text|,
    subgraph titles and -- edge text -->. form is "quoted" for a label quoted as a whole (unwrapped
    here), "bare" for an unquoted one between delimiters, "text" for -- edge text -->, which
    Mermaid reads as plain text up to the arrow, and "unclosed" for a box never closed (its line
    from the id on). arrow is (opening half, head) for -- edge text --> (quoted or not), else
    None. Comments and accTitle/accDescr are left out."""
    body = re.sub(r"^[ \t]*%%[^\n]*", "", diagram, flags=re.M)  # Mermaid's comments are whole lines
    body = re.sub(r"accDescr\s*\{[^}]*\}", "", body)
    body = re.sub(r"^\s*acc(?:Title|Descr)\s*:[^\n]*", "", body, flags=re.M)
    # A label quoted as a whole (A["..."], {"..."}, |"..."|) is held aside, so what it holds can't
    # be read as a shape; a quote inside an unquoted label (B[it's "here"]) stays where it is
    held = []

    def hold(m):
        held.append(m.group(1))
        return f"\0{len(held) - 1}\0"

    def label(text, form="bare"):
        whole = re.fullmatch(r"\s*\0(\d+)\0\s*", text)
        if whole:
            return held[int(whole.group(1))], "quoted", None
        return re.sub(r"\0(\d+)\0", lambda m: f'"{held[int(m.group(1))]}"', text).strip(), form, None

    body = re.sub(r'(?<=[\[({|>/\\])"([^"]*)"(?=[\])}|/\\])', hold, body)  # a quoted label can run over lines
    title = r"^(\s*subgraph\s+[A-Za-z0-9_-]+\s*)\[([^\]\n]*)\]"
    labels = [label(t) for _, t in re.findall(title, body, re.M)]
    body = re.sub(title, r"\1", body, flags=re.M)
    # Then left to right, as Mermaid reads a line (label_spans)
    for m, text, _, end in label_spans(body):
        if text is None:
            labels.append(label(body[m.start():end], "unclosed"))
        elif m.group("open") or m.group("pipe") is not None:
            labels.append(label(text))
        else:
            quoted = re.fullmatch(r'\s*"([^"]*)"\s*', text)
            arrow = (m.group("dopen"), m.group("dclose"))
            labels.append((quoted.group(1), "quoted", arrow) if quoted else (text.strip(), "text", arrow))
    return labels


def unquoted_bad_labels(diagram: str) -> list:
    """Labels Mermaid 11 won't parse: ( ) [ ] { } or a quote inside an unquoted label of any node
    shape, in |edge text| or in a subgraph title. A diagram that doesn't draw takes the tour and
    the box links with it."""
    special = re.compile(r'[()\[\]{}"|]')
    return [text for text, form, _ in diagram_labels(diagram) if form == "bare" and special.search(text)]


def bad_edge_text(diagram: str) -> list:
    """Problems with -- edge text --> Mermaid won't parse, as messages: unquoted text holding its
    own opening half (-- a --base -->: Mermaid ends the text at the second --) or a quote, and a
    two-way arrow without a > at its head (<-- a --o)."""
    found = []
    for text, form, arrow in diagram_labels(diagram):
        if not arrow:
            continue
        opener, head = arrow
        line = opener.lstrip("<")
        if form == "text" and (line in text or '"' in text):
            found.append(f"the edge text '{text}' has {line} or a quote in it, which Mermaid won't read as "
                         f"text; quote it ({opener} \"...\" {head}) or put it in pipes (-->|...|)")
        if opener.startswith("<") and not head.endswith(">"):
            want = {"--": "-->", "==": "==>", "-.": ".->"}[line]
            found.append(f"the two-way edge '{opener} {text} {head}' needs a > at its head, which Mermaid "
                         f"won't parse otherwise; end it as {want}")
    return found


# Mermaid 11 reads every label as Markdown, quoted or not, and draws one it can't render as
# "Unsupported markdown: list" (or heading, link...). Its #code; escapes are read as text.
MARKDOWN_BLOCKS = [
    ("hr", re.compile(r"^\s*([-*_])(?:\s*\1){2,}\s*$"), "write the first - * or _ as #45;, #42; or #95;"),
    ("list", re.compile(r"^\s*(?:\d{1,9}[.)]|[-+*])(?:\s|$)"),
     "write the . or ) after a leading number as #46; or #41;, and a leading - + or * as #45;, #43; or #42;"),
    ("heading", re.compile(r"^\s*#{1,6}(?:\s|$)"), "write a leading # as #35;"),
    ("blockquote", re.compile(r"^\s*>"), "write a leading > as #gt;"),
    ("definition", re.compile(r"^\s{0,3}\[[^\]\n]+\]:"), "write the [ as #91;"),
]
MARKDOWN_INLINE = [
    ("codespan", re.compile(r"`"), "write each ` as #96;"),
    ("del", re.compile(r"(~~?)(?=[^\s~])[^~]*?[^\s~]\1(?!~)"), "write each ~ as #126;"),
    ("image", re.compile(r"!\[[^\]\n]*\]\([^)\n]*\)"), "write the [ as #91;"),
    ("link", re.compile(r"\[[^\]\n]*\]\([^)\n]*\)|\b(?:https?|ftp)://|\bwww\.|[\w.+-]+@[\w-]+(?:\.[\w-]+)+", re.I),
     "write the [ of [text](url) as #91;, the : of a URL as #58;, the . after www as #46; and the @ of an "
     "email as #64;"),
]


def markdown_kind(text: str):
    """What Mermaid draws instead of a label it reads as Markdown it can't render, as (kind, fix),
    or None ("definition" draws nothing). Each line of a label is a Markdown line, and under a
    line of text the rules change as they do for a paragraph: a line indented 4 or more carries
    the paragraph on, a list starts only with an item at 1 or a bullet with text ("Steps\\n2. two"
    and "Steps\\n*" are text), a line of = or - makes the text above it a heading, a row of
    --|-- makes it a table, and no link definition starts."""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        under_text = i > 0 and lines[i - 1].strip() != ""
        if under_text and re.match(r"(?: {4}|\t)", line):
            pass                                                     # the paragraph carries on
        elif i > 0 and not under_text and re.match(r"(?: {4}|\t)\s*\S", line):
            return "code", "remove the indent after the blank line"
        elif under_text and re.fullmatch(r"\s*(?:=+|-+)\s*", line):
            return "heading", "write the first = or - of the line under the text as #61; or #45;"
        elif under_text and "|" in lines[i - 1] and re.fullmatch(r"\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)+\|?\s*", line):
            return "table", "write the | as #124;"
        else:
            for kind, pattern, fix in MARKDOWN_BLOCKS:
                if pattern.search(line) and not (under_text and (
                        kind == "definition" or
                        kind == "list" and not re.match(r"\s*(?:[-+*]|1[.)])\s+\S", line))):
                    return kind, fix
        for kind, pattern, fix in MARKDOWN_INLINE:
            if pattern.search(line):
                return kind, fix
        if i < len(lines) - 1 and re.search(r"\S {2,}$", line):    # two spaces before a line break
            return "br", "remove the spaces at the end of the line"
    return None
# Emphasis draws, but loses its marks: __init__ shows as a bold "init", x*y*z as an italic y
MARKDOWN_EMPHASIS = re.compile(r"(?<!\w)__(?=\S)(?:(?!__).)+?(?<=\S)__(?!\w)|\*\*(?=\S)(?:(?!\*\*).)+?(?<=\S)\*\*"
                               r"|(?<!\w)_(?=[^\s_])[^_]*?(?<=[^\s_])_(?!\w)|(?<!\*)\*(?=[^\s*])[^*]*?(?<=[^\s*])\*(?!\*)")


def markdown_labels(diagram: str) -> list:
    """Labels Mermaid draws as Markdown, as (label, kind, fix): kind is what the box shows instead
    ("Unsupported markdown: kind"), or None for emphasis, which draws without its marks. A quoted
    "`...`" label is Mermaid's markdown string, which asks for Markdown, and is left alone."""
    found = []
    for text, form, _ in diagram_labels(diagram):
        if form == "unclosed" or form == "quoted" and re.fullmatch(r"`[^`]+`", text):
            continue
        hit = markdown_kind(text)
        if hit:
            found.append((text, *hit))
        elif MARKDOWN_EMPHASIS.search(text):
            found.append((text, None, "to show them, write each * or _ as #42; or #95;"))
    return found


MERMAID_RESERVED = {"end", "graph", "flowchart", "subgraph", "direction", "style", "linkstyle",
                    "classdef", "class", "click", "call", "href", "default"}


# Statement keywords, case-sensitive as Mermaid reads them: "end" closes a subgraph, "End" is a box.
FLOWCHART_DIRECTIVES = {"flowchart", "graph", "classDef", "class", "style", "linkStyle", "click", "direction", "end",
                        "accTitle", "accDescr"}


def statement_words(diagram: str) -> list:
    """Each statement's words in order, with comments, labels, edge text and arrowheads taken out,
    so 'API' from SearchAPI[Search API] doesn't pass for a node."""
    text = re.sub(r"^[ \t]*%%[^\n]*", "", diagram, flags=re.M)              # comments (whole lines only)
    text = re.sub(r":::[A-Za-z0-9_-]+", "", text)                           # A:::className
    text = re.sub(r'"[^"]*"', "", text)                                    # "quoted labels", over lines too
    text = re.sub(r"@\{[^}]*\}", "", text)                                 # A@{ shape: cyl }, Mermaid 11
    text = re.sub(r"accDescr\s*\{[^}]*\}", "", text)                       # accDescr { a block over lines }
    # A <-- edge text --> B: a two-way arrow's text, up to its closing head
    text = re.sub(r"<(?:--|==|-\.)(?![->=.])(?:(?!-->|==>|\.->)[^\n])*?(?:-->|==>|\.->)", " --> ", text)  # not <--> itself
    # Labels and edge text go before the flag shape, whose > would otherwise start at the > of a
    # <br> or of |n>1| or -- n>1 --> and swallow the rest of the line up to the next ]
    for _ in range(3):                                                     # nested shapes: [(..)], ((..)), {{..}}
        text = re.sub(r"\[[^\[\]\n]*\]|\([^()\n]*\)|\{[^{}\n]*\}", "", text)
    text = re.sub(r"([>\-=.ox])[ \t]*\|[^|\n]*\|", r"\1", text)            # -->|edge text|, right after an arrow
    # A -- edge text --> B and A--text-->B, up to a head of its own line (see DASHED_EDGE_TEXT)
    # (an o or x straight after the dashes is an arrowhead, as Mermaid's docs say: A---oB is a circle
    # edge to B, so edge text starting with o or x needs a space first; -. maybe .- closes a dotted link)
    text = DASHED_EDGE_TEXT.sub("", text)
    text = re.sub(r"(?<=[A-Za-z0-9_])>[^\]\n]*\]", "", text)               # A>flag shape]
    text = re.sub(r"(?<![A-Za-z0-9_])[ox](?=--|==|-\.)|(?<=--)[ox]|(?<===)[ox]", "", text)  # o--o, --x, ---oB heads
    statements = []
    for statement in re.split(r"[;\n]", text):                            # graph TD; A-->B is one line
        # search-api is one id to Mermaid, not two: a hyphenated word is taken whole and then left
        # out, since only simple ids (NODE_ID_RE) can be named, so neither half passes for a node
        words = [w for w in re.findall(r"[A-Za-z][A-Za-z0-9_]*(?:-[A-Za-z0-9_]+)*", statement) if "-" not in w]
        if words:
            statements.append(words)
    return statements


def diagram_nodes(diagram: str, groups: bool = False) -> set:
    """The node ids a flowchart declares or links, leaving out words from labels and edge text.
    Subgraph ids are included only with groups=True: Mermaid can colour a group, but the page
    can't light or link one."""
    ids, subgraphs = set(), set()
    for words in statement_words(diagram):
        if words[0] == "subgraph":
            subgraphs.update(words[1:2])
        elif words[0] not in FLOWCHART_DIRECTIVES:
            ids.update(w for w in words if w.lower() not in MERMAID_RESERVED)
    return (ids | subgraphs) if groups else ids - subgraphs


def subgraph_cycles(diagram: str) -> list:
    """Subgraphs holding a node of their own id, directly or in a nested group (subgraph API ...
    API --> B ... end). Mermaid reads the diagram but can't lay it out: "would create a cycle"."""
    inside, bad = [], []
    for words in statement_words(diagram):
        if words[0] == "subgraph":
            inside.append(words[1] if len(words) > 1 else "")
        elif words[0] == "end":
            inside = inside[:-1]
        elif words[0] not in FLOWCHART_DIRECTIVES:
            bad += [w for w in words if w in inside and w not in bad]
    return bad


def _node_ids(rep, o, key, diagram, where, groups=True, boxes="a tour step lights boxes"):
    """Node ids for highlighting (groups=True: a subgraph can be coloured), or for what only boxes
    can do (light up in the tour, open a component); `boxes` says which, for the error. `diagram`
    may be a list, each read on its own: a group in the before diagram can be a box after."""
    ids = _str_list(rep, o, key, where)
    diagrams = diagram if isinstance(diagram, list) else [diagram]
    declared = set().union(*(diagram_nodes(d, groups) for d in diagrams))
    for nid in ids:
        if not NODE_ID_RE.match(nid):
            rep.err(f"{where}.{key}", f"'{nid}' must be a simple Mermaid id (letters, digits, _)")
        elif nid.lower() in MERMAID_RESERVED:
            rep.err(f"{where}.{key}", f"'{nid}' is a Mermaid keyword and breaks the diagram; rename the node")
        elif nid not in declared:
            group = not groups and any(nid in diagram_nodes(d, True) for d in diagrams)
            rep.err(f"{where}.{key}", f"'{nid}' is a subgraph; {boxes}, so name the boxes inside it"
                    if group else f"'{nid}' does not appear in the diagram as a node id (a word in a label doesn't count)")
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
    for i, t in enumerate(tldr):
        _long(rep, t.strip(), "tldr", f"explainer.tldr[{i}]", "keep it to one short sentence")

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
        # A box opens its component, in either diagram; a label word or a subgraph opens nothing
        node_ids = _node_ids(rep, c, "node_ids", [after, before], where, groups=False,
                             boxes="selecting a box opens its component")
        summary = _str(rep, c, "summary", where)
        _long(rep, summary, "summary", f"{where}.summary", "keep it to one or two sentences")
        components.append({
            "id": cid, "name": _str(rep, c, "name", where), "change_type": ctype,
            "summary": summary, "files": _str_list(rep, c, "files", where),
            "node_ids": node_ids,
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
        headline = _str(rep, h, "headline", where, False)
        if not headline and not (h.get("headline") is not None and not isinstance(h.get("headline"), str)):
            rep.err(f"{where}.headline", "is required (new in template v3): one line saying what could break. An "
                    "explainer.json written before v3 needs a fresh reviewer run")
        _long(rep, headline, "headline", f"{where}.headline", "say what could break in a few words")
        why = _str(rep, h, "why_it_matters", where)
        _long(rep, why, "why", f"{where}.why_it_matters", "keep it to two or three sentences")
        focus = h.get("focus_lines") or []
        snippet_lines = snippet.count("\n") + 1 if snippet else 0
        if not isinstance(focus, list) or not all(isinstance(n, int) and not isinstance(n, bool) for n in focus):
            rep.err(f"{where}.focus_lines", "must be a list of line numbers in the snippet (1 is its first line)")
            focus = []
        elif any(not 1 <= n <= snippet_lines for n in focus):
            rep.err(f"{where}.focus_lines", f"line numbers must be between 1 and {snippet_lines}, the snippet's lines")
            focus = []
        hotspots.append({
            "rank": rank if isinstance(rank, int) else i + 1, "file": file, "lines": lines,
            "risk": hrisk, "category": _str(rep, h, "category", where, False), "headline": headline,
            "why_it_matters": why, "snippet": snippet, "snippet_format": fmt, "focus_lines": sorted(set(focus)),
        })
    hotspots.sort(key=lambda h: h["rank"])
    refs = {f"hotspot-{n}" for n in range(1, len(hotspots) + 1)} | {c["id"] for c in components}

    quiz = []
    raw_quiz = _list(rep, o, "quiz", "explainer")
    if len(raw_quiz) > MAX_QUIZ:
        rep.err("explainer.quiz", f"has {len(raw_quiz)} scenarios; keep the best {MAX_QUIZ}")
    for i, q in enumerate(raw_quiz):
        where = f"explainer.quiz[{i}]"
        q = _obj(rep, q, where)
        options = _str_list(rep, q, "options", where)
        raw_options = q.get("options") if isinstance(q.get("options"), list) else []
        if len(options) != len(raw_options):
            # answer indexes the list as written: dropping a blank would hand the key to its neighbour
            rep.err(f"{where}.options", "every answer must be non-empty text")
        if not 2 <= len(options) <= 4:
            rep.err(f"{where}.options", "needs 2 to 4 answers")
        answer = q.get("answer")
        if not isinstance(answer, int) or isinstance(answer, bool) or not 0 <= answer < len(options):
            rep.err(f"{where}.answer", f"must be the index of the right option (0 to {max(len(options) - 1, 0)})")
            answer = 0
        setup, explain = _str(rep, q, "setup", where), _str(rep, q, "explain", where)
        _long(rep, setup, "setup", f"{where}.setup", "set the scene in two sentences")
        _long(rep, explain, "explain", f"{where}.explain", "explain it in two sentences")
        for j, opt in enumerate(options):
            _long(rep, opt, "headline", f"{where}.options[{j}]", "keep answers short")
        # Every answer is the reviewer's reading, so it must point at the code that decides it.
        ref = _str(rep, q, "ref", where)
        if ref and ref not in refs:
            rep.err(f"{where}.ref", f"'{ref}' must be hotspot-1..{len(hotspots)} or a component id: the code that "
                                    "shows the answer")
            ref = ""
        quiz.append({"setup": setup, "question": _str(rep, q, "question", where), "options": options,
                     "answer": answer, "explain": explain, "ref": ref})
    if len(quiz) >= 3 and len({q["answer"] for q in quiz}) == 1:
        rep.warn("explainer.quiz", "every right answer is in the same position; vary it so it can't be guessed")

    tour = []
    raw_tour = _list(rep, o, "tour", "explainer")
    if len(raw_tour) > MAX_TOUR:
        rep.err("explainer.tour", f"has {len(raw_tour)} steps; keep it to {MAX_TOUR}")
    for i, s in enumerate(raw_tour):
        where = f"explainer.tour[{i}]"
        s = _obj(rep, s, where)
        nodes = _node_ids(rep, s, "nodes", after, where, groups=False)
        if not nodes:
            rep.err(f"{where}.nodes", "needs at least one node id from diagram_after")
        text = _str(rep, s, "text", where)
        _long(rep, text, "tour", f"{where}.text", "one sentence per step")
        tour.append({"nodes": nodes, "text": text})
    if len(tour) == 1:
        rep.warn("explainer.tour", "a tour needs at least 2 steps; it isn't shown")

    ic = _obj(rep, o.get("intent_check"), "explainer.intent_check")
    matches = ic.get("matches_ticket")
    if matches is not None and not isinstance(matches, bool):
        rep.err("explainer.intent_check.matches_ticket", "must be true, false or null")
        matches = None
    te = _obj(rep, o.get("test_evidence"), "explainer.test_evidence")
    questions = _str_list(rep, o, "reviewer_questions", "explainer")
    if len(questions) > 5:
        rep.warn("explainer.reviewer_questions", "more than 5; the page shows them all, but fewer land better")
    # A repeated question or scenario would share one progress tick, so the review could never finish
    if len(set(questions)) != len(questions):
        rep.err("explainer.reviewer_questions", "has the same question twice")
    scenes = [(q["setup"], q["question"]) for q in quiz]
    if len(set(scenes)) != len(scenes):
        rep.err("explainer.quiz", "has the same scenario twice")
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
            "extras_not_requested": _long_items(rep, _str_list(rep, ic, "extras_not_requested", "explainer.intent_check"),
                                                "explainer.intent_check.extras_not_requested"),
            "possibly_missing": _long_items(rep, _str_list(rep, ic, "possibly_missing", "explainer.intent_check"),
                                            "explainer.intent_check.possibly_missing"),
        },
        "test_evidence": {
            "tests_added": _long_items(rep, _str_list(rep, te, "tests_added", "explainer.test_evidence"),
                                       "explainer.test_evidence.tests_added"),
            "weak_spots": _long_items(rep, _str_list(rep, te, "weak_spots", "explainer.test_evidence"),
                                      "explainer.test_evidence.weak_spots"),
        },
        "reviewer_questions": _long_items(rep, questions, "explainer.reviewer_questions"),
        "low_risk_collapsed": _long_items(rep, _str_list(rep, o, "low_risk_collapsed", "explainer"),
                                          "explainer.low_risk_collapsed"),
        "quiz": quiz,
        "tour": tour if len(tour) >= 2 else [],
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


# A command flag in escaped text: -ea, --base. Not a hyphen inside a word (set-head).
FLAG_RE = re.compile(r"(?<![\w&#;-])(--?[A-Za-z][\w-]*)")


def nowrap_flags(escaped: str) -> str:
    """Keep command flags whole when a title wraps; browsers may break a line after the hyphen."""
    return FLAG_RE.sub(r'<span class="nw">\1</span>', escaped)


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


def section(sid, title, body, aside="", lede=""):
    lede = f'<p class="lede">{lede}</p>' if lede else ""
    return (f'<section id="{sid}" aria-labelledby="{sid}-h"><div class="section-head">'
            f'<h2 id="{sid}-h">{e(title)}</h2>{aside}</div>{lede}{body}</section>')


def diffstat_blocks(adds, dels):
    total = adds + dels
    green = round(5 * adds / total) if total else 0
    red = 5 - green if total else 0
    kinds = ["add"] * green + ["del"] * red + ["none"] * (5 - green - red)
    return "".join(f'<i class="b-{k}"></i>' for k in kinds)


def folded(title, values, open_=False, empty="None given"):
    """A list behind its title and count, so long lists don't wall up the page."""
    if not values:
        return f'<p class="none-line"><span>{e(title)}</span> {e(empty)}</p>'
    return (f'<details class="fold"{" open" if open_ else ""}><summary>{e(title)} <span class="n">{len(values)}</span>'
            f'</summary><ul>{"".join(f"<li>{e(v)}</li>" for v in values)}</ul></details>')


MAP_ROWS = 12


def change_map(ctx, hotspots, gh, head, gone=frozenset()):
    """Changed files as bars sized by churn, with a marker on each file that has a hotspot."""
    marks = {}
    for n, h in enumerate(hotspots, 1):
        marks.setdefault(h["file"], []).append((n, h["risk"]))
    # Excluded files (lockfiles, generated) stay off the map unless a hotspot is on one: a risky
    # dependency bump is exactly where a marker matters.
    files = ctx.get("review_files", []) + [f for f in ctx.get("excluded_files", []) if f["path"] in marks]
    if not files:
        return ""
    # The biggest files, plus every file with a hotspot however small: the map points at risk, not just size.
    shown = [f for i, f in enumerate(files) if i < MAP_ROWS or f["path"] in marks]
    rest = [f for f in files if f not in shown]
    # Bars share the reviewed files' scale: a lockfile with a hotspot on it would otherwise shrink
    # every source file to a sliver. A bigger excluded file fills its bar and fades out at the end.
    widest = max(f["additions"] + f["deletions"] for f in [f for f in shown if not f.get("reason")] or shown) or 1
    rows = []
    for f in shown:
        path = f["path"]
        folder, _, name = path.rpartition("/")
        a, d = f["additions"], f["deletions"]
        fit = min(1, widest / ((a + d) or 1))
        badges = "".join(f'<a class="hb hb-{risk}" href="#hotspot-{n}" title="Hotspot {n}">{n}</a>'
                         for n, risk in marks.get(path, []))
        label = (f'<span class="dir">{e(folder)}/</span>' if folder else "") + f'<b>{e(name)}</b>' + \
            (f' <span class="reason">{e(f["reason"])}</span>' if f.get("reason") else "") + \
            (' <span class="reason">deleted</span>' if path in gone else "")
        # A deleted file has nothing at head: link it as it was before the change
        url = blob_link(gh, ctx["merge_base"] if path in gone else head, path)
        label = f'<a href="{e(url)}" target="_blank" rel="noopener">{label}</a>' if url else label
        rows.append(f'<li class="crow{" hot" if badges else ""}"><span class="cpath" title="{e(path)}">{label}</span>'
                    f'<span class="cbar{" over" if fit < 1 else ""}" aria-hidden="true">'
                    f'<i class="a" style="width:{100 * a * fit / widest:.1f}%"></i>'
                    f'<i class="d" style="width:{100 * d * fit / widest:.1f}%"></i></span>'
                    f'<span class="cnum"><span class="add">+{a:,}</span> <span class="del">&minus;{d:,}</span></span>'
                    f'<span class="cmarks">{badges}</span></li>')
    more = ""
    if rest:
        lines = sum(f["additions"] + f["deletions"] for f in rest)
        more = f'<p class="cmore">and {len(rest)} more file{"s" if len(rest) != 1 else ""}, {lines:,} lines in all</p>'
    lede = ("The biggest changed files, and every file with a hotspot" if rest else "Every changed file, biggest first") + \
        (". Numbered markers are the hotspots below." if marks else ".")
    return section("map", "Where the change is", f'<ol class="cmap">{"".join(rows)}</ol>{more}', lede=lede)


def track_id(kind: str, *content) -> str:
    """A progress id for a hotspot (h), scenario (s) or question (q), from its content, so the same
    item on a later page keeps its tick or pick and a changed one gets a new id; and for a file's
    change (c), which marks a new hotspot on code the reader already looked at."""
    return f"{kind}-{hashlib.sha1(json.dumps(content).encode('utf-8')).hexdigest()[:10]}"


def game(quiz, change=()):
    """Predict it: scenario cards from the independent read. Pick an answer, see why. `change`
    (the PR's whole change) goes into each scenario's progress id."""
    if not quiz:
        return ""
    cards = []
    for i, q in enumerate(quiz):
        opts = "".join(f'<button type="button" class="opt" data-i="{j}"><span class="key">{"ABCD"[j]}</span>'
                       f'<span class="otext">{e(o)}<span class="who"></span></span></button>'
                       for j, o in enumerate(q["options"]))
        ref = ""
        if q["ref"]:
            target = q["ref"] if q["ref"].startswith("hotspot-") else f"component-{q['ref']}"
            label = f"See hotspot {q['ref'][len('hotspot-'):]}" if q["ref"].startswith("hotspot-") else "See the component"
            ref = f' <a class="qref" href="#{e(target)}" data-jump="{e(target)}">{label} &rarr;</a>'
        qid = track_id("s", q["setup"], q["question"], q["options"], q["answer"], list(change))
        cards.append(f"""<article class="qcard" data-answer="{q["answer"]}" data-qid="{qid}" aria-label="Scenario {i + 1} of {len(quiz)}">
  <p class="qnum">Scenario {i + 1} <span>of {len(quiz)}</span></p>
  <p class="setup">{e(q["setup"])}</p>
  <p class="question">{e(q["question"])}</p>
  <div class="opts" role="group" aria-label="Answers">{opts}</div>
  <div class="reveal" hidden><p class="rtitle"></p><p class="explain">{e(q["explain"])}{ref}</p></div>
</article>""")
    dots = "".join(f'<button type="button" class="dot" data-i="{i}" aria-label="Scenario {i + 1}"></button>'
                   for i in range(len(quiz)))
    body = (f'<div class="game" id="game-deck">{"".join(cards)}'
            '<div class="game-end" hidden><p class="big" id="game-result"></p><p class="small" id="game-note"></p>'
            '<button type="button" class="btn" id="game-again">Play again</button></div>'
            f'<div class="game-foot"><div class="dots" aria-label="Scenarios">{dots}</div>'
            '<button type="button" class="btn ghost" id="game-next" hidden>Next scenario &rarr;</button></div>'
            '<p class="sr" id="game-live" aria-live="polite"></p></div>')
    return section("predict", "Predict it", body, '<span class="score" id="game-score"></span>',
                   "Before the hotspots: situations this change creates. Pick what you think happens now, then "
                   "compare with the independent reviewer&rsquo;s read of the diff. Where you differ, the code it "
                   "links to decides, and if the reviewer got it wrong, that&rsquo;s worth a comment on the PR.")


def build_page(repo: Repo, ctx: dict, it: dict, ex: dict) -> str:
    head = ctx["head_sha"]
    gh = github_base(repo)
    stats = ctx["stats"]
    base_name = branch_name(ctx["base_ref"])
    author = git(["config", "user.name"], repo.cwd, check=False).strip()
    rendered = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    risk = ex["overall_risk"]

    ticket = ""
    if it["ticket"]:
        t = it["ticket"]
        ticket = (f'<a class="chip ticket" href="{e(t)}" target="_blank" rel="noopener">{e(t)}</a>'
                  if re.match(r"^https?://", t) else f'<span class="chip ticket">{e(t)}</span>')
    # Prose only (not diagrams or code), at a reviewer's skimming pace.
    prose = [v for k, v in iter_strings({"ex": {k: v for k, v in ex.items() if not k.startswith("diagram")},
                                         "it": it}, "") if "snippet" not in k]
    minutes = max(1, round(sum(len(s.split()) for s in prose) / 230))
    mast = f"""<header class="mast">
  <p class="eyebrow">Pull request explainer</p>
  <h1>{nowrap_flags(e(it["title"]))}</h1>
  <div class="facts">
    <span class="risk risk-{risk}">{RISK_LABEL[risk]}</span>
    <span class="chip sha" title="{e(head)}"><span class="k">commit</span> {e(head[:7])}</span>
    <span class="chip branch">{e(ctx["branch"])} <span aria-hidden="true">&rarr;</span><span class="sr"> into </span> {e(base_name)}</span>
    <span class="chip diffstat"><span class="add">+{stats["additions"]:,}</span> <span class="del">&minus;{stats["deletions"]:,}</span> <span class="n">{stats["files_changed"]:,} files</span> <span class="blocks" aria-hidden="true">{diffstat_blocks(stats["additions"], stats["deletions"])}</span></span>
    <span class="chip read" title="Reading time for this page">&asymp; {minutes} min read</span>
    {ticket}
  </div>
  <p class="provenance">The summary, diagram, hotspots, game and tests come from an independent read: a separate
  agent that saw only the diff and the ticket, not the session that wrote the code. The author&rsquo;s own account
  is labelled as a claim.</p>
</header>"""

    tldr = section("tldr", "In 20 seconds", '<ol class="tldr">' + "".join(f"<li>{e(t)}</li>" for t in ex["tldr"]) + "</ol>")

    hotspot_list = ex["hotspots"]
    # Files the change deletes have nothing at head, so their links show them before the change.
    gone = set(git(["diff", "--name-only", "--diff-filter=D", "-z", ctx["merge_base"], head], repo.cwd,
                   check=False).split("\0")) - {""}

    def at(path):
        return ctx["merge_base"] if path in gone else head
    cmap = change_map(ctx, hotspot_list, gh, head, gone)
    # The PR's whole change, by tree ids: questions and scenarios are about all of it, so their
    # ticks and picks carry over to a later version only while none of it changed.
    whole_change = [git(["rev-parse", f"{rev}^{{tree}}"], repo.cwd, check=False).strip() for rev in (ctx["merge_base"], head)]

    node_map = {n: c["id"] for c in ex["components"] for n in c["node_ids"]}
    after_src = with_highlight(ex["diagram_after"], ex["changed_node_ids"], "prxChanged")
    tools = []
    if ex["diagram_before"]:
        before_src = with_highlight(ex["diagram_before"], ex["removed_node_ids"], "prxRemoved")
        tools.append('<div class="seg" role="tablist" aria-label="Diagram version">'
                     '<button type="button" role="tab" id="tab-before" aria-controls="panel-before" aria-selected="false" tabindex="-1">Before</button>'
                     '<button type="button" role="tab" id="tab-after" aria-controls="panel-after" aria-selected="true">After</button></div>')
        panels = (f'<figure class="panel" id="panel-before" role="tabpanel" aria-labelledby="tab-before" aria-hidden="true"><pre class="mermaid">{e(before_src)}</pre></figure>'
                  f'<figure class="panel is-active" id="panel-after" role="tabpanel" aria-labelledby="tab-after"><pre class="mermaid">{e(after_src)}</pre></figure>')
    else:
        panels = f'<figure class="panel is-active" id="panel-after"><pre class="mermaid">{e(after_src)}</pre></figure>'
    if ex["tour"]:
        tools.append(f'<button type="button" class="tool primary" id="tour-start">&#9654; Take the tour <span class="n">{len(ex["tour"])}</span></button>')
    tools.append('<button type="button" class="tool" id="expand" aria-pressed="false" title="Show the diagram full size">&#10530; Expand</button>')
    legend = []
    if ex["changed_node_ids"]:
        legend.append('<span><i class="sw sw-changed"></i>Added or changed</span>')
    if ex["removed_node_ids"]:
        legend.append('<span><i class="sw sw-removed"></i>Removed (before view)</span>')
    if node_map:
        legend.append("<span>Select a box to open its component</span>")
    tour_box = ""
    if ex["tour"]:
        tour_box = ('<div class="tour" id="tour" hidden><div class="tour-text"><span class="tour-count" id="tour-count"></span>'
                    '<p id="tour-caption"></p></div><div class="tour-nav">'
                    '<button type="button" class="btn ghost" id="tour-prev">&larr; Back</button>'
                    '<button type="button" class="btn" id="tour-next">Next &rarr;</button>'
                    '<button type="button" class="btn ghost" id="tour-end">Done</button></div></div>'
                    # read out from here: the bar above starts hidden, and a live region that does isn't
                    '<p class="sr" id="tour-live" aria-live="polite"></p>'
                    f'<script type="application/json" id="prx-tour">{json_for_script(ex["tour"])}</script>')
    diagram = section("diagram", "How it fits together", f'<div class="stage">{panels}<button type="button" class="close-full" id="close-full" hidden>Close &times;</button></div>'
                      + tour_box + (f'<p class="legend">{"".join(legend)}</p>' if legend else ""),
                      f'<div class="tools">{"".join(tools)}</div>')

    cards = []
    # "I've looked at this" ticks this exact hotspot (its file's change, headline, code, risk and why). After a
    # push a fresh reviewer's hotspots are new risks even where the code is the same, so they start
    # unticked; one on a file whose change the reader already looked at gets a "looked at this code
    # before" marker (data-code) instead, which says what's familiar without claiming it's reviewed.
    # A file's change is its blob ids before and after: stable whatever git's diff settings are.
    def file_change(path):
        return [git(["rev-parse", "--verify", "--quiet", f"{rev}:{path}"], repo.cwd, check=False).strip()
                for rev in (ctx["merge_base"], head)]
    for n, h in enumerate(hotspot_list, 1):
        change_of_file = file_change(h["file"])
        track = track_id("h", h["file"], change_of_file, h["headline"], h["snippet"], h["risk"], h["why_it_matters"])
        code_id = track_id("c", h["file"], change_of_file)
        lang = code_lang(h["file"], h["snippet_format"])
        code = ""
        if h["snippet"]:
            cls = f"language-{lang}" if lang else "nohighlight"
            focus = f' data-focus="{",".join(map(str, h["focus_lines"]))}"' if h["focus_lines"] else ""
            code = f'<pre class="code"{focus}><code class="{cls}">{e(h["snippet"])}</code></pre>'
        where = f' <span class="lines">{fmt_lines(h["lines"])}</span>' if h["lines"] else ""
        cat = f'<span class="cat">{e(h["category"])}</span>' if h["category"] else ""
        cards.append(f"""<article class="hotspot hs-{h["risk"]}" id="hotspot-{n}">
  <div class="hs-head"><span class="rank" aria-label="Hotspot {n}">{n}</span>
    <div class="hs-title"><h3 class="headline">{e(h["headline"])}</h3>
      <div class="hs-meta">{path_html(h["file"], blob_link(gh, at(h["file"]), h["file"], h["lines"]), where)}<span class="risk risk-{h["risk"]}">{RISK_LABEL[h["risk"]]}</span>{cat}</div></div></div>
  <p class="why">{e(h["why_it_matters"])}</p>
  {code}
  <label class="check"><input type="checkbox" data-track="{track}" data-code="{code_id}"><span>I&rsquo;ve looked at this</span></label>
</article>""")
    hotspots = section("hotspots", "Where to look first", "".join(cards) if cards
                       else '<p class="none">No high or medium risk hotspots found.</p>')

    comps = []
    for c in ex["components"]:
        files = "".join(f"<li>{path_html(f, blob_link(gh, at(f), f))}</li>" for f in c["files"])
        count = len(c["files"])
        comps.append(f"""<details class="component" id="component-{e(c["id"])}">
  <summary><span class="ctype ctype-{c["change_type"]}">{CHANGE_LABEL.get(c["change_type"], "")}</span><span class="cname">{e(c["name"])}</span><span class="ccount">{count} file{"" if count == 1 else "s"}</span></summary>
  <div class="cbody"><p>{e(c["summary"])}</p>{f'<ul class="files">{files}</ul>' if files else ""}</div>
</details>""")
    components = section("components", "The parts that changed", f'<div class="components">{"".join(comps)}</div>' if comps
                         else '<p class="none">No components listed.</p>')

    alts = [f'{a["option"]}: {a["why_not"]}' if a["why_not"] else a["option"] for a in it["alternatives_rejected"]]
    approach = it["approach"]
    approach_html = (f'<ol class="steps">{"".join(f"<li>{e(s)}</li>" for s in approach)}</ol>'
                     if isinstance(approach, list) else f'<p class="approach">{e(approach)}</p>')
    ic = ex["intent_check"]
    verdict = {True: ("yes", "&#10003;", "Matches the ticket"), False: ("no", "&#10007;", "Does not match the ticket"),
               None: ("unclear", "?", "Couldn’t tell from the ticket")}[ic["matches_ticket"]]
    intent = section("intent", "Why it was done this way", f"""<p class="verdict verdict-{verdict[0]}"><span class="vicon" aria-hidden="true">{verdict[1]}</span><span><b>Independent read:</b> {e(verdict[2])}</span></p>
<div class="pair">
  <div class="claim"><h3>Author&rsquo;s intent <span class="tag">claim</span></h3>
    <p class="goal">{e(it["goal"])}</p>
    <h4>Approach</h4>{approach_html}
    {folded("Risks the author flagged", it["author_flagged_risks"], open_=True)}
    {folded("How to verify", it["how_to_verify"])}
    {folded("Rejected alternatives", alts)}
    {folded("Deliberately not done", it["deliberately_not_done"])}
  </div>
  <div class="independent"><h3>Independent read</h3>
    <h4>Possibly missing</h4>{items(ic["possibly_missing"], "Nothing found")}
    <h4>Extras nobody asked for</h4>{items(ic["extras_not_requested"], "None found")}
  </div>
</div>""", lede="The author&rsquo;s account, next to what a separate agent read from the diff and the ticket alone.")

    te = ex["test_evidence"]
    tests = section("tests", "What the tests cover", f"""<div class="pair plain">
  <div><h3><span class="tick ok" aria-hidden="true">&#10003;</span>Tests added</h3>{items(te["tests_added"], "No tests added")}</div>
  <div><h3><span class="tick warn" aria-hidden="true">!</span>Weak spots</h3>{items(te["weak_spots"], "None found")}</div>
</div>""")

    questions = section("questions", "Before you approve",
                        '<ul class="questions">' + "".join(
                            f'<li><label class="check"><input type="checkbox" data-track="{track_id("q", q, whole_change)}"><span>{e(q)}</span></label></li>'
                            for q in ex["reviewer_questions"]) + "</ul>",
                        lede="Tick each one you can answer.") if ex["reviewer_questions"] else ""

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
        low = section("low-risk", "Safe to skim", (items(ex["low_risk_collapsed"]) if ex["low_risk_collapsed"] else "") + skipped)

    # The game comes before the hotspots: their headlines would give its answers away, and a
    # prediction that turns out different is what sends a reader to the hotspot it links to.
    sections = [("tldr", "Summary", tldr), ("map", "Files", cmap), ("diagram", "Diagram", diagram),
                ("predict", "Predict it", game(ex["quiz"], whole_change)), ("hotspots", "Hotspots", hotspots),
                ("components", "Parts", components), ("intent", "Intent", intent), ("tests", "Tests", tests),
                ("questions", "Approve", questions), ("low-risk", "Skim", low)]
    sections = [s for s in sections if s[2]]
    jump = ('<nav class="jump" aria-label="Sections"><div class="jump-links">'
            + "".join(f'<a href="#{sid}" data-jump="{sid}">{label}</a>' for sid, label, _ in sections)
            + '</div><div class="progress" id="progress" hidden><span class="plabel" id="progress-label"></span>'
              '<span class="pbar" aria-hidden="true"><i id="progress-fill"></i></span></div></nav>')
    footer = (f'<footer class="foot"><span>Template v{TEMPLATE_VERSION}</span>'
              f'<span>Base {e(ctx["base_ref"])} at {e(ctx["merge_base"][:7])}</span>'
              f'<span>Head {e(head)}</span>'
              f'<span>Rendered {e(rendered)}{" for " + e(author) if author else ""}</span></footer>')
    node_json = f'<script type="application/json" id="prx-node-map">{json_for_script(node_map)}</script>'
    # Progress belongs to the PR's page (not the branch: a branch name reused for a later PR gets a
    # page of its own in the same artifact). Each tick and pick is kept under its exact item
    # (track_id), so a re-render of the same review keeps them; a push brings a fresh review whose
    # items are new, and only the "looked at this file's change" marker (data-code) carries over.
    page_key = f'<script type="application/json" id="prx-key">{json_for_script(ctx.get("page_id") or ctx["branch"])}</script>'
    # When this version was rendered, so the page can tell a newer version from an older copy, and
    # for which commit, so its update note says "new commits" only when there are some
    page_key += (f'<script type="application/json" id="prx-rendered">'
                 f'{json_for_script(dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))}</script>'
                 f'<script type="application/json" id="prx-commit">{json_for_script(head)}</script>')

    body = "\n".join([mast, jump] + [s[2] for s in sections] + [footer, node_json, page_key])
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    values = {"TITLE": e(it["short_title"]), "BODY": body}
    return re.sub(r"\{\{([A-Z_]+)\}\}", lambda m: values.get(m.group(1), m.group(0)), template)


# Draws the diagrams in the page's theme, and again from their source whenever it changes.
# In the repo artifact the main page passes on the viewer's explicit light/dark choice
# (prx-theme), which can arrive after the first draw; opened on its own, the system's applies.
PAGE_SCRIPT = """(function () {
  "use strict";
  var root = document.documentElement, drawnDark = null, queue = Promise.resolve();
  var blocks = Array.prototype.slice.call(document.querySelectorAll("pre.mermaid"));
  var sources = blocks.map(function (b) { return b.textContent; });
  function dark() {
    var t = root.getAttribute("data-theme");
    return t ? t === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  }
  function render() {
    var want = dark();
    if (!window.mermaid || !blocks.length || drawnDark === want) return;
    drawnDark = want;
    window.mermaid.initialize({ startOnLoad: false, securityLevel: "strict", theme: want ? "dark" : "neutral",
                                suppressErrorRendering: true,
                                fontFamily: '"Source Sans 3", "Segoe UI", system-ui, sans-serif' });
    // Each diagram is read, then drawn on its own. One Mermaid can't read or lay out (validate's
    // check of the syntax is a close reading, not Mermaid's own) shows as its source with a note,
    // not as Mermaid's error picture, and the other still draws.
    function failed(b, i) {
      b.classList.add("mm-failed");
      b.textContent = "This diagram didn't draw: Mermaid couldn't read it. Its source:\\n\\n" + sources[i];
    }
    return blocks.reduce(function (done, b, i) {
      return done.then(function () {
        b.removeAttribute("data-processed");
        b.classList.remove("mm-failed");
        b.textContent = sources[i];
        return Promise.resolve(window.mermaid.parse(sources[i], { suppressErrors: true })).then(function (ok) {
          if (ok === false) return failed(b, i);
          return window.mermaid.run({ nodes: [b], suppressErrors: true }).then(function () {
            if (!b.querySelector("svg") || b.querySelector("[aria-roledescription=error]")) failed(b, i);
          }, function () { failed(b, i); });
        }, function () { failed(b, i); });
      });
    }, Promise.resolve());
  }
  function draw() { queue = queue.then(render).catch(function () {}); }  // one render at a time
  window.addEventListener("message", function (ev) {
    var d = ev.data;
    if (!d || d.type !== "prx-theme") return;
    if (d.theme === "dark" || d.theme === "light") root.setAttribute("data-theme", d.theme);
    else root.removeAttribute("data-theme");
    draw();
  });
  try { matchMedia("(prefers-color-scheme: dark)").addEventListener("change", draw); } catch (e) {}
  if (window.parent !== window) {
    try { window.parent.postMessage({ type: "prx-ready" }, "*"); } catch (e) {}
    setTimeout(draw, 400);
  } else {
    draw();
  }
})();
"""


def page_document(page: str) -> str:
    """The page as a document of its own: a PR's file in the repo artifact, and the local preview.
    Files other than an artifact's main page get no skeleton and no built-in Mermaid, so it brings both."""
    return ('<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
            '<style>body{margin:0}img{max-width:100%}[hidden]{display:none!important}</style></head><body>\n'
            + page + f'\n<script src="{PAGE_MERMAID}"></script>\n<script>\n{PAGE_SCRIPT}</script>\n</body></html>\n')


def repo_name(repo: Repo) -> str:
    """The repo's name from origin (github.com/o/pr-explainer.git -> pr-explainer), which is the
    same in every clone and worktree; the checkout folder's name only without an origin."""
    url = git(["remote", "get-url", "origin"], repo.cwd, check=False).strip()
    m = re.search(r"([^/:\\]+?)(?:\.git)?/*$", url)
    return m.group(1) if url and m else Path(repo.root).name


def index_page(repo: Repo) -> str:
    """The repo artifact's main page: it shows the PR page named by the link's #anchor."""
    return INDEX_TEMPLATE_PATH.read_text(encoding="utf-8").replace("{{REPO}}", html.escape(repo_name(repo), quote=True))


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
        write_text(out, page_document(page))
        print(json.dumps({"html": str(out), "head_sha": head, "short_sha": head[:7],
                          "bytes": out.stat().st_size, "warnings": len(rep.warnings)}, indent=2))
        return 0
    # The PR's page in the repo artifact, the artifact's main page, and the single-page
    # form a branch recorded before the repo artifact still publishes to its own artifact.
    out = Path(args.out) if args.out else repo.path("html")
    write_text(out, page)
    write_text(repo.path("page.html"), page_document(page))
    write_text(repo_file(repo, "index.html"), index_page(repo))
    write_text(repo.path("rendered.json"), json.dumps(
        {"head_sha": head, "html": str(out), "inputs": inputs_digest(repo, ctx)}, indent=2))
    published = repo.path("page.html") if ctx.get("page_id") else out
    print(json.dumps({"html": str(out), "page": str(repo.path("page.html")), "index": str(repo_file(repo, "index.html")),
                      "head_sha": head, "short_sha": head[:7], "bytes": published.stat().st_size,
                      "warnings": len(rep.warnings)}, indent=2))
    return 0


# ------------------------------------------------------------------- record

ARTIFACT_URL_RE = re.compile(r"^https://claude\.ai/(?:code/)?artifacts?/[A-Za-z0-9_-]+/?$")


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def pr_body(url: str, ex: dict) -> str:
    # Say what's on the page, so the link reads as worth a click rather than as more reading.
    inside = ["a guided tour of the change" if ex.get("tour")
              else "a before/after diagram" if ex.get("diagram_before") else "a diagram of the change"]
    if ex["hotspots"]:
        n = len(ex["hotspots"])
        inside.append("the hotspot to look at first" if n == 1 else f"the top {n} hotspots")
    if ex.get("quiz"):
        inside.append(f"{plural(len(ex['quiz']), 'scenario')} to predict before you approve")
    inside.append("an independent intent check")
    summary = f"{', '.join(inside[:-1])} and {inside[-1]}"
    lines = [f"> **Interactive explainer:** [open the review guide]({url}). "
             f"{summary[0].upper()}{summary[1:]}. Opens on claude.ai with an org account.",
             "", "**TL;DR**"]
    lines += [f"- {t}" for t in ex["tldr"]]
    return "\n".join(lines) + "\n"


def cmd_record(args):
    repo = Repo()
    url = args.url.strip().rstrip("/")
    if not ARTIFACT_URL_RE.match(url):
        raise PrxError("Expected a claude.ai artifact URL like https://claude.ai/code/artifact/<id> (without a #page)")
    ctx = load_json(repo.path("context.json"), "context.json (run `prx.py prepare` first)")
    page_id = ctx.get("page_id")
    repo_url = repo_artifact(repo)
    existing = read_text(repo.path("url"))
    existing_home, existing_page = split_link(existing)
    existing_home = existing_home.rstrip("/")  # older records may end in /: the same artifact
    existing_link = existing_home + (f"#{existing_page}" if existing_page else "") if existing else None
    # An open PR stays in the artifact its link points at; a new PR goes to the repo's current one.
    # Moving either is its own flag, so following a refusal can't start a second repo artifact.
    home = existing_home if existing_page else repo_url
    retired = [u.rstrip("/") for u in repo_record(repo).get("retired", [])]
    if page_id and home and home != url and not args.new_artifact:
        # No ready-made command here: moving the repo is the user's call, not a way past this error.
        raise PrxError(f"This PR's page belongs in {home}, not {url}. Publish it to {home} (that adds or "
                       f"replaces this PR's page beside the others) and record that. Never make a second "
                       f"artifact for the repo; if {url} was made by mistake, tell the user it can be deleted. "
                       "Moving the repo's explainers to another artifact is only for when the user asks "
                       "(SKILL.md, step 5).")
    if args.new_artifact and not page_id:
        raise PrxError(f"Branch {repo.branch} has an artifact of its own from before the repo artifact "
                       f"({existing}); --new-artifact only moves PR pages. Pass --replace if that artifact is gone.")
    if args.new_artifact and (url == home or url in retired):
        why = "already holds this PR's page" if url == home else "is one this repo's explainers moved away from"
        raise PrxError(f"{url} {why}; --new-artifact only moves to a new artifact.")
    if args.replace and not page_id and url in [repo_url] + retired:
        raise PrxError(f"{url} is the repo's artifact, holding every PR's page. Branch {repo.branch}'s explainer "
                       "is a single page from before it, and publishing that there replaces the main page "
                       "for every PR. Publish it to an artifact of its own.")
    # A PR's link is the artifact plus its page; a branch recorded before the repo
    # artifact keeps an artifact of its own.
    link = f"{url}#{page_id}" if page_id else url
    if existing_link and existing_link != link and not (args.replace or args.new_artifact):
        raise PrxError(f"Branch {repo.branch} already has an explainer at {existing}. Republish to that "
                       "artifact instead of creating a second one (pass --replace only if the old one is gone).")
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
    if page_id and (args.new_artifact or not repo_url) and url != repo_url:  # the repo's first page, or a move
        record = {"artifact_url": url}
        if repo_url:
            record["retired"] = retired + [repo_url]  # --new-artifact refuses these
        write_text(repo_file(repo, "repo.json"), json.dumps(record, indent=2))
    write_text(repo.path("pr-body.md"), pr_body(link, ex))
    # What the published page shows, so the gate can check a PR's base against it
    # rather than against whatever the last prepare used.
    write_text(repo.path("published.json"), json.dumps(
        {"url": link, "sha": head, "base_ref": ctx["base_ref"], "merge_base": ctx["merge_base"]}, indent=2))
    write_text(repo.path("url"), link + "\n")
    write_text(repo.path("sha"), head + "\n")
    out = {"artifact_url": link, "head_sha": head, "pr_body": str(repo.path("pr-body.md"))}
    if existing_link and existing_link != link:
        out["warnings"] = [f"This PR's link moved from {existing_link} to {link}. If its PR is open, "
                           "replace the old link in its description (gh pr edit)."]
    print(json.dumps(out, indent=2))
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
        if not p.is_file():  # the repo-artifact folder
            continue
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
                       help="a new PR (e.g. a reused branch name): archive the old link and start a new page")
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
    move = s.add_mutually_exclusive_group()
    move.add_argument("--replace", action="store_true",
                      help="a branch with an artifact of its own: replace its URL (the old artifact is gone)")
    move.add_argument("--new-artifact", action="store_true",
                      help="move the repo's explainers to this new artifact: this PR and every new one")
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
