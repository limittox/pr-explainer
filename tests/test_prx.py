"""Tests for the PR Explainer helper and hooks. Run: python -m unittest discover -s tests -v"""
import base64
import importlib.util
import json
import shlex
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

os.environ["PRX_NO_GITHUB"] = "1"  # no gh calls from throwaway repos; finished_pr has its own test
ENV = {**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@example.com",
       "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@example.com", "PYTHONUTF8": "1"}
URL = "https://claude.ai/code/artifact/0b5e7c2a-1111-4222-8333-944455556666"
PS = "PowerShell"


def run(args, cwd, check=True, input=None):
    return subprocess.run(args, cwd=cwd, env=ENV, check=check, capture_output=True,
                          text=True, encoding="utf-8", input=input)


def prx_cli(cwd, *args):
    return run([sys.executable, str(SKILL / "prx.py"), *args], cwd, check=False)


def hook(name, payload, cwd=None):
    data = payload if isinstance(payload, str) else json.dumps(payload)
    return run([sys.executable, str(HOOKS / name)], cwd or ROOT, check=False, input=data)


def encoded(script):
    return base64.b64encode(script.encode("utf-16-le")).decode()


def pr_create_cases():
    """(should match, should not match) for gh pr create; each case is a command or (command, tool)."""
    yes = [
        "gh pr create --title x", "git push && gh pr create -t x -b y", "cd repo; gh pr create",
        "gh -R o/r pr create", "gh pr new", "GH_TOKEN=x gh pr create", "x=$(gh pr create)",
        'x="$(gh pr create -t x)"', "/usr/bin/gh pr create", '"gh" pr create',
        "gh pr create --body \"$(cat <<'EOF'\nhi\nEOF\n)\"",
        # keywords and wrappers
        "if x; then gh pr create; fi", "time gh pr create", "env FOO=1 gh pr create", "sudo -E gh pr create",
        "sudo -u bob gh pr create", "nice -n 5 gh pr create", "timeout 60 gh pr create", "! gh pr create",
        "echo x | xargs -I{} gh pr create", "{ gh pr create; }", "cmd /c gh pr create", "wsl gh pr create",
        'eval "gh pr create"', "env -S 'gh pr create -t x'", "env - gh pr create", "env --uns FOO gh pr create",
        "sudo -iu bob gh pr create", "cd x & gh pr create",
        # shells given a script
        'bash -c "gh pr create -t x"', "bash -lc 'cd r && gh pr create'", "bash -o pipefail -c 'gh pr create'",
        "bash <<'EOF'\ncd repo\ngh pr create -t x\nEOF", "cat <<'EOF' | bash\ngh pr create -t x\nEOF",
        'echo "gh pr create -t x" | bash', "cat <<'EOF' | gh pr create --body-file -\nbody\nEOF",
        "cat > body.md <<'EOF'\nbody\nEOF\ngh pr create --body-file body.md",
        "echo x # a comment\ngh pr create",
        'git commit -m "Gate `gh pr create`"',  # bash runs backticks inside double quotes
        # a heredoc marker inside a quote or comment isn't a heredoc, so it can't hide later lines
        "# cat <<EOF writes the body\ngh pr create -t x", "git commit -m 'explain <<EOF'\ngh pr create -t x",
        'echo "use <<EOF"\ngh pr create', "git commit -F - <<'EOF'\nbody\nEOF\ngh pr create",
        # line continuations
        "gh pr create \\\n  --title x \\\n  --body-file body.md", "gh \\\n  pr create",
        ("gh pr create `\n  --title x", PS), ("gh `\n  pr create", PS),
        # PowerShell assignments and script blocks
        ("$r = gh pr create -t x", PS), ("$null = gh pr create", PS), ("$r=gh pr create", PS),
        ("[void](gh pr create)", PS), ("try { gh pr create } catch {}", PS),
        ("1 | ForEach-Object { gh pr create }", PS), ("Invoke-Command { gh pr create }", PS),
        ('Write-Output "<<EOF"\ngh pr create', PS),
        # `#` mid-word isn't a comment; << in arithmetic is a shift, not a heredoc
        "n=${#files[@]}; gh pr create -t x -b hi", "x=$(date)#tag; gh pr create", "echo {#}; gh pr create",
        "mask=$((1<<bits))\ngh pr create -t x", "(( x = 1 << 2 ))\ngh pr create",
        # unquoted heredocs and @"..."@ strings run their substitutions
        "cat <<EOF\n$(gh pr create -t x)\nEOF", ('$s = @"\n$(gh pr create)\n"@', PS),
        'cat <<E"OF"\nbody\nEOF\ngh pr create',  # a partly quoted delimiter still ends the body
        "cat <<NEVER\ngh pr create",  # an unterminated heredoc is checked as commands too
        # quotes and escapes the shell drops while building words
        "gh p''r create", 'gh "p"r create', "gh p\\r create", ("gh p`r create", PS), "g''h pr cre''ate",
        "gh p\\\nr create -t x", ("gh p`\nr create", PS),  # a line continuation inside the word
        # backstop: gh pr create anywhere in a command's words, behind wrappers the parser doesn't model
        "find . -exec gh pr create -t x \\;", "setsid gh pr create", "coproc gh pr create",
        "function f { gh pr create; }; f", "echo x | parallel gh pr create", "echo gh pr create",
        # powershell.exe switches written with a slash, and a payload with a stray byte
        ('powershell /c "gh pr create -t x"', PS), (f"powershell /ec {encoded('gh pr create -t x')}", PS),
        (f"pwsh /EncodedCommand {encoded('gh pr create')}", PS),
        ("pwsh -EncodedCommand " + base64.b64encode("gh pr create -t x\n".encode("utf-16-le") + b"A").decode(), PS),
        # any abbreviation of -Command / -EncodedCommand, as PowerShell accepts
        (f"powershell -en {encoded('gh pr create -t x')}", PS), (f"powershell /en {encoded('gh pr create')}", PS),
        ("pwsh -co 'gh pr create -t x'", PS), ("powershell -comm 'gh pr create'", PS),
        # value-taking switches, abbreviated too, so their value isn't read as a script file
        ("pwsh -exec Bypass -c 'gh pr create -t x'", PS), "pwsh -exec Bypass -c 'gh pr create -t x'",
        ("powershell -ExecutionP Bypass -win hidden -c 'gh pr create'", PS),
        ("pwsh -o text -con x -c 'gh pr create'", PS),
        # PowerShell
        ("& gh pr create", PS), ('& "C:\\Program Files\\GitHub CLI\\gh.exe" pr create -t x', PS),
        ('pwsh -NoProfile -Command "gh pr create"', PS), ("powershell -c gh pr create", PS),
        ('powershell -ExecutionPolicy Bypass -Command "gh pr create"', PS),
        (f"pwsh -NoProfile -EncodedCommand {encoded('gh pr create -t x')}", PS),
        ('iex "gh pr create -t x"', PS), ('"gh pr create -t x" | Invoke-Expression', PS),
        ("if ($ok) { gh pr create }", PS),
    ]
    no = [
        'echo "gh pr create"', "gh pr view 1", "gh pr list", "gh issue create",
        "grep 'gh pr create' notes.md", "rg -n 'gh pr create|gh pr new' .",
        "git commit -m 'run gh pr create later'", 'git commit -m "Gate PRs; gh pr create now needs a link"',
        "git commit -m 'one\ngh pr create is gated'", "gh issue create -b '(gh pr create fails)'",
        "git commit -m 'Gate `gh pr create`'",  # single quotes: no substitution
        "echo hi # gh pr create",
        "git commit -F - <<'EOF'\nGate `gh pr create` on the explainer\ngh pr create is gated\nEOF",
        "python - <<'EOF'\nprint('gh pr create')\nEOF",
        ("git commit -m @'\ngh pr create is gated\n'@", PS), ("Write-Output 'gh pr create'", PS),
        "cat <<< 'gh pr create'", ("<# gh pr create #>", PS), ("$msg = 'gh pr create'", PS),
        "(echo hi)# gh pr create", "cat <<'EOF'\n$(gh pr create)\nEOF", 'cat <<E"OF"\n$(gh pr create)\nEOF',
        ("$s = @'\n$(gh pr create)\n'@", PS), "rg __PRX_DOC0__ .", "echo $((2#101))",
    ]
    return yes, no


