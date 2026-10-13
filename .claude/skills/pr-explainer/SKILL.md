---
name: pr-explainer
description: Generate and publish an interactive HTML PR explainer page as a Claude Code artifact, and put its link in the PR body. ALWAYS use before running `gh pr create`, and in update mode after pushing new commits to a branch that already has an explainer (a hook will ask for this).
---

# PR Explainer

Builds a review page for the current branch: before/after diagram, top 3 hotspots, intent
check and test evidence. The page is published as a claude.ai artifact and linked from the PR.

`PRX` below means `python .claude/skills/pr-explainer/prx.py`, run from the repo root. Use
`python`, not `python3` (on Windows `python3` is often the Microsoft Store stub).

## 1. Prepare

```bash
python .claude/skills/pr-explainer/prx.py prepare            # add --base <branch> if not the default
```

This writes `<key>.context.json` and `<key>.reviewer-prompt.md` to the state directory and
prints `mode` (`create` or `update`), `artifact_url`, `stats`, `large` and all file paths.
Use those paths below. Don't guess them. Each prepare gives the explainer a new file name
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
- Same PR after a rebase or force-push: `prepare --same-pr` keeps the artifact URL.
- New PR reusing an old branch name: `prepare --new` archives the old URL and starts a new
  artifact, so the merged PR's link keeps showing its own code.

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
claim and puts the independent read beside it.

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
- To look at the page locally, `render --standalone` writes `<key>.preview.html` with Mermaid
  loaded for preview. Publish only the normal render.

## 5. Publish

Read the rendered `<paths.html>` in full first (it was written by a script, not by you).

- **create** (no `artifact_url`): publish `<paths.html>` with the Artifact tool, with
  `icon: "code"` and a one-sentence description such as "Review guide for <short_title>
  at commit <short sha>". Then:
  ```bash
  python .claude/skills/pr-explainer/prx.py record <artifact URL>
  ```
- **update** (`artifact_url` is set): first read that URL with the Artifact tool
  (`action: "read"`); a publish to an artifact this conversation hasn't read is refused.
  Then publish `<paths.html>` with `url: <artifact_url>` and no icon, so the link stays the
  same. Then run `record <artifact_url>`. Never create a second artifact for the same branch;
  `record` refuses a different URL.

`record` stores the URL and the commit it shows, and writes `<key>.pr-body.md`. It refuses
if either JSON file has errors or changed after the last render, so render and republish first.

## 6. Hand off

- **create:** create the PR against the base the page was rendered for (prepare's
  `base_ref`, without `origin/`), with the generated body so the link sits at the top:
  `gh pr create --base <base> --title "<title>" --body-file <state dir>/<key>.pr-body.md`
  (append anything else the PR needs to that file first). The gate blocks a PR whose base
  doesn't match the published page. Then tell the user once that the
  artifact is private until they share it. They should open it, choose Share, then
  "Everyone in your organization", so reviewers can see it. Viewers need a claude.ai
  account in the same org as the account that published it.
- **update:** confirm in one line that the explainer now shows commit `<short sha>`. The PR
  body link doesn't change.

## Rules

- The `gh pr create` hook blocks until an explainer exists for HEAD and its link is in the
  body. Don't try to get around it. If the user wants a PR without an explainer, they can
  run `gh` themselves.
- Never put secrets, tokens or `.env` contents in either JSON file or the page.
- Never edit `template.html` or `prx.py` for a single PR. Every page uses the same layout so
  reviewers learn where to look.
- Keep the PR body short: the link and the TL;DR. The page holds the detail.
- `prx.py prune` removes state for branches that no longer exist locally or on origin.
