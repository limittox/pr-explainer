# PR Explainer data contract (v1)

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
  "approach": "The chosen approach in plain English.",
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
| `short_title` | required, 2-4 words, becomes the page `<title>`. Keep it unchanged on updates. |
| `ticket` | optional; an ID or a URL (URLs become links) |
| `goal`, `approach` | required |
| `alternatives_rejected` | list of `{option, why_not}` |
| `deliberately_not_done`, `author_flagged_risks`, `how_to_verify` | lists of strings |

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
      "why_it_matters": "The rank constant k controls fusion; an off-by-one in rank indexing silently degrades relevance.",
      "snippet": "fun fuse(a: List<Hit>, b: List<Hit>, k: Int = 60): List<Hit> {\n  ...\n}",
      "snippet_format": "code"
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
| `components` | required list. `id` uses letters, digits, `-`, `_` and is unique. `change_type` is `added` / `modified` / `removed` / `renamed`. `node_ids` (optional) are diagram node ids; selecting that box on the page opens this component. |
| `diagram_after` | required Mermaid **flowchart** (`flowchart LR` or `flowchart TD`). Keep it to roughly 15 nodes. Plain-text labels only: any tag other than `<br>` is an **error**, so write `<` and `>` as `#lt;` and `#gt;` (e.g. `List#lt;Hit#gt;`). `click` statements and `%%{init}%%` directives are stripped. |
| `diagram_before` | optional flowchart of the same area before the change. Omit for brand-new systems. |
| `changed_node_ids` | ids from `diagram_after` that were added or changed. Simple ids only (`[A-Za-z][A-Za-z0-9_]*`), and each must appear in the diagram (**error**). Highlighted orange. |
| `removed_node_ids` | ids from `diagram_before` that no longer exist. Highlighted red, dashed. |
| `hotspots` | at most 3 (**error**), `risk` is `high` or `medium`. `file` should be a changed file. `lines` is `"40-78"` or `"40"` and links to GitHub at the exact commit. `snippet` is at most 25 lines (**error**). `snippet_format` is `code` (default) or `diff` for a hunk with `+`/`-` lines. |
| `intent_check` | `matches_ticket` is `true`, `false` or `null` (couldn't tell). Lists of strings. |
| `test_evidence` | lists of strings |
| `reviewer_questions` | up to about 5 |
| `low_risk_collapsed` | one-line summaries of everything not worth a reviewer's time |
| `risk_heatmap` | accepted but not rendered yet (Phase 2) |

**Secrets:** every string in both files is scanned for keys, tokens, private keys, JWTs,
passwords in URLs, Bearer/Basic auth tokens, quoted values of 6+ characters assigned to any
name containing a credential word (`password`, `pwd`, `secret`, `token`, `credential`,
`api_key`... so `DB_PASSWORD="..."` and `GITHUB_TOKEN` count, `tokenizer` doesn't), and
unquoted `.env` / YAML values containing a digit. Any hit is an **error**. Placeholders
such as `<redacted>`, `***`, `${VAR}`, `example...`, type words like `string`, and file
paths are allowed.

Diagram node ids must not be Mermaid keywords (`end`, `graph`, `subgraph`, `class`, `style`...).
