"""Find the commands a Bash or PowerShell command line would run.

Used by the PR explainer hooks to spot `gh pr create` and `git push`. One
quote-aware pass (_Scanner) finds the shell's own syntax, so text in quotes or
comments can never act as syntax. It removes comments, joins line
continuations, and swaps $(...) substitutions, backticks, heredoc bodies and
here-strings for placeholders. shlex then splits the result into words. From
there it looks inside the places a command can hide:
- separators and pipelines: ; && || | & ( ) newlines, and { } in PowerShell
- keywords and wrappers: if/then/do, !, time, sudo, env, nice, nohup, timeout,
  xargs, wsl, cmd /c, eval, Invoke-Expression / iex, PowerShell assignments
- shells given a script: bash -c, sh -lc, pwsh -Command, -EncodedCommand, and
  heredocs, echo output or here-strings piped into a shell
- command substitution: $(...) and, in Bash, backticks (not inside single quotes)

It doesn't expand aliases, functions or variables. It's a guard rail for a
cooperative agent, not a sandbox. Where it can't be sure it errs towards
finding commands: anything it can't follow raises ParseError, which the hooks
treat as "might be a PR creation", and an unterminated heredoc's body is
checked as commands too.
"""
from __future__ import annotations

import base64
import binascii
import os
import re
import shlex
from dataclasses import dataclass
from typing import List, Optional

MAX_DEPTH = 5  # wrappers within wrappers (bash -c 'eval ...'); deeper raises ParseError
MAX_NESTING = 64  # $( within $(
PUNCT = "();<>|&\n"
# `#` starts a comment only at the start of a word: after one of these characters.
COMMENT_AFTER = " \t\n;|&()"
PWSH_COMMENT_AFTER = COMMENT_AFTER + "{}"
PWSH_PUNCT = PUNCT + "{}"  # script blocks: try { }, ForEach-Object { }, Invoke-Command { }
POSIX_SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
POWERSHELLS = {"pwsh", "powershell"}
KEYWORDS = {"if", "then", "elif", "else", "do", "while", "until", "!", "{", "}", "command", "builtin", "nohup", "unbuffer"}
# Wrappers that run the rest of their arguments as a command, with the options
# that take a value, so `sudo -u bob gh ...` finds gh rather than bob.
WRAPPERS = {
    "sudo": {"-u", "-g", "-h", "-p", "-C", "-D", "-r", "-t", "-U", "-T"},
    "doas": {"-u", "-C"},
    "env": {"-u", "-C", "-S", "--unset", "--chdir"},
    "nice": {"-n", "--adjustment"},
    "time": {"-f", "-o", "--format", "--output"},
    "timeout": {"-s", "-k", "--signal", "--kill-after"},
    "xargs": {"-I", "-L", "-n", "-P", "-d", "-E", "-s", "-a", "--max-args", "--max-procs", "--delimiter", "--arg-file"},
    "stdbuf": {"-i", "-o", "-e"},
    "exec": {"-a"},
    "wsl": {"-d", "-u", "--distribution", "--user", "--cd"},
}
PWSH_VALUE_OPTIONS = {"-executionpolicy", "-ex", "-ep", "-windowstyle", "-w", "-outputformat", "-of",
                      "-inputformat", "-if", "-configurationname", "-workingdirectory", "-wd", "-version",
                      "-psconsolefile", "-custompipename", "-settingsfile"}
BASH_VALUE_OPTIONS = {"-o", "+o", "-O", "+O", "--rcfile", "--init-file"}

_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_PS_CAST = r"(?:\[[^\]\s]+\])*"
_PS_TARGET = re.compile(rf"^{_PS_CAST}\$[\w:{{}}?]+$")  # $r, $null, $env:X, [void]$x
_PS_GLUED = re.compile(rf"^{_PS_CAST}\$[\w:]+=(.*)$")  # $r=gh
_PS_ASSIGN_OPS = {"=", "+=", "-=", "*=", "/=", "%=", "??="}
_REDIRECT = re.compile(r"^(?:<<<|<<|<>|<&|>&|>>|>\||&>>|&>|<|>)$")
# The delimiter is one word, possibly partly quoted (<<'EOF', <<E"OF", <<\EOF); any quoting makes the body literal.
_HEREDOC_MARK = re.compile(r"<<(-?)[ \t]*((?:[^\s;&|<>()'\"\\]+|'[^'\n]*'|\"[^\"\n]*\"|\\.)+)")
_HERESTRING = re.compile(r"@(['\"])[ \t]*\r?\n(.*?)\r?\n\1@", re.S)


