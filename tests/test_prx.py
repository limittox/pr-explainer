"""Tests for the PR Explainer helper and hooks. Run: python -m unittest discover -s tests -v"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / ".claude" / "skills" / "pr-explainer"
HOOKS = ROOT / ".claude" / "hooks"
sys.path.insert(0, str(SKILL))
import prx  # noqa: E402

ENV = {**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@example.com",
       "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@example.com", "PYTHONUTF8": "1"}
URL = "https://claude.ai/code/artifact/0b5e7c2a-1111-4222-8333-944455556666"


def run(args, cwd, check=True, input=None):
    return subprocess.run(args, cwd=cwd, env=ENV, check=check, capture_output=True,
                          text=True, encoding="utf-8", input=input)


def prx_cli(cwd, *args):
    return run([sys.executable, str(SKILL / "prx.py"), *args], cwd, check=False)


def hook(name, payload, cwd=None):
    data = payload if isinstance(payload, str) else json.dumps(payload)
    return run([sys.executable, str(HOOKS / name)], cwd or ROOT, check=False, input=data)


class CommandMatching(unittest.TestCase):
    def test_pr_create(self):
        yes = ["gh pr create --title x", "git push && gh pr create -t x -b y", "cd repo; gh pr create",
               "& gh pr create", '& "C:\\Program Files\\GitHub CLI\\gh.exe" pr create -t x',
               "gh -R o/r pr create", "gh pr new", "GH_TOKEN=x gh pr create", "x=$(gh pr create)",
               "/usr/bin/gh pr create", "gh pr create --body \"$(cat <<'EOF'\nhi\nEOF\n)\"",
               "if x; then gh pr create; fi", "time gh pr create", "env FOO=1 gh pr create",
               "sudo -E gh pr create", "echo x | xargs -I{} gh pr create", "! gh pr create",
               'bash -c "gh pr create -t x"', "bash -lc 'cd r && gh pr create'",
               'pwsh -NoProfile -Command "gh pr create"', "powershell -c gh pr create",
               "cmd /c gh pr create", "wsl gh pr create", 'iex "gh pr create -t x"',
               "bash <<'EOF'\ncd repo\ngh pr create -t x\nEOF"]
        no = ['echo "gh pr create"', "gh pr view 1", "gh pr list", "grep 'gh pr create' notes.md",
              "gh issue create", "git commit -m 'run gh pr create later'"]
        yes += ["cat <<'EOF' | gh pr create --body-file -\nbody\nEOF",
                "cat > body.md <<'EOF'\nbody\nEOF\ngh pr create --body-file body.md"]
        no += ["git commit -F - <<'EOF'\nGate `gh pr create` on the explainer\ngh pr create is gated\nEOF",
               "git commit -m @'\ngh pr create is gated\n'@"]
        for c in yes:
            self.assertTrue(prx.is_pr_create(c), c)
        for c in no:
            self.assertFalse(prx.is_pr_create(c), c)

    def test_push(self):
        yes = ["git push", "git push -u origin feat", "git -C repo push", "cd x && git push origin HEAD",
               "& git push", "git --no-pager push"]
        no = ["git push --dry-run", "git push -n origin x", "git push origin --delete feat",
              "git push origin :feat", "echo git push", "git stash push", "git pushx",
              "git commit -F - <<'EOF'\ngit push refreshes the explainer\nEOF"]
        for c in yes:
            self.assertTrue(prx.is_refreshing_push(c), c)
        for c in no:
            self.assertFalse(prx.is_refreshing_push(c), c)

    def test_branch_key(self):
        self.assertEqual(prx.branch_key("plain-name_1.2"), "plain-name_1.2")
        self.assertRegex(prx.branch_key("feature/search/rrf"), r"^feature__search__rrf-[0-9a-f]{6}$")
        self.assertRegex(prx.branch_key('odd"name|x'), r"^odd_name_x-[0-9a-f]{6}$")
        self.assertNotEqual(prx.branch_key("fix/login"), prx.branch_key("fix__login"))
        self.assertNotEqual(prx.branch_key("Feature-x").lower(), prx.branch_key("feature-x").lower())


class SecretScan(unittest.TestCase):
    def hits(self, text):
        rep = prx.Report()
        prx.scan_secrets({"s": text}, "x", rep)
        return rep.errors

    def test_flags_real_looking_secrets(self):
        for text in ["AKIAABCDEFGHIJKLMNOP", "ghp_" + "a" * 36, "-----BEGIN RSA PRIVATE KEY-----",
                     'password = "hunter2hunter2"', "postgres://app:s3cretpass@db/prod",
                     '"api_key": "abcd1234efgh5678"', 'DB_PASSWORD="hunter2hunter2"',
                     'stripeApiKey: "abcd1234efgh5678"', "AWS_SECRET_ACCESS_KEY=abcd1234efgh5678ijkl",
                     "db:\n  password: s3cretvalue123", "export API_KEY=abcd1234efgh5678",
                     "Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456", "GITHUB_TOKEN=abcd1234efgh5678",
                     '{"token": "abcdef123456"}', 'refresh_token = "r3fr3sh-me"', 'password = "hunter2"',
                     'pwd: "s3cret!"', 'credentials = "abc123def456"']:
            self.assertTrue(self.hits(text), text)

    def test_allows_placeholders_and_references(self):
        for text in ['password = "<redacted>"', 'api_key: "${API_KEY}"', "postgres://app:<redacted>@db/prod",
                     "the password field is validated", 'token = "example-token-value"',
                     'password = request.form["password"]', 'api_key = os.environ["API_KEY"]',
                     "DB_PASSWORD=${DB_PASSWORD}", "password: settings.db_password",
                     "client_secret: changeme", "Authorization: Bearer ${TOKEN}",
                     'tokenizer = "bert-base-uncased"', 'credentials_file: "service-account.json"',
                     'token_type: "Bearer"', 'password: "string"', 'key_path = "~/.ssh/id_rsa"']:
            self.assertEqual(self.hits(text), [], text)


class DiagramCleaning(unittest.TestCase):
    def clean(self, text):
        rep = prx.Report()
        return prx.clean_diagram(text, "d", rep), rep

    def test_strips_click_anywhere(self):
        out, rep = self.clean('flowchart LR\n  A-->B; click A href "https://evil.example"\n  click B call x()')
        self.assertNotIn("click", out)
        self.assertIn("A-->B", out)
        self.assertEqual(rep.errors, [])

    def test_rejects_tags_but_allows_br(self):
        for label in ["<img src=x>", "<style>body{display:none}</style>", "<a href=https://x.example>x</a>",
                      "List<Hit>"]:
            _, rep = self.clean(f'flowchart LR\n  A["{label}"]')
            self.assertTrue(rep.errors, label)
        _, rep = self.clean('flowchart LR\n  A["two<br>lines"] --> B["List#lt;Hit#gt;"] <--> C')
        self.assertEqual(rep.errors, [])

    def test_rejects_mermaid_keywords_as_ids(self):
        rep = prx.Report()
        prx._node_ids(rep, {"changed_node_ids": ["end", "Api"]}, "changed_node_ids", "flowchart LR\n  Api --> end", "x")
        self.assertEqual(len(rep.errors), 1)
        self.assertIn("Mermaid keyword", rep.errors[0])


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="prx-test-")
        d = Path(self.dir)
        run(["git", "init", "-q", "-b", "main"], d)
        run(["git", "config", "user.name", "Test Author"], d)
        self.write("src/search/api/SearchController.kt", "class SearchController\n")
        self.write("src/search/legacy/SynonymBoost.kt", "object SynonymBoost\n")
        self.commit("base")
        run(["git", "checkout", "-q", "-b", "feature/hybrid-rrf"], d)
        self.write("src/search/api/SearchController.kt", "class SearchController {\n  // hybrid\n}\n")
        self.write("src/search/fusion/Rrf.kt", "fun fuse() = Unit\n" * 30)
        self.write("src/search/fusion/RrfTest.kt", "class RrfTest\n")
        self.write("src/search/vector/VectorClient.kt", "class VectorClient\n" * 25)
        self.write("package-lock.json", "{}\n")
        (d / "src/search/legacy/SynonymBoost.kt").unlink()
        self.commit("hybrid ranking")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, rel, text):
        p = Path(self.dir) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def commit(self, msg):
        run(["git", "add", "-A"], self.dir)
        run(["git", "commit", "-q", "-m", msg], self.dir)

    def state(self, suffix, branch="feature/hybrid-rrf"):
        return Path(self.dir) / ".git" / "pr-explainer" / f"{prx.branch_key(branch)}.{suffix}"

    def prepare_with_examples(self, explainer=None):
        r = prx_cli(self.dir, "prepare", "--base", "main", "--no-fetch")
        self.assertEqual(r.returncode, 0, r.stderr)
        shutil.copy(SKILL / "examples" / "intent.json", self.state("intent.json"))
        if explainer is None:
            shutil.copy(SKILL / "examples" / "explainer.json", self.state("explainer.json"))
        else:
            self.state("explainer.json").write_text(json.dumps(explainer), encoding="utf-8")
        return json.loads(r.stdout)

    def test_full_flow(self):
        self.assertIn("No PR explainer", prx.gate_problem("gh pr create -t x", self.dir))
        out = self.prepare_with_examples()
        self.assertEqual(out["mode"], "create")
        ctx = json.loads(self.state("context.json").read_text(encoding="utf-8"))
        self.assertEqual(ctx["stats"]["files_changed"], 6)
        self.assertEqual([f["path"] for f in ctx["excluded_files"]], ["package-lock.json"])
        self.assertIn(self.state("explainer.json").name, self.state("reviewer-prompt.md").read_text(encoding="utf-8"))

        r = prx_cli(self.dir, "render")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        page = self.state("html").read_text(encoding="utf-8")
        head = run(["git", "rev-parse", "HEAD"], self.dir).stdout.strip()
        self.assertIn("<title>Hybrid RRF ranking</title>", page)
        self.assertIn(head[:7], page)
        self.assertIn("class SearchAPI,VEC,RRF prxChanged", page)
        self.assertIn("class SYN prxRemoved", page)
        self.assertIn("List&lt;Hit&gt;", page)
        self.assertNotIn("{{BODY}}", page)
        self.assertNotIn("<!doctype", page.lower())

        self.assertEqual(prx_cli(self.dir, "record", URL).returncode, 0)
        self.assertIn(URL, self.state("pr-body.md").read_text(encoding="utf-8"))
        self.assertIn("Put the PR explainer link", prx.gate_problem("gh pr create -t x -b hi", self.dir))
        self.assertIsNone(prx.gate_problem(f"gh pr create -t x -b '{URL}'", self.dir))
        self.assertIsNone(prx.gate_problem(f'gh pr create -t x --body-file "{self.state("pr-body.md")}"', self.dir))
        self.assertEqual(prx_cli(self.dir, "record", URL.replace("0b5e", "ffff")).returncode, 1)
        self.assertIn("No PR explainer for branch 'main'", prx.gate_problem(f"gh pr create --head main -b {URL}", self.dir))
        self.assertIn("prepared against 'main'", prx.gate_problem(f"gh pr create --base develop -b {URL}", self.dir))
        self.assertIsNone(prx.gate_problem(f"gh pr create --base main -b {URL} && curl -H x y", self.dir))
        self.assertIn("Put the PR explainer link", prx.gate_problem(f"gh pr create -t 'see {URL}' -b hi", self.dir))

        # The gate hook end to end, from the hook's own cwd field
        self.assertEqual(hook("pr_create_gate.py", {"cwd": self.dir, "tool_name": "Bash",
                              "tool_input": {"command": "gh pr create -t x"}}).returncode, 2)
        self.assertEqual(hook("pr_create_gate.py", {"cwd": self.dir, "tool_name": "PowerShell",
                              "tool_input": {"command": f"gh pr create -t x -b '{URL}'"}}).returncode, 0)

        # Already fresh: pushing doesn't ask for a refresh
        self.assertEqual(hook("pr_push_refresh.py", {"cwd": self.dir, "tool_input": {"command": "git push"}}).stdout, "")

        # A new commit makes the explainer stale
        self.write("src/search/fusion/Rrf.kt", "fun fuse() = 1\n")
        self.commit("fix rank")
        self.assertIn("update mode", prx.gate_problem(f"gh pr create -b {URL}", self.dir))
        r = hook("pr_push_refresh.py", {"cwd": self.dir, "tool_input": {"command": "git push -u origin HEAD"}})
        self.assertEqual(json.loads(r.stdout)["decision"], "block")
        self.assertIn(URL, r.stdout)
        self.assertEqual(hook("pr_push_refresh.py", {"cwd": self.dir, "tool_input": {"command": "git push --dry-run"}}).stdout, "")
        r = prx_cli(self.dir, "render")
        self.assertIn("HEAD moved", r.stderr)
        # A new prepare starts a new review: the old analysis can't be rendered under the new SHA
        self.assertEqual(prx_cli(self.dir, "prepare", "--base", "main", "--no-fetch").returncode, 0)
        self.assertTrue(self.state("explainer.previous.json").exists())
        r = prx_cli(self.dir, "render")
        self.assertEqual(r.returncode, 1)
        self.assertIn("explainer.json not found", r.stderr)

    def test_validation_errors(self):
        ex = json.loads((SKILL / "examples" / "explainer.json").read_text(encoding="utf-8"))
        ex["hotspots"] = ex["hotspots"] + [dict(ex["hotspots"][2], rank=4)]
        ex["hotspots"][0]["snippet"] = "x\n" * 30
        ex["changed_node_ids"] = ["SearchAPI", "Nope", "bad-id"]
        ex["tldr"].append("A fourth sentence.")
        ex["components"][0]["summary"] = 'uses password = "hunter2hunter2"'
        ex["commit_sha"] = "deadbee"
        self.prepare_with_examples(ex)
        r = prx_cli(self.dir, "validate", "--only", "explainer")
        self.assertEqual(r.returncode, 1)
        for needle in ["keep the top 3", "keep it to 25", "'Nope' does not appear", "'bad-id' must be a simple",
                       "keep it to 3", "credential assignment", "commit_sha: ignored"]:
            self.assertIn(needle, r.stdout)
        self.assertFalse(self.state("html").exists())

    def test_record_writes_nothing_if_explainer_is_broken(self):
        self.prepare_with_examples()
        self.assertEqual(prx_cli(self.dir, "render").returncode, 0)
        self.state("explainer.json").write_text("{ not json", encoding="utf-8")
        r = prx_cli(self.dir, "record", URL)
        self.assertEqual(r.returncode, 1)
        self.assertFalse(self.state("url").exists())
        self.assertFalse(self.state("sha").exists())

    def test_record_refuses_errors_and_changes_after_render(self):
        self.prepare_with_examples()
        self.assertEqual(prx_cli(self.dir, "render").returncode, 0)
        ex = json.loads(self.state("explainer.json").read_text(encoding="utf-8"))
        ex["tldr"] = ['Sets DB_PASSWORD="hunter2hunter2" in prod']
        self.state("explainer.json").write_text(json.dumps(ex), encoding="utf-8")
        r = prx_cli(self.dir, "record", URL)
        self.assertEqual(r.returncode, 1)
        self.assertIn("has errors", r.stderr)
        self.assertFalse(self.state("pr-body.md").exists())
        ex["tldr"] = ["A harmless edit made after rendering."]
        self.state("explainer.json").write_text(json.dumps(ex), encoding="utf-8")
        r = prx_cli(self.dir, "record", URL)
        self.assertEqual(r.returncode, 1)
        self.assertIn("changed after the last render", r.stderr)
        self.assertFalse(self.state("url").exists())

    def test_untrusted_text_is_escaped(self):
        ex = json.loads((SKILL / "examples" / "explainer.json").read_text(encoding="utf-8"))
        ex["tldr"] = ["</script><script>alert(1)</script>"]
        ex["components"][0]["name"] = "<img src=x onerror=alert(1)>"
        original = ex["diagram_after"]
        ex["diagram_after"] = original + '\n  X["<a href=javascript:alert(1)>x</a>"]'
        self.prepare_with_examples(ex)
        r = prx_cli(self.dir, "render")
        self.assertEqual(r.returncode, 1, "HTML in a diagram label must be rejected")
        ex["diagram_after"] = original + '\n  click SearchAPI href "javascript:alert(1)"\n%%{init: {"securityLevel": "loose"}}%%'
        self.state("explainer.json").write_text(json.dumps(ex), encoding="utf-8")
        r = prx_cli(self.dir, "render")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        page = self.state("html").read_text(encoding="utf-8")
        self.assertNotIn("<script>alert", page)
        self.assertNotIn("<img src=x", page)
        self.assertNotIn("click SearchAPI", page)
        self.assertNotIn("securityLevel", page)
        self.assertIn("&lt;/script&gt;", page)


class HookEdges(unittest.TestCase):
    def test_other_commands_pass(self):
        self.assertEqual(hook("pr_create_gate.py", {"cwd": str(ROOT), "tool_input": {"command": "ls -la"}}).returncode, 0)

    def test_pr_create_outside_a_repo_blocks(self):
        d = tempfile.mkdtemp(prefix="prx-norepo-")
        try:
            r = hook("pr_create_gate.py", {"cwd": d, "tool_input": {"command": "gh pr create"}}, cwd=d)
            self.assertEqual(r.returncode, 2)
            self.assertIn("couldn't check", r.stderr)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_unreadable_input(self):
        self.assertEqual(hook("pr_create_gate.py", "not json gh pr create").returncode, 2)
        self.assertEqual(hook("pr_create_gate.py", "not json ls").returncode, 0)
        self.assertEqual(hook("pr_push_refresh.py", "not json").returncode, 0)

    def test_bom_prefixed_input(self):
        payload = "\ufeff" + json.dumps({"cwd": str(ROOT), "tool_input": {"command": "ls"}})
        self.assertEqual(hook("pr_create_gate.py", payload).returncode, 0)


if __name__ == "__main__":
    unittest.main()
