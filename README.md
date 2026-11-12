# PR Explainer

A [Claude Code](https://claude.com/claude-code) skill and two hooks that give every pull request an
interactive review page. When Claude opens a PR, it publishes the page as a claude.ai artifact and
puts the link at the top of the PR description. When Claude pushes new commits, the page is updated
at the same link.

Agents write most code now, and long PR descriptions get skimmed and rubber-stamped. The page helps
a reviewer understand a change's shape, intent and risk in a few minutes, and shows exactly where to
look.

## What a reviewer gets

- **In 20 seconds:** three sentences on what changed.
- **Where the change is:** every changed file as a bar sized by churn. Test files are dimmed, and
  numbered markers point to the hotspots.
- **How it fits together:** before and after diagrams with the changed parts highlighted, a guided
  tour, and boxes that open the component they stand for.
- **Predict it:** a short game. Guess what the code does in a few situations, then compare with the
  author's answer. Where you differ, the linked code decides.
- **Where to look first:** the top three hotspots, each with the code and a "looked at this" tick.
- **Why it was done this way:** the goal, the approach, the alternatives rejected and the known risks.
- **Tests and questions:** what the tests cover, their weak spots, and the questions to answer
  before approving.
- **Since your last visit:** after a push, the file list tags what changed since the commit you
  last read. Each tag links that file's diff.

The page is a **summary, not a review**. The session that made the change writes it from what it
already knows, so no agent re-reads the code. That keeps it cheap, and it means nobody independent
has checked it, which the page says. When a PR needs that, run `/code-review`. Git facts (files, line
counts, commit) come from git, never from a model, and every code snippet on the page is checked
against its file at the commit, so the page can't show code that isn't there.

## How it works

```
gh pr create ──► pr_create_gate.py blocks until an explainer for HEAD is linked in the body
                     │
/pr-explainer skill  ▼
  1. prx.py prepare      git facts and the changed-file list
  2. write the summary   intent.json (why) and explainer.json (what), by the same session
  3. prx.py render       validates, checks snippets against the code, scans for secrets
  4. publish             one artifact per repo; each PR is a page in it (artifact#<page id>),
                         and the pages share one stylesheet and script, published once
  5. prx.py record       stores the link and writes the PR body
                     │
git push ──────────► pr_push_refresh.py asks Claude to update the page at the same link
```

The session writes JSON. A fixed, versioned template turns it into HTML, so every PR's page has the
same layout and reviewers learn where to look. The summary carries over between pushes: an update
edits only what the new commits changed, and `render` warns if the code moved but the summary didn't.

## Set it up in a repository

You need:

- Claude Code, signed in to a claude.ai organisation account with artifacts enabled. Viewers need an
  account in the same organisation.
- Python 3.9 or later, runnable as `python` on every machine that uses the repository. Where only
  `python3` exists, make `python` run it (on Debian or Ubuntu, `sudo apt install python-is-python3`).
  Don't change the committed hook command to `python3`: on Windows that is often the Microsoft Store
  stub, and a hook that can't start doesn't block.
- `git` 2.31 or later, and the [GitHub CLI](https://cli.github.com/) (`gh`) signed in to the
  repository's host.

Then:

1. Copy `.claude/skills/pr-explainer/` and `.claude/hooks/` into your repository.
2. Add the two hook entries from `.claude/settings.json` to your own `.claude/settings.json`, or copy
   the file if you don't have one.
3. Commit both. The hooks only run for commands Claude runs, in sessions started in that repository.
4. Check the gate once. In Claude Code, on a branch with a commit, ask Claude to run
   `gh pr create --dry-run --fill`. It should stop with "PR explainer gate: …". A dry run creates no
   PR even if the gate isn't working, though it may push the branch.

The first PR creates the repository's artifact, which is private to you. Share it once (open it,
choose Share, then "Everyone in your organization"), and every later PR's page is covered.

## Use it

Ask Claude to open a pull request. The gate blocks `gh pr create` until the skill has run. You can also run
`/pr-explainer` yourself. After you ask Claude to push more commits, the refresh hook asks it to
update the page. The link in the PR description doesn't change.

`prx.py` does the work and can be run by hand, from the repository's root as shown here:

| Command | What it does |
|---|---|
| `status` | Mode (create or update), the PR's link, and whether the page shows HEAD |
| `prepare [--base REF] [--same-pr \| --new] [--assets]` | Collects git facts, and lists the files changed since the page's commit and the files to publish. `--assets` lists the pages' shared stylesheet and scripts again |
| `validate [--only intent\|explainer]` | Checks the JSON against the contract in `schema.md`, and each hotspot's snippet against its file |
| `render [--standalone]` | Validates, scans for secrets and writes the page; `--standalone` writes a local preview |
| `record URL` | Remembers the published artifact and writes the PR body with its link |
| `prune [--dry-run]` | Removes state for branches that no longer exist locally or on origin |

```bash
python .claude/skills/pr-explainer/prx.py status
```

State lives in `<git common dir>/pr-explainer/`, so it is never committed and every worktree of the
clone shares it.

## What's in this repository

| Path | What it is |
|---|---|
| `.claude/skills/pr-explainer/SKILL.md` | The steps Claude follows |
| `.claude/skills/pr-explainer/prx.py` | The helper the skill and hooks share; standard library only |
| `.claude/skills/pr-explainer/cmdparse.py` | Finds the commands a Bash or PowerShell line would run, for the hooks |
| `.claude/skills/pr-explainer/template.html` | The page: styles and script, with no build step. Render splits them into shared files for the repo artifact |
| `.claude/skills/pr-explainer/index.html` | The artifact's main page, which shows the PR named by the link's `#anchor` |
| `.claude/skills/pr-explainer/schema.md` | The JSON contract for `intent.json` and `explainer.json` |
| `.claude/skills/pr-explainer/examples/` | A sample `intent.json` and `explainer.json` |
| `.claude/hooks/` | `pr_create_gate.py` (before a command) and `pr_push_refresh.py` (after one) |
| `tests/` | The test suite |
| `pr-explainer-plan.md` | The design: problem, decisions, data contracts, known limits and roadmap |

## Run the tests

```bash
python tests/run.py
```

That runs every test, spread over one process per CPU (about a minute on 16). While working on
a change, two shorter runs help:

```bash
python tests/run.py --fast
```

```bash
python tests/run.py -k retarget
```

- **`--fast`** leaves out the tests marked `@slow` in `tests/test_prx.py`: the ones that build
  repositories and run `prx.py` as a command. What's left checks command matching, validation, the
  secret scan, diagrams and the hooks' edge cases in a few seconds. It doesn't cover prepare, render
  or record, so run every test before pushing.
- **`-k TEXT`** runs only the tests whose name contains the text.

`python -m unittest discover -s tests` still runs every test in one process, which takes several
minutes.

The tests build throwaway git repositories and never call GitHub. Two groups need an extra tool and
are skipped without it:

- **Page logic** (since-last-visit marks, the update note): needs Node.js.
- **Diagram layout** (scrolling, centring and padding of the diagram, in the page and in Expand):
  needs Chrome, Edge or Chromium. Set `PRX_BROWSER` to a browser's path if it isn't found.

## Limits

- The page is the author's own account. It can repeat a misunderstanding the code has, so it doesn't
  replace reading the risky parts, or `/code-review` on a PR that needs a second opinion.
- Only PRs and pushes made through Claude Code get or refresh a page.
- A hook that can't start, because `python` isn't found or it times out, doesn't block: Claude Code
  reports an error and the PR goes through without a page. Step 4 of the set-up catches this.
- Page content, including code snippets, goes to claude.ai and stays within your organisation. The
  render blocks on anything the secret scan matches, but that scan is pattern-based.
- The gate expects a cooperative agent: it parses commands, so it can't see inside script files or
  expand aliases.

The full list, with mitigations, is in section 10 of [`pr-explainer-plan.md`](pr-explainer-plan.md).
