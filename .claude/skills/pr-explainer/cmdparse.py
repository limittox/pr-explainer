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
from typing import List, Optional, Tuple

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
    "sudo": {"-u", "-g", "-h", "-p", "-C", "-D", "-r", "-t", "-U", "-T", "-c", "-R", "--user", "--group", "--host",
             "--prompt", "--close-from", "--chdir", "--role", "--type", "--other-user", "--command-timeout",
             "--chroot", "--login-class"},
    "doas": {"-u", "-C"},
    "env": {"-u", "-C", "-S", "--unset", "--chdir", "--split-string"},
    "nice": {"-n", "--adjustment"},
    "time": {"-f", "-o", "--format", "--output"},
    "timeout": {"-s", "-k", "--signal", "--kill-after"},
    "xargs": {"-I", "-L", "-n", "-P", "-d", "-E", "-s", "-a", "--max-args", "--max-procs", "--delimiter", "--arg-file"},
    "stdbuf": {"-i", "-o", "-e"},
    "exec": {"-a"},
    "wsl": {"-d", "-u", "--distribution", "--user", "--cd"},
}
# Wrapper options that run the command in another directory.
WRAPPER_CHDIR = {"env": {"-C", "--chdir"}, "sudo": {"-D", "--chdir"}, "wsl": {"--cd"}}
# powershell.exe / pwsh switches that take a value, by full name and by their documented
# short aliases. PowerShell also accepts any unambiguous abbreviation (-exec Bypass).
PWSH_VALUE_PARAMS = ("-executionpolicy", "-windowstyle", "-outputformat", "-inputformat", "-configurationname",
                     "-configurationfile", "-workingdirectory", "-version", "-psconsolefile", "-custompipename",
                     "-settingsfile")
PWSH_VALUE_ALIASES = {"-ex": "-executionpolicy", "-ep": "-executionpolicy", "-w": "-windowstyle",
                      "-o": "-outputformat", "-of": "-outputformat", "-if": "-inputformat",
                      "-wd": "-workingdirectory", "-v": "-version"}
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
    # Runs in a child shell (a Bash subshell, $(...) or pipeline stage, bash -c,
    # a background job), so a cd here doesn't reach the commands after it.
    nested: bool = False
    # Comes from a $(...) or similar: listed before the command it belongs to,
    # so its place in the list isn't when it runs relative to an earlier cd.
    early: bool = False
    # Wrappers run it in other directories, outermost first: env -C DIR, sudo -D DIR,
    # wsl --cd DIR, pwsh -WorkingDirectory DIR. Each is relative to the one before.
    chdir: Tuple[str, ...] = ()


def _mark(cmds: List[Command], nested=False, early=False, chdir: Tuple[str, ...] = ()) -> List[Command]:
    for c in cmds:
        c.nested = c.nested or nested
        c.early = c.early or early
        c.chdir = tuple(chdir) + c.chdir
    return cmds


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
    for script in scan.scripts:  # PowerShell runs $(...) in the current runspace, Bash in a subshell
        found += _mark(commands(script, shell, _depth + 1), nested=not pwsh, early=True)
    punct = PWSH_PUNCT if pwsh else PUNCT
    for pipeline in _pipelines(_tokens(clean, pwsh, punct), scan, punct, subshells=not pwsh):
        stages_are_subshells = len(pipeline) > 1 and not pwsh  # Bash runs each stage in a subshell
        for i, cmd in enumerate(pipeline):
            cmd.nested = cmd.nested or stages_are_subshells
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


