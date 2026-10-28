# PR Explainer data contract (v2)

Two JSON files feed the fixed template. `prx.py validate` and `prx.py render` enforce
everything marked **error** below; anything else is a warning.

Git facts (commit SHA, base, merge-base, file and line counts, excluded files) are **not**
part of either file. `prx.py` reads them from git so the page can't show a wrong SHA.
If you include `commit_sha`, `base_ref` or `stats`, they are ignored.

## intent.json: written by the author agent (the session that wrote the code)

```json
{
  "title": "Add hybrid RRF ranking to ad search",
  "short_title": "Hybrid RRF ranking",
  "ticket": "SRCH-1234",
  "goal": "One or two sentences: what problem this solves and for whom.",
  "approach": ["Query BM25 and the vector index in parallel", "Merge the two lists with RRF (k = 60)"],
  "alternatives_rejected": [
    { "option": "Linear score blending", "why_not": "Needs per-query weight tuning" }
  ],
  "deliberately_not_done": ["Did not change the indexing pipeline"],
  "author_flagged_risks": ["Latency on long-tail queries"],
  "how_to_verify": ["Run the relevance eval suite", "Try query 'iphone 15' on staging"]
}
```

| Field | Rule |
|---|---|
| `title` | required |
| `short_title` | required, 2-4 words, becomes the PR page's `<title>` (and the artifact's name for a branch that has an artifact of its own; the repo artifact is named after the repo). Keep it unchanged on updates. |
| `ticket` | optional; an ID or a URL (URLs become links) |
| `goal` | required; one or two sentences, shown large |
| `approach` | required; a list of short steps (shown numbered, up to 8), or one string for a small change. A string over 700 characters warns: split it into steps. |
| `alternatives_rejected` | list of `{option, why_not}` |
| `deliberately_not_done`, `author_flagged_risks`, `how_to_verify` | lists of strings. The page folds them behind their titles (risks open), so keep each item to one sentence. |

## explainer.json: written by the fresh-context reviewer (diff + ticket only)

```json
{
  "tldr": ["Up to three short sentences."],
  "overall_risk": "medium",
  "components": [
    {
      "id": "search-api",
      "name": "Search API",
      "change_type": "modified",
      "summary": "Now calls both BM25 and vector retrievers and fuses with RRF.",
      "files": ["src/search/api/SearchController.kt"],
      "node_ids": ["SearchAPI"]
    }
  ],
  "diagram_before": "flowchart LR\n  Client --> SearchAPI --> ES[(BM25 index)]",
  "diagram_after": "flowchart LR\n  Client --> SearchAPI\n  SearchAPI --> ES[(BM25 index)]\n  SearchAPI --> VEC[(Vector index)]\n  SearchAPI --> RRF[RRF fusion]",
  "changed_node_ids": ["SearchAPI", "VEC", "RRF"],
  "removed_node_ids": [],
  "hotspots": [
    {
      "rank": 1,
      "file": "src/search/fusion/Rrf.kt",
      "lines": "40-78",
      "risk": "high",
      "category": "correctness",
      "headline": "The two rank lists are fused on different scales",
      "why_it_matters": "BM25 ranks arrive 1-based but vector ranks start at 0, so every vector hit gets a small boost.",
      "snippet": "fun fuse(a: List<Hit>, b: List<Hit>, k: Int = 60): List<Hit> {\n  ...\n}",
      "snippet_format": "code",
      "focus_lines": [1]
    }
  ],
  "tour": [
    { "nodes": ["Client", "SearchAPI"], "text": "A search still enters through the Search API." },
    { "nodes": ["ES", "VEC"], "text": "It now asks both indexes at once." },
    { "nodes": ["RRF"], "text": "Reciprocal rank fusion merges the two lists." }
  ],
  "quiz": [
    {
      "setup": "The vector index takes 300 ms to answer.",
      "question": "What does the user get?",
      "options": ["An error page", "BM25 results only, after about 120 ms", "Fused results after 300 ms"],
      "answer": 1,
      "explain": "The client gives up after 120 ms and the fallback ranks BM25 hits alone.",
      "ref": "hotspot-2"
    }
  ],
  "intent_check": {
    "matches_ticket": true,
    "extras_not_requested": ["Refactored logging helper"],
    "possibly_missing": ["No feature flag for rollback"]
  },
  "test_evidence": {
    "tests_added": ["RrfTest.kt (6 cases)"],
    "weak_spots": ["No test for empty vector results"]
  },
  "reviewer_questions": ["What happens if the vector index times out?"],
  "low_risk_collapsed": ["12 files: formatting only"]
}
```