def push_cases():
    """(should match, should not match) for a git push that sends commits."""
    yes = ["git push", "git push -u origin feat", "git -C repo push", "git --git-dir .git push",
           "cd x && git push origin HEAD", "git --no-pager push", 'bash -c "git push"', ("& git push", PS),
           "git \\\n  push origin feat", ("$out = git push", PS), "# note <<EOF\ngit push",
           "git pu''sh", "git pu\\\nsh", ("git pu`\nsh", PS)]
    no = ["git push --dry-run", "git \\\n  push --dry-run", "git push -n origin x", "git push origin --delete feat",
          "git push origin :feat", "echo git push", "git stash push", "git pushx", "rg 'git push' .",
          "git commit -F - <<'EOF'\ngit push refreshes the explainer\nEOF"]
    return yes, no


class CommandMatching(unittest.TestCase):
    def check(self, fn, yes, no):
        for case in yes:
            cmd, tool = case if isinstance(case, tuple) else (case, "Bash")
            self.assertTrue(fn(cmd, tool), f"{tool}: {cmd!r}")
        for case in no:
            cmd, tool = case if isinstance(case, tuple) else (case, "Bash")
            self.assertFalse(fn(cmd, tool), f"{tool}: {cmd!r}")

    def test_pr_create(self):
        yes, no = pr_create_cases()
        self.check(prx.is_pr_create, yes, no)

    def test_push(self):
        yes, no = push_cases()
        self.check(prx.is_refreshing_push, yes, no)

    def test_unparseable_command_fails_closed(self):
        self.assertTrue(prx.is_pr_create("gh pr create -t 'unclosed"))
        self.assertIn("couldn't parse", prx.gate_problem("gh pr create -t 'unclosed", str(ROOT)))
        self.assertFalse(prx.is_pr_create("echo 'unclosed"))
        # Nesting deeper than the parser follows is "can't tell", not "no PR here"
        self.assertTrue(prx.is_pr_create("$(" * 600 + "gh pr create" + ")" * 600))
        nested = "gh pr create"
        for _ in range(7):
            nested = f"bash -c {shlex.quote(nested)}"
        self.assertTrue(prx.is_pr_create(nested))

    def test_fallback_sees_through_quote_splitting(self):
        # Bash runs line 1 before it reaches the unclosed quote that breaks the parser.
        for cmd in ["gh p''r create -t x\necho 'unclosed", 'gh "p"r create -t x\necho "unclosed',
                    "gh p\\r create -t x\necho 'unclosed", "$(" * 600 + "gh p''r create" + ")" * 600]:
            self.assertTrue(prx.is_pr_create(cmd), cmd)
            self.assertIn("couldn't parse", prx.gate_problem(cmd, str(ROOT)), cmd)

    def test_parser_crash_fails_closed(self):
        real = prx.cmdparse.commands
        prx.cmdparse.commands = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("parser bug"))
        try:
            self.assertTrue(prx.is_pr_create("gh pr create -t x"))
            self.assertIn("couldn't parse", prx.gate_problem("gh pr create -t x", str(ROOT)))
            self.assertFalse(prx.is_pr_create("ls -la"))
        finally:
            prx.cmdparse.commands = real

    def test_placeholder_lookalikes_are_just_text(self):
        self.assertFalse(prx.is_pr_create("rg __PRX_DOC0__ ."))
        args = prx.gh_pr_create_args(prx.parse_commands("gh pr create -t __PRX_SUB7__ -b hi")[0])
        self.assertEqual(args, ["-t", "__PRX_SUB7__", "-b", "hi"])

    def test_commands_know_their_shell_and_directory(self):
        def flags(cmd, shell="bash"):
            return [(c.argv[0], c.nested, c.early, c.chdir) for c in prx.cmdparse.commands(cmd, shell)]
        self.assertEqual(flags("cd x && gh pr create"), [("cd", False, False, ()), ("gh", False, False, ())])
        self.assertEqual(flags("(cd x); gh pr create"), [("cd", True, False, ()), ("gh", False, False, ())])
        self.assertEqual(flags("bash -c 'cd x'"), [("cd", True, False, ())])
        self.assertEqual(flags('echo "$(cd x)"'), [("cd", True, True, ()), ("echo", False, False, ())])
        self.assertEqual(flags("cd x | cat"), [("cd", True, False, ()), ("cat", True, False, ())])
        self.assertEqual(flags("eval 'cd x'"), [("cd", False, False, ())])  # eval runs in this shell
        # A background job runs in a subshell; && and a PowerShell call operator don't
        self.assertEqual(flags("cd x & gh pr create"), [("cd", True, False, ()), ("gh", False, False, ())])
        self.assertEqual(flags("cd x && gh pr create")[0][1], False)
        self.assertEqual(flags("cd x; & gh pr create", "powershell"), [("cd", False, False, ()), ("gh", False, False, ())])
        # PowerShell runs $(...) and pipelines in the current runspace
        self.assertEqual(flags('Write-Host "$(cd x)"', "powershell")[0], ("cd", False, True, ()))
        self.assertEqual(flags("cd x | Out-Null", "powershell")[0][1], False)
        # Wrappers that change directory, however their options are spelled, outermost first
        for cmd in ["env -C d gh pr create", "env -Cd gh pr create", "env -iC d gh pr create", "env -u FOO -C d gh pr create",
                    "env --chdir=d gh pr create", "env --ch d gh pr create", "sudo -D d gh pr create",
                    "sudo --chdir=d gh pr create", "wsl --cd d gh pr create", "env - -C d gh pr create"]:
            self.assertEqual(flags(cmd), [("gh", False, False, ("d",))], cmd)
        self.assertEqual(flags("env -C a env -C b gh pr create"), [("gh", False, False, ("a", "b"))])
        self.assertEqual(flags("env -S 'gh pr create' -C d"), [("gh", False, False, ("d",))])
        for cmd in ["pwsh -wd d -c 'gh pr create'", "pwsh -WorkingDirectory:d -c 'gh pr create'",
                    "pwsh -wd:d -Command:'gh pr create'"]:
            self.assertEqual(flags(cmd, "powershell"), [("gh", True, False, ("d",))], cmd)
        self.assertEqual(flags("env -C a pwsh -wd b -c 'gh pr create'"), [("gh", True, False, ("a", "b"))])

    def test_gh_options_follow_pflag(self):
        opts = prx.gh_create_options
        self.assertEqual(opts(["-Bdevelop", "-Hfeat"]), {"base": "develop", "head": "feat"})
        self.assertEqual(opts(["-b=x"]), {"body": "x"})
        self.assertEqual(opts(["-dB", "develop"]), {"base": "develop"})  # boolean -d, then -B's value
        self.assertEqual(opts(["-b", "A", "--body", "B"]), {"body": "B"})  # the last value wins
        self.assertEqual(opts(["--body-file=f.md", "-w"]), {"body-file": "f.md"})
        self.assertEqual(opts(["-t", "-b", "-b", "real"]), {"title": "-b", "body": "real"})  # a value can look like a flag
        self.assertEqual(opts(["-d=FALSE", "-b", "x"]), {"body": "x"})  # =FALSE is -d's value, not -F ALSE

    def test_branch_key(self):
        self.assertEqual(prx.branch_key("plain-name_1.2"), "plain-name_1.2")
        self.assertRegex(prx.branch_key("feature/search/rrf"), r"^feature__search__rrf-[0-9a-f]{6}$")
        self.assertRegex(prx.branch_key('odd"name|x'), r"^odd_name_x-[0-9a-f]{6}$")
        self.assertNotEqual(prx.branch_key("fix/login"), prx.branch_key("fix__login"))
        self.assertNotEqual(prx.branch_key("Feature-x").lower(), prx.branch_key("feature-x").lower())