def _pipelines(tokens: List[str], scan: _Scanner, punct: str, subshells: bool = True) -> List[List[Command]]:
    """Group words into commands and pipelines. With subshells (Bash), commands inside
    ( ... ) are marked nested: a cd there doesn't reach the commands after the group.
    So is everything sent to the background with `&`: the whole && / || list before
    it (in PowerShell 7 too, as a job). A `&` that starts a PowerShell command is the
    call operator instead."""
    pipelines: List[List[Command]] = []
    pipe: List[Command] = []
    argv: List[str] = []
    stdin: Optional[str] = None
    depth = 0
    starts = [0]  # where the current && / || list began in pipelines, per ( ... ) level

    def end_command():
        nonlocal argv, stdin
        if argv or stdin is not None:
            pipe.append(Command(argv, stdin, nested=subshells and depth > 0))
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
            after_words = bool(argv)
            end_command()
            depth = max(0, depth + tok.count("(") - tok.count(")"))
            if tok.strip("()") not in ("|", "|&"):  # anything but a pipe ends the pipeline
                if pipe:
                    pipelines.append(pipe)
                pipe = []
            for op in re.findall(r"&&|\|\||\|&|.", tok, re.S):
                if op == "(":
                    starts.append(len(pipelines))
                elif op == ")" and len(starts) > 1:
                    starts.pop()
                elif op == "&" and (after_words or subshells):  # background, not the call operator
                    for p in pipelines[starts[-1]:]:
                        for c in p:
                            c.nested = True
                    starts[-1] = len(pipelines)
                elif op in (";", "\n"):
                    starts[-1] = len(pipelines)
            i += 1
            continue
        argv.append(scan.resolve(tok))
        i += 1
    end_command()
    if pipe:
        pipelines.append(pipe)
    return pipelines


def _wrapper_options(name: str, args: List[str]):
    """Read a wrapper's options the way getopt does: values attached (-Cdir,
    --chdir=dir) or separate, short flags clustered (-iC dir). Returns the
    command's words and the directories the wrapper runs it in (env -C, sudo -D,
    wsl --cd).

    A long option takes a value only when its name matches exactly. getopt also
    accepts abbreviations, but an exact boolean option wins over them (sudo's
    --login vs --login-class), and only value options are listed here. So an
    abbreviation is read as a flag: its value becomes the command, and a
    gh pr create after it is found by the backstop, which blocks."""
    with_value, chdir_opts = WRAPPERS[name], WRAPPER_CHDIR.get(name, set())
    args = list(args)
    dirs: List[str] = []
    i = 0

    def take(opt: str, value: str):
        if opt in chdir_opts:
            dirs.append(value)
        elif opt in ("-S", "--split-string"):  # env -S splits its value into more arguments, options included
            try:
                args[i:i] = shlex.split(value)
            except ValueError as err:
                raise ParseError(str(err)) from None

    while i < len(args):
        a = args[i]
        if a == "--":
            i += 1
            break
        if _ASSIGN.match(a) or (a == "-" and name == "env"):  # `env -` is env -i
            i += 1
            continue
        if not a.startswith("-") or a == "-":
            break
        i += 1
        if a.startswith("--"):
            opt, eq, value = a.partition("=")
            if opt in with_value:
                if not eq:
                    value, i = (args[i] if i < len(args) else ""), i + 1
                take(opt, value)
            continue
        for j in range(1, len(a)):
            opt = "-" + a[j]
            if opt in with_value:
                value = a[j + 1:]
                if not value:
                    value, i = (args[i] if i < len(args) else ""), i + 1
                take(opt, value)
                break  # the rest of the cluster was the value
    return args[i:], dirs


def _pwsh_value_option(low: str) -> Optional[str]:
    """The full name of a value-taking powershell.exe / pwsh switch, from an alias or any abbreviation."""
    if low in PWSH_VALUE_ALIASES:
        return PWSH_VALUE_ALIASES[low]
    if len(low) >= 3:
        return next((full for full in PWSH_VALUE_PARAMS if full.startswith(low)), None)
    return None


