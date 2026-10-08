#!/usr/bin/env python3
"""PR Explainer helper, used by the /pr-explainer skill and by the hooks.

Subcommands (run from anywhere inside the repo):
  status                 where this branch stands: mode, artifact URL, freshness
  prepare [--base REF]   collect git facts and the changed-file list into <key>.context.json
  validate [--only P]    check intent/explainer JSON against the contract (P = intent|explainer)
  render [--standalone]  validate, scan for secrets, write <key>.html
  record URL             remember the published artifact URL and the commit it shows

State lives in <git common dir>/pr-explainer/, so it is never committed and is
shared by every worktree of the clone. Standard library only (Python 3.9+).
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

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
    """File-name-safe key for a branch: feature/x -> feature__x."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", branch.replace("/", "__"))


class Repo:
    def __init__(self, cwd=None):
        self.cwd = str(cwd or os.getcwd())
        self.root = git(["rev-parse", "--show-toplevel"], self.cwd).strip()
        common = git(["rev-parse", "--path-format=absolute", "--git-common-dir"], self.cwd).strip()
        self.state_dir = Path(common) / "pr-explainer"
        self.branch = git(["branch", "--show-current"], self.cwd).strip()

    def head(self) -> str:
        return git(["rev-parse", "HEAD"], self.cwd).strip()

    def path(self, suffix: str) -> Path:
        if not self.branch:
            raise PrxError("HEAD is detached. Check out the PR branch first.")
        return self.state_dir / f"{branch_key(self.branch)}.{suffix}"


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


def explainer_state(repo: Repo) -> dict:
    url = read_text(repo.path("url"))
    sha = read_text(repo.path("sha"))
    head = repo.head()
    return {
        "branch": repo.branch,
        "mode": "update" if url else "create",
        "artifact_url": url or None,
        "explained_sha": sha or None,
        "head_sha": head,
        "fresh": bool(url) and sha == head,
        "state_dir": str(repo.state_dir),
    }


# ---------------------------------------------------------- command matching
# Shared by the hooks. A command "segment" starts at the beginning of the
# string or after ; & | ( { $( or a newline, so `echo "gh pr create"` is ignored.
# Backticks don't start a segment: they're far more common as Markdown in
# commit messages and PR bodies than as bash command substitution.

_SEGMENT = r"(?:^|[;&|({\n]|\$\()\s*"
_ENV = r"(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*"
_CALL = r"(?:&\s*)?"  # PowerShell call operator


def _exe(name):
    return (rf"""(?:"[^"\n]*[\\/]{name}(?:\.exe)?"|'[^'\n]*[\\/]{name}(?:\.exe)?'"""
            rf"""|(?:[^\s;&|()'"]*[\\/])?{name}(?:\.exe)?)""")


GH_PR_CREATE_RE = re.compile(
    _SEGMENT + _ENV + _CALL + _exe("gh")
    + r"\s+(?:(?:-R|--repo)(?:=|\s+)\S+\s+)?pr\s+(?:create|new)\b", re.I)
GIT_PUSH_RE = re.compile(
    _SEGMENT + _ENV + _CALL + _exe("git")
    + r"\s+(?:(?:-C|-c)\s+\S+\s+|--[a-z-]+(?:=\S+)?\s+)*push\b(?P<args>[^;&|\n)]*)", re.I)
_NON_REFRESH_PUSH = re.compile(r"(?:^|\s)(?:--dry-run|-n|--delete|-d)(?=\s|$)|\s:\S")
BODY_FILE_RE = re.compile(r"""(?:--body-file|(?<![\w-])-F)(?:=|\s+)(?:"([^"]+)"|'([^']+)'|([^\s;&|)]+))""")


# Heredoc and PowerShell here-string bodies are data (commit messages, PR
# bodies), not commands. The text after the heredoc marker on its own line is kept.
_HEREDOC_RE = re.compile(r"(<<-?[ \t]*(['\"]?)([A-Za-z_]\w*)\2)([^\n]*\n).*?^[ \t]*\3[ \t]*$", re.S | re.M)
_HERESTRING_RE = re.compile(r"@(['\"])[ \t]*\r?\n.*?\r?\n\1@", re.S)


def command_text(cmd: str) -> str:
    """The parts of a shell command that are commands, for matching."""
    cmd = _HEREDOC_RE.sub(lambda m: m.group(1) + m.group(4), cmd or "")
    return _HERESTRING_RE.sub(lambda m: f"@{m.group(1)}{m.group(1)}@", cmd)


def is_pr_create(cmd: str) -> bool:
    return bool(GH_PR_CREATE_RE.search(command_text(cmd)))


def is_refreshing_push(cmd: str) -> bool:
    """A git push that sends new commits (not --dry-run, not a branch delete)."""
    return any(not _NON_REFRESH_PUSH.search(m.group("args")) for m in GIT_PUSH_RE.finditer(command_text(cmd)))


def _local_path(name: str, cwd: str) -> Path:
    name = os.path.expanduser(name)
    m = re.match(r"^/([A-Za-z])/(.*)$", name)  # Git Bash /c/Users/... on Windows
    if m and os.name == "nt":
        name = f"{m.group(1)}:/{m.group(2)}"
    p = Path(name)
    return p if p.is_absolute() else Path(cwd) / p


def short(sha) -> str:
    return (sha or "none")[:7]