@dataclass
class Command:
    argv: List[str]
    stdin: Optional[str] = None  # heredoc or here-string body fed to this command


class ParseError(ValueError):
    """The command line couldn't be tokenised, for example because of an unclosed quote."""


def program(word: str) -> str:
    """`/usr/bin/gh`, `C:\\...\\gh.exe` and `GH` all name the program `gh`."""
    name = re.split(r"[\\/]", word)[-1].lower()
    return re.sub(r"\.(?:exe|cmd|bat)$", "", name)


def commands(text: str, shell: str = "bash", _depth: int = 0) -> List[Command]:
    """Every simple command `text` would run, in order, wrappers unwrapped.

    Raises ParseError for anything it can't follow (an unclosed quote, nesting
    deeper than it tracks), so callers can fail closed instead of missing a command.
    """
    if not text:
        return []
    if _depth > MAX_DEPTH:
        raise ParseError("commands nested too deeply to follow")
    pwsh = shell == "powershell"
    scan = _Scanner(text, pwsh)
    clean = scan.run()
    found: List[Command] = []
    for script in scan.scripts:
        found += commands(script, shell, _depth + 1)
    punct = PWSH_PUNCT if pwsh else PUNCT
    for pipeline in _pipelines(_tokens(clean, pwsh, punct), scan, punct):
        for i, cmd in enumerate(pipeline):
            found += _expand(cmd, shell, _depth, pipeline[:i])
    return found


def _append(items, item) -> int:
    items.append(item)
    return len(items) - 1