def _expand(cmd: Command, shell: str, depth: int, upstream: List[Command]) -> List[Command]:
    argv, chdir = list(cmd.argv), cmd.chdir
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
            argv, dirs = _wrapper_options(name, argv[1:])
            chdir += tuple(dirs)
            if name == "timeout" and argv and re.fullmatch(r"\d+(?:\.\d+)?[smhd]?", argv[0]):
                argv = argv[1:]  # the duration
        elif name == "cmd":
            k = next((j for j, a in enumerate(argv) if a.lower() in ("/c", "/k")), None)
            if k is None:
                break
            return _mark(commands(" ".join(argv[k + 1:]), "bash", depth + 1), True, cmd.early, chdir)
        elif name == "eval":  # runs in this shell, so a cd inside it isn't nested
            return _mark(commands(" ".join(argv[1:]), shell, depth + 1), cmd.nested, cmd.early, chdir)
        elif name in ("invoke-expression", "iex"):
            args = [a for a in argv[1:] if a.lower() not in ("-command", "-c")]
            script = " ".join(args) if args else "\n".join(_output_of(c) for c in upstream)
            return _mark(commands(script, "powershell", depth + 1), cmd.nested, cmd.early, chdir)
        elif name in POSIX_SHELLS or name in POWERSHELLS:
            return _mark(_shell(name, argv, cmd.stdin, depth, upstream), cmd.nested, cmd.early, chdir)
        else:
            break
    return [Command(argv, cmd.stdin, cmd.nested, cmd.early, chdir)] if argv else []


def _shell(name: str, argv: List[str], stdin: Optional[str], depth: int, upstream: List[Command]) -> List[Command]:
    """What a shell invocation runs: its -c / -Command script, or its stdin. The script
    runs in a child shell, so its commands are nested (a cd there stays there)."""
    pwsh = name in POWERSHELLS
    lang = "powershell" if pwsh else "bash"
    args, i, workdir = argv[1:], 0, ()

    def child(script: str) -> List[Command]:
        return _mark(commands(script, lang, depth + 1), nested=True, chdir=workdir)

    while i < len(args):
        a = args[i]
        if pwsh:
            if a[:1] in ("-", "/") and ":" in a:  # -WorkingDirectory:dir, -Command:'...'
                a, _, value = a.partition(":")
                args[i:i + 1] = [a, value]
            low = a.lower()
            if low.startswith("/"):  # powershell.exe also takes /c, /Command, /ec, /EncodedCommand
                low = "-" + low[1:]
            # PowerShell accepts any abbreviation of a switch (-co, -en). Where one is
            # ambiguous (-co: -Command or -ConfigurationName), assume it runs a script.
            if low == "-cwa" or (len(low) > len("-command") and "-commandwithargs".startswith(low)):
                return child(args[i + 1] if i + 1 < len(args) else "")  # PowerShell 7.4+: the rest are $args
            if len(low) >= 2 and "-command".startswith(low):
                script = " ".join(args[i + 1:])
                if script.strip() != "-":
                    return child(script)
                break  # `-Command -` reads the script from stdin
            if low == "-ec" or (len(low) >= 2 and "-encodedcommand".startswith(low)):
                return child(_decode_ps(args[i + 1] if i + 1 < len(args) else ""))
            if len(low) >= 2 and "-file".startswith(low):
                return [Command(argv, stdin)]  # runs a script file we can't see
            if not low.startswith("-"):
                if name == "powershell":  # Windows PowerShell treats a bare argument as -Command
                    return child(" ".join(args[i:]))
                return [Command(argv, stdin)]  # pwsh treats it as -File
            option = _pwsh_value_option(low)
            if option == "-workingdirectory" and i + 1 < len(args):
                workdir = (args[i + 1],)
            i += 2 if option else 1
        else:
            if a in BASH_VALUE_OPTIONS:
                i += 2
            elif a.startswith("-") and not a.startswith("--") and "c" in a[1:]:
                return child(args[i + 1] if i + 1 < len(args) else "")
            elif a.startswith(("-", "+")):
                i += 1
            else:
                return [Command(argv, stdin)]  # `bash script.sh`: a script file we can't see
    return child(stdin if stdin is not None else "\n".join(_output_of(c) for c in upstream))


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