| Field | Rule |
|---|---|
| `tldr` | required, list of 1-3 strings (**error** over 3) |
| `overall_risk` | required, `low` / `medium` / `high` |
| `components` | required list. `id` uses letters, digits, `-`, `_` and is unique. `change_type` is `added` / `modified` / `removed` / `renamed`. `node_ids` (optional) are diagram node ids; selecting that box on the page opens this component. Each must be a box in either diagram (**error**): a word from a label or a subgraph id opens nothing. |
| `diagram_after` | required Mermaid **flowchart** (`flowchart LR` or `flowchart TD`). Keep it to about 12 nodes, and use `TD` for chains longer than five boxes: a wide chart shrinks its labels. Plain-text labels only: any tag other than `<br>` is an **error**, so write `<` and `>` as `#lt;` and `#gt;` (e.g. `List#lt;Hit#gt;`). A box label or edge text with `( ) { }` or a quote must be quoted (`A["Search (BM25)"]`, `-->|"calls (async)"|`), or Mermaid won't draw the diagram (**error**). Mermaid reads labels as Markdown: one starting with `1.`, `-`, `+`, `*`, `#` or `>`, or holding a backtick, `~~x~~` or a URL, draws as "Unsupported markdown" (**error**; write the character as Mermaid's code, e.g. `1#46; First`, `#95;#95;init#95;#95;`), and `__x__` or `*x*` loses its marks (warning). Edge text written `A -- text --> B` can't hold its own `--` (or `==`, `-.`) or a quote unless it's quoted, and the arrow must end with the line it started with (**error**); pipes (`A -->|text| B`) avoid both. A `--o` or `--x` head glued to a word draws an arrowhead on a stray box (`Client--overrides-->Store` gets a box `verrides`; **error**). A `%%` comment after a statement on the same line breaks the diagram (**error**), and so does a subgraph holding a node of its own id (`subgraph API` … `API --> B` … `end`; **error**). `click` statements and `%%{init}%%` directives are stripped. |
| `diagram_before` | optional flowchart of the same area before the change. Omit for brand-new systems. |
| `changed_node_ids` | ids from `diagram_after` that were added or changed. Simple ids only (`[A-Za-z][A-Za-z0-9_]*`), and each must appear in the diagram (**error**). Highlighted orange. |
| `removed_node_ids` | ids from `diagram_before` that no longer exist. Highlighted red, dashed. |
| `hotspots` | at most 3 (**error**), `risk` is `high` or `medium`. `headline` is required: what could break, in under 90 characters (it leads the card). `why_it_matters` is two or three sentences (a warning over 420 characters). `file` should be a changed file. `lines` is `"40-78"` or `"40"`, starting at the snippet's first line in the file at head: it links to GitHub at the exact commit, and it's how the page knows which lines a reader saw (a warning naming the line when the file doesn't hold the snippet there; a `...` line in the snippet stands for lines left out). `snippet` is at most 25 lines (**error**). `snippet_format` is `code` (default) or `diff` for a hunk with `+`/`-` lines. `focus_lines` (optional) are the snippet's line numbers to highlight, 1 being its first line (**error** outside the snippet). |
| `tour` | optional, 2-6 steps (**error** over 6; one step isn't shown). Each `{nodes, text}`: `nodes` are ids from `diagram_after` (**error** if missing) lit up for that step, `text` is one sentence. The page's "Take the tour" button walks through them. |
| `quiz` | optional, up to 5 scenarios (**error** over 5) for the page's "Predict it" game. Each `{setup, question, options, answer, explain, ref}`: 2-4 `options`, `answer` is the right one's index (**error** if out of range), `explain` says what happens and why, `ref` is required: `hotspot-N` or a component id, the code that shows the answer (**error** otherwise). The page shows `answer` as the reviewer's read, not as fact, so a reader whose pick differs always gets a link to the code that settles it. A warning if every right answer is in the same position. |
| `intent_check` | `matches_ticket` is `true`, `false` or `null` (couldn't tell). Lists of strings. |
| `test_evidence` | lists of strings |
| `reviewer_questions` | up to about 5; each becomes a checkbox in the reviewer's progress |
| `low_risk_collapsed` | one-line summaries of everything not worth a reviewer's time |
| `risk_heatmap` | accepted and ignored: the page draws its own map of changed files from git, with markers on the hotspots |

Text fields warn past a length where they stop being read and start being skimmed: a TL;DR
sentence over 200 characters, a component summary over 280, a list item over 220.

**Secrets:** every string in both files is scanned for keys, tokens, private keys, JWTs,
passwords in URLs and connection strings, Bearer/Basic auth tokens, Slack/Discord webhook
URLs, npm tokens, quoted values of 6+ characters assigned to any name containing a
credential word (`password`, `passphrase`, `pwd`, `secret`, `token`, `credential`,
`api_key`... so `DB_PASSWORD="..."` and `GITHUB_TOKEN` count, `tokenizer` doesn't),
unquoted `.env` / YAML values containing a digit, and random-looking values for names
ending in `key` (`ENCRYPTION_KEY`, but not `cache_key = "user:123"`). Any hit is an
**error**. Placeholders such as `<redacted>`, `***`, `${VAR}`, `{0}`, `your_api_key_here`,
type words like `string`, and file paths are allowed.

Diagram node ids must not be Mermaid keywords (`end`, `graph`, `subgraph`, `class`, `style`...).