class _Scanner:
    """One pass over a command line that knows where quotes, comments and
    heredocs are, so that only the shell's real syntax is treated as syntax.
    run() returns text for shlex with:
    - comments removed, and line continuations (backslash or backtick before
      a newline) joined
    - $(...) and backtick substitutions, Bash arithmetic, heredoc bodies and
      PowerShell here-strings replaced by placeholders (resolve() puts the
      original text back into words)
    scripts collects text that would also run as commands: substitutions,
    substitutions inside unquoted heredocs and @"..."@ strings, and the body of
    an unterminated heredoc (so a misread `<<` can't hide what follows it).
    """

    def __init__(self, text: str, pwsh: bool):
        self.text, self.pwsh = text, pwsh
        self.esc = "`" if pwsh else "\\"
        self.comment_after = PWSH_COMMENT_AFTER if pwsh else COMMENT_AFTER
        # A random tag, so text that merely looks like a placeholder stays text.
        self.tag = "PRX" + os.urandom(4).hex()
        self.placeholder = re.compile(rf"__{self.tag}_(DOC|STR|SUB|ARI)(\d+)__")
        self.saved = {"DOC": [], "STR": [], "SUB": [], "ARI": []}
        self.scripts: List[str] = []
        self.nesting = 0

    def _hold(self, kind: str, original: str) -> str:
        return f"__{self.tag}_{kind}{_append(self.saved[kind], original)}__"

    def run(self) -> str:
        return self._context(0, nested=False)[0]

    def _context(self, i: int, nested: bool):
        self.nesting += 1
        if self.nesting > MAX_NESTING:
            raise ParseError("substitutions nested too deeply to follow")
        try:
            return self._scan_context(i, nested)
        finally:
            self.nesting -= 1

    def _scan_context(self, i: int, nested: bool):
        """Scan one command context. Nested (inside $(...)): stop at its closing paren."""
        t, n = self.text, len(self.text)
        out: List[str] = []
        quote: Optional[str] = None
        depth = 0
        pending = []  # heredocs on this line, waiting for the newline before their bodies
        while i < n:
            c = t[i]
            if quote == "'":  # single quotes are literal in both shells
                quote = None if c == "'" else quote
                out.append(c)
                i += 1
                continue
            if c == self.esc:
                if t.startswith("\n", i + 1) or t.startswith("\r\n", i + 1):
                    i += 2 if t[i + 1] == "\n" else 3  # line continuation: join the lines
                    continue
                out.append(t[i:i + 2])
                i += 2
                continue
            if not self.pwsh and t.startswith("$((", i):
                i = self._arithmetic(i, out)
                continue
            if t.startswith("$(", i):
                i = self._substitution(i, out)
                continue
            if c == "`" and not self.pwsh:
                i = self._backticks(i, out)
                continue
            if quote == '"':
                quote = None if c == '"' else quote
                out.append(c)
                i += 1
                continue
            # Outside quotes: the only place the shell's own syntax lives.
            word_start = not out or out[-1][-1] in self.comment_after
            if c in "'\"":
                quote = c
            elif c == "#" and word_start:  # `${#arr[@]}` and `$(cmd)#x` aren't comments
                j = t.find("\n", i)
                i = n if j == -1 else j  # keep the newline: it separates commands
                continue
            elif self.pwsh and t.startswith("<#", i):
                j = t.find("#>", i + 2)
                i = n if j == -1 else j + 2
                continue
            elif not self.pwsh and word_start and t.startswith("((", i):
                i = self._arithmetic(i, out)  # (( x << 2 )): a shift, not a heredoc
                continue
            elif t.startswith("<<<", i):
                out.append("<<<")
                i += 3
                continue
            elif not self.pwsh and t.startswith("<<", i):
                m = _HEREDOC_MARK.match(t, i)
                if m:
                    word = m.group(2)
                    idx = _append(self.saved["DOC"], "")
                    pending.append((idx, re.sub(r"['\"\\]", "", word), bool(m.group(1)), bool(re.search(r"['\"\\]", word))))
                    out.append(f" << __{self.tag}_DOC{idx}__ ")
                    i = m.end()
                    continue
            elif self.pwsh and c == "@" and t.startswith(("@'", '@"'), i):
                m = _HERESTRING.match(t, i)
                if m:
                    if m.group(1) == '"':  # @"..."@ expands $(...)
                        self._expansions(m.group(2))
                    out.append(" " + self._hold("STR", m.group(2)) + " ")
                    i = m.end()
                    continue
            elif c == "\n" and pending:
                out.append("\n")
                i = self._heredoc_bodies(i + 1, pending)
                pending = []
                continue
            elif c == "(":
                depth += 1
            elif c == ")":
                if nested and depth == 0:
                    return "".join(out), i
                depth -= 1
            out.append(c)
            i += 1
        return "".join(out), n

    def _substitution(self, i: int, out: List[str]) -> int:
        _, close = self._context(i + 2, nested=True)  # handles quotes and heredocs inside
        self.scripts.append(self.text[i + 2:close])
        out.append(self._hold("SUB", self.text[i:close + 1]))
        return close + 1

    def _backticks(self, i: int, out: List[str]) -> int:
        j = self.text.find("`", i + 1)
        if j == -1:
            out.append("`")
            return i + 1
        self.scripts.append(self.text[i + 1:j])
        out.append(self._hold("SUB", self.text[i:j + 1]))
        return j + 1

    def _arithmetic(self, i: int, out: List[str]) -> int:
        """Bash $(( ... )) or (( ... )): kept as one word. Its text is also checked
        as commands, which is harmless for arithmetic and catches a misread `((`."""
        t, n = self.text, len(self.text)
        k = t.index("((", i)
        depth, inner_start = 0, k + 2
        while k < n:
            if t.startswith("$(", k) and not t.startswith("$((", k):
                k = self._substitution(k, [])
                continue
            if t[k] == "(":
                depth += 1
            elif t[k] == ")":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        self.scripts.append(t[inner_start:max(inner_start, k - 1)])
        out.append(self._hold("ARI", t[i:k + 1]))
        return k + 1

    def _expansions(self, body: str) -> None:
        """Substitutions that run inside an unquoted heredoc body or an @"..."@ string."""
        inner = _Scanner(body, self.pwsh)
        t, i = body, 0
        while i < len(t):
            if t[i] == inner.esc:
                i += 2
            elif t.startswith("$(", i) and not (not self.pwsh and t.startswith("$((", i)):
                i = inner._substitution(i, [])
            elif t[i] == "`" and not self.pwsh:
                i = inner._backticks(i, [])
            else:
                i += 1
        self.scripts += inner.scripts

    def _heredoc_bodies(self, i: int, pending) -> int:
        """Read each pending heredoc's body, in order, starting at i. Returns the index after the last terminator."""
        t, n = self.text, len(self.text)
        for idx, delim, strip_tabs, quoted in pending:
            start = i
            while True:
                nl = t.find("\n", i)
                end = n if nl == -1 else nl
                line = t[i:end].rstrip("\r")
                if (line.lstrip("\t") if strip_tabs else line) == delim:
                    body = t[start:i]
                    i = end if nl == -1 else nl + 1
                    break
                if nl == -1:
                    # Unterminated: bash reads to the end. If the `<<` was misread,
                    # that would hide everything after it, so check it as commands too.
                    body = t[start:]
                    self.scripts.append(body)
                    i = n
                    break
                i = nl + 1
            self.saved["DOC"][idx] = body
            if not quoted:  # <<EOF (unquoted) runs $(...) and backticks in its body
                self._expansions(body)
        return i

    def resolve(self, word: str) -> str:
        """Put placeholders back as the text they stood for."""
        def back(m):
            items, k = self.saved[m.group(1)], int(m.group(2))
            return items[k] if k < len(items) else m.group(0)
        return self.placeholder.sub(back, word)


