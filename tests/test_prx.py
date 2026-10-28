"""Tests for the PR Explainer helper and hooks. Run: python -m unittest discover -s tests -v"""
import base64
import importlib.util
import hashlib
import json
import shlex
import os
import re
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


def prx_with_gh(cwd, *args, prs=None, fail=None, view=None, view_fail=None, head=None):
    """prx.py with tests/fake_gh.py standing in for gh: it lists `prs` (none unless --head is
    `head`, when given), shows the PRs in `view` by number (or fails with `view_fail`), or fails
    with `fail`."""
    env = {k: v for k, v in ENV.items() if k != "PRX_NO_GITHUB"}
    env.update(PRX_GH=str(ROOT / "tests" / "fake_gh.py"), PRX_GH_ANSWER=json.dumps(prs or []),
               PRX_GH_VIEW=json.dumps(view or []))
    if fail:
        env["PRX_GH_FAIL"] = fail
    if view_fail:
        env["PRX_GH_VIEW_FAIL"] = view_fail
    if head:
        env["PRX_GH_HEAD"] = head
    return subprocess.run([sys.executable, str(SKILL / "prx.py"), *args], cwd=cwd, env=env,
                          capture_output=True, text=True, encoding="utf-8")


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
        ("pwsh -cwa 'gh pr create -t $args[0]' x", PS), "pwsh -CommandWithArgs 'gh pr create'",
        'eval "gh pr create"', "env -S 'gh pr create -t x'", "env - gh pr create", "env --uns FOO gh pr create",
        "sudo -iu bob gh pr create", "cd x & gh pr create",
        "sudo --login gh pr create", "sudo -E --login gh pr create",  # --login is a flag, not --login-class's prefix
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
        for cmd in ["cd x && true & gh pr create", "cd x || true & gh pr create", "cd x && (true) & gh pr create"]:
            self.assertEqual([c[1] for c in flags(cmd)], [True, True, False], cmd)  # the whole list goes to the background
        self.assertEqual([c[1] for c in flags("cd x; true & gh pr create")], [False, True, False])
        self.assertEqual(flags("cd x; & gh pr create", "powershell"), [("cd", False, False, ()), ("gh", False, False, ())])
        # PowerShell runs $(...) and pipelines in the current runspace
        self.assertEqual(flags('Write-Host "$(cd x)"', "powershell")[0], ("cd", False, True, ()))
        self.assertEqual(flags("cd x | Out-Null", "powershell")[0][1], False)
        # Wrappers that change directory, however their options are spelled, outermost first
        for cmd in ["env -C d gh pr create", "env -Cd gh pr create", "env -iC d gh pr create", "env -u FOO -C d gh pr create",
                    "env --chdir=d gh pr create", "sudo -D d gh pr create",
                    "sudo --chdir=d gh pr create", "wsl --cd d gh pr create", "env - -C d gh pr create",
                    "sudo --user bob -D d gh pr create", "env -S '-C d gh pr create'", "env -C d -S 'gh pr create'"]:
            self.assertEqual(flags(cmd), [("gh", False, False, ("d",))], cmd)
        self.assertEqual(flags("env -C a env -C b gh pr create"), [("gh", False, False, ("a", "b"))])
        # ...but within one wrapper the last directory wins: env and sudo change directory once
        for cmd in ["env -C a -C b gh pr create", "env -Ca --chdir=b gh pr create", "sudo -D a -D b gh pr create"]:
            self.assertEqual(flags(cmd), [("gh", False, False, ("b",))], cmd)
        # -EncodedCommand takes one argument; PowerShell reads options after it
        payload = base64.b64encode("gh pr create".encode("utf-16-le")).decode()
        for cmd in [f"pwsh -ec {payload} -wd d", f"pwsh -EncodedCommand {payload} -WorkingDirectory:d", f"pwsh -wd d -ec {payload}",
                    f"pwsh -ec {payload} -ea QQ== -wd d"]:  # -EncodedArguments takes a value
            self.assertEqual(flags(cmd, "powershell"), [("gh", True, False, ("d",))], cmd)
        # Windows PowerShell 5.1 accepts -WorkingDirectory but stays where it started
        self.assertEqual(flags("powershell -wd d -c 'gh pr create'", "powershell"), [("gh", True, False, ())])
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

    def test_game_tour_and_focus_lines_are_checked(self):
        ex = self.example("explainer.json")
        ex["quiz"][0]["answer"] = 3  # three options
        ex["quiz"][1]["options"] = ["only one"]
        ex["quiz"][2]["ref"] = "hotspot-9"
        ex["tour"][0]["nodes"] = ["Nowhere"]
        ex["hotspots"][0]["focus_lines"] = [99]
        del ex["hotspots"][1]["headline"]
        rep = prx.Report()
        prx.validate_explainer(ex, self.CTX, rep)
        errors = "\n".join(rep.errors)
        for needle in ["quiz[0].answer: must be the index", "quiz[1].options: needs 2 to 4", "'Nowhere' does not appear",
                       "focus_lines: line numbers must be between 1 and", "hotspots[1].headline: is required",
                       "quiz[2].ref: 'hotspot-9' must be"]:
            self.assertIn(needle, errors)
        ex = self.example("explainer.json")
        ex["quiz"].append(dict(ex["quiz"][0]))  # a repeat would share one tick, so the review could never finish
        ex["reviewer_questions"].append(ex["reviewer_questions"][0])
        rep = prx.Report()
        prx.validate_explainer(ex, self.CTX, rep)
        self.assertIn("explainer.quiz: has the same scenario twice", rep.errors)
        self.assertIn("explainer.reviewer_questions: has the same question twice", rep.errors)
        ex = self.example("explainer.json")
        ex["quiz"][0]["options"] = [""] + ex["quiz"][0]["options"][:3]  # dropping it would move the key (tenth review)
        rep = prx.Report()
        prx.validate_explainer(ex, self.CTX, rep)
        self.assertIn("explainer.quiz[0].options: every answer must be non-empty text", rep.errors)
        ex = self.example("explainer.json")
        del ex["quiz"][0]["ref"]  # a pick that differs from the reviewer's must always link to the code that decides it
        rep = prx.Report()
        prx.validate_explainer(ex, self.CTX, rep)
        self.assertIn("explainer.quiz[0].ref: is required", rep.errors)
        ex = self.example("explainer.json")
        ex["quiz"] = ex["quiz"] * 2
        ex["tour"] = ex["tour"][:1]
        ex["hotspots"][0]["why_it_matters"] = "word " * 120
        rep = prx.Report()
        out = prx.validate_explainer(ex, self.CTX, rep)
        self.assertIn("keep the best 5", "\n".join(rep.errors))
        self.assertEqual(out["tour"], [])  # one step isn't a tour
        self.assertTrue(any("why_it_matters" in w and "over 420 it gets skimmed" in w for w in rep.warnings), rep.warnings)
        # The PR description says what's there, in plain words for one hotspot and no tour or game
        ex = self.example("explainer.json")
        ex["hotspots"], ex["tour"], ex["quiz"] = ex["hotspots"][:1], [], []
        body = prx.pr_body("https://x.example/a#p", prx.validate_explainer(ex, self.CTX, prx.Report()))
        self.assertIn("A before/after diagram, the hotspot to look at first and an independent intent check.", body)
        # ...and doesn't promise a "before" the page doesn't have (eighth review)
        ex["diagram_before"], ex["removed_node_ids"] = "", []
        ex["components"] = [c for c in ex["components"] if c["node_ids"] != ["SYN"]]
        body = prx.pr_body("https://x.example/a#p", prx.validate_explainer(ex, self.CTX, prx.Report()))
        self.assertIn("A diagram of the change, the hotspot to look at first", body)

    def test_component_ids_dont_read_as_hotspots(self):
        # A scenario's ref hotspot-N is a hotspot; a component can't be named that way
        ex = json.loads((SKILL / "examples" / "explainer.json").read_text(encoding="utf-8"))
        ex["components"][0]["id"] = "hotspot-9"
        rep = prx.Report()
        prx.validate_explainer(ex, Validation.CTX, rep)
        self.assertTrue(any("reads as a hotspot" in e for e in rep.errors), rep.errors)
        # ...while one that only starts that way is linked as the component it is (ninth review)
        html = prx.game([{"setup": "s", "question": "q", "options": ["a", "b"], "answer": 0, "explain": "e",
                          "ref": "hotspot-lines"}, {"setup": "s", "question": "q", "options": ["a", "b"], "answer": 1,
                                                    "explain": "e", "ref": "hotspot-2"}])
        self.assertIn('href="#component-hotspot-lines" data-jump="component-hotspot-lines">See the component', html)
        self.assertIn('href="#hotspot-2" data-jump="hotspot-2">See hotspot 2', html)

    def test_change_map_dims_test_files(self):
        # The biggest change can be a test fixture; the map tags and dims test files so the source
        # stands out, keeping the order (thirtieth round: PR #6's own map led with a test fixture)
        files = [{"path": "tests/cases.json", "additions": 800, "deletions": 0},
                 {"path": "src/app.py", "additions": 300, "deletions": 20}]
        page = prx.change_map({"review_files": files}, [{"file": "src/app.py", "risk": "high"}], None, "0" * 40)
        self.assertIn('<li class="crow test"><span class="cpath" title="tests/cases.json">', page)
        self.assertIn('<b>cases.json</b> <span class="reason">test</span>', page)
        self.assertIn('<li class="crow hot"><span class="cpath" title="src/app.py">', page)
        self.assertIn("Numbered markers are the hotspots below, and test files are dimmed.", page)
        for path in ["tests/test_prx.py", "src/test/java/AppTest.java", "pkg/foo_test.go", "web/a.test.ts", "web/a.spec.js",
                     "src/search/fusion/RrfTest.kt", "spec/models/user_spec.rb", "test_app.py", "conftest.py",
                     "__tests__/x.js", "Tests/FooTests.cs",
                     # Colocated names (PR #7's reviews)
                     "web/foo-test.js", "web/foo-spec.ts", "lua/foo_spec.lua", "src/FooTest.cpp", "src/FooTest.ts",
                     "ios/AppTests/LoginTests.m", "src/FooTests.fs", "spec/spec_helper.rb", "spec/support/auth.rb",
                     "app/foo_spec.py", "spec/javascripts/cart_spec.js", "ios/MyAppUITests.swift", "ios/APITests.swift",
                     "src/HTTPTest.kt", "lib/test_utils.c",
                     # Fixtures in test folders, and Django's tests.py (fourth review)
                     "spec/fixtures/orders.json", "cypress/fixtures/users.json", "spec/support/helpers.js", "app/tests.py",
                     # SwiftPM's and .NET's test folders (sixth review)
                     "Tests/AppTests/Fixtures/a.json", "src/MyApp.Tests/Helpers.cs"]:
            self.assertTrue(prx.TEST_FILE_RE.search(path), path)
        for path in ["src/app.py", "src/latest.py", "src/Contest.java", "docs/testing.md", "src/attestation.go",
                     "src/protest/x.py", "src/specification.py",
                     # Names that can be the code the change is about (thirty-first review)
                     "src/retry/RetrySpec.kt", "api/openapi.spec.yaml", "specs/search.yaml", "fixtures/prod-seed.json",
                     "src/Test.java", "src/attest.ts", "myapp/fixtures/seed.json",
                     # End-to-end encryption, and Django's test command (fifth review)
                     "app/e2e/client/crypto.ts", "django/core/management/commands/test.py"]:
            self.assertFalse(prx.TEST_FILE_RE.search(path), path)

    def test_change_map_marks_every_hotspot(self):
        # A hotspot in a small file still gets its row and marker (first review of PR #6's own page)
        files = [{"path": f"src/f{i}.py", "additions": 100 - i, "deletions": 0} for i in range(14)]
        page = prx.change_map({"review_files": files}, [{"file": "src/f13.py", "risk": "high"}], None, "0" * 40)
        self.assertIn('href="#hotspot-1"', page)
        self.assertIn("f13.py", page)
        self.assertIn("and 1 more file,", page)  # f12 is the one left out
        self.assertIn("The biggest changed files, and every file with a hotspot.", page)  # not "every" (twenty-second review)
        # A hotspot on an excluded file (a lockfile bump) gets a row too, labelled with why it was excluded
        lock = {"path": "package-lock.json", "additions": 40, "deletions": 2, "reason": "lockfile"}
        page = prx.change_map({"review_files": files[:2], "excluded_files": [lock]},
                              [{"file": "package-lock.json", "risk": "medium"}], None, "0" * 40)
        self.assertIn('<b>package-lock.json</b> <span class="reason">lockfile</span>', page)
        self.assertIn('href="#hotspot-1"', page)
        # A huge lockfile keeps the source files' scale: it fills its own bar, fading out (seventh review)
        lock = {**lock, "additions": 9000, "deletions": 4000}
        page = prx.change_map({"review_files": files[:2], "excluded_files": [lock]},
                              [{"file": "package-lock.json", "risk": "medium"}], None, "0" * 40)
        self.assertIn('<span class="cbar" aria-hidden="true"><i class="a" style="width:100.0%">', page)  # f0, the widest
        self.assertIn('<span class="cbar over" aria-hidden="true"><i class="a" style="width:69.2%">', page)
        # A deleted file links to itself before the change, not to a 404 at head (tenth review)
        page = prx.change_map({"review_files": files[:2], "merge_base": "b" * 40}, [], "https://github.com/o/r",
                              "0" * 40, gone={"src/f1.py"})
        self.assertIn('https://github.com/o/r/blob/' + "b" * 40 + '/src/f1.py', page)
        self.assertIn('<b>f1.py</b> <span class="reason">deleted</span>', page)
        self.assertIn('https://github.com/o/r/blob/' + "0" * 40 + '/src/f0.py', page)
        # A row carries its file's code id and, on GitHub, the file's anchor in a compare view, where
        # "updated since your last visit" opens that file's diff (tenth review)
        page = prx.change_map({"review_files": files[:2]}, [], "https://github.com/o/r", "0" * 40, codes={"src/f0.py": "c-1"})
        anchor = hashlib.sha256(b"src/f0.py").hexdigest()
        self.assertIn(f'<li class="crow" data-code="c-1" data-diff="diff-{anchor}"><span class="cpath" title="src/f0.py">', page)
        self.assertIn('<li class="crow"><span class="cpath" title="src/f1.py">', page)  # no code id, no marks

    def test_approach_can_be_steps(self):
        it = self.example("intent.json")
        rep = prx.Report()
        self.assertEqual(len(prx.validate_intent(it, rep)["approach"]), 3)
        it["approach"] = "x" * 800
        prx.validate_intent(it, rep)
        self.assertTrue(any("split it into a list of short steps" in w for w in rep.warnings), rep.warnings)
        it["approach"] = []
        rep = prx.Report()
        prx.validate_intent(it, rep)
        self.assertIn("intent.approach: is required (a string, or a list of steps)", rep.errors)
        # The author's other text gets the same length warnings as the reviewer's (twenty-first review)
        it = self.example("intent.json")
        it["goal"] = "word " * 80
        it["approach"] = ["step " * 70]
        it["how_to_verify"] = ["check " * 50]
        rep = prx.Report()
        prx.validate_intent(it, rep)
        for where in ("intent.goal", "intent.approach[0]", "intent.how_to_verify[0]"):
            self.assertTrue(any(w.startswith(where + ":") for w in rep.warnings), (where, rep.warnings))

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
        # Removing one directive can join the leftovers into another; an unclosed one mid-line still counts
        for text in ['flowchart LR\n  A --> B %%%{x}%%%{init: {"securityLevel": "loose"}}%%',
                     'flowchart LR\n  A --> B %%{init: {"securityLevel": "loose"}\n  C --> D']:
            out, _ = self.clean(text)
            self.assertNotIn("%%{", out, text)
            self.assertIn("A --> B", out)

    def test_title_flags_stay_whole(self):
        self.assertEqual(prx.nowrap_flags("pwsh -ea and powershell -wd; set-head --auto"),
                         'pwsh <span class="nw">-ea</span> and powershell <span class="nw">-wd</span>; '
                         'set-head <span class="nw">--auto</span>')

    def test_node_ids_are_declared_nodes_not_label_words(self):
        nodes = prx.diagram_nodes('flowchart LR\n  A -- calls with retry --> SearchAPI[Search API]\n'
                                  '  SearchAPI -.->|hybrid on| C((Circle))\n  subgraph Box [Group title]\n'
                                  '  D{{Hex}} & E>Flag] --> F\n  end\n  class A,SearchAPI prxChanged')
        self.assertEqual(nodes, {"A", "SearchAPI", "C", "D", "E", "F"})  # Box is a subgraph, not a box
        # A subgraph can be highlighted (Mermaid colours the group) but not toured; A:::cls isn't a node 'cls'
        diagram = "flowchart LR\n  subgraph Svc [Search service]\n  A:::hot --> B\n  end"
        self.assertEqual(prx.diagram_nodes(diagram, groups=True), {"Svc", "A", "B"})
        rep = prx.Report()
        self.assertEqual(prx._node_ids(rep, {"changed": ["Svc", "A"]}, "changed", diagram, "x"), ["Svc", "A"])
        self.assertEqual(rep.errors, [])
        prx._node_ids(rep, {"nodes": ["Svc", "hot"]}, "nodes", diagram, "x", groups=False)
        self.assertEqual(len(rep.errors), 2)
        self.assertIn("is a subgraph", rep.errors[0])
        # Plain links, one-line diagrams and pipes inside a label are still nodes (second review of PR #6)
        self.assertEqual(prx.diagram_nodes("flowchart LR\n  Cache --- Store --- DB\n  X === Y === Z"),
                         {"Cache", "Store", "DB", "X", "Y", "Z"})
        self.assertEqual(prx.diagram_nodes("graph TD; A-->B; B-->C"), {"A", "B", "C"})
        self.assertEqual(prx.diagram_nodes('flowchart LR\n  P["a | b"] --> Q\n  Q -->|go| R'), {"P", "Q", "R"})
        # Keywords are case-sensitive: a line starting with a box named End or Style still declares its nodes
        self.assertEqual(prx.diagram_nodes("flowchart TD\n  End[Done] --> Report[Write report]\n  Style --> Audit"),
                         {"Report", "Audit"})  # End and Style themselves are refused by the keyword rule for ids
        # Edge text without spaces, before a circle or cross head, and o/x arrowheads (fifth review of PR #6)
        self.assertEqual(prx.diagram_nodes("flowchart LR\n  A--calls-->B\n  B -- uses --o C\n  C o--o D\n  D --x E\n"
                                           "  E[Search -- fast] --> F"), {"A", "B", "C", "D", "E", "F"})
        rep = prx.Report()
        prx._node_ids(rep, {"tour": ["API", "hybrid", "SearchAPI"]}, "tour", "flowchart LR\n  SearchAPI[Search API] -->|hybrid| B", "x")
        self.assertEqual(len(rep.errors), 2, rep.errors)
        # The > of a <br> or of edge text isn't a flag shape that swallows the rest of the line (seventh review)
        self.assertEqual(prx.diagram_nodes("flowchart LR\n  Flag{Hybrid on?<br>per request} -->|yes| VEC[(Vector index)]\n"
                                           "  VEC -->|n>1| RRF[Fuse] --> E>Flag]"), {"Flag", "VEC", "RRF", "E"})
        # ...nor in -- text -->, and accessibility lines aren't nodes (ninth review)
        self.assertEqual(prx.diagram_nodes("flowchart LR\n  accTitle: Search flow\n  accDescr: How a query is ranked\n"
                                           "  A -- n>1 --> B\n  B-- x>y -->C"), {"A", "B", "C"})
        # ...nor swallows a flag shape after it, and an accDescr block over lines is text (tenth review)
        self.assertEqual(prx.diagram_nodes("flowchart LR\n  accDescr {\n    Words here\n    and here\n  }\n"
                                           "  A -- n>1 --> E>Flag]"), {"A", "E"})
        # Two-way arrow text and a quoted label over two lines are text too (tenth review)
        self.assertEqual(prx.diagram_nodes('flowchart LR\n  A <-- sync --> B\n  B --> C["first line\n  second line"]'),
                         {"A", "B", "C"})
        # ...a dotted link's text without a head (eleventh review), and an o or x straight after the
        # dashes is an arrowhead, as Mermaid's docs have it: A---oB is a circle edge to B (sixteenth review)
        self.assertEqual(prx.diagram_nodes("flowchart LR\n  A---oB\n  B -. maybe .- C\n  C --o D\n  D--xE\n  E -- ok --> F"),
                         {"A", "B", "C", "D", "E", "F"})
        # ...and a thick link's text before a circle or cross head (eighteenth review)
        self.assertEqual(prx.diagram_nodes("flowchart LR\n  A == retry ==o B\n  B == give up ==x C"), {"A", "B", "C"})
        # A two-way arrow without text keeps the node after it (twelfth review)
        self.assertEqual(prx.diagram_nodes("flowchart LR\n  Cache <--> Store --> DB"), {"Cache", "Store", "DB"})
        # ...and a dotted two-way arrow's text is text (thirteenth review)
        self.assertEqual(prx.diagram_nodes("flowchart LR\n  A <-. sync .-> B"), {"A", "B"})
        # A hyphenated id is one id, not two words, and Mermaid 11's @{ } shape block is no node (fifteenth review)
        self.assertEqual(prx.diagram_nodes("flowchart LR\n  search-api --> B\n  A@{ shape: cyl,\n  label: Store } --> B"),
                         {"A", "B"})

    def test_labels_mermaid_cant_parse_are_caught(self):
        # Unquoted ( ) { } or a quote in a box label or edge text: Mermaid 11 won't draw the diagram,
        # and the tour and box links go with it (twentieth review). Quoted, and shapes, are fine.
        rep = prx.Report()
        prx.clean_diagram('flowchart LR\n  A[Search (BM25)] -->|calls (async)| B[it\'s "here"]', "x", rep)
        self.assertEqual(sum("Mermaid won't parse" in e for e in rep.errors), 3, rep.errors)
        rep = prx.Report()
        prx.clean_diagram('flowchart LR\n  A["Search (BM25)"] -->|"calls (async)"| B[(Store)] --> C[[Sub]] --> D[/In/]',
                          "x", rep)
        self.assertEqual(rep.errors, [])
        # Every node shape, not only [ ] boxes (twenty-first review)...
        for label in ["Flag{Hybrid on (per request)?}", "R(Search (x))", "S[(Store (PG))]", "C((Ring (1)))",
                      "T([Stadium (s)])", "H{{Hex (h)}}", "F>Flag (f)]",
                      "P[a | b]", "subgraph Rank [Ranking (new)]\n  Z2\n  end\n  Y"]:  # (twenty-second review)
            rep = prx.Report()
            prx.clean_diagram(f"flowchart LR\n  {label} --> Z", "x", rep)
            self.assertTrue(any("Mermaid won't parse" in e for e in rep.errors), label)
        # ...and not inside comments, accessibility lines, quoted labels, a <br> or edge text with >
        rep = prx.Report()
        prx.clean_diagram('flowchart LR\n  %% a note (with brackets) [x] {y}\n  accTitle: Search (hybrid)\n'
                          '  accDescr: How {it} ranks\n  Flag{"Hybrid (per request)"} --> B\n'
                          '  Q{Hybrid on?<br>per request} -->|yes| VEC[(Vector index)]\n'
                          '  VEC -->|n>1| RRF[Fuse] --> E>Flag]\n  E -- n>1 --> G[Done]\n'
                          '  G --> D2(((Double circle)))\n  subgraph Rank [Ranking]\n  D2\n  end', "x", rep)
        self.assertEqual(rep.errors, [])
        # A comment after a statement breaks the diagram; %% inside a label is text (twenty-third review)
        rep = prx.Report()
        prx.clean_diagram("flowchart LR\n  A --> B %% note", "x", rep)
        self.assertTrue(any("on a line of their own" in e for e in rep.errors), rep.errors)
        rep = prx.Report()
        self.assertEqual(prx.diagram_nodes(prx.clean_diagram("flowchart LR\n  A[100%% sure] --> B", "x", rep)), {"A", "B"})
        self.assertEqual(rep.errors, [])

    def test_trailing_comments_as_mermaid_reads_them(self):
        # Mermaid 11.4's own parse of each line (twenty-fourth review): a %% after a statement breaks
        # the diagram with or without a space; inside a label, edge text or a value that runs to the
        # end of its line (style, subgraph, accTitle) it's text
        for line in ["A --> B %% note", "A --> B%% note", "A[x] %% note", "A --> B; %% note", "class A hot %% x",
                     "end %% x", "A:::hot --> B %% x", 'subgraph S ["Title"] %% x', "subgraph S[Title]%%x"]:
            self.assertTrue(prx.trailing_comment(f"flowchart LR\n  {line}"), line)
        for line in ['A["CPU at 90 %%"] --> B', "A[100%% sure] --> B", "A[CPU at 90 %%] --> B", "A{90 %% hot} --> B",
                     "A([90 %% x]) --> B", "A>90 %% hot] --> B", "A -->|50 %% done| B", "A -- 50 %% done --> B",
                     'A -- "50 %% x" --> B', "%% a note", "style A fill:#f9f %% pink", "classDef hot fill:#f00 %% red",
                     "linkStyle 0 stroke:#f00 %% red", "subgraph S [50 %% x]", "subgraph S %% x",
                     'subgraph "Title" %% x', "subgraph S Ranking stuff %% x",
                     "accTitle: 50%% done", "direction TB %% x"]:
            self.assertIsNone(prx.trailing_comment(f"flowchart LR\n  {line}"), line)

    def test_markdown_in_labels_as_mermaid_draws_it(self):
        # Mermaid 11 reads every label as Markdown, quoted or not; these are what it drew for each
        # (twenty-fourth review). Its #code; escapes keep the text as written.
        def problems(label):
            rep = prx.Report()
            prx.clean_diagram(f'flowchart LR\n  A["{label}"] --> B', "x", rep)
            return rep.errors, rep.warnings
        unsupported = {"1. First": "list", "2024. Year": "list", "1) First": "list", "- item": "list", "+ item": "list",
                       "* item": "list", "-": "list", "# head": "heading", "## two": "heading", "> 5 ms": "blockquote",
                       ">5": "blockquote", "---": "hr", "***": "hr", "- - -": "hr", "a `b` c": "codespan",
                       "`code` path": "codespan", "~~old~~": "del", "~x~": "del", "http://x.com": "link",
                       "see https://x for docs": "link", "www.example.com": "link", "user@example.com": "link",
                       # Any case, and each line of a quoted label over lines (twenty-fifth review)
                       "HTTPS://X.COM": "link", "WWW.X.COM": "link", "USER@X.COM": "link",
                       "Steps\n- one": "list", "Steps\n# two": "heading", "a\n1. b": "list",
                       # Under a line of text, as in a paragraph: = or - lines make a heading, a list
                       # starts only at 1 unless a blank line comes first; and links (twenty-sixth review)
                       "Totals\n===": "heading", "Totals\n---": "heading", "Totals\n***": "hr", "a\n\n2. b": "list",
                       "a\n> b": "blockquote", "[docs](url)": "link", "see [docs](http://x) now": "link",
                       "![img](x)": "image", "a\n1) b": "list",
                       # Two spaces before a line break, and an indent after a blank line (twenty-seventh)
                       "a  \nb": "br", "a\n\n    code": "code"}
        for label, kind in unsupported.items():
            errors, _ = problems(label)
            self.assertTrue(any(f"Unsupported markdown: {kind}" in e for e in errors), (label, errors))
        for label in ["**kw**", "__init__", "obj.__dict__ x", "_a_", "x*y*z"]:
            errors, warnings = problems(label)
            self.assertEqual(errors, [], label)
            self.assertTrue(any("bold or italics" in w for w in warnings), label)
        for label in ["1.5 ms", "1.First", "Step 1. x", "-item", "+1 vote", "*args", "*args and **kw", "_private",
                      "snake_case", "a__b__c", "x_1 and y_2", "#1 priority", "C# code", "see #12", "~/a and ~/b",
                      "a * b * c", "*.py files", "**/*.ts", "a - b", "=== x", "a@b", "api.example.com", "x.com/path",
                      "`**bold** text`", "1#46; First", "#45; item", "#35; heading", "#96;code#96;",
                      "#95;#95;init#95;#95;", "http#58;//x.com", "#gt; 5 ms", "user#64;example.com",
                      "Steps\n2. two", "[x]", "[x][y]", "#91;docs](url)", "Totals\n#61;==", "a\n01. b"]:
            self.assertEqual(problems(label), ([], []), label)
        # Every kind of label: unquoted, edge text either way, a subgraph title
        for diagram in ["A[- item] --> B", "A(1. x) --> B", "A{# x} --> B", "A -->|1. first| B", "A -- 1. first --> B",
                        "subgraph S [1. first]\n  A\n  end"]:
            rep = prx.Report()
            prx.clean_diagram(f"flowchart LR\n  {diagram}", "x", rep)
            self.assertTrue(any("Unsupported markdown" in e for e in rep.errors), diagram)

    def test_box_labels_are_read_before_edge_text(self):
        # A flag or == in a box label is the label's text, as Mermaid reads the line left to right;
        # it isn't the start of -- edge text --> (twenty-fifth review). Mermaid draws all of these.
        for diagram in ["P[prepare --base main] --> V[validate]", "A[git diff --stat] --> B[Removed]",
                        "A[status == 200] --> B[OK]", "A[--base] --> B", "A -- n>1 --> B[x]",
                        "A[run --new-artifact] -->|then| B{--force?}", 'A["Steps\n50 %% done"] --> B']:
            rep = prx.Report()
            prx.clean_diagram(f"flowchart LR\n  {diagram}", "x", rep)
            self.assertEqual(rep.errors, [], diagram)
        # A label starting with / opens a slanted box: the error quotes the whole id and names the fix,
        # which validate then passes (thirty-first review)
        rep = prx.Report()
        prx.clean_diagram("flowchart LR\n  API[/search endpoint] --> B", "x", rep)
        self.assertTrue(any("the box 'API[/search endpoint] --> B' is never closed" in e and 'A["/search"]' in e
                            for e in rep.errors), rep.errors)
        rep = prx.Report()
        prx.clean_diagram('flowchart LR\n  API["/search endpoint"] --> B', "x", rep)
        self.assertEqual(rep.errors, [])
        # ...and quotes the id alone, not an arrow before it, a hyphenated id whole (PR #7's review)
        rep = prx.Report()
        prx.clean_diagram("flowchart LR\n  A---B[/x y] --> C\n  search-api[/x] --> D", "x", rep)
        self.assertTrue(any("the box 'B[/x y] --> C' is never closed" in e for e in rep.errors), rep.errors)
        self.assertTrue(any("the box 'search-api[/x] --> D' is never closed" in e for e in rep.errors), rep.errors)
        # A box label over lines is one label for every check, the node ids too: its second line
        # starting "style" or "end" is its text, not a statement (twenty-ninth review)
        for diagram in ["A[Read the\n  style guide] --> B", "A[Wait until\n  end of day] --> B",
                        "A[Pick a\n  class name] --> B", "A{Is it the\n  end?} -->|yes| B"]:
            self.assertEqual(prx.diagram_nodes(f"flowchart LR\n  {diagram}"), {"A", "B"}, diagram)
        self.assertEqual(prx.trailing_comment("flowchart LR\n  A[Read the\n  style guide] --> B %% x"),
                         "style guide] --> B %% x")
        self.assertEqual(prx.diagram_labels("flowchart LR\n  P[prepare --base main] -- runs --> V[validate]"),
                         [("prepare --base main", "bare", None), ("runs", "text", ("--", "-->")),
                          ("validate", "bare", None)])
        # -- edge text --> as Mermaid 11.4.1 reads it (twenty-sixth review): the text can't hold its
        # own opening half or a quote unless it's quoted, and the head must be the line it started with
        for diagram in ["P -- prepare --base main --> V", "A -- a -- b --> B", "A == a == b ==> B",
                        "A -. a -. b .-> B", 'A -- a "x" --> B', "A -- a ==> B", "A <-- a --o B",
                        "A --o|calls (x)| B", "A --x|1. x| B", "A <-- 1. x --> B",
                        # Text with no head of its own line after it, and a head glued to a word, which
                        # draws a stray box (twenty-seventh review); A --oB is a real head, but the space
                        # the error asks for (A --o B) is the same arrow
                        "A == a --> B", "A -. a --> B", "A -- x .-> B", "A -- a", "A -- a ==> B -- c --> D",
                        "Client--overrides-->Store", "A -- flag--off --> B", "A --oB", "A ==xB",
                        "A--x-->B", "A -- yes -->|no| B", 'A["```"] --> B',
                        # ...after any run of - or =, or -.- (twenty-ninth review)
                        "A ---oB", "Client---overrides-->Store", "A ===xyz===> B", "A ---ox B", "A-.-oops.->B"]:
            rep = prx.Report()
            prx.clean_diagram(f"flowchart LR\n  {diagram}", "x", rep)
            self.assertTrue(rep.errors, diagram)
        for diagram in ["A -- a - b --> B", "A -- a == b --> B", "A == a -- b ==> B", "A -. a -- b .-> B",
                        "A -- a=b --> B", "A -- a -> b --> B", "A -- a (x) --> B", "A -- a [x] --> B",
                        'P -- "prepare --base main" --> V', "P -->|prepare --base main| V", 'A == "a == b" ==> B',
                        "A <-- a (x) --> B", "A <== a ==> B", "A <-. a .-> B", "A x-- a --x B", "A o-- a --o B",
                        "A --o |ok| B", "A ==x|ok| B", "A -- a --x B", "A -- a --o B",
                        # Text ends only at a head of its own line, so another line's marks are text
                        "A -- status==ok --> B", "A -- mode==on --> B", "A -- a.-b --> B", "A == x--on ==> B",
                        "A -- a->b --> B", "A -- a --- B", "A == a === B", "A -. a .- B"]:
            rep = prx.Report()
            prx.clean_diagram(f"flowchart LR\n  {diagram}", "x", rep)
            self.assertEqual(rep.errors, [], diagram)
        # A trailing comment after a quoted label over lines is still found, on its own line
        self.assertEqual(prx.trailing_comment('flowchart LR\n  A["one\n  two"] --> B %% note'), 'two"] --> B %% note')

    def test_validate_agrees_with_mermaid(self):
        # Mermaid 11.4.1's own verdict on each diagram of a review's corpus (tests/mermaid_cases.json,
        # recorded in a browser): validate errors where Mermaid fails or garbles a label and passes
        # what it draws, except where decided otherwise (twenty-seventh review)
        no_html = "no HTML in labels, even where Mermaid would draw it"
        decided = {'A["<b>x</b>"] --> B': no_html, 'A["<!-- x -->"] --> B': no_html, 'A["<https://x.io>"] --> B': no_html}
        doc = json.loads((ROOT / "tests" / "mermaid_cases.json").read_text(encoding="utf-8"))
        self.assertEqual(doc["mermaid"], prx.PAGE_MERMAID.split("@")[1].split("/")[0])  # re-record on an upgrade
        wrong = []
        for src, mermaid in doc["cases"]:
            rep = prx.Report()
            prx.clean_diagram("flowchart LR\n  " + src, "x", rep)
            if ((mermaid == "drawn") == bool(rep.errors)) != (src in decided):
                wrong.append((src, mermaid, rep.errors))
        self.assertEqual(wrong, [])

    def test_subgraph_holding_its_own_id(self):
        # Mermaid parses it, then fails to lay it out: "Setting API as parent of API would create a
        # cycle" (twenty-fourth review). Linking to the group from outside, styling it or moving it
        # into another group is fine.
        rep = prx.Report()
        prx.clean_diagram("flowchart LR\n  subgraph API [Search API]\n    API --> HYB[Hybrid]\n  end", "x", rep)
        self.assertTrue(any("'API' holds a node of its own id" in e for e in rep.errors), rep.errors)
        self.assertEqual(prx.subgraph_cycles("flowchart LR\n  subgraph A\n    subgraph B\n      A --> C\n    end\n  end"),
                         ["A"])
        self.assertEqual(prx.subgraph_cycles("flowchart LR\n  subgraph S\n    B\n    style S fill:#f00\n  end\n"
                                             "  S --> C\n  X --> S\n  subgraph T\n    S\n  end"), [])

    def test_component_node_ids_are_boxes(self):
        # A component opens from its box: a word from a label or a subgraph opens nothing (seventh review of PR #6)
        ex = json.loads((SKILL / "examples" / "explainer.json").read_text(encoding="utf-8"))
        ex["diagram_after"] += "\n  subgraph Ranking [Ranking]\n    RRF\n  end"
        ex["components"][0]["node_ids"] = ["API", "Ranking"]
        rep = prx.Report()
        prx.validate_explainer(ex, Validation.CTX, rep)
        errors = "\n".join(rep.errors)
        self.assertIn("components[0].node_ids: 'API' does not appear in the diagram as a node id", errors)
        self.assertIn("'Ranking' is a subgraph; selecting a box opens its component", errors)
        # A box that only the before diagram has still opens its (removed) component
        ex = json.loads((SKILL / "examples" / "explainer.json").read_text(encoding="utf-8"))
        rep = prx.Report()
        prx.validate_explainer(ex, Validation.CTX, rep)
        self.assertEqual(rep.errors, [])
        # A group before that became a box after is a box: each diagram is read on its own (thirteenth review)
        ex["diagram_before"] += "\n  subgraph RRF [Old ranking]\n    SYN\n  end"
        ex["components"][1]["node_ids"] = ["RRF"]
        rep = prx.Report()
        prx.validate_explainer(ex, Validation.CTX, rep)
        self.assertEqual(rep.errors, [])

    def test_rejects_mermaid_keywords_as_ids(self):
        rep = prx.Report()
        prx._node_ids(rep, {"changed_node_ids": ["end", "Api"]}, "changed_node_ids", "flowchart LR\n  Api --> end", "x")
        self.assertEqual(len(rep.errors), 1)
        self.assertIn("Mermaid keyword", rep.errors[0])