class Validation(unittest.TestCase):
    CTX = {"review_files": [], "excluded_files": []}

    def example(self, name):
        return json.loads((SKILL / "examples" / name).read_text(encoding="utf-8"))

    def test_wrong_types_are_reported_not_crashes(self):
        for key, value in [("components", 5), ("components", "x"), ("hotspots", {"a": 1})]:
            ex = self.example("explainer.json")
            ex[key] = value
            rep = prx.Report()
            prx.validate_explainer(ex, self.CTX, rep)
            self.assertTrue(any(e.startswith(f"explainer.{key}:") for e in rep.errors), (key, value, rep.errors))
        it = self.example("intent.json")
        it["alternatives_rejected"] = 5
        rep = prx.Report()
        prx.validate_intent(it, rep)
        self.assertIn("intent.alternatives_rejected: must be a list", rep.errors)

    def test_blank_required_fields_count_as_missing(self):
        ex = self.example("explainer.json")
        ex["diagram_after"] = "   "
        rep = prx.Report()
        prx.validate_explainer(ex, self.CTX, rep)
        self.assertIn("explainer.diagram_after: is required", rep.errors)
        it = self.example("intent.json")
        it["goal"] = "\n\t "
        rep = prx.Report()
        prx.validate_intent(it, rep)
        self.assertIn("intent.goal: is required", rep.errors)


