---
name: pr-explainer
description: Generate and publish an interactive HTML PR explainer page as a Claude Code artifact, and put its link in the PR body. ALWAYS use before running `gh pr create`, and in update mode after pushing new commits to a branch that already has an explainer (a hook will ask for this).
---

# PR Explainer

Builds a review page for the current branch: a map of the changed files, a before/after
diagram with a guided tour, a short "Predict it" game, the top 3 hotspots, an intent check and
test evidence. The page is published as a claude.ai artifact and linked from the PR.

`PRX` below means `python .claude/skills/pr-explainer/prx.py`, run from the repo root. Use
`python`, not `python3` (on Windows `python3` is often the Microsoft Store stub).

## 1. Prepare

```bash
python .claude/skills/pr-explainer/prx.py prepare            # add --base <branch> if not the default
```

This writes `<key>.context.json` and `<key>.reviewer-prompt.md` to the state directory and
prints `mode` (`create` or `update`), `artifact_url` (the PR's recorded link), `stats`,
`large`, all file paths and a `publish` block for step 5. Use those paths below. Don't guess them. Each prepare gives the explainer a new file name
(`paths.explainer`) and deletes earlier runs' files, so every prepare needs a fresh reviewer
run, and a reviewer from an earlier run can't land under this commit's SHA.

If prepare stops because the recorded explainer "belongs to a PR that's finished" (its
commit is already in the base branch, or GitHub shows the branch's PR as merged or closed),
this is a new PR: run `prepare --new`. Only use `--same-pr` if it really is the same PR.

Without `--base`, prepare uses origin's default branch, asking origin for it if the clone
doesn't record it. In update mode it keeps the base the page was published against, since
the open PR targets it, and warns if that isn't origin's default. To change the base of an
open PR, run prepare with `--base <branch>`, republish, and retarget the PR itself with
`gh pr edit --base <branch>`; the page and the PR must agree.

Read prepare's `warnings`. If it couldn't ask GitHub about a squash merge, run the `gh pr
list` it suggests yourself before republishing to an existing URL. If it says the base is a
guess, check which branch the PR should target and pass `--base` if it isn't that one.

If prepare stops because the recorded explainer "isn't in this branch's history", decide
which case it is. `gh pr list --head <branch> --state all --json number,state,url` shows
the PRs that used the branch.
- Same PR after a rebase or force-push: `prepare --same-pr` keeps the PR's link.
- New PR reusing an old branch name: `prepare --new` archives the old link and starts a new
  page, so the merged PR's link keeps showing its own code.

## 2. Start the independent reviewer (fresh context)

Spawn one `general-purpose` agent in the background with this prompt:

> Read and follow the instructions in `<paths.reviewer_prompt>`. The ticket or request, verbatim:
> <the ticket text, or the user's original request in their own words>

- Pass the ticket or request **verbatim**, not your summary of it.
- Don't pass your conversation, your reasoning or intent.json. The independence is the point.
- In update mode, start a new reviewer too. Don't reuse an old one.

## 3. Write intent.json yourself while the reviewer works

Write `<paths.intent>` following the intent.json section of `schema.md`. You know the
"why": the goal, the approach, alternatives you rejected and why, what you deliberately left
out, risks you know about and how to verify. Be honest; the page labels this as the author's
claim and puts the independent read beside it. Write for a skimming reader: `approach` as a
list of short steps, one sentence per list item, and leave history (what each review round
fixed) to the commit messages rather than piling it into the lists.

In update mode, keep `short_title` unchanged (it's the artifact's name) and revise the rest
only where the intent changed.

## 4. Render

When the reviewer reports back:

```bash
python .claude/skills/pr-explainer/prx.py render
```

- If it prints `ERROR` lines in `explainer.*`, send them to the same reviewer with
  SendMessage and ask it to fix and re-validate. Fix `intent.*` errors yourself. Re-run
  render. After two failed rounds, stop and tell the user what's failing.
- A secret-scan error means a key or password made it into the JSON. Remove it and never
  publish around it.
- To look at the page locally, `render --standalone` writes `<key>.preview.html`. Publish
  only the normal render.

## 5. Publish

All of a repo's explainers live in one artifact: its main page shows the PR named by the
link's `#anchor`, and each PR's page is a file beside it. prepare's `publish` block says
exactly what to send; use it as given rather than building paths yourself.

Read every file you'll publish in full first (they were written by a script, not by you):
`publish.page` and each file in `publish.files`.

- **No repo artifact yet** (`publish.artifact_url` is null): publish `publish.page` with the
  Artifact tool, with `files: publish.files`, `icon: "code"` and a one-sentence description
  such as "Review guides for <repo>'s pull requests". Then run `record` with the URL the
  publish returned.
- **Repo artifact exists** (`publish.artifact_url` is set): if this conversation hasn't read
  or published it yet, read it first (`action: "read"`, `url: publish.artifact_url`) and list
  its files (`action: "list"`, `scope: "files"`, same `url`). The tool refuses a publish to
  an artifact this conversation hasn't read, and refuses to replace a file (an update's
  `prs/<page id>.html`) it hasn't read, listed or published. If a publish is refused because
  someone published since, publish again onto the version it hands you. Then publish
  `publish.page` with `url: publish.artifact_url`, `files: publish.files` and no icon. Files
  you don't pass are kept, so other PRs' pages stay as they are. Then run
  `record <publish.artifact_url>`.
- A branch recorded before the repo artifact existed has an artifact of its own:
  `publish.files` is empty and `publish.page` is its single page. Publish it to
  `publish.artifact_url` as above.

```bash
python .claude/skills/pr-explainer/prx.py record <artifact URL, without any #page>
```

`record` stores the PR's link (the artifact plus `#<page id>`), the commit it shows and the
repo artifact, and writes `<key>.pr-body.md` with that link. It refuses a different artifact
than the repo's, and refuses if either JSON file has errors or changed after the last
render, so render and republish first. Never create a second artifact for the repo. If one
was made by mistake, publish to the right one and tell the user the stray can be deleted.
Only when the user asks to move the repo's explainers (the artifact was deleted or is full)
does `record <new URL> --new-artifact` make a new artifact the repo's: this PR and every new
one go there, and other open PRs keep theirs until they're recorded with it too. It refuses
the artifact the PR is already in and any the repo moved away from. `--replace` is only for a
branch with an artifact of its own whose artifact is gone, and never takes the repo's.

## 6. Hand off

- **create:** create the PR against the base the page was rendered for (prepare's
  `base_ref`, without `origin/`), with the generated body so the link sits at the top:
  `gh pr create --base <base> --title "<title>" --body-file <state dir>/<key>.pr-body.md`
  (append anything else the PR needs to that file first). The gate blocks a PR whose base
  doesn't match the published page, and one whose body lacks this PR's own link (the
  artifact's bare URL isn't enough). If this publish created the repo artifact, tell the user
  once that it's private until they share it: open it, choose Share, then "Everyone in your
  organization". That one share covers every later PR in the repo. Viewers need a claude.ai
  account in the same org as the account that published it.
- **update:** confirm in one line that the explainer now shows commit `<short sha>`. The PR
  body link doesn't change.

## Rules

- The `gh pr create` hook blocks until an explainer exists for HEAD and its link is in the
  body. Don't try to get around it. If the user wants a PR without an explainer, they can
  run `gh` themselves.
- Never put secrets, tokens or `.env` contents in either JSON file or the page.
- Never edit `template.html`, `index.html` or `prx.py` for a single PR. Every page uses the
  same layout so reviewers learn where to look.
- Keep the PR body short: the link and the TL;DR. The page holds the detail.
- `prx.py prune` removes state for branches that no longer exist locally or on origin.