@unittest.skipUnless(shutil.which("node"), "needs node to run the page's script")
class PageLogic(unittest.TestCase):
    """The page's update logic (updateView in template.html), run in node over one reader's visits:
    each step applies what the view says to store, as the page does (twenty-eighth review; the
    marker had changed in three reviews running without a test)."""

    def call(self, name, *args):
        """The page's own function `name`, from template.html, called in node with args (as JSON)."""
        text = (SKILL / "template.html").read_text(encoding="utf-8")
        fn = re.search(r"\n  function " + name + r"\(.*?\) \{\n.*?\n  \}\n", text, re.S).group()
        script = fn + ("const args = JSON.parse(require('fs').readFileSync(0, 'utf8'));"
                       f"process.stdout.write(JSON.stringify({name}(...args)));")
        out = subprocess.run(["node", "-e", script], input=json.dumps(args), capture_output=True, text=True,
                             encoding="utf-8", check=True)
        return json.loads(out.stdout)

    def test_marks_and_note_over_a_readers_visits(self):
        X, Y, Z = "commit-x", "commit-y", "commit-z"
        saved = {"checks": {}, "quiz": {}}

        def visit(rendered, commit, files, hotspots, items, legacy=0, glance=False):
            # As the page does: what to store at once, and the visit once the reader spends time on it
            view = self.call("updateView", saved, {"rendered": rendered, "commit": commit, "items": items,
                                                   "files": files, "hotspots": hotspots, "legacy": legacy})
            saved.update(view["store"] or {})
            if view["visit"] and not glance:
                saved.update({k: {**v, "at": rendered} if k == "visit" else v for k, v in view["visit"].items()})
            return view

        def marks(view):
            return {k: view[k] for k in ("since", "sinceAt", "changed", "added", "removed", "unchanged")}
        nothing = {"since": None, "sinceAt": None, "changed": [], "added": [], "removed": [], "unchanged": []}

        # Commit X changes a.py, b.py, c.py and gone.sql: a first visit marks and notes nothing, and keeps them
        files_x = {"c1": "src/a.py", "c2": "src/b.py", "c3": "src/c.py", "c9": "db/gone.sql"}
        at_x = ("2026-01-01T00:00:00Z", X, files_x, [{"track": "h-a", "code": "c1"}], ["h-a", "q-1"])
        v1 = visit(*at_x[:4], ["h-old"] + at_x[4])
        self.assertEqual((marks(v1), v1["note"]), (nothing, None))
        self.assertEqual(v1["visit"], {"visit": {"commit": X, "files": files_x}})
        self.assertNotIn("visit", v1["store"])  # kept only once the reader spends time on the page
        saved["checks"]["h-a"], saved["quiz"]["q-1"] = 1, 0
        # A reload, or a re-render of X (an intent fix), has no other commit to compare with, nor a visit to keep
        self.assertEqual(visit(*at_x), {**nothing, "note": None, "store": None, "visit": None})
        self.assertEqual(marks(visit("2026-01-01T01:00:00Z", *at_x[1:])), nothing)
        # Commit Y changed b.py, added d.py and dropped gone.sql from the PR: each is told apart, and the
        # fresh review's hotspots in a.py and c.py, files the push left alone, are tagged
        files_y = {"c1": "src/a.py", "c2y": "src/b.py", "c3": "src/c.py", "c4": "src/d.py"}
        at_y = ("2026-01-02T00:00:00Z", Y, files_y,
                [{"track": "h-c", "code": "c1"}, {"track": "h-d", "code": "c2y"}, {"track": "h-e", "code": "c3"}],
                ["h-c", "h-d", "h-e", "q-1"])
        v2 = visit(*at_y)
        marked = {"since": X, "sinceAt": "2026-01-01T00:00:00Z", "changed": ["c2y"], "added": ["c4"],
                  "removed": ["db/gone.sql"], "unchanged": ["h-c", "h-e"]}
        self.assertEqual(marks(v2), marked)
        self.assertEqual(v2["note"], {"newCommit": True, "since": X, "changed": ["src/b.py"], "added": ["src/d.py"],
                                      "removed": ["db/gone.sql"], "unchanged": 2, "kept": 1})
        self.assertEqual(v2["visit"]["lastVisit"], {"commit": X, "files": files_x, "at": "2026-01-01T00:00:00Z"})
        # A reload keeps the marks, with no second note; so does a re-render of Y, still against X
        self.assertEqual(visit(*at_y), {**marked, "note": None, "store": None, "visit": None})
        at_y = ("2026-01-02T01:00:00Z",) + at_y[1:]
        v2b = visit(*at_y)
        self.assertEqual((marks(v2b), v2b["note"]), (marked, None))
        # Ticking a hotspot takes its tag off
        saved["checks"]["h-c"] = 1
        self.assertEqual(visit(*at_y)["unchanged"], ["h-e"])
        # An older copy marks nothing, says it's older and stores nothing
        self.assertEqual(visit(*at_x), {**nothing, "note": {"older": True}, "store": None, "visit": None})
        # A glance at commit Z isn't a visit: commit W is still compared with Y, the last one read (tenth review)
        visit("2026-01-03T00:00:00Z", Z, {"c2y": "src/b.py", "c3z": "src/c.py"}, [], ["h-z"], glance=True)
        self.assertEqual(saved["visit"]["commit"], Y)
        v3 = visit("2026-01-04T00:00:00Z", "commit-w", {"c2y": "src/b.py", "c3w": "src/c.py"},
                   [{"track": "h-f", "code": "c2y"}], ["h-f"])
        self.assertEqual(marks(v3), {"since": Y, "sinceAt": "2026-01-02T00:00:00Z", "changed": ["c3w"], "added": [],
                                     "removed": ["src/a.py", "src/d.py"], "unchanged": ["h-f"]})
        # Progress an earlier page stored, without visits, marks nothing and names no files
        for kind in ("visit", "lastVisit"):
            saved.pop(kind)
        v4 = visit("2026-01-05T00:00:00Z", "commit-v", {"c1": "src/a.py"}, [{"track": "h-g", "code": "c1"}], ["h-g"])
        self.assertEqual((marks(v4), v4["note"]["since"]), (nothing, None))

    def test_the_update_notes_words(self):
        # What the reader reads, from updateView's note and marks (PR #7's ninth review: none of it was run)
        note = {"newCommit": True, "since": "commit-x", "changed": ["src/b.py"], "added": ["src/d.py"],
                "removed": ["db/gone.sql"], "unchanged": 2, "kept": 1}
        self.assertEqual(self.call("noteText", note),
                         "Updated since your last visit: new commits and a new review. Updated since then: b.py. New in the "
                         "PR: d.py. No longer in the PR: gone.sql. 2 hotspots are in files that didn't change, tagged below. "
                         "1 tick or answer carried over where nothing changed; the rest start fresh.")
        # Many files: the first two by name; none changed: said so; no last visit: no files at all
        many = {**note, "changed": [f"src/f{i}.py" for i in range(5)], "added": [], "removed": [], "unchanged": 1, "kept": 0}
        self.assertEqual(self.call("noteText", many),
                         "Updated since your last visit: new commits and a new review. Updated since then: f0.py, f1.py and "
                         "3 more. 1 hotspot is in a file that didn't change, tagged below. Your ticks and answers start fresh.")
        self.assertIn("No file in the PR changed since then.",
                      self.call("noteText", {**many, "changed": [], "unchanged": 0}))
        self.assertEqual(self.call("noteText", {**many, "since": None}),
                         "Updated since your last visit: new commits and a new review. Your ticks and answers start fresh.")
        self.assertEqual(self.call("noteText", {**many, "newCommit": False}),
                         "Updated since your last visit: a new review. Your ticks and answers start fresh.")
        self.assertIn("older version", self.call("noteText", {"older": True}))
        # Two files sharing a name are named with their folders (tenth review: "index.ts and index.ts")
        self.assertIn("Updated since then: web/index.ts and api/index.ts. New in the PR: d.py.",
                      self.call("noteText", {**note, "changed": ["web/index.ts", "api/index.ts"]}))
        # The map's line counts every file, those with no row too, and says when the visit was
        view = {"since": "0123456789abcdef", "sinceAt": "2026-10-28T12:00:00Z", "changed": ["c1", "c2"], "added": ["c3"],
                "removed": ["x.sql"]}
        self.assertEqual(self.call("sinceLine", view),
                         "Files since your last visit (0123456, 28 Oct): 2 updated, 1 new, 1 no longer in the PR.")
        self.assertEqual(self.call("sinceLine", {**view, "changed": [], "added": [], "removed": [], "sinceAt": None}),
                         "No file changed since your last visit (0123456).")
        self.assertEqual(self.call("sinceLine", {**view, "since": None}), "")
        # GitHub's diff since then, at a file's anchor, only between commit ids
        base, a, b = "https://github.com/o/r/compare/", "0" * 40, "f" * 40
        self.assertEqual(self.call("compareUrl", base, a, b, "diff-abc"), f"{base}{a}..{b}#diff-abc")
        self.assertEqual(self.call("compareUrl", base, a, b, None), f"{base}{a}..{b}")
        self.assertEqual([self.call("compareUrl", *args) for args in [("", a, b, None), (base, "main", b, None),
                                                                       (base, a, "", None)]], ["", "", ""])


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

    @property
    def link(self):
        """The PR's link as recorded (the repo artifact plus this PR's #page), else the bare URL."""
        p = self.state("url")
        return p.read_text(encoding="utf-8").strip() if p.exists() else URL

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

    def remember_pr(self, number):
        """Record the page's PR number, as the push hook does when gh pr create prints it."""
        record = json.loads(self.state("published.json").read_text(encoding="utf-8"))
        self.state("published.json").write_text(json.dumps({**record, "pr": number}), encoding="utf-8")

    def publish(self, url=URL):
        """prepare + render + record, as the skill does it."""
        self.prepare_with_examples()
        self.assertEqual(prx_cli(self.dir, "render").returncode, 0)
        r = prx_cli(self.dir, "record", url)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_a_renamed_file_is_one_file_at_its_new_path(self):
        # As the change map lists it, so what changed since a reader's last visit counts it once, on
        # its row (PR #7's tenth review: renames off made it an updated file and a new one)
        (Path(self.dir) / "src/search/api/SearchController.kt").unlink()
        self.write("src/search/api/Search.kt", "class SearchController\n")
        self.commit("rename the controller")
        self.prepare_with_examples()
        self.assertEqual(prx_cli(self.dir, "render").returncode, 0)
        page = self.state("html").read_text(encoding="utf-8")
        files = json.loads(re.search(r'id="prx-files">(.*?)</script>', page).group(1))
        self.assertIn("src/search/api/Search.kt", files.values())
        self.assertNotIn("src/search/api/SearchController.kt", files.values())
        self.assertEqual(len(files), 6)
        code = next(c for c, path in files.items() if path == "src/search/api/Search.kt")
        self.assertRegex(page, rf'<li class="crow[a-z ]*" data-code="{code}"><span class="cpath" title="src/search/api/Search.kt">')

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
        # The page leads with what could break, walks the diagram, and ends with a game
        for needle in ['<h3 class="headline">The two rank lists are fused on different scales</h3>',
                       'data-focus="4,7"', 'id="prx-tour"', 'id="tour-start"', 'class="cmap"', 'href="#hotspot-1"',
                       'id="game-deck"', 'class="qcard" data-answer="2"', '<ol class="steps">', "min read",
                       'id="prx-key"']:
            self.assertIn(needle, page)
        # Progress belongs to this PR's page, not the branch (a reused branch name gets its own page,
        # tenth review), and each tick and pick to its item's content, so an update keeps the ones for
        # what didn't change (the reviews' most repeated point) and a changed one starts fresh
        ctx = json.loads(self.state("context.json").read_text(encoding="utf-8"))
        self.assertIn(f'id="prx-key">"{ctx["page_id"]}"', page)

        def ids(html):
            return re.findall(r'data-(?:track|qid)="([hqs]-[0-9a-f]{10})"', html)
        before = ids(page)
        self.assertEqual(len(before), 3 + 3 + 3)  # hotspots, scenarios, questions
        ex = json.loads(self.explainer.read_text(encoding="utf-8"))
        ex["quiz"][0]["answer"] = (ex["quiz"][0]["answer"] + 1) % len(ex["quiz"][0]["options"])  # a fixed key
        ex["reviewer_questions"][0] += " Really?"
        self.explainer.write_text(json.dumps(ex), encoding="utf-8")
        self.assertEqual(prx_cli(self.dir, "render").returncode, 0)
        after = ids(self.state("html").read_text(encoding="utf-8"))
        self.assertEqual(len(set(before) - set(after)), 2)  # that scenario and that question start fresh
        self.assertEqual(len(set(before) & set(after)), 7)  # everything else carries over

        def codes(html):  # the hotspots' file changes
            return re.findall(r'data-track="h-[0-9a-f]{10}" data-code="(c-[0-9a-f]{10})"', html)

        def files(html):  # every changed file's
            return json.loads(re.search(r'id="prx-files">(.*?)</script>', html).group(1))
        # A hotspot re-rated (same wording, new risk or why) is a new judgement, so a new tick (nineteenth review)
        ex["hotspots"][1]["risk"] = "high" if ex["hotspots"][1]["risk"] != "high" else "medium"
        self.explainer.write_text(json.dumps(ex), encoding="utf-8")
        self.assertEqual(prx_cli(self.dir, "render").returncode, 0)
        rerated = ids(self.state("html").read_text(encoding="utf-8"))
        self.assertEqual(len(set(after) - set(rerated)), 1)
        after = rerated
        # A new reviewer's hotspot is a new risk even on the same code, so its tick doesn't carry over;
        # its file's code id does, which tells a returning reader that code is unchanged (twelfth review)
        code_before = codes(self.state("html").read_text(encoding="utf-8"))
        ex["hotspots"][0]["headline"] = "Reworded by the next reviewer"
        self.explainer.write_text(json.dumps(ex), encoding="utf-8")
        self.assertEqual(prx_cli(self.dir, "render").returncode, 0)
        reworded = self.state("html").read_text(encoding="utf-8")
        self.assertNotIn([i for i in after if i.startswith("h-")][0], ids(reworded))
        self.assertEqual(codes(reworded), code_before)
        # Every changed file has a code id, the deleted one and the lockfile too, the same on its map
        # row and its hotspots: the page compares them with the reader's last visit
        self.assertEqual(len(files(reworded)), 6)
        self.assertLessEqual(set(code_before), set(files(reworded)))
        for code in code_before:
            self.assertRegex(reworded, rf'<li class="crow[a-z ]*" data-code="{code}">')
        # ...and a push that changes that file's code changes its code id alone
        self.write("src/search/fusion/Rrf.kt", "fun fuse() = 2\n")
        self.commit("fuse differently")
        self.prepare_with_examples(ex, "--base", "main", "--no-fetch")
        self.assertEqual(prx_cli(self.dir, "render").returncode, 0)
        pushed_page = self.state("html").read_text(encoding="utf-8")
        pushed = codes(pushed_page)
        self.assertNotEqual(pushed[0], code_before[0])
        self.assertEqual(pushed[1:], code_before[1:])
        self.assertEqual(set(files(pushed_page)) - set(files(reworded)), {pushed[0]})
        # Questions and scenarios are about the whole change, so the same wording after a push that
        # changed code doesn't keep an old tick or pick (fourteenth review)
        self.assertFalse(set(i for i in ids(reworded) if i[0] in "qs") & set(i for i in ids(pushed_page) if i[0] in "qs"))
        # The game comes before the hotspots, whose headlines would give its answers away (eighth review)
        self.assertLess(page.index('<section id="predict"'), page.index('<section id="hotspots"'))
        # The game compares a pick with the reviewer's read; it doesn't call the reader wrong
        self.assertIn('id="game-live" aria-live="polite"', page)
        self.assertIn("independent reviewer&rsquo;s read", page)
        self.assertNotIn("Not quite", page)
        shutil.copy(SKILL / "examples" / "explainer.json", self.explainer)
        self.assertEqual(prx_cli(self.dir, "render").returncode, 0)

        self.assertEqual(prx_cli(self.dir, "record", URL).returncode, 0)
        body = self.state("pr-body.md").read_text(encoding="utf-8")
        self.assertIn(URL, body)
        self.assertIn("A guided tour of the change, the top 3 hotspots, 3 scenarios to predict before you approve", body)
        self.assertEqual(prx_cli(self.dir, "record", URL.replace("0b5e", "ffff")).returncode, 1)

        # The gate hook end to end, from the hook's own cwd and tool fields
        self.assertEqual(hook("pr_create_gate.py", {"cwd": self.dir, "tool_name": "Bash",
                              "tool_input": {"command": "gh pr create -t x"}}).returncode, 2)
        self.assertEqual(hook("pr_create_gate.py", {"cwd": self.dir, "tool_name": PS,
                              "tool_input": {"command": f"gh pr create -t x -b '{self.link}'"}}).returncode, 0)

        # Already fresh: pushing doesn't ask for a refresh
        self.assertEqual(hook("pr_push_refresh.py", {"cwd": self.dir, "tool_input": {"command": "git push"}}).stdout, "")

        # A new commit makes the explainer stale
        self.write("src/search/fusion/Rrf.kt", "fun fuse() = 1\n")
        self.commit("fix rank")
        self.assertIn("update mode", prx.gate_problem(f"gh pr create -b {self.link}", self.dir))
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
        ok = [f"gh pr create -t x -b '{self.link}'", f"gh pr create -t x --body={self.link}",
              f'gh pr create -t x --body-file "{body_file}"',
              f'gh pr create \\\n  --title x \\\n  --body-file "{body_file}"',  # options on continued lines
              f"gh pr create -t x -F - <<'EOF'\nSee {self.link}\nEOF",
              f"gh pr create -t x --body \"$(cat <<'EOF'\nDon't \"panic\" (really): {self.link}\nEOF\n)\"",
              f'body="see {self.link}"; gh pr create -t x --body "$body"',  # a variable: falls back to the command text
              f"gh pr create --base main -b {self.link} && curl -H x y"]
        for cmd in ok:
            self.assertIsNone(prx.gate_problem(cmd, self.dir), cmd)
        self.assertIsNone(prx.gate_problem(f"gh pr create -t x --body @'\nSee {self.link}\n'@", self.dir, PS))
        notes = Path(self.dir) / "notes.md"
        notes.write_text(URL, encoding="utf-8")
        blocked = ["gh pr create -t x -b hi", f"gh pr create -t 'see {self.link}' -b hi",
                   f"gh pr create -t x -b hi # {self.link}", f"git commit -F {notes} && gh pr create -t x -b hi",
                   f"gh pr create -b {self.link} --body 'No link'"]  # gh keeps the last --body
        for cmd in blocked:
            self.assertIn("Put the PR explainer link", prx.gate_problem(cmd, self.dir), cmd)
        self.assertIsNone(prx.gate_problem(f"gh pr create -t x -b{self.link}", self.dir))  # attached value
        self.assertIn("No PR explainer for branch 'main'", prx.gate_problem(f"gh pr create --head main -b {self.link}", self.dir))
        self.assertIn("No PR explainer for branch 'main'", prx.gate_problem(f"gh pr create -Hmain -b {self.link}", self.dir))
        self.assertIn("rendered against 'main'", prx.gate_problem(f"gh pr create --base develop -b {self.link}", self.dir))
        self.assertIn("rendered against 'main'", prx.gate_problem(f"gh pr create -Bdevelop -b {self.link}", self.dir))
        self.assertIn("rendered against 'main'", prx.gate_problem(f"gh pr create -dB develop -b {self.link}", self.dir))

    def test_changing_the_base_needs_a_republish(self):
        run(["git", "branch", "develop", "main"], self.dir)
        self.publish()  # rendered against main
        self.prepare("--base", "develop")  # a later prepare, without rendering or publishing
        self.assertIn("rendered against 'main'", prx.gate_problem(f"gh pr create --base develop -b {self.link}", self.dir))
        self.assertIsNone(prx.gate_problem(f"gh pr create --base main -b {self.link}", self.dir))
        # Without --base, gh targets the repo's default branch
        sha = run(["git", "rev-parse", "develop"], self.dir).stdout.strip()
        run(["git", "update-ref", "refs/remotes/origin/develop", sha], self.dir)
        run(["git", "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/develop"], self.dir)
        self.assertIn("gh targets the default branch 'develop'", prx.gate_problem(f"gh pr create -b {self.link}", self.dir))

    def test_default_base_without_a_recorded_default_branch(self):
        run(["git", "branch", "develop", "main"], self.dir)
        main_sha = run(["git", "rev-parse", "main"], self.dir).stdout.strip()
        run(["git", "remote", "add", "origin", "https://example.invalid/x.git"], self.dir)
        run(["git", "update-ref", "refs/remotes/origin/main", main_sha], self.dir)  # no refs/remotes/origin/HEAD
        self.publish()  # rendered against main
        # GitHub's default might be develop, so the gate asks rather than guessing main (review of e0b063b)
        self.assertIn("Pass --base main", prx.gate_problem(f"gh pr create -b {self.link}", self.dir))
        self.assertIsNone(prx.gate_problem(f"gh pr create --base main -b {self.link}", self.dir))
        self.assertIn("rendered against 'origin/main'", prx.gate_problem(f"gh pr create --base develop -b {self.link}", self.dir))
        run(["git", "config", f"branch.{self.BRANCH}.gh-merge-base", "develop"], self.dir)  # gh reads this first
        self.assertIn("gh targets the default branch 'develop'", prx.gate_problem(f"gh pr create -b {self.link}", self.dir))

    def test_unknown_default_base_needs_base_flag(self):
        self.publish()
        self.assertIsNone(prx.gate_problem(f"gh pr create -b {self.link}", self.dir))  # no remotes: gh can't make a PR
        run(["git", "remote", "add", "upstream", "https://example.invalid/x.git"], self.dir)  # but no origin
        self.assertIn("Pass --base main", prx.gate_problem(f"gh pr create -b {self.link}", self.dir))
        self.assertIsNone(prx.gate_problem(f"gh pr create --base main -b {self.link}", self.dir))

    def test_repo_artifact_holds_each_prs_page(self):
        out = self.prepare_with_examples()
        head = run(["git", "rev-parse", "HEAD"], self.dir).stdout.strip()
        page = f"{prx.branch_key(self.BRANCH)}-{head[:7]}"
        pub = out["publish"]
        self.assertIsNone(pub["artifact_url"])  # no repo artifact yet: the first publish creates it
        self.assertEqual(list(pub["files"]), [f"prs/{page}.html"])
        self.assertEqual(Path(pub["page"]).name, "index.html")
        self.assertEqual(prx_cli(self.dir, "render").returncode, 0)
        doc = Path(pub["files"][f"prs/{page}.html"]).read_text(encoding="utf-8")
        self.assertTrue(doc.startswith("<!doctype html>"))
        self.assertIn(prx.PAGE_MERMAID, doc)  # only the main page gets the viewer's diagrams
        self.assertIn(f"<title>{Path(self.dir).name} PR Explainers</title>", Path(pub["page"]).read_text(encoding="utf-8"))
        self.assertEqual(prx_cli(self.dir, "record", URL).returncode, 0)
        self.assertEqual(self.link, f"{URL}#{page}")
        self.assertIn(self.link, self.state("pr-body.md").read_text(encoding="utf-8"))
        # The PR must link its own page, not the artifact's main page or a longer page id (Greptile, PR #4)
        self.assertIn("Put the PR explainer link", prx.gate_problem(f"gh pr create -b {URL}", self.dir))
        self.assertIn("Put the PR explainer link", prx.gate_problem(f"gh pr create -b {self.link}-2", self.dir))
        self.assertIsNone(prx.gate_problem(f"gh pr create -b {self.link}", self.dir))
        self.assertIsNone(prx.gate_problem(f"gh pr create -b 'See {self.link}.'", self.dir))
        first = self.link
        # Another branch adds its page to the same artifact, not a new one
        run(["git", "checkout", "-q", "-b", "second"], self.dir)
        self.write("docs/second.md", "second\n")
        self.commit("second")
        out = self.prepare()
        shutil.copy(SKILL / "examples" / "intent.json", self.state("intent.json", "second"))
        shutil.copy(SKILL / "examples" / "explainer.json", self.explainer)
        self.assertEqual(out["publish"]["artifact_url"], URL)
        self.assertEqual(prx_cli(self.dir, "render").returncode, 0)
        stray = URL.replace("0b5e", "ffff")
        r = prx_cli(self.dir, "record", stray)
        self.assertEqual(r.returncode, 1)
        self.assertIn("This PR's page belongs in", r.stderr)
        for flag in ("--replace", "--new-artifact"):  # no command that moves the repo to follow
            self.assertNotIn(flag, r.stderr)
        self.assertEqual(prx_cli(self.dir, "record", stray, "--replace").returncode, 1)
        self.assertEqual(prx.repo_artifact(prx.Repo(self.dir)), URL)
        # A repo.json written before record stripped a trailing / still names the same artifact
        prx.write_text(prx.repo_file(prx.Repo(self.dir), "repo.json"), json.dumps({"artifact_url": URL + "/"}))
        self.assertEqual(prx_cli(self.dir, "record", URL).returncode, 0)
        self.assertRegex(self.state("url", "second").read_text(encoding="utf-8").strip(), "^" + re.escape(URL) + "#second-[0-9a-f]{7}$")
        # An update keeps the PR's page; a new PR on a reused branch name gets another
        run(["git", "checkout", "-q", self.BRANCH], self.dir)
        self.write("docs/more.md", "more\n")
        self.commit("more")
        out = self.prepare_with_examples()
        self.assertEqual((out["mode"], out["publish"]["link"]), ("update", first))
        out = self.prepare_with_examples(None, "--new")
        self.assertNotIn(f"prs/{page}.html", out["publish"]["files"])
        # prune never touches the repo-level folder, and a branch called "index" can't clash with it
        self.assertEqual(Path(pub["page"]).parent.name, prx.REPO_DIR)
        dry = json.loads(prx_cli(self.dir, "prune", "--dry-run").stdout)["would_remove"]
        self.assertNotIn(prx.REPO_DIR, dry)

    def test_open_prs_stay_in_their_artifact_when_the_repo_moves(self):
        self.publish()
        first = self.link
        moved = URL.replace("0b5e", "4e14")
        # New PRs move to a new artifact (the old one filled up), recorded from another branch
        run(["git", "checkout", "-q", "-b", "later"], self.dir)
        self.write("docs/later.md", "later\n")
        self.commit("later")
        self.prepare()
        shutil.copy(SKILL / "examples" / "intent.json", self.state("intent.json", "later"))
        shutil.copy(SKILL / "examples" / "explainer.json", self.explainer)
        self.assertEqual(prx_cli(self.dir, "render").returncode, 0)
        self.assertEqual(prx_cli(self.dir, "record", moved, "--new-artifact").returncode, 0)
        self.assertEqual(prx.repo_artifact(prx.Repo(self.dir)), moved)
        # The first PR's update still goes to the artifact its link points at
        run(["git", "checkout", "-q", self.BRANCH], self.dir)
        self.write("docs/more.md", "more\n")
        self.commit("more")
        out = self.prepare_with_examples()
        self.assertEqual(out["publish"]["artifact_url"], URL)
        self.assertEqual(prx_cli(self.dir, "render").returncode, 0)
        self.assertEqual(prx_cli(self.dir, "record", moved).returncode, 1)
        self.assertEqual(prx_cli(self.dir, "record", URL + "/").returncode, 0)  # a trailing / is the same artifact
        self.assertEqual((self.link, prx.repo_artifact(prx.Repo(self.dir))), (first, moved))
        # --new-artifact only moves to a new artifact: not the one the PR is in (which would point
        # every new PR back at the old artifact)
        self.assertEqual(prx_cli(self.dir, "record", URL, "--new-artifact").returncode, 1)
        self.assertEqual(prx.repo_artifact(prx.Repo(self.dir)), moved)
        # Moving an open PR (its artifact was deleted) is explicit too, and says its link changed
        r = prx_cli(self.dir, "record", moved, "--new-artifact")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.link, f"{moved}#{prx.split_link(first)[1]}")
        self.assertIn("replace the old link", json.loads(r.stdout)["warnings"][0])
        # ...nor one the repo moved away from
        r = prx_cli(self.dir, "record", URL, "--new-artifact")
        self.assertEqual(r.returncode, 1)
        self.assertIn("moved away from", r.stderr)
        self.assertEqual(prx.repo_artifact(prx.Repo(self.dir)), moved)

    def test_new_pr_at_the_same_commit_gets_a_new_page(self):
        self.publish()
        first = self.link
        out = self.prepare_with_examples(None, "--new")  # same HEAD as the archived page
        self.assertEqual(list(out["publish"]["files"]), [f"prs/{prx.split_link(first)[1]}-2.html"])

    def test_branch_recorded_before_the_repo_artifact_keeps_its_own(self):
        self.publish()
        own = URL.replace("0b5e", "1e9a")  # how PRs #1-#3 were recorded: an artifact each, no #page
        self.state("url").write_text(own + "/\n", encoding="utf-8")  # older records could end in / (Greptile, PR #4)
        self.write("docs/more.md", "more\n")
        self.commit("more")
        out = self.prepare_with_examples()
        self.assertEqual((out["publish"]["files"], out["publish"]["page"]), ({}, out["paths"]["html"]))
        self.assertEqual(out["publish"]["artifact_url"].rstrip("/"), own)
        self.assertEqual(prx_cli(self.dir, "render").returncode, 0)
        self.assertEqual(prx_cli(self.dir, "record", URL, "--new-artifact").returncode, 1)  # it has no page to move
        # Its single page would replace the repo artifact's main page
        r = prx_cli(self.dir, "record", URL, "--replace")
        self.assertEqual(r.returncode, 1)
        self.assertIn("replaces the main page", r.stderr)
        self.assertEqual(prx_cli(self.dir, "record", own).returncode, 0)
        self.assertEqual(self.link, own)

    def test_page_ids_and_repo_name(self):
        repo = prx.Repo(self.dir)
        # Long branch keys are cut but keep a hash of the whole key (Greptile, PR #4)
        a, b = prx.new_page_id(repo, "a" * 120, "0" * 40), prx.new_page_id(repo, "a" * 100 + "b" * 20, "0" * 40)
        self.assertNotEqual(a, b)
        self.assertTrue(all(prx.PAGE_ID_RE.match(p) for p in (a, b)), (a, b))
        # The repo artifact is named after origin, the same in every clone and worktree
        self.assertEqual(prx.repo_name(repo), Path(self.dir).name)  # no origin: the folder
        for remote, name in [("https://github.com/limittox/pr-explainer.git", "pr-explainer"),
                             ("git@github.com:limittox/pr-explainer.git", "pr-explainer"),
                             ("https://github.com/limittox/pr-explainer/", "pr-explainer")]:
            run(["git", "remote", "remove", "origin"], self.dir, check=False)
            run(["git", "remote", "add", "origin", remote], self.dir)
            self.assertEqual(prx.repo_name(repo), name, remote)

    def test_prepare_asks_origin_for_its_default_branch(self):
        remote = tempfile.mkdtemp(prefix="prx-remote-")
        try:
            run(["git", "init", "-q", "--bare", remote], self.dir)
            run(["git", "branch", "develop", "main"], self.dir)
            run(["git", "remote", "add", "origin", remote], self.dir)
            run(["git", "push", "-q", "origin", "main", "develop"], self.dir)
            run(["git", "symbolic-ref", "HEAD", "refs/heads/develop"], remote)  # origin's default is develop
            # This clone has origin/main and origin/develop but doesn't record the default.
            # Without fetching, main is only a guess, and prepare says so (review of f000d54)
            out = json.loads(prx_cli(self.dir, "prepare", "--no-fetch").stdout)
            self.assertEqual(out["base_ref"], "origin/main")
            self.assertTrue(any("is a guess" in w for w in out["warnings"]), out["warnings"])
            # A named base doesn't need origin's default, so prepare doesn't ask for it
            out = json.loads(prx_cli(self.dir, "prepare", "--base", "main").stdout)
            self.assertEqual((out["base_ref"], out["warnings"]), ("origin/main", []))
            self.assertEqual(prx.origin_head(prx.Repo(self.dir)), "")
            # With fetching and no base, prepare asks origin first instead of guessing
            out = json.loads(prx_cli(self.dir, "prepare").stdout)
            self.assertEqual((out["base_ref"], out["warnings"]), ("origin/develop", []))
            # An update keeps the base the page was published against, and says when that
            # isn't origin's default: the published base may have been a guess (review of 3e8bfe1)
            self.publish()  # against main
            self.write("docs/more.md", "more\n")
            self.commit("more")
            out = json.loads(prx_cli(self.dir, "prepare", "--no-fetch").stdout)
            self.assertEqual((out["mode"], out["base_ref"]), ("update", "origin/main"))
            self.assertTrue(any("gh pr edit --base develop" in w for w in out["warnings"]), out["warnings"])
        finally:
            shutil.rmtree(remote, ignore_errors=True)

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
            self.assertIn("No PR explainer for branch 'elsewhere'", prx.gate_problem(f"cd {o} && gh pr create -b {self.link}", self.dir))
            self.assertIn("No PR explainer", prx.gate_problem(f"pushd {o}; gh pr create -b {self.link}", self.dir))
            self.assertIsNone(prx.gate_problem(f"pushd {o} && popd && gh pr create -b {self.link}", self.dir))
            self.assertIsNone(prx.gate_problem(f'cd "$(git rev-parse --show-toplevel)" && gh pr create -b {self.link}', self.dir))
            self.assertIn("can't tell which repository", prx.gate_problem(f'cd "$REPO" && gh pr create -b {self.link}', self.dir))
            self.assertIn("No PR explainer", prx.gate_problem(f"Set-Location -Path {o}; gh pr create -b {self.link}", self.dir, PS))
            self.assertIn("No PR explainer", prx.gate_problem(f"Set-Location -Path:{o}; gh pr create -b {self.link}", self.dir, PS))
            # A cd in a child shell never reaches a gh pr create in the main shell (Greptile, PR #2)
            for cmd in [f"bash -c 'cd {o}'; gh pr create -b {self.link}", f"(cd {o}); gh pr create -b {self.link}",
                        f'echo "$(cd {o} && pwd)"; gh pr create -b {self.link}', f"cd {o} | true; gh pr create -b {self.link}",
                        f"cd {o} & gh pr create -b {self.link}", f"cd {o} && true & gh pr create -b {self.link}"]:
                self.assertIsNone(prx.gate_problem(cmd, self.dir), cmd)
            # Inside a program the parser doesn't model, it can't tell where or how gh runs
            # (an abbreviated wrapper option, `env --ch DIR`, ends up here too: its value reads as the program)
            for cmd in [f"setsid gh pr create -b {self.link}", f"find . -maxdepth 0 -exec gh pr create -b {self.link} ;",
                        f"env --ch {o} gh pr create -b {self.link}"]:
                self.assertIn("runs inside another program", prx.gate_problem(cmd, self.dir), cmd)
            # ...but one in the same child shell as gh pr create might, and the gate can't tell. Nor
            # can it for PowerShell's $(...), which runs in the main shell at a point the list doesn't show.
            missing = f"{o}/missing"
            for cmd, tool in [(f"(cd {o} && gh pr create -b {self.link})", "Bash"),
                              (f"bash -c 'cd {o} && gh pr create -b {self.link}'", "Bash"),
                              (f'cd {o} && echo "$(gh pr create -b {self.link})"', "Bash"),
                              (f'env -C "$X" gh pr create -b {self.link}', "Bash"),
                              (f"env -C {missing} gh pr create -b {self.link}", "Bash"),
                              (f"wsl --cd {missing} gh pr create -b {self.link}", "Bash"),
                              (f'Write-Host "$(Set-Location {o})"; gh pr create -b {self.link}', PS),
                              (f'Set-Location {o}; Write-Host "$(gh pr create -b {self.link})"', PS)]:
                self.assertIn("can't tell which repository", prx.gate_problem(cmd, self.dir, tool), cmd)
            # Pipeline stages after a top-level cd, and wrappers that change directory, are followed
            parent, name = Path(other).parent.as_posix(), Path(other).name
            encoded = base64.b64encode(f"gh pr create -b {self.link}".encode("utf-16-le")).decode()
            for cmd in [f"cd {o} && gh pr create -b {self.link} | tee log", f"env -C {o} gh pr create -b {self.link}",
                        f"env --chdir={o} gh pr create -b {self.link}", f"pwsh -wd {o} -c 'gh pr create -b {self.link}'",
                        f"env -u FOO -C {o} gh pr create -b {self.link}", f"pwsh -WorkingDirectory:{o} -c 'gh pr create -b {self.link}'",
                        f"sudo -D {o} gh pr create -b {self.link}", f"env -C {parent} env -C {name} gh pr create -b {self.link}",
                        f"pwsh -ec {encoded} -wd {o}"]:
                self.assertIn("No PR explainer for branch 'elsewhere'", prx.gate_problem(cmd, self.dir), cmd)
            # Within one env the last -C wins: from the parent, `-C <this repo> -C <other>` runs in other
            here = Path(self.dir)
            self.assertEqual(here.parent, Path(other).parent)
            cmd = f"env -C {here.name} -C {name} gh pr create -b {self.link}"
            self.assertIn("No PR explainer for branch 'elsewhere'", prx.gate_problem(cmd, str(here.parent)))
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
        self.assertIn("isn't in this branch's history", prx.gate_problem(f"gh pr create -b {self.link}", self.dir))
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
        self.assertIn("already in main", prx.gate_problem(f"gh pr create -b {self.link}", self.dir))
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
            self.assertIn("--new", prx.gate_problem(f"gh pr create -b {self.link}", self.dir))
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
        sha = json.loads(prx_cli(self.dir, "status").stdout)["explained_sha"]
        self.write("docs/next.md", "follow-up\n")
        self.commit("follow-up after a squash merge")
        merged = {"number": 7, "state": "MERGED", "headRefOid": sha, "baseRefName": "main", "url": "u"}
        r = prx_with_gh(self.dir, "prepare", "--no-fetch", prs=[merged])
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("GitHub shows PR #7 for this branch as merged", r.stderr)
        r = prx_with_gh(self.dir, "prepare", "--no-fetch", prs=[{**merged, "number": 8, "state": "CLOSED"}])
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("GitHub shows PR #8 for this branch as closed", r.stderr)
        # A fork's open PR from a branch with the same name isn't this branch's, so it doesn't hide the merge
        main = run(["git", "rev-parse", "main"], self.dir).stdout.strip()
        fork = {"number": 12, "state": "OPEN", "headRefOid": main, "baseRefName": "release", "url": "u"}
        stranger = {**fork, "number": 13, "headRefOid": "f" * 40}
        r = prx_with_gh(self.dir, "prepare", "--no-fetch", prs=[merged, fork, stranger])
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("GitHub shows PR #7 for this branch as merged", r.stderr)
        # GitHub never reopens a merged PR, so an open one from the branch is a new PR and the merged page stays
        newer = {"number": 9, "state": "OPEN", "headRefOid": sha, "baseRefName": "main", "url": "u"}
        r = prx_with_gh(self.dir, "prepare", "--no-fetch", prs=[merged, newer])
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("as merged", r.stderr)
        r = prx_with_gh(self.dir, "prepare", "--no-fetch", "--same-pr", prs=[merged, newer])
        self.assertEqual(r.returncode, 1, r.stdout)  # a merge is final
        # A closed PR can be reopened: with one of the branch's PRs open, it's that PR's update
        closed = {**merged, "number": 8, "state": "CLOSED"}
        r = prx_with_gh(self.dir, "prepare", "--no-fetch", prs=[closed, newer])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["mode"], "update")
        # Two of the branch's PRs open: it can't pick a base, and says so
        r = prx_with_gh(self.dir, "prepare", "--no-fetch", prs=[newer, {**newer, "number": 10, "baseRefName": "release"}])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(any("#9 and #10 are all open" in w for w in json.loads(r.stdout)["warnings"]))

    def test_prepare_stops_when_github_cant_say_whether_the_pr_was_squash_merged(self):
        self.publish()
        # Nothing new to explain: republishing the same commit can't overwrite another PR's page
        login = "To get started with GitHub CLI, please run: gh auth login"
        self.assertEqual(prx_with_gh(self.dir, "prepare", "--no-fetch", fail=login).returncode, 0)
        self.write("docs/next.md", "follow-up\n")
        self.commit("follow-up")
        # With one artifact for the repo, a wrong "same PR" overwrites a merged PR's page, so it asks
        r = prx_with_gh(self.dir, "prepare", "--no-fetch", fail=login)
        self.assertEqual(r.returncode, 1, r.stdout)
        for text in ("gh auth login", "--same-pr", "--new"):
            self.assertIn(text, r.stderr)
        r = prx_with_gh(self.dir, "prepare", "--no-fetch", "--same-pr", fail=login)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(any("Couldn't ask GitHub" in w for w in json.loads(r.stdout)["warnings"]))
        # An answer that isn't a list of PRs is a problem too, not a crash
        r = prx_with_gh(self.dir, "prepare", "--no-fetch", prs={"message": "Bad credentials"})
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("didn't answer with a list", r.stderr)
        # gh missing altogether (keep git, drop gh from PATH) is the same: it can't tell
        env = {k: v for k, v in ENV.items() if k != "PRX_NO_GITHUB"}
        env["PATH"] = os.pathsep.join(p for p in env["PATH"].split(os.pathsep)
                                      if not (Path(p) / "gh.exe").exists() and not (Path(p) / "gh").exists())
        r = subprocess.run([sys.executable, str(SKILL / "prx.py"), "prepare", "--base", "main", "--no-fetch"],
                           cwd=self.dir, env=env, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("Couldn't ask GitHub", r.stderr)
        # The branch remade from main (after a squash merge, or a rebase): the rebase message alone
        # would offer --same-pr blind, so GitHub's silence is named too
        run(["git", "checkout", "-q", "-B", self.BRANCH, "main"], self.dir)
        self.write("docs/redo.md", "redo\n")
        self.commit("redo")
        r = prx_with_gh(self.dir, "prepare", "--no-fetch", fail=login)
        self.assertEqual(r.returncode, 1, r.stdout)
        for text in ("isn't in this branch's history", "Couldn't ask GitHub", "only if"):
            self.assertIn(text, r.stderr)
        self.assertEqual(self.prepare("--new")["mode"], "create")

    def test_update_explains_against_the_open_prs_base(self):
        # A stacked PR: published against its parent branch, which later merged and was deleted,
        # and GitHub retargeted the PR to main
        run(["git", "branch", "stack", "main"], self.dir)
        self.prepare_with_examples(None, "--base", "stack")
        self.assertEqual(prx_cli(self.dir, "render").returncode, 0)
        self.assertEqual(prx_cli(self.dir, "record", URL).returncode, 0)
        run(["git", "branch", "-q", "-D", "stack"], self.dir)
        self.write("docs/next.md", "follow-up\n")
        self.commit("follow-up")
        r = prx_cli(self.dir, "prepare", "--no-fetch")  # without GitHub it keeps the published base
        self.assertEqual(r.returncode, 1)
        self.assertIn("Couldn't find the base branch", r.stderr)
        head = run(["git", "rev-parse", "HEAD"], self.dir).stdout.strip()
        pr = {"number": 4, "state": "OPEN", "headRefOid": head, "baseRefName": "main", "url": "u"}
        main = run(["git", "rev-parse", "main"], self.dir).stdout.strip()
        fork = {"number": 12, "state": "OPEN", "headRefOid": main, "baseRefName": "release", "url": "u"}
        r = prx_with_gh(self.dir, "prepare", "--no-fetch", prs=[pr, fork])  # a fork's same-name PR doesn't count
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual((out["mode"], out["base_ref"]), ("update", "main"))
        self.assertTrue(any("PR #4 now targets main" in w for w in out["warnings"]), out["warnings"])
        # Naming another base than the open PR's stops: the page is #4's, and explaining it against
        # main would show #4 a diff it doesn't merge, and hand the page to the next PR into main (a
        # backport) once the hook records it (nineteenth review). Retarget #4; a backport goes on a
        # branch of its own, since --new here would leave #4's page behind (twenty-first review)
        r = prx_with_gh(self.dir, "prepare", "--no-fetch", "--base", "main", prs=[{**pr, "baseRefName": "release"}])
        self.assertEqual(r.returncode, 1, r.stdout)
        for text in ("PR #4, the one this page explains, targets release", "gh pr edit 4 --base main",
                     "goes on a branch of its own"):
            self.assertIn(text, r.stderr)
        self.assertNotIn("--new", r.stderr)

    def test_the_pages_own_pr_is_told_from_its_siblings(self):
        self.publish()
        c1 = json.loads(prx_cli(self.dir, "status").stdout)["explained_sha"]
        self.write("docs/next.md", "follow-up\n")
        self.commit("follow-up")
        head = run(["git", "rev-parse", "HEAD"], self.dir).stdout.strip()
        open4 = {"number": 4, "state": "OPEN", "headRefOid": head, "baseRefName": "main", "url": "u"}
        # No number remembered: a sibling merged into another base (a backport) doesn't finish the page
        # while the branch's PR into the page's base is open (tenth review)...
        backport = {**open4, "number": 9, "state": "MERGED", "headRefOid": c1, "baseRefName": "release"}
        r = prx_with_gh(self.dir, "prepare", "--no-fetch", prs=[open4, backport])
        self.assertEqual(r.returncode, 0, r.stderr)
        # ...but a PR merged into the page's own base does
        r = prx_with_gh(self.dir, "prepare", "--no-fetch", prs=[open4, {**backport, "baseRefName": "main"}])
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("PR #9 for this branch as merged", r.stderr)
        # The page's PR closed and opened again from the same page: the stop names the new PR, and
        # --same-pr moves the page to it
        record = json.loads(self.state("published.json").read_text(encoding="utf-8"))
        self.state("published.json").write_text(json.dumps({**record, "pr": 4}), encoding="utf-8")
        again = [{**open4, "state": "CLOSED", "headRefOid": c1}, {**open4, "number": 12}]
        r = prx_with_gh(self.dir, "prepare", "--no-fetch", prs=again)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("GitHub also shows PR #12 from this branch open", r.stderr)
        self.assertEqual(prx_with_gh(self.dir, "prepare", "--no-fetch", "--same-pr", prs=again).returncode, 0)
        self.assertEqual(json.loads(self.state("context.json").read_text(encoding="utf-8"))["pr"], 12)
        # The open PR's base isn't in this clone. Without asking origin (--no-fetch, offline) it can't
        # tell a fork's clone from a stale one, so it stops (eleventh review)...
        upstream = [{**open4, "baseRefName": "upstream-only"}]
        r = prx_with_gh(self.dir, "prepare", "--no-fetch", prs=upstream)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("targets upstream-only, which this clone doesn't have (--no-fetch)", r.stderr)
        # ...and once origin says it has no such branch (origin is a fork's), it keeps the published
        # base and says why, without suggesting a retarget of the live PR
        remote = tempfile.mkdtemp(prefix="prx-remote-")
        try:
            run(["git", "init", "-q", "--bare", "-b", "main", remote], self.dir)
            run(["git", "remote", "add", "origin", remote], self.dir)
            # a branch that only ends in the name doesn't count as origin having it (thirteenth review)
            run(["git", "push", "-q", "origin", "main", self.BRANCH, "main:refs/heads/team/upstream-only"], self.dir)
            r = prx_with_gh(self.dir, "prepare", prs=upstream)
            self.assertEqual(r.returncode, 0, r.stderr)
            out = json.loads(r.stdout)
            self.assertIn(out["base_ref"], ("main", "origin/main"))
            self.assertTrue(any("targets upstream-only, which origin doesn't have" in w for w in out["warnings"]))
            self.assertFalse(any("gh pr edit" in w for w in out["warnings"]), out["warnings"])
        finally:
            shutil.rmtree(remote, ignore_errors=True)

    def test_a_merge_is_final_at_the_same_commit_too(self):
        # The page remembers PR #7 at HEAD; #7 is merged and a sibling #9 is opened at the same commit
        # into another base. A re-run at that commit (a new --base, say) must not hand #7's page to #9
        # (eleventh review)
        self.publish()
        head = run(["git", "rev-parse", "HEAD"], self.dir).stdout.strip()
        record = json.loads(self.state("published.json").read_text(encoding="utf-8"))
        self.state("published.json").write_text(json.dumps({**record, "pr": 7}), encoding="utf-8")
        merged7 = {"number": 7, "state": "MERGED", "headRefOid": head, "baseRefName": "main", "url": "u"}
        sibling = {**merged7, "number": 9, "state": "OPEN", "baseRefName": "release"}
        run(["git", "branch", "release", "main"], self.dir)
        r = prx_with_gh(self.dir, "prepare", "--no-fetch", "--base", "release", prs=[merged7, sibling])
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("GitHub shows PR #7 for this branch as merged", r.stderr)
        # The same commit against the same base renders #7's page as it was (a typo fixed in its
        # explainer), with a warning, not a stop that only --new would clear (eighteenth review)
        r = prx_with_gh(self.dir, "prepare", "--no-fetch", prs=[merged7, sibling])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["base_ref"], "main")
        self.assertTrue(any("re-renders its page at the same commit and base" in w for w in json.loads(r.stdout)["warnings"]))
        # A remembered PR that's closed doesn't hand its number to the open sibling without --same-pr
        r = prx_with_gh(self.dir, "prepare", "--no-fetch", prs=[{**merged7, "state": "CLOSED"}, sibling])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(self.state("context.json").read_text(encoding="utf-8"))["pr"], 7)
        self.assertEqual(json.loads(r.stdout)["base_ref"], "main")
        # The same commit against a new base is a new page under the same link: with GitHub silent it
        # asks, as a new commit does (thirteenth review)
        login = "To get started with GitHub CLI, please run: gh auth login"
        self.assertEqual(prx_with_gh(self.dir, "prepare", "--no-fetch", fail=login).returncode, 0)  # same base
        r = prx_with_gh(self.dir, "prepare", "--no-fetch", "--base", "release", fail=login)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("Couldn't ask GitHub", r.stderr)
        # The remembered PR alone decides: closed (it may be reopened) outranks a merged backport that
        # contains the page's commit, so --same-pr can answer (thirteenth review)
        self.write("docs/next.md", "follow-up\n")
        self.commit("follow-up")
        backport = {**merged7, "number": 9, "baseRefName": "release"}  # merged, head at the page's commit
        r = prx_with_gh(self.dir, "prepare", "--no-fetch", prs=[backport, {**merged7, "state": "CLOSED"}])
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("GitHub shows PR #7 for this branch as closed", r.stderr)
        r = prx_with_gh(self.dir, "prepare", "--no-fetch", "--same-pr", prs=[backport, {**merged7, "state": "CLOSED"}])
        self.assertEqual(r.returncode, 0, r.stderr)
        # With a backport into another base open, --same-pr for the closed #7 means reopening it: the
        # page keeps #7 and its base instead of moving to the backport (sixteenth review)
        head = run(["git", "rev-parse", "HEAD"], self.dir).stdout.strip()
        open9 = {**backport, "state": "OPEN", "headRefOid": head}
        r = prx_with_gh(self.dir, "prepare", "--no-fetch", prs=[open9, {**merged7, "state": "CLOSED"}])
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertNotIn("the page moves to #9", r.stderr)
        r = prx_with_gh(self.dir, "prepare", "--no-fetch", "--same-pr", prs=[open9, {**merged7, "state": "CLOSED"}])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["base_ref"], "main")
        self.assertEqual(json.loads(self.state("context.json").read_text(encoding="utf-8"))["pr"], 7)
        # #7 closed, opened again from the page as #11 into the same base, and #11 squash-merged since
        # the page was recorded: the merge is final, --same-pr or not (seventeenth review)
        published_at = json.loads(self.state("published.json").read_text(encoding="utf-8"))["published_at"]
        merged11 = {**merged7, "number": 11, "headRefOid": head, "mergedAt": "2999-01-01T00:00:00Z"}
        for flags in ([], ["--same-pr"]):
            r = prx_with_gh(self.dir, "prepare", "--no-fetch", *flags, prs=[merged11, {**merged7, "state": "CLOSED"}])
            self.assertEqual(r.returncode, 1, r.stdout)
            self.assertIn("GitHub shows PR #11 for this branch as merged", r.stderr)
        # ...but one merged before the page was recorded was an earlier PR, and #7 decides
        r = prx_with_gh(self.dir, "prepare", "--no-fetch", "--same-pr",
                        prs=[{**merged11, "mergedAt": "2000-01-01T00:00:00Z"}, {**merged7, "state": "CLOSED"}])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertLess("2000-01-01T00:00:00Z", published_at)

    def test_the_pages_pr_is_found_whatever_the_branch_is_called_on_github(self):
        # The branch pushes under another name, or its name has too many PRs for the list: the page's
        # own PR is asked for by number, so it isn't taken for gone (eleventh review)
        self.publish()
        record = json.loads(self.state("published.json").read_text(encoding="utf-8"))
        self.state("published.json").write_text(json.dumps({**record, "pr": 12}), encoding="utf-8")
        self.write("docs/next.md", "follow-up\n")
        self.commit("follow-up")
        head = run(["git", "rev-parse", "HEAD"], self.dir).stdout.strip()
        open12 = {"number": 12, "state": "OPEN", "headRefOid": head, "baseRefName": "main", "url": "u"}
        r = prx_with_gh(self.dir, "prepare", "--no-fetch", prs=[], view=[open12])
        self.assertEqual(r.returncode, 0, r.stderr)
        merged12 = {**open12, "state": "MERGED"}
        r = prx_with_gh(self.dir, "prepare", "--no-fetch", prs=[], view=[merged12])
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("GitHub shows PR #12 for this branch as merged", r.stderr)
        # gh pr view failing for another reason than "no such PR" means GitHub couldn't be asked (twelfth review)
        r = prx_with_gh(self.dir, "prepare", "--no-fetch", prs=[], view_fail="error connecting to api.github.com")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("Couldn't ask GitHub about this branch's PRs (error connecting to api.github.com)", r.stderr)
        # The list is asked for the local branch name, not branch.<b>.merge: a branch made from
        # origin/main tracks main, and asking about head main would miss its own merged PR (twelfth review)
        run(["git", "config", f"branch.{self.BRANCH}.remote", "origin"], self.dir)
        run(["git", "config", f"branch.{self.BRANCH}.merge", "refs/heads/main"], self.dir)
        self.state("published.json").write_text(json.dumps(record), encoding="utf-8")  # no number remembered
        c1 = record["sha"]
        r = prx_with_gh(self.dir, "prepare", "--no-fetch", prs=[{**merged12, "headRefOid": c1}], head=self.BRANCH)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("GitHub shows PR #12 for this branch as merged", r.stderr)
        # Pushed under another name that holds the page's commit (git push -u origin HEAD:pushed-as),
        # with no number remembered: that name is asked too (sixteenth review)
        run(["git", "config", f"branch.{self.BRANCH}.merge", "refs/heads/pushed-as"], self.dir)
        run(["git", "update-ref", "refs/remotes/origin/pushed-as", head], self.dir)
        r = prx_with_gh(self.dir, "prepare", "--no-fetch", prs=[{**merged12, "headRefOid": c1}], head="pushed-as")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("GitHub shows PR #12 for this branch as merged", r.stderr)
        # A remembered number GitHub has, but on another branch's PR (a wrong number), isn't the page's:
        # it doesn't decide merged or the base, and prepare asks (sixteenth review)
        self.state("published.json").write_text(json.dumps({**record, "pr": 12}), encoding="utf-8")
        stranger = {**merged12, "headRefName": "someone-else", "headRefOid": "d" * 40}
        r = prx_with_gh(self.dir, "prepare", "--no-fetch", prs=[], view=[stranger], head="nobody")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("doesn't include PR #12", r.stderr)

    def test_merged_after_allows_for_a_clock_running_ahead(self):
        # published_at is this clock's, mergedAt GitHub's: a merge a minute "before" the page is
        # within the grace and counts as after, which stops prepare (eighteenth review)
        at = "2026-01-01T00:10:00Z"
        self.assertTrue(prx.merged_after({"mergedAt": "2026-01-01T00:09:00Z"}, at))
        self.assertFalse(prx.merged_after({"mergedAt": "2026-01-01T00:07:00Z"}, at))
        self.assertTrue(prx.merged_after({"mergedAt": "2026-01-01T00:11:00Z"}, at))
        self.assertFalse(prx.merged_after({"mergedAt": None}, at))
        self.assertFalse(prx.merged_after({"mergedAt": "2026-01-01T00:11:00Z"}, None))

    def test_a_pr_merged_behind_the_pages_commit_is_told_by_when_it_merged(self):
        # No number remembered. The page was refreshed for c2 before it was pushed, and PR #7 was
        # squash-merged at c1: it merged after the page was recorded, so the page was #7's and new
        # work needs --new (fourteenth review)...
        self.publish()
        c1 = json.loads(prx_cli(self.dir, "status").stdout)["explained_sha"]
        self.write("docs/a.md", "a\n")
        self.commit("not pushed yet")
        self.prepare_with_examples()
        self.assertEqual(prx_cli(self.dir, "render").returncode, 0)
        self.assertEqual(prx_cli(self.dir, "record", URL).returncode, 0)
        published_at = json.loads(self.state("published.json").read_text(encoding="utf-8"))["published_at"]
        self.write("docs/b.md", "b\n")
        self.commit("after the merge")
        merged7 = {"number": 7, "state": "MERGED", "headRefOid": c1, "baseRefName": "main", "url": "u",
                   "mergedAt": "2999-01-01T00:00:00Z"}
        r = prx_with_gh(self.dir, "prepare", "--no-fetch", prs=[merged7])
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("GitHub shows PR #7 for this branch as merged", r.stderr)
        # ...but a PR merged before the page was recorded was an earlier PR from the branch, and the
        # page belongs to the next one
        r = prx_with_gh(self.dir, "prepare", "--no-fetch", prs=[{**merged7, "mergedAt": "2000-01-01T00:00:00Z"}])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertLess("2000-01-01T00:00:00Z", published_at)
        # A page recorded before published_at was kept takes the record's file time (sixteenth review)
        record = json.loads(self.state("published.json").read_text(encoding="utf-8"))
        del record["published_at"]
        self.state("published.json").write_text(json.dumps(record), encoding="utf-8")
        r = prx_with_gh(self.dir, "prepare", "--no-fetch", prs=[merged7])
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("GitHub shows PR #7 for this branch as merged", r.stderr)
        # ...and recording that older page again at the same commit keeps the file time rather than
        # moving it to now (seventeenth review)
        run(["git", "reset", "-q", "--hard", "HEAD~1"], self.dir)
        os.utime(self.state("published.json"), (946684800, 946684800))  # 2000-01-01
        r = prx_with_gh(self.dir, "prepare", "--no-fetch", fail="gh auth login")
        self.assertEqual(r.returncode, 0, r.stderr)
        shutil.copy(SKILL / "examples" / "intent.json", self.state("intent.json"))
        shutil.copy(SKILL / "examples" / "explainer.json", json.loads(r.stdout)["paths"]["explainer"])
        self.assertEqual(prx_cli(self.dir, "render").returncode, 0)
        self.assertEqual(prx_cli(self.dir, "record", URL).returncode, 0)
        self.assertEqual(json.loads(self.state("published.json").read_text(encoding="utf-8"))["published_at"],
                         "2000-01-01T00:00:00Z")
        self.write("docs/b.md", "b\n")
        self.commit("after the merge")
        self.state("published.json").write_text(json.dumps({**record, "published_at": published_at}), encoding="utf-8")
        # Recording the same commit again (a re-render, gh down so nothing asked) keeps the time the
        # page moved to that commit, so it can't jump past the merge (fifteenth review)
        run(["git", "reset", "-q", "--hard", "HEAD~1"], self.dir)
        self.state("published.json").write_text(json.dumps(
            {**json.loads(self.state("published.json").read_text(encoding="utf-8")),
             "published_at": "2001-01-01T00:00:00Z"}), encoding="utf-8")
        login = "To get started with GitHub CLI, please run: gh auth login"
        r = prx_with_gh(self.dir, "prepare", "--no-fetch", fail=login)
        self.assertEqual(r.returncode, 0, r.stderr)
        shutil.copy(SKILL / "examples" / "intent.json", self.state("intent.json"))
        shutil.copy(SKILL / "examples" / "explainer.json", json.loads(r.stdout)["paths"]["explainer"])
        self.assertEqual(prx_cli(self.dir, "render").returncode, 0)
        self.assertEqual(prx_cli(self.dir, "record", URL).returncode, 0)
        self.assertEqual(json.loads(self.state("published.json").read_text(encoding="utf-8"))["published_at"],
                         "2001-01-01T00:00:00Z")
        self.write("docs/b.md", "b\n")
        self.commit("after the merge")
        r = prx_with_gh(self.dir, "prepare", "--no-fetch", prs=[{**merged7, "mergedAt": "2002-01-01T00:00:00Z"}])
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("GitHub shows PR #7 for this branch as merged", r.stderr)

    def test_a_backport_page_isnt_taken_by_the_branchs_main_pr(self):
        # A page made for a PR into release that isn't open yet: the branch's open PR into main isn't
        # this page's, so an update asks instead of moving the page (twentieth review)
        run(["git", "branch", "release", "main"], self.dir)
        self.prepare_with_examples(None, "--base", "release")
        self.assertEqual(prx_cli(self.dir, "render").returncode, 0)
        self.assertEqual(prx_cli(self.dir, "record", URL).returncode, 0)
        self.write("docs/next.md", "follow-up\n")
        self.commit("follow-up")
        head = run(["git", "rev-parse", "HEAD"], self.dir).stdout.strip()
        main4 = {"number": 4, "state": "OPEN", "headRefOid": head, "baseRefName": "main", "url": "u"}
        r = prx_with_gh(self.dir, "prepare", "--no-fetch", prs=[main4])
        self.assertEqual(r.returncode, 1, r.stdout)
        for text in ("PR #4 from this branch open into main", "prepare --same-pr", "prepare --base release"):
            self.assertIn(text, r.stderr)
        # Keeping the page's base: it stays the backport's, and #4 doesn't become its PR
        r = prx_with_gh(self.dir, "prepare", "--no-fetch", "--base", "release", prs=[main4])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["base_ref"], "release")
        self.assertIsNone(json.loads(self.state("context.json").read_text(encoding="utf-8"))["pr"])
        # #4 really is the page's PR, retargeted: --same-pr takes it and its base
        r = prx_with_gh(self.dir, "prepare", "--no-fetch", "--same-pr", prs=[main4])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["base_ref"], "main")
        self.assertEqual(json.loads(self.state("context.json").read_text(encoding="utf-8"))["pr"], 4)

    def test_a_stacked_parent_deleted_on_origin_isnt_kept_alive_by_a_stale_ref(self):
        # Published against its stacked parent, which merged and was deleted on origin, while this
        # clone still has origin/stack (no fetch --prune): the parent is gone, so the branch's PR,
        # retargeted to main, is the page's (twenty-first review)
        remote = tempfile.mkdtemp(prefix="prx-remote-")
        try:
            run(["git", "init", "-q", "--bare", "-b", "main", remote], self.dir)
            run(["git", "remote", "add", "origin", remote], self.dir)
            run(["git", "push", "-q", "origin", "main", self.BRANCH, "main:refs/heads/stack"], self.dir)
            run(["git", "fetch", "-q", "origin"], self.dir)
            self.prepare_with_examples(None, "--base", "origin/stack", "--no-fetch")
            self.assertEqual(prx_cli(self.dir, "render").returncode, 0)
            self.assertEqual(prx_cli(self.dir, "record", URL).returncode, 0)
            run(["git", "-C", remote, "branch", "-q", "-D", "stack"], self.dir)  # origin/stack stays here
            self.write("docs/next.md", "follow-up\n")
            self.commit("follow-up")
            head = run(["git", "rev-parse", "HEAD"], self.dir).stdout.strip()
            pr = {"number": 4, "state": "OPEN", "headRefOid": head, "baseRefName": "main", "url": "u"}
            r = prx_with_gh(self.dir, "prepare", prs=[pr])
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn(json.loads(r.stdout)["base_ref"], ("origin/main", "main"))
        finally:
            shutil.rmtree(remote, ignore_errors=True)

    def test_same_pr_for_a_pr_github_doesnt_have_keeps_the_page_where_it_is(self):
        # The remembered PR #4 is gone from GitHub, and a backport #9 is open at HEAD. --same-pr says
        # "still the same PR", so the page keeps its base instead of moving to #9 (twelfth review)
        self.publish()
        record = json.loads(self.state("published.json").read_text(encoding="utf-8"))
        self.state("published.json").write_text(json.dumps({**record, "pr": 4}), encoding="utf-8")
        self.write("docs/next.md", "follow-up\n")
        self.commit("follow-up")
        head = run(["git", "rev-parse", "HEAD"], self.dir).stdout.strip()
        backport = {"number": 9, "state": "OPEN", "headRefOid": head, "baseRefName": "release", "url": "u"}
        run(["git", "branch", "release", "main"], self.dir)
        r = prx_with_gh(self.dir, "prepare", "--no-fetch", prs=[backport])
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("doesn't include PR #4", r.stderr)
        r = prx_with_gh(self.dir, "prepare", "--no-fetch", "--same-pr", prs=[backport])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["base_ref"], "main")
        ctx = json.loads(self.state("context.json").read_text(encoding="utf-8"))
        self.assertEqual((ctx["pr"], ctx["pr_dropped"]), (None, True))
        # The page's PR opened again after that (the hook records #21): the record keeps #21 rather
        # than the dropped nothing (twentieth review)
        shutil.copy(SKILL / "examples" / "intent.json", self.state("intent.json"))
        shutil.copy(SKILL / "examples" / "explainer.json", self.state("context.json").parent / ctx["explainer_file"])
        old = self.state("context.json").stat().st_mtime - 10
        os.utime(self.state("context.json"), (old, old))
        record = json.loads(self.state("published.json").read_text(encoding="utf-8"))
        self.state("published.json").write_text(json.dumps({**record, "pr": 21}), encoding="utf-8")
        self.assertEqual(prx_cli(self.dir, "render").returncode, 0)
        self.assertEqual(prx_cli(self.dir, "record", URL).returncode, 0)
        self.assertEqual(json.loads(self.state("published.json").read_text(encoding="utf-8"))["pr"], 21)

    def test_a_fork_clones_upstream_base_is_kept(self):
        # In a fork's clone gh asks upstream, whose PR base is "main"; the page was published against
        # upstream/main, and an update keeps it rather than switching to the fork's origin/main (twelfth review)
        remote = tempfile.mkdtemp(prefix="prx-upstream-")
        try:
            run(["git", "init", "-q", "--bare", "-b", "main", remote], self.dir)
            run(["git", "remote", "add", "upstream", remote], self.dir)
            run(["git", "push", "-q", "upstream", "main"], self.dir)
            run(["git", "fetch", "-q", "upstream"], self.dir)
            self.prepare_with_examples(None, "--base", "upstream/main", "--no-fetch")
            self.assertEqual(prx_cli(self.dir, "render").returncode, 0)
            self.assertEqual(prx_cli(self.dir, "record", URL).returncode, 0)
            # The gate lets gh pr create --base main through for a page rendered against upstream/main:
            # gh targets upstream's main (twenty-first review)
            link = json.loads(self.state("published.json").read_text(encoding="utf-8"))["url"]
            r = hook("pr_create_gate.py", {"cwd": self.dir, "tool_name": "Bash",
                                           "tool_input": {"command": f"gh pr create --base main -t x -b '{link}'"}})
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.remember_pr(4)  # as the push hook does at gh pr create
            self.write("docs/next.md", "follow-up\n")
            self.commit("follow-up")
            head = run(["git", "rev-parse", "HEAD"], self.dir).stdout.strip()
            pr = {"number": 4, "state": "OPEN", "headRefOid": head, "baseRefName": "main", "url": "u"}
            r = prx_with_gh(self.dir, "prepare", "--no-fetch", prs=[pr])
            self.assertEqual(r.returncode, 0, r.stderr)
            out = json.loads(r.stdout)
            self.assertEqual(out["base_ref"], "upstream/main")
            self.assertFalse(any("targets" in w for w in out["warnings"]), out["warnings"])
            # Retargeted on upstream: the new base is upstream's too, not the fork's copy (thirteenth review)
            run(["git", "push", "-q", "upstream", "main:refs/heads/release"], self.dir)
            r = prx_with_gh(self.dir, "prepare", prs=[{**pr, "baseRefName": "release"}])
            self.assertEqual(r.returncode, 0, r.stderr)
            out = json.loads(r.stdout)
            self.assertEqual(out["base_ref"], "upstream/release")
            self.assertTrue(any("PR #4 now targets release" in w for w in out["warnings"]), out["warnings"])
        finally:
            shutil.rmtree(remote, ignore_errors=True)

    def test_git_sees_a_merge_before_github_is_asked(self):
        self.publish()
        self.merge_into_main()
        run(["git", "checkout", "-q", self.BRANCH], self.dir)
        self.write("docs/next.md", "follow-up\n")
        self.commit("follow-up")
        r = prx_with_gh(self.dir, "prepare", "--no-fetch", fail="please run: gh auth login")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("already in main", r.stderr)  # the merge, not "is it still open? --same-pr"
        self.assertNotIn("Couldn't ask GitHub", r.stderr)
        # --same-pr answers GitHub's silence, not a merge git can see
        r = prx_with_gh(self.dir, "prepare", "--no-fetch", "--same-pr", fail="please run: gh auth login")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("already in main", r.stderr)
        self.assertEqual(self.prepare("--new")["mode"], "create")

    def test_rebased_branch_keeps_its_open_prs_base(self):
        run(["git", "branch", "release", "main"], self.dir)
        self.publish()
        self.write("src/search/fusion/Rrf.kt", "fun fuse() = 2\n")
        self.commit("hybrid ranking, reworded", "--amend")  # rewrites the explained commit, then force-pushed
        head = run(["git", "rev-parse", "HEAD"], self.dir).stdout.strip()
        pr = {"number": 4, "state": "OPEN", "headRefOid": head, "baseRefName": "release", "url": "u"}
        r = prx_with_gh(self.dir, "prepare", "--no-fetch", "--same-pr", prs=[pr])
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out["base_ref"], "release")  # the PR's base, not the published main
        self.assertTrue(any("now targets release" in w for w in out["warnings"]), out["warnings"])
        # Without a remembered number, an open PR at HEAD could be a new PR on a reused name: it asks
        self.assertEqual(prx_with_gh(self.dir, "prepare", "--no-fetch", prs=[pr]).returncode, 1)
        # With the page's own PR remembered and GitHub showing it open at HEAD, it's the same PR (ninth review)
        record = json.loads(self.state("published.json").read_text(encoding="utf-8"))
        self.state("published.json").write_text(json.dumps({**record, "pr": 4}), encoding="utf-8")
        r = prx_with_gh(self.dir, "prepare", "--no-fetch", prs=[pr])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(any("a rebase or force-push, so this update keeps the page" in w
                            for w in json.loads(r.stdout)["warnings"]))
        # ...but not when GitHub's PR #4 is at another commit (the push hasn't happened, or it's not ours)
        r = prx_with_gh(self.dir, "prepare", "--no-fetch", prs=[{**pr, "headRefOid": "e" * 40}])
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("isn't in this branch's history", r.stderr)

    def test_commit_in_the_base_while_the_pr_is_still_open_asks(self):
        self.publish()
        self.merge_into_main()  # say it reached main through an earlier PR in a stack
        run(["git", "checkout", "-q", self.BRANCH], self.dir)
        self.write("docs/next.md", "follow-up\n")
        self.commit("follow-up")
        head = run(["git", "rev-parse", "HEAD"], self.dir).stdout.strip()
        pr = {"number": 4, "state": "OPEN", "headRefOid": head, "baseRefName": "main", "url": "u"}
        r = prx_with_gh(self.dir, "prepare", "--no-fetch", prs=[pr])
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("PR #4 from this branch still open", r.stderr)
        self.assertEqual(prx_with_gh(self.dir, "prepare", "--no-fetch", "--same-pr", prs=[pr]).returncode, 0)

    def test_pr_seen_open_is_finished_once_merged_whatever_its_head(self):
        self.publish()
        c1 = json.loads(prx_cli(self.dir, "status").stdout)["explained_sha"]
        self.write("docs/a.md", "a\n")
        self.commit("not pushed yet")
        open4 = {"number": 4, "state": "OPEN", "headRefOid": c1, "baseRefName": "main", "url": "u"}
        r = prx_with_gh(self.dir, "prepare", "--no-fetch", prs=[open4])  # the page is refreshed for this commit
        self.assertEqual(r.returncode, 0, r.stderr)
        shutil.copy(SKILL / "examples" / "intent.json", self.state("intent.json"))
        shutil.copy(SKILL / "examples" / "explainer.json", json.loads(r.stdout)["paths"]["explainer"])
        self.assertEqual(prx_cli(self.dir, "render").returncode, 0)
        self.assertEqual(prx_cli(self.dir, "record", URL).returncode, 0)
        self.assertEqual(json.loads(self.state("published.json").read_text(encoding="utf-8"))["pr"], 4)
        # A re-render at the same commit doesn't ask, so a list that leaves PR #4 out (cut short by
        # --limit, say) can't erase the number (ninth review)
        self.assertEqual(prx_with_gh(self.dir, "prepare", "--no-fetch", prs=[]).returncode, 0)
        ctx = json.loads(self.state("context.json").read_text(encoding="utf-8"))
        self.assertEqual((ctx["pr"], ctx["pr_dropped"]), (4, False))
        # PR #4 is squash-merged at c1, before the refreshed commit was pushed
        self.write("docs/b.md", "b\n")
        self.commit("after the merge")
        head = run(["git", "rev-parse", "HEAD"], self.dir).stdout.strip()
        # GitHub leaving the page's own PR out of its answer is "can't tell", not "same PR"
        r = prx_with_gh(self.dir, "prepare", "--no-fetch", prs=[])
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("doesn't include PR #4", r.stderr)
        # The page's own PR closed, while another PR from the branch is open: the page stays #4's
        r = prx_with_gh(self.dir, "prepare", "--no-fetch",
                        prs=[{**open4, "state": "CLOSED"}, {**open4, "number": 9, "headRefOid": head}])
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("PR #4 for this branch as closed", r.stderr)
        # With the page's own PR open, a fork's same-name PR this clone can't check doesn't stop it
        fork = {**open4, "number": 2, "state": "MERGED", "headRefOid": "f" * 40}
        self.assertEqual(prx_with_gh(self.dir, "prepare", "--no-fetch", prs=[open4, fork]).returncode, 0)
        # ...nor does a sibling PR from the branch merged into another base (a backport)
        c2 = json.loads(prx_cli(self.dir, "status").stdout)["explained_sha"]
        backport = {**open4, "number": 9, "state": "MERGED", "headRefOid": c2, "baseRefName": "release"}
        self.assertEqual(prx_with_gh(self.dir, "prepare", "--no-fetch", prs=[open4, backport]).returncode, 0)
        # Two of the branch's PRs open: the remembered one picks the base
        other = {**open4, "number": 10, "headRefOid": head, "baseRefName": "release"}
        r = prx_with_gh(self.dir, "prepare", "--no-fetch", prs=[other, open4])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["base_ref"], "main")
        # --same-pr answers "GitHub doesn't list PR #4" once: the number is forgotten
        self.assertEqual(prx_with_gh(self.dir, "prepare", "--no-fetch", "--same-pr", prs=[]).returncode, 0)
        self.assertIsNone(json.loads(self.state("context.json").read_text(encoding="utf-8"))["pr"])
        # A corrupt record is a one-line error
        saved = self.state("published.json").read_text(encoding="utf-8")
        self.state("published.json").write_text("{not json", encoding="utf-8")
        r = prx_with_gh(self.dir, "prepare", "--no-fetch", prs=[open4])
        self.assertEqual(r.returncode, 1)
        self.assertIn("isn't valid JSON", r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.state("published.json").write_text(saved, encoding="utf-8")
        merged4 = {**open4, "state": "MERGED"}
        r = prx_with_gh(self.dir, "prepare", "--no-fetch", prs=[merged4])
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("GitHub shows PR #4 for this branch as merged", r.stderr)
        # A newer closed PR listed first doesn't stand in for the merged one (it would take --same-pr)
        c2 = json.loads(prx_cli(self.dir, "status").stdout)["explained_sha"]
        closed8 = {**open4, "number": 8, "state": "CLOSED", "headRefOid": c2}  # it contains the page's commit
        r = prx_with_gh(self.dir, "prepare", "--no-fetch", prs=[closed8, merged4])
        self.assertIn("PR #4 for this branch as merged", r.stderr)
        # A --new that fails after archiving leaves context.json behind; the next page doesn't inherit PR #4
        run(["git", "checkout", "-q", "-B", self.BRANCH, "main"], self.dir)
        self.assertIn("No commits on", prx_cli(self.dir, "prepare", "--base", "main", "--no-fetch", "--new").stderr)
        self.write("docs/new.md", "new work\n")
        self.commit("new work")
        self.publish()
        self.write("docs/more.md", "more\n")
        self.commit("more new work")
        r = prx_with_gh(self.dir, "prepare", "--no-fetch", prs=[merged4])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["mode"], "update")

    def test_created_pr_number_is_remembered_from_the_start(self):
        self.publish()
        created = {"cwd": self.dir, "tool_name": "Bash",
                   "tool_input": {"command": f"gh pr create --base main --title x --body-file {self.state('pr-body.md')}"},
                   "tool_response": {"stdout": "https://github.com/o/r/pull/12\n", "stderr": ""}}
        self.assertEqual(hook("pr_push_refresh.py", created).returncode, 0)
        self.assertEqual(json.loads(self.state("published.json").read_text(encoding="utf-8"))["pr"], 12)
        # Squash-merged before any update saw it open: the number alone says the page is finished
        self.write("docs/next.md", "after the merge\n")
        self.commit("after the merge")
        merged = {"number": 12, "state": "MERGED", "headRefOid": "f" * 40, "baseRefName": "main", "url": "u"}
        r = prx_with_gh(self.dir, "prepare", "--no-fetch", prs=[merged])
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("GitHub shows PR #12 for this branch as merged", r.stderr)

    def test_created_pr_number_is_only_taken_for_this_branch(self):
        self.publish()
        body = self.state("pr-body.md")

        def create(command, stdout="Creating pull request\nhttps://github.com/o/r/pull/12\n"):
            hook("pr_push_refresh.py", {"cwd": self.dir, "tool_name": "Bash", "tool_input": {"command": command},
                                        "tool_response": {"stdout": stdout, "stderr": ""}})
            return json.loads(self.state("published.json").read_text(encoding="utf-8")).get("pr")

        # Another branch's or repo's PR, or one created elsewhere, never lands on this branch's page
        for command in [f"gh pr create --head other -F {body}", f"gh pr create -R o/fork -F {body}",
                        f"gh pr create --dry-run -F {body}",  # creates nothing (fourteenth review)
                        # --repo before pr create works in gh too (fifteenth review)
                        f"gh -R o/fork pr create -F {body}", f"gh --repo=o/fork pr create -F {body}",
                        f"gh pr --repo o/fork create -F {body}",
                        f"cd .. && gh pr create -F {body}", f"GH_REPO=o/other gh pr create -F {body}",
                        f"export GH_REPO=o/other; gh pr create -F {body}", "gh pr list"]:
            self.assertIsNone(create(command), command)
        # Chained, a URL in the output may be another command's PR (the only one, if the create failed),
        # so the create must be the chain's only gh run and the output must name one PR
        for command, stdout in [
                (f"gh pr create -F {body} && gh pr comment 3 --body thanks",
                 "https://github.com/o/r/pull/12\nhttps://github.com/o/r/pull/3#issuecomment-1\n"),
                (f"gh pr create -F {body}; gh pr view 3 --json url -q .url", "https://github.com/o/r/pull/3\n"),
                (f"git push -u origin HEAD && gh pr create -F {body}",
                 "see https://github.com/o/r/pull/3\nhttps://github.com/o/r/pull/12\n")]:
            self.assertIsNone(create(command, stdout), command)
        self.assertEqual(create(f"git push -u origin HEAD && gh pr create -F {body}"), 12)
        record = json.loads(self.state("published.json").read_text(encoding="utf-8"))
        del record["pr"]
        self.state("published.json").write_text(json.dumps(record), encoding="utf-8")
        # Alone, the URL gh prints last is the new PR's, whatever else the output mentions
        self.assertEqual(create(f"gh pr create -F {body}", "see https://github.com/o/r/pull/3\nhttps://github.com/o/r/pull/12\n"), 12)
        # A number the hook wrote after the last prepare (the PR opened again) survives a re-render and
        # record, rather than reverting to the one prepare saw (eighteenth review)
        self.state("published.json").write_text(json.dumps({**record, "pr": 12}), encoding="utf-8")
        self.assertEqual(prx_cli(self.dir, "prepare", "--base", "main", "--no-fetch").returncode, 0)  # context: pr 12
        ctx = json.loads(self.state("context.json").read_text(encoding="utf-8"))
        shutil.copy(SKILL / "examples" / "intent.json", self.state("intent.json"))
        shutil.copy(SKILL / "examples" / "explainer.json", self.state("context.json").parent / ctx["explainer_file"])
        old = self.state("context.json").stat().st_mtime - 10
        os.utime(self.state("context.json"), (old, old))  # the hook writes after prepare
        self.assertEqual(create(f"gh pr create -F {body}", "https://github.com/o/r/pull/21\n"), 21)
        self.assertEqual(prx_cli(self.dir, "render").returncode, 0)
        self.assertEqual(prx_cli(self.dir, "record", URL).returncode, 0)
        self.assertEqual(json.loads(self.state("published.json").read_text(encoding="utf-8"))["pr"], 21)
        self.state("published.json").write_text(json.dumps(record), encoding="utf-8")
        # From the PowerShell tool too, whose response may name its output differently (thirteenth review)
        self.state("published.json").write_text(json.dumps(record), encoding="utf-8")
        hook("pr_push_refresh.py", {"cwd": self.dir, "tool_name": "PowerShell",
                                    "tool_input": {"command": f"gh pr create -F {body}"},
                                    "tool_response": {"output": "https://github.com/o/r/pull/14\n"}})
        self.assertEqual(json.loads(self.state("published.json").read_text(encoding="utf-8"))["pr"], 14)
        self.state("published.json").write_text(json.dumps(record), encoding="utf-8")
        self.assertEqual(create(f"gh pr create -F {body}"), 12)
        # A PR opened again from the page (gh only creates one while none into that base is open, so
        # #12 was closed) replaces the number (seventeenth review)
        self.assertEqual(create(f"gh pr create -F {body}", "https://github.com/o/r/pull/15\n"), 15)
        # --head for a remote branch at this very commit (pushed under another name) is this branch's PR
        head = run(["git", "rev-parse", "HEAD"], self.dir).stdout.strip()
        run(["git", "update-ref", "refs/remotes/origin/pushed-as", head], self.dir)
        self.assertEqual(create(f"gh pr create --head pushed-as -F {body}", "https://github.com/o/r/pull/16\n"), 16)
        self.assertEqual(create(f"gh pr create --head someone:pushed-as -F {body}", "https://github.com/o/r/pull/17\n"), 16)
        self.state("published.json").write_text(json.dumps({**record, "pr": 12}), encoding="utf-8")
        # Recording the same page again (a re-render at the same commit) keeps the number
        self.assertEqual(prx_cli(self.dir, "render").returncode, 0)
        self.assertEqual(prx_cli(self.dir, "record", URL).returncode, 0)
        self.assertEqual(json.loads(self.state("published.json").read_text(encoding="utf-8"))["pr"], 12)

    def test_open_prs_base_is_fetched_when_this_clone_lacks_it(self):
        self.publish()
        remote = tempfile.mkdtemp(prefix="prx-remote-")
        try:
            run(["git", "init", "-q", "--bare", "-b", "main", remote], self.dir)
            run(["git", "remote", "add", "origin", remote], self.dir)
            run(["git", "push", "-q", "origin", "main", self.BRANCH, "main:refs/heads/develop"], self.dir)
            run(["git", "update-ref", "-d", "refs/remotes/origin/develop"], self.dir)  # never fetched here
            self.remember_pr(4)  # as the push hook does at gh pr create
            self.write("docs/next.md", "follow-up\n")
            self.commit("follow-up")
            head = run(["git", "rev-parse", "HEAD"], self.dir).stdout.strip()
            pr = {"number": 4, "state": "OPEN", "headRefOid": head, "baseRefName": "develop", "url": "u"}
            r = prx_with_gh(self.dir, "prepare", prs=[pr])
            self.assertEqual(r.returncode, 0, r.stderr)
            out = json.loads(r.stdout)
            self.assertEqual(out["base_ref"], "origin/develop")
            self.assertTrue(any("now targets develop" in w for w in out["warnings"]), out["warnings"])
        finally:
            shutil.rmtree(remote, ignore_errors=True)

    def test_merged_pr_head_missing_here_is_fetched_from_github(self):
        self.publish()
        remote, other = tempfile.mkdtemp(prefix="prx-remote-"), tempfile.mkdtemp(prefix="prx-other-")
        try:
            run(["git", "init", "-q", "--bare", "-b", "main", remote], self.dir)
            run(["git", "remote", "add", "origin", remote], self.dir)
            run(["git", "push", "-q", "origin", "main", self.BRANCH], self.dir)
            # A commit added to the PR on GitHub (an "Update branch" merge, a committed suggestion)
            # and never pulled here. GitHub keeps it at refs/pull/7/head.
            shutil.rmtree(other)
            run(["git", "clone", "-q", "-b", self.BRANCH, remote, other], self.dir)
            (Path(other) / "suggestion.md").write_text("from review\n", encoding="utf-8")
            run(["git", "add", "-A"], other)
            run(["git", "commit", "-q", "-m", "Apply suggestion"], other)
            run(["git", "push", "-q", "origin", "HEAD:refs/pull/7/head"], other)
            oid = run(["git", "rev-parse", "HEAD"], other).stdout.strip()
            self.write("docs/next.md", "follow-up\n")
            self.commit("follow-up after the squash merge")
            merged = {"number": 7, "state": "MERGED", "headRefOid": oid, "baseRefName": "main", "url": "u"}
            r = prx_with_gh(self.dir, "prepare", "--no-fetch", prs=[merged])
            self.assertEqual(r.returncode, 1, r.stdout)
            self.assertIn("isn't in this clone", r.stderr)  # can't tell, so it asks
            r = prx_with_gh(self.dir, "prepare", prs=[merged])
            self.assertEqual(r.returncode, 1, r.stdout)
            self.assertIn("GitHub shows PR #7 for this branch as merged", r.stderr)
        finally:
            shutil.rmtree(remote, ignore_errors=True)
            shutil.rmtree(other, ignore_errors=True)

    def test_context_from_before_pr_pages_is_refused(self):
        self.prepare_with_examples()
        ctx = json.loads(self.state("context.json").read_text(encoding="utf-8"))
        del ctx["page_id"]  # as the prepare before PR pages wrote it
        self.state("context.json").write_text(json.dumps(ctx), encoding="utf-8")
        for cmd in (["render"], ["record", URL]):  # refused before anything is published
            r = prx_cli(self.dir, *cmd)
            self.assertEqual(r.returncode, 1, cmd)
            self.assertIn("older prx.py", r.stderr)
        self.assertFalse(self.state("url").exists())

    def test_rebased_branch_keeps_its_url(self):
        self.publish()
        self.write("src/search/fusion/Rrf.kt", "fun fuse() = 2\n")
        self.commit("hybrid ranking, reworded", "--amend")  # rewrites the explained commit
        self.assertTrue(json.loads(prx_cli(self.dir, "status").stdout)["diverged"])
        out = self.prepare("--same-pr")
        self.assertEqual((out["mode"], out["artifact_url"]), ("update", self.link))

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