class SecretScan(unittest.TestCase):
    def hits(self, text):
        rep = prx.Report()
        prx.scan_secrets({"s": text}, "x", rep)
        return rep.errors

    def test_flags_real_looking_secrets(self):
        for text in [
            "AKIAABCDEFGHIJKLMNOP", "ghp_" + "a" * 36, "-----BEGIN RSA PRIVATE KEY-----", "npm_" + "a1" * 18,
            'password = "hunter2hunter2"', 'password = "hunter2"', "postgres://app:s3cretpass@db/prod",
            '"api_key": "abcd1234efgh5678"', 'DB_PASSWORD="hunter2hunter2"', 'stripeApiKey: "abcd1234efgh5678"',
            "AWS_SECRET_ACCESS_KEY=abcd1234efgh5678ijkl", "db:\n  password: s3cretvalue123",
            "export API_KEY=abcd1234efgh5678", "GITHUB_TOKEN=abcd1234efgh5678", '{"token": "abcdef123456"}',
            'refresh_token = "r3fr3sh-me"', 'pwd: "s3cret!"', 'credentials = "abc123def456"',
            "Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456", "Authorization: Basic dXNlcjpwYXNz",
            # names ending in key, if the value looks like key material
            "ENCRYPTION_KEY=3q2+7w8kL0aZx9Yb1cD4eF", 'jwtSigningKey: "c2VjcmV0LXNpZ25pbmcta2V5LTEyMzQ1"',
            'SESSION_KEY = "f3a9c1e7b2d84f6a"', "client_key: 9f86d081884c7d659a2feaa0c55ad015",
            'passphrase = "correct horse battery staple"', "Server=db;User Id=sa;Password=Sup3rS3cret!;",
            # Built at runtime: a literal webhook URL here trips GitHub push protection.
            "https://hooks.slack.com/services/" + "/".join(["T01ABCD2E", "B01ABCD2E", "abcdEFGHijklMNOPqrstUVWX"]),
            "//registry.npmjs.org/:_authToken=abcd1234efgh5678",
            'password = "test_sk_live_abcdef123456"',  # a "test" prefix doesn't excuse a value
            'password = "test1234"', 'DB_PASSWORD="dummy2024"',
        ]:
            self.assertTrue(self.hits(text), text)

    def test_allows_placeholders_and_references(self):
        for text in [
            'password = "<redacted>"', 'api_key: "${API_KEY}"', "postgres://app:<redacted>@db/prod",
            "the password field is validated", 'token = "example-token-value"', 'api_key = "your_api_key_here"',
            'password = request.form["password"]', 'api_key = os.environ["API_KEY"]', "DB_PASSWORD=${DB_PASSWORD}",
            "password: settings.db_password", "client_secret: changeme", "Authorization: Bearer ${TOKEN}",
            'tokenizer = "bert-base-uncased"', 'credentials_file: "service-account.json"', 'token_type: "Bearer"',
            'password: "string"', 'key_path = "~/.ssh/id_rsa"', 'cache_key = "user:123:profile"',
            'sort_key: "created_at"', 'partition_key = "tenant_id_and_region"', "Password={0};",
            "a basic understanding of the auth flow", 'password = "$DB_PASSWORD"',
        ]:
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

    def test_strips_directives_anywhere(self):
        out, rep = self.clean('flowchart LR\n  A --> B %%{init: {"theme": "forest"}}%%\n  %%{init:\n {"x": 1}}%%\n  C --> D')
        self.assertNotIn("%%{", out)
        self.assertIn("A --> B", out)
        self.assertIn("C --> D", out)
        self.assertTrue(rep.warnings)

    def test_rejects_mermaid_keywords_as_ids(self):
        rep = prx.Report()
        prx._node_ids(rep, {"changed_node_ids": ["end", "Api"]}, "changed_node_ids", "flowchart LR\n  Api --> end", "x")
        self.assertEqual(len(rep.errors), 1)
        self.assertIn("Mermaid keyword", rep.errors[0])