def _tokens(text: str, pwsh: bool, punct: str) -> List[str]:
    lex = shlex.shlex(text, posix=True, punctuation_chars=punct)
    lex.whitespace = " \t\r"  # newlines separate commands, so they're punctuation
    lex.whitespace_split = True
    lex.commenters = ""  # the scanner removed comments; it knows where they can start
    if pwsh:
        lex.escape = "`"  # backslashes are path separators in PowerShell
    try:
        return list(lex)
    except ValueError as err:
        raise ParseError(str(err)) from None


def _pipelines(tokens: List[str], scan: _Scanner, punct: str) -> List[List[Command]]:
    pipelines: List[List[Command]] = []
    pipe: List[Command] = []
    argv: List[str] = []
    stdin: Optional[str] = None

    def end_command():
        nonlocal argv, stdin
        if argv or stdin is not None:
            pipe.append(Command(argv, stdin))
        argv, stdin = [], None

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok and set(tok) <= set(punct):
            if _REDIRECT.match(tok):
                target = tokens[i + 1] if i + 1 < len(tokens) else ""
                if tok in ("<<", "<<<"):
                    stdin = scan.resolve(target)
                i += 2
                continue
            end_command()
            if tok.strip("()") not in ("|", "|&"):  # anything but a pipe ends the pipeline
                if pipe:
                    pipelines.append(pipe)
                pipe = []
            i += 1
            continue
        argv.append(scan.resolve(tok))
        i += 1
    end_command()
    if pipe:
        pipelines.append(pipe)
    return pipelines


def _skip_options(args: List[str], with_value) -> List[str]:
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            return args[i + 1:]
        if _ASSIGN.match(a):
            i += 1
        elif a.startswith("-") and a != "-":
            i += 2 if a in with_value else 1
        else:
            break
    return args[i:]


def _expand(cmd: Command, shell: str, depth: int, upstream: List[Command]) -> List[Command]:
    argv = list(cmd.argv)
    while argv:
        name = program(argv[0])
        if shell == "powershell" and len(argv) > 1 and _PS_TARGET.match(argv[0]) and argv[1] in _PS_ASSIGN_OPS:
            argv = argv[2:]  # $r = gh pr create, $null = ..., [void]$x = ...
        elif shell == "powershell" and _PS_GLUED.match(argv[0]):
            rest = _PS_GLUED.match(argv[0]).group(1)  # $r=gh pr create
            argv = ([rest] if rest else []) + argv[1:]
        elif shell == "powershell" and re.fullmatch(_PS_CAST, argv[0]) and argv[0]:
            argv = argv[1:]  # [void] gh ...
        elif _ASSIGN.match(argv[0]) or name in KEYWORDS:
            argv = argv[1:]
        elif name in WRAPPERS:
            argv = _skip_options(argv[1:], WRAPPERS[name])
            if name == "timeout" and argv and re.fullmatch(r"\d+(?:\.\d+)?[smhd]?", argv[0]):
                argv = argv[1:]  # the duration
        elif name == "cmd":
            k = next((j for j, a in enumerate(argv) if a.lower() in ("/c", "/k")), None)
            if k is None:
                break
            return commands(" ".join(argv[k + 1:]), "bash", depth + 1)
        elif name == "eval":
            return commands(" ".join(argv[1:]), shell, depth + 1)
        elif name in ("invoke-expression", "iex"):
            args = [a for a in argv[1:] if a.lower() not in ("-command", "-c")]
            script = " ".join(args) if args else "\n".join(_output_of(c) for c in upstream)
            return commands(script, "powershell", depth + 1)
        elif name in POSIX_SHELLS or name in POWERSHELLS:
            return _shell(name, argv, cmd.stdin, depth, upstream)
        else:
            break
    return [Command(argv, cmd.stdin)] if argv else []