def gate_problem(cmd: str, cwd: str):
    """Why `gh pr create` must not run yet, or None when it may."""
    repo = Repo(cwd)
    if not repo.branch:
        return "HEAD is detached. Check out the PR branch, run /pr-explainer, then create the PR."
    st = explainer_state(repo)
    if not st["artifact_url"]:
        return (f"No PR explainer for branch '{repo.branch}' yet. Run the /pr-explainer skill first, "
                "then re-run gh pr create with the explainer link in the PR body.")
    if not st["fresh"]:
        return (f"The PR explainer shows commit {short(st['explained_sha'])} but HEAD is "
                f"{short(st['head_sha'])}. Run /pr-explainer in update mode (republish to the same URL "
                f"{st['artifact_url']}), then re-run gh pr create.")
    url = st["artifact_url"]
    if url in cmd:
        return None
    for m in BODY_FILE_RE.finditer(cmd):
        name = next(g for g in m.groups() if g)
        if name == "-":
            continue
        try:
            if url in _local_path(name, cwd).read_text(encoding="utf-8-sig", errors="replace"):
                return None
        except OSError:
            pass
    return (f"Put the PR explainer link near the top of the PR body: {url}  "
            f"A ready-made body is at {repo.path('pr-body.md')} (pass it with --body-file).")


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


def cmd_prepare(args):
    repo = Repo()
    if not repo.branch:
        raise PrxError("HEAD is detached. Check out the PR branch first.")
    base, warnings = resolve_base(repo, args.base, fetch=not args.no_fetch)
    head = repo.head()
    merge_base = git(["merge-base", base, "HEAD"], repo.cwd).strip()
    if merge_base == head:
        raise PrxError(f"No commits on {repo.branch} beyond {base}. Commit the change first.")
    files = numstat(repo, merge_base, head)
    review, excluded = classify(files, linguist_flags(repo, [f["path"] for f in files]))
    adds = sum(f["additions"] for f in files)
    dels = sum(f["deletions"] for f in files)
    st = explainer_state(repo)
    context = {
        "schema": 1,
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
        ("context", "context.json"), ("intent", "intent.json"), ("explainer", "explainer.json"),
        ("html", "html"), ("reviewer_prompt", "reviewer-prompt.md")]}
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
    if v is None or v == "":
        if required:
            rep.err(f"{where}.{key}", "is required")
        return ""
    if not isinstance(v, str):
        rep.err(f"{where}.{key}", "must be a string")
        return ""
    return v.strip()


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
    for i, a in enumerate(o.get("alternatives_rejected") or []):
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
        if re.match(r"click\s", s):
            rep.warn(where, "removed a click directive (the page links nodes to components itself)")
            continue
        kept.append(line)
    first = next((ln.strip() for ln in kept if ln.strip() and not ln.strip().startswith("%%")), "")
    if not re.match(r"(?:flowchart|graph)\b", first):
        rep.err(where, "must be a Mermaid flowchart (start with 'flowchart LR' or 'flowchart TD')")
    joined = "\n".join(kept).strip()
    if re.search(r"javascript:|<\s*(?:script|iframe|object|embed)|<[^>]*\son[a-z]+\s*=", joined, re.I):
        rep.err(where, "contains HTML or script; use plain-text labels")
    return joined


def _node_ids(rep, o, key, diagram, where):
    ids = _str_list(rep, o, key, where)
    for nid in ids:
        if not NODE_ID_RE.match(nid):
            rep.err(f"{where}.{key}", f"'{nid}' must be a simple Mermaid id (letters, digits, _)")
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
    if not isinstance(o.get("components"), list):
        rep.err("explainer.components", "is required (a list)")
    for i, c in enumerate(o.get("components") or []):
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
    raw_hotspots = o.get("hotspots") or []
    if not isinstance(raw_hotspots, list):
        rep.err("explainer.hotspots", "must be a list")
        raw_hotspots = []
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

SECRET_PATTERNS = [
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})")),
    ("Slack token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}")),
    ("Anthropic API key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}")),
    ("OpenAI-style key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{32,}")),
    ("Stripe live key", re.compile(r"\b[rs]k_live_[A-Za-z0-9]{20,}")),
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    ("URL with password", re.compile(r"\b[a-z][a-z0-9+.\-]*://[^\s:/@]+:(?P<val>[^\s@/]{6,})@")),
    ("credential assignment", re.compile(
        r"(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret)"
        r"\b[\"']?\s*[:=]\s*[\"'](?P<val>[^\"'\s]{8,})[\"']")),
]
PLACEHOLDER_RE = re.compile(
    r"^(?:<[^>]*>|\*+|x+|\.{3}|\$\{[^}]*\}|\{\{[^}]*\}\}|%\(?[A-Za-z_]+\)?s?|redacted|changeme|"
    r"(?:your|example|dummy|test|fake|placeholder|sample)[\w.-]*)$", re.I)


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
        for name, rx in SECRET_PATTERNS:
            for m in rx.finditer(text):
                val = m.groupdict().get("val")
                if val and PLACEHOLDER_RE.match(val):
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
        raw = load_json(repo.path("explainer.json"), "explainer.json")
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
        write_text(repo.path("rendered.json"), json.dumps({"head_sha": head, "html": str(out)}, indent=2))
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
    write_text(repo.path("url"), url + "\n")
    write_text(repo.path("sha"), head + "\n")
    ctx, it, ex, _ = _check(repo, "explainer")
    write_text(repo.path("pr-body.md"), pr_body(url, ex))
    print(json.dumps({"artifact_url": url, "head_sha": head, "pr_body": str(repo.path("pr-body.md"))}, indent=2))
    return 0


def cmd_status(args):
    print(json.dumps(explainer_state(Repo()), indent=2))
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
    args = p.parse_args(argv)
    try:
        return args.func(args)
    except PrxError as err:
        print(f"prx: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