class EndToEnd(unittest.TestCase):
    BRANCH = "feature/hybrid-rrf"

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="prx-test-")
        d = Path(self.dir)
        run(["git", "init", "-q", "-b", "main"], d)
        run(["git", "config", "user.name", "Test Author"], d)
        self.write("src/search/api/SearchController.kt", "class SearchController\n")
        self.write("src/search/legacy/SynonymBoost.kt", "object SynonymBoost\n")
        self.commit("base")
        run(["git", "checkout", "-q", "-b", self.BRANCH], d)
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

    def commit(self, msg, *extra):
        run(["git", "add", "-A"], self.dir)
        run(["git", "commit", "-q", "-m", msg, *extra], self.dir)

    def state(self, suffix, branch=BRANCH):
        return Path(self.dir) / ".git" / "pr-explainer" / f"{prx.branch_key(branch)}.{suffix}"

    def prepare(self, *flags):
        r = prx_cli(self.dir, "prepare", "--base", "main", "--no-fetch", *flags)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.explainer = Path(out["paths"]["explainer"])
        return out

    def prepare_with_examples(self, explainer=None, *flags):
        out = self.prepare(*flags)
        shutil.copy(SKILL / "examples" / "intent.json", self.state("intent.json"))
        if explainer is None:
            shutil.copy(SKILL / "examples" / "explainer.json", self.explainer)
        else:
            self.explainer.write_text(json.dumps(explainer), encoding="utf-8")
        return out

    def publish(self, url=URL):
        """prepare + render + record, as the skill does it."""
        self.prepare_with_examples()
        self.assertEqual(prx_cli(self.dir, "render").returncode, 0)
        r = prx_cli(self.dir, "record", url)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_full_flow(self):
        self.assertIn("No PR explainer", prx.gate_problem("gh pr create -t x", self.dir))
        out = self.prepare_with_examples()
        self.assertEqual(out["mode"], "create")
        ctx = json.loads(self.state("context.json").read_text(encoding="utf-8"))
        self.assertEqual(ctx["stats"]["files_changed"], 6)
        self.assertEqual([f["path"] for f in ctx["excluded_files"]], ["package-lock.json"])
        self.assertIn(self.explainer.name, self.state("reviewer-prompt.md").read_text(encoding="utf-8"))

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
        self.assertEqual(prx_cli(self.dir, "record", URL.replace("0b5e", "ffff")).returncode, 1)

        # The gate hook end to end, from the hook's own cwd and tool fields
        self.assertEqual(hook("pr_create_gate.py", {"cwd": self.dir, "tool_name": "Bash",
                              "tool_input": {"command": "gh pr create -t x"}}).returncode, 2)
        self.assertEqual(hook("pr_create_gate.py", {"cwd": self.dir, "tool_name": PS,
                              "tool_input": {"command": f"gh pr create -t x -b '{URL}'"}}).returncode, 0)

        # Already fresh: pushing doesn't ask for a refresh
        self.assertEqual(hook("pr_push_refresh.py", {"cwd": self.dir, "tool_input": {"command": "git push"}}).stdout, "")

        # A new commit makes the explainer stale
        self.write("src/search/fusion/Rrf.kt", "fun fuse() = 1\n")
        self.commit("fix rank")
        self.assertIn("update mode", prx.gate_problem(f"gh pr create -b {URL}", self.dir))
        r = hook("pr_push_refresh.py", {"cwd": self.dir, "tool_input": {"command": "git push -u origin HEAD"}})
        self.assertEqual(json.loads(r.stdout)["decision"], "block")
        self.assertIn("same artifact URL", r.stdout)
        self.assertEqual(hook("pr_push_refresh.py", {"cwd": self.dir, "tool_input": {"command": "git push --dry-run"}}).stdout, "")
        self.assertIn("HEAD moved", prx_cli(self.dir, "render").stderr)

        # A new prepare starts a new review: the old analysis can't be rendered under the new SHA
        old = self.explainer
        self.prepare()
        self.assertFalse(old.exists())
        self.assertNotEqual(old, self.explainer)
        r = prx_cli(self.dir, "render")
        self.assertEqual(r.returncode, 1)
        self.assertIn("not found", r.stderr)

    def test_refresh_waits_for_the_push_to_land(self):
        self.publish()
        remote = tempfile.mkdtemp(prefix="prx-remote-")
        try:
            run(["git", "init", "-q", "--bare", remote], self.dir)
            run(["git", "remote", "add", "origin", remote], self.dir)
            run(["git", "push", "-q", "-u", "origin", self.BRANCH], self.dir)
            self.write("src/search/fusion/Rrf.kt", "fun fuse() = 3\n")
            self.commit("not pushed yet")
            push = {"cwd": self.dir, "tool_input": {"command": "git push 2>&1 | tail -1"}}
            self.assertEqual(hook("pr_push_refresh.py", push).stdout, "")  # rejected push, exit code hidden
            run(["git", "push", "-q"], self.dir)
            self.assertIn("same artifact URL", hook("pr_push_refresh.py", push).stdout)
        finally:
            shutil.rmtree(remote, ignore_errors=True)

    def test_late_reviewer_from_an_earlier_prepare_is_ignored(self):
        self.prepare()
        first = self.explainer
        self.prepare_with_examples()
        shutil.copy(SKILL / "examples" / "explainer.json", first)  # the old reviewer finishes late
        self.explainer.unlink()  # and this run's reviewer hasn't written yet
        r = prx_cli(self.dir, "render")
        self.assertEqual(r.returncode, 1)
        self.assertIn("not found", r.stderr)

    def test_gate_reads_the_body_wherever_it_comes_from(self):
        self.publish()
        body_file = self.state("pr-body.md")
        ok = [f"gh pr create -t x -b '{URL}'", f"gh pr create -t x --body={URL}",
              f'gh pr create -t x --body-file "{body_file}"',
              f'gh pr create \\\n  --title x \\\n  --body-file "{body_file}"',  # options on continued lines
              f"gh pr create -t x -F - <<'EOF'\nSee {URL}\nEOF",
              f"gh pr create -t x --body \"$(cat <<'EOF'\nDon't \"panic\" (really): {URL}\nEOF\n)\"",
              f'body="see {URL}"; gh pr create -t x --body "$body"',  # a variable: falls back to the command text
              f"gh pr create --base main -b {URL} && curl -H x y"]
        for cmd in ok:
            self.assertIsNone(prx.gate_problem(cmd, self.dir), cmd)
        self.assertIsNone(prx.gate_problem(f"gh pr create -t x --body @'\nSee {URL}\n'@", self.dir, PS))
        notes = Path(self.dir) / "notes.md"
        notes.write_text(URL, encoding="utf-8")
        blocked = ["gh pr create -t x -b hi", f"gh pr create -t 'see {URL}' -b hi",
                   f"gh pr create -t x -b hi # {URL}", f"git commit -F {notes} && gh pr create -t x -b hi",
                   f"gh pr create -b {URL} --body 'No link'"]  # gh keeps the last --body
        for cmd in blocked:
            self.assertIn("Put the PR explainer link", prx.gate_problem(cmd, self.dir), cmd)
        self.assertIsNone(prx.gate_problem(f"gh pr create -t x -b{URL}", self.dir))  # attached value
        self.assertIn("No PR explainer for branch 'main'", prx.gate_problem(f"gh pr create --head main -b {URL}", self.dir))
        self.assertIn("No PR explainer for branch 'main'", prx.gate_problem(f"gh pr create -Hmain -b {URL}", self.dir))
        self.assertIn("rendered against 'main'", prx.gate_problem(f"gh pr create --base develop -b {URL}", self.dir))
        self.assertIn("rendered against 'main'", prx.gate_problem(f"gh pr create -Bdevelop -b {URL}", self.dir))
        self.assertIn("rendered against 'main'", prx.gate_problem(f"gh pr create -dB develop -b {URL}", self.dir))

    def test_changing_the_base_needs_a_republish(self):
        run(["git", "branch", "develop", "main"], self.dir)
        self.publish()  # rendered against main
        self.prepare("--base", "develop")  # a later prepare, without rendering or publishing
        self.assertIn("rendered against 'main'", prx.gate_problem(f"gh pr create --base develop -b {URL}", self.dir))
        self.assertIsNone(prx.gate_problem(f"gh pr create --base main -b {URL}", self.dir))
        # Without --base, gh targets the repo's default branch
        sha = run(["git", "rev-parse", "develop"], self.dir).stdout.strip()
        run(["git", "update-ref", "refs/remotes/origin/develop", sha], self.dir)
        run(["git", "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/develop"], self.dir)
        self.assertIn("gh targets the default branch 'develop'", prx.gate_problem(f"gh pr create -b {URL}", self.dir))

    def test_default_base_without_a_recorded_default_branch(self):
        run(["git", "branch", "develop", "main"], self.dir)
        main_sha = run(["git", "rev-parse", "main"], self.dir).stdout.strip()
        run(["git", "update-ref", "refs/remotes/origin/main", main_sha], self.dir)  # no refs/remotes/origin/HEAD
        self.prepare_with_examples(None, "--base", "develop")
        self.assertEqual(prx_cli(self.dir, "render").returncode, 0)
        self.assertEqual(prx_cli(self.dir, "record", URL).returncode, 0)
        self.assertIn("gh targets the default branch 'main'", prx.gate_problem(f"gh pr create -b {URL}", self.dir))
        self.assertIsNone(prx.gate_problem(f"gh pr create --base develop -b {URL}", self.dir))
        run(["git", "config", f"branch.{self.BRANCH}.gh-merge-base", "develop"], self.dir)  # gh reads this first
        self.assertIsNone(prx.gate_problem(f"gh pr create -b {URL}", self.dir))

    def test_unknown_default_base_needs_base_flag(self):
        self.publish()
        self.assertIsNone(prx.gate_problem(f"gh pr create -b {URL}", self.dir))  # no remotes: gh can't make a PR
        run(["git", "remote", "add", "upstream", "https://example.invalid/x.git"], self.dir)  # but no origin
        self.assertIn("Pass --base main", prx.gate_problem(f"gh pr create -b {URL}", self.dir))
        self.assertIsNone(prx.gate_problem(f"gh pr create --base main -b {URL}", self.dir))

    def test_gate_checks_the_repo_gh_runs_in(self):
        self.publish()
        other = tempfile.mkdtemp(prefix="prx-other-")
        try:
            run(["git", "init", "-q", "-b", "main", other], self.dir)
            (Path(other) / "x.txt").write_text("x\n", encoding="utf-8")
            run(["git", "add", "-A"], other)
            run(["git", "commit", "-q", "-m", "x"], other)
            run(["git", "checkout", "-q", "-b", "elsewhere"], other)
            o = Path(other).as_posix()
            self.assertIn("No PR explainer for branch 'elsewhere'", prx.gate_problem(f"cd {o} && gh pr create -b {URL}", self.dir))
            self.assertIn("No PR explainer", prx.gate_problem(f"pushd {o}; gh pr create -b {URL}", self.dir))
            self.assertIsNone(prx.gate_problem(f"pushd {o} && popd && gh pr create -b {URL}", self.dir))
            self.assertIsNone(prx.gate_problem(f'cd "$(git rev-parse --show-toplevel)" && gh pr create -b {URL}', self.dir))
            self.assertIn("can't tell which repository", prx.gate_problem(f'cd "$REPO" && gh pr create -b {URL}', self.dir))
            self.assertIn("No PR explainer", prx.gate_problem(f"Set-Location -Path {o}; gh pr create -b {URL}", self.dir, PS))
            # A cd in a child shell never reaches a gh pr create in the main shell (Greptile, PR #2)
            for cmd in [f"bash -c 'cd {o}'; gh pr create -b {URL}", f"(cd {o}); gh pr create -b {URL}",
                        f'echo "$(cd {o} && pwd)"; gh pr create -b {URL}', f"cd {o} | true; gh pr create -b {URL}",
                        f"cd {o} & gh pr create -b {URL}"]:
                self.assertIsNone(prx.gate_problem(cmd, self.dir), cmd)
            # ...but one in the same child shell as gh pr create might, and the gate can't tell. Nor
            # can it for PowerShell's $(...), which runs in the main shell at a point the list doesn't show.
            missing = f"{o}/missing"
            for cmd, tool in [(f"(cd {o} && gh pr create -b {URL})", "Bash"),
                              (f"bash -c 'cd {o} && gh pr create -b {URL}'", "Bash"),
                              (f'cd {o} && echo "$(gh pr create -b {URL})"', "Bash"),
                              (f'env -C "$X" gh pr create -b {URL}', "Bash"),
                              (f"env -C {missing} gh pr create -b {URL}", "Bash"),
                              (f"wsl --cd {missing} gh pr create -b {URL}", "Bash"),
                              (f'Write-Host "$(Set-Location {o})"; gh pr create -b {URL}', PS),
                              (f'Set-Location {o}; Write-Host "$(gh pr create -b {URL})"', PS)]:
                self.assertIn("can't tell which repository", prx.gate_problem(cmd, self.dir, tool), cmd)
            # Pipeline stages after a top-level cd, and wrappers that change directory, are followed
            parent, name = Path(other).parent.as_posix(), Path(other).name
            for cmd in [f"cd {o} && gh pr create -b {URL} | tee log", f"env -C {o} gh pr create -b {URL}",
                        f"env --chdir={o} gh pr create -b {URL}", f"pwsh -wd {o} -c 'gh pr create -b {URL}'",
                        f"env -u FOO -C {o} gh pr create -b {URL}", f"pwsh -WorkingDirectory:{o} -c 'gh pr create -b {URL}'",
                        f"sudo -D {o} gh pr create -b {URL}", f"env -C {parent} env -C {name} gh pr create -b {URL}"]:
                self.assertIn("No PR explainer for branch 'elsewhere'", prx.gate_problem(cmd, self.dir), cmd)
        finally:
            shutil.rmtree(other, ignore_errors=True)

    def test_reused_branch_name_needs_a_decision(self):
        self.publish()
        # The PR merged, the branch was deleted, and a new PR reuses the name
        run(["git", "checkout", "-q", "main"], self.dir)
        run(["git", "branch", "-q", "-D", self.BRANCH], self.dir)
        run(["git", "checkout", "-q", "-b", self.BRANCH], self.dir)
        self.write("docs/typo.md", "fixed\n")
        self.commit("unrelated change")
        self.assertTrue(json.loads(prx_cli(self.dir, "status").stdout)["diverged"])
        self.assertIn("isn't in this branch's history", prx.gate_problem(f"gh pr create -b {URL}", self.dir))
        r = hook("pr_push_refresh.py", {"cwd": self.dir, "tool_input": {"command": "git push"}})
        self.assertIn("prepare will ask", r.stdout)
        r = prx_cli(self.dir, "prepare", "--base", "main", "--no-fetch")
        self.assertEqual(r.returncode, 1)
        self.assertIn("--same-pr", r.stderr)
        self.assertIn("--new", r.stderr)
        out = self.prepare("--new")
        self.assertEqual(out["mode"], "create")
        self.assertFalse(self.state("url").exists())
        self.assertTrue(list(self.state("url").parent.glob("*.archived-*.url")))

    def merge_into_main(self):
        run(["git", "checkout", "-q", "main"], self.dir)
        run(["git", "merge", "-q", "--no-ff", "-m", "Merge PR", self.BRANCH], self.dir)

    def assert_needs_new_artifact(self):
        st = json.loads(prx_cli(self.dir, "status").stdout)
        self.assertEqual((st["merged"], st["diverged"]), (True, False))
        self.assertIn("already in main", prx.gate_problem(f"gh pr create -b {URL}", self.dir))
        self.assertIn("that PR was merged", hook("pr_push_refresh.py", {"cwd": self.dir, "tool_input": {"command": "git push"}}).stdout)
        r = prx_cli(self.dir, "prepare", "--base", "main", "--no-fetch")
        self.assertEqual(r.returncode, 1)
        self.assertIn("finished", r.stderr)
        self.assertEqual(self.prepare("--new")["mode"], "create")

    def test_work_continuing_after_a_merge_gets_a_new_artifact(self):
        self.publish()
        self.merge_into_main()
        run(["git", "checkout", "-q", self.BRANCH], self.dir)
        self.write("docs/next.md", "follow-up\n")
        self.commit("follow-up work on the same branch")
        self.assert_needs_new_artifact()

    def test_branch_recreated_from_a_merged_main_gets_a_new_artifact(self):
        self.publish()
        self.merge_into_main()
        run(["git", "branch", "-q", "-D", self.BRANCH], self.dir)
        run(["git", "checkout", "-q", "-b", self.BRANCH], self.dir)
        self.write("docs/typo.md", "fixed\n")
        self.commit("unrelated change")
        self.assert_needs_new_artifact()

    def test_merge_on_the_server_is_caught_by_prepare_and_the_hooks_defer_to_it(self):
        self.publish()
        remote, other = tempfile.mkdtemp(prefix="prx-remote-"), tempfile.mkdtemp(prefix="prx-other-")
        try:
            run(["git", "init", "-q", "--bare", "-b", "main", remote], self.dir)
            run(["git", "remote", "add", "origin", remote], self.dir)
            run(["git", "push", "-q", "origin", "main"], self.dir)
            run(["git", "push", "-q", "-u", "origin", self.BRANCH], self.dir)
            # The PR is merged on the server, from another clone; this one hasn't fetched.
            shutil.rmtree(other)
            run(["git", "clone", "-q", remote, other], self.dir)
            run(["git", "merge", "-q", "--no-ff", "-m", "Merge PR", f"origin/{self.BRANCH}"], other)
            run(["git", "push", "-q", "origin", "main"], other)
            self.write("docs/next.md", "follow-up\n")
            self.commit("follow-up on the same branch")
            run(["git", "push", "-q"], self.dir)
            # The hooks can't see the merge yet, so they must not rule out a new artifact...
            nudge = hook("pr_push_refresh.py", {"cwd": self.dir, "tool_input": {"command": "git push"}}).stdout
            self.assertNotIn("Do not create a new artifact", nudge)
            self.assertIn("--new", nudge)
            self.assertIn("--new", prx.gate_problem(f"gh pr create -b {URL}", self.dir))
            # ...and prepare, which fetches the base, catches it.
            r = prx_cli(self.dir, "prepare", "--base", "main")
            self.assertEqual(r.returncode, 1, r.stdout)
            self.assertIn("finished", r.stderr)
            self.assertIn("origin/main", r.stderr)
        finally:
            shutil.rmtree(remote, ignore_errors=True)
            shutil.rmtree(other, ignore_errors=True)

    def test_squash_merged_pr_found_through_github(self):
        self.publish()
        self.write("docs/next.md", "follow-up\n")
        self.commit("follow-up after a squash merge")
        sha = json.loads(prx_cli(self.dir, "status").stdout)["explained_sha"]
        repo = prx.Repo(self.dir)

        def fake_gh(prs):
            def fake_run(args, **kw):
                if args[:3] == ["gh", "pr", "list"]:
                    return subprocess.CompletedProcess(args, 0, json.dumps(prs), "")
                return real_run(args, **kw)
            return fake_run

        def failing_gh(args, **kw):
            if args[:3] == ["gh", "pr", "list"]:
                return subprocess.CompletedProcess(args, 1, "", "To get started with GitHub CLI, please run: gh auth login")
            return real_run(args, **kw)

        real_run, os.environ["PRX_NO_GITHUB"] = prx.subprocess.run, ""
        try:
            prx.subprocess.run = fake_gh([{"number": 7, "state": "MERGED", "headRefOid": sha, "url": "u"}])
            self.assertEqual(prx.finished_pr(repo, self.BRANCH, sha)[0]["number"], 7)
            prx.subprocess.run = fake_gh([{"number": 7, "state": "MERGED", "headRefOid": sha, "url": "u"},
                                          {"number": 9, "state": "OPEN", "headRefOid": sha, "url": "u"}])
            self.assertEqual(prx.finished_pr(repo, self.BRANCH, sha), (None, None))  # an open PR: still the same PR
            prx.subprocess.run = failing_gh
            pr, problem = prx.finished_pr(repo, self.BRANCH, sha)
            self.assertIsNone(pr)
            self.assertIn("gh auth login", problem)  # can't tell is reported, not swallowed
        finally:
            prx.subprocess.run, os.environ["PRX_NO_GITHUB"] = real_run, "1"
        self.assertEqual(prx.finished_pr(repo, self.BRANCH, sha), (None, None))  # PRX_NO_GITHUB skips gh

    def test_prepare_warns_when_github_cant_say_whether_the_pr_was_squash_merged(self):
        self.publish()
        self.write("docs/next.md", "follow-up\n")
        self.commit("follow-up")
        env = {k: v for k, v in ENV.items() if k != "PRX_NO_GITHUB"}
        # Keep git, drop gh: the squash-merge lookup can't run at all.
        env["PATH"] = os.pathsep.join(p for p in env["PATH"].split(os.pathsep)
                                      if not (Path(p) / "gh.exe").exists() and not (Path(p) / "gh").exists())
        r = subprocess.run([sys.executable, str(SKILL / "prx.py"), "prepare", "--base", "main", "--no-fetch"],
                           cwd=self.dir, env=env, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stderr)
        warnings = json.loads(r.stdout)["warnings"]
        self.assertTrue(any("Couldn't ask GitHub" in w for w in warnings), warnings)

    def test_rebased_branch_keeps_its_url(self):
        self.publish()
        self.write("src/search/fusion/Rrf.kt", "fun fuse() = 2\n")
        self.commit("hybrid ranking, reworded", "--amend")  # rewrites the explained commit
        self.assertTrue(json.loads(prx_cli(self.dir, "status").stdout)["diverged"])
        out = self.prepare("--same-pr")
        self.assertEqual((out["mode"], out["artifact_url"]), ("update", URL))

    def test_prune_removes_state_for_deleted_branches(self):
        self.publish()
        run(["git", "checkout", "-q", "main"], self.dir)
        run(["git", "branch", "-q", "-D", self.BRANCH], self.dir)
        keep = self.state("url", "main")
        keep.write_text("x", encoding="utf-8")
        dry = json.loads(prx_cli(self.dir, "prune", "--dry-run").stdout)["would_remove"]
        self.assertIn(self.state("url").name, dry)
        self.assertTrue(self.state("url").exists())
        removed = json.loads(prx_cli(self.dir, "prune").stdout)["removed"]
        self.assertEqual(sorted(dry), sorted(removed))
        self.assertFalse(self.state("url").exists())
        self.assertTrue(keep.exists())

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
        self.explainer.write_text("{ not json", encoding="utf-8")
        r = prx_cli(self.dir, "record", URL)
        self.assertEqual(r.returncode, 1)
        self.assertFalse(self.state("url").exists())
        self.assertFalse(self.state("sha").exists())

    def test_record_refuses_errors_and_changes_after_render(self):
        self.prepare_with_examples()
        self.assertEqual(prx_cli(self.dir, "render").returncode, 0)
        ex = json.loads(self.explainer.read_text(encoding="utf-8"))
        ex["tldr"] = ['Sets DB_PASSWORD="hunter2hunter2" in prod']
        self.explainer.write_text(json.dumps(ex), encoding="utf-8")
        r = prx_cli(self.dir, "record", URL)
        self.assertEqual(r.returncode, 1)
        self.assertIn("has errors", r.stderr)
        self.assertFalse(self.state("pr-body.md").exists())
        ex["tldr"] = ["A harmless edit made after rendering."]
        self.explainer.write_text(json.dumps(ex), encoding="utf-8")
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
        self.explainer.write_text(json.dumps(ex), encoding="utf-8")
        r = prx_cli(self.dir, "render")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        page = self.state("html").read_text(encoding="utf-8")
        self.assertNotIn("<script>alert", page)
        self.assertNotIn("<img src=x", page)
        self.assertNotIn("click SearchAPI", page)
        self.assertNotIn("securityLevel", page)
        self.assertIn("&lt;/script&gt;", page)