def _shell(name: str, argv: List[str], stdin: Optional[str], depth: int, upstream: List[Command]) -> List[Command]:
    """What a shell invocation runs: its -c / -Command script, or its stdin."""
    pwsh = name in POWERSHELLS
    lang = "powershell" if pwsh else "bash"
    args, i = argv[1:], 0
    while i < len(args):
        a, low = args[i], args[i].lower()
        if pwsh:
            if low.startswith("/"):  # powershell.exe also takes /c, /Command, /ec, /EncodedCommand
                low = "-" + low[1:]
            # PowerShell accepts any abbreviation of a switch (-co, -en). Where one is
            # ambiguous (-co: -Command or -ConfigurationName), assume it runs a script.
            if len(low) >= 2 and "-command".startswith(low):
                script = " ".join(args[i + 1:])
                if script.strip() != "-":
                    return commands(script, lang, depth + 1)
                break  # `-Command -` reads the script from stdin
            if low == "-ec" or (len(low) >= 2 and "-encodedcommand".startswith(low)):
                return commands(_decode_ps(args[i + 1] if i + 1 < len(args) else ""), lang, depth + 1)
            if len(low) >= 2 and "-file".startswith(low):
                return [Command(argv, stdin)]  # runs a script file we can't see
            if not low.startswith("-"):
                if name == "powershell":  # Windows PowerShell treats a bare argument as -Command
                    return commands(" ".join(args[i:]), lang, depth + 1)
                return [Command(argv, stdin)]  # pwsh treats it as -File
            i += 2 if low in PWSH_VALUE_OPTIONS else 1
        else:
            if a in BASH_VALUE_OPTIONS:
                i += 2
            elif a.startswith("-") and not a.startswith("--") and "c" in a[1:]:
                return commands(args[i + 1] if i + 1 < len(args) else "", lang, depth + 1)
            elif a.startswith(("-", "+")):
                i += 1
            else:
                return [Command(argv, stdin)]  # `bash script.sh`: a script file we can't see
    script = stdin if stdin is not None else "\n".join(_output_of(c) for c in upstream)
    return commands(script, lang, depth + 1)


def _output_of(cmd: Command) -> str:
    """Roughly what a command upstream in a pipeline writes, when that's knowable."""
    if not cmd.argv:
        return cmd.stdin or ""
    name = program(cmd.argv[0])
    if name in ("cat", "type", "get-content", "gc") and cmd.stdin is not None:
        return cmd.stdin
    if name in ("echo", "printf", "write-output", "write-host", "write", "echo.exe"):
        return " ".join(a for a in cmd.argv[1:] if not re.fullmatch(r"-[a-zA-Z]+", a))
    if len(cmd.argv) == 1 and " " in cmd.argv[0]:  # a PowerShell string literal: "gh pr create" | iex
        return cmd.argv[0]
    return ""


def _decode_ps(b64: str) -> str:
    """-EncodedCommand payload as text. Stray bytes are replaced, the way PowerShell
    decodes them, so `gh pr create` plus an odd trailing byte still reads as a command."""
    try:
        raw = base64.b64decode(b64 + "=" * (-len(b64) % 4))
    except (binascii.Error, ValueError):
        raise ParseError("couldn't decode the -EncodedCommand payload") from None
    return raw.decode("utf-16-le", errors="replace")