def load_hook(name):
    spec = importlib.util.spec_from_file_location(name.replace(".py", ""), HOOKS / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HookEdges(unittest.TestCase):
    def test_fast_path_never_skips_a_pr_creation(self):
        gate = load_hook("pr_create_gate.py")
        yes, _ = pr_create_cases()
        for case in yes:
            cmd = case[0] if isinstance(case, tuple) else case
            self.assertFalse(gate.plainly_unrelated(cmd), cmd)
        self.assertFalse(gate.plainly_unrelated("gh $'\\x70r' create"))  # ANSI-C escapes always get parsed
        self.assertTrue(gate.plainly_unrelated("ls -la"))
        refresh = load_hook("pr_push_refresh.py")
        yes, _ = push_cases()
        for case in yes:
            cmd = case[0] if isinstance(case, tuple) else case
            self.assertFalse(refresh.plainly_unrelated(cmd), cmd)
        self.assertTrue(refresh.plainly_unrelated("ls -la"))

    def test_quote_split_pr_create_blocks_through_the_real_hook(self):
        d = tempfile.mkdtemp(prefix="prx-norepo-")
        try:
            for cmd, tool in [("gh p''r create", "Bash"), ('gh "p"r create', "Bash"), ("gh p\\r create", "Bash"),
                              ("gh p`r create", PS)]:
                r = hook("pr_create_gate.py", {"cwd": d, "tool_name": tool, "tool_input": {"command": cmd}}, cwd=d)
                self.assertEqual(r.returncode, 2, cmd)
        finally:
            shutil.rmtree(d, ignore_errors=True)

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

    def test_encoded_powershell_is_not_skipped_by_the_fast_path(self):
        d = tempfile.mkdtemp(prefix="prx-norepo-")
        try:
            cmd = f"pwsh -EncodedCommand {encoded('gh pr create -t x')}"
            r = hook("pr_create_gate.py", {"cwd": d, "tool_name": PS, "tool_input": {"command": cmd}}, cwd=d)
            self.assertEqual(r.returncode, 2)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_deeply_nested_command_blocks_without_a_traceback(self):
        d = tempfile.mkdtemp(prefix="prx-norepo-")
        try:
            cmd = "$(" * 600 + "gh pr create" + ")" * 600
            r = hook("pr_create_gate.py", {"cwd": d, "tool_input": {"command": cmd}}, cwd=d)
            self.assertEqual(r.returncode, 2)
            self.assertNotIn("Traceback", r.stderr)
            r = hook("pr_create_gate.py", {"cwd": d, "tool_input": {"command": "rg __PRX_DOC0__ ."}}, cwd=d)
            self.assertEqual((r.returncode, r.stderr), (0, ""))
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
