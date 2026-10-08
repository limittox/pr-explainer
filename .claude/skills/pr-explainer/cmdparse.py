"""Find the commands a Bash or PowerShell command line would run.

Used by the PR explainer hooks to spot `gh pr create` and `git push`. Words come
from shlex, so quoted text is never mistaken for a command. It also looks inside
the places a command can hide:
- separators and pipelines: ; && || | & ( ) and newlines
- keywords and wrappers: if/then/do, !, { }, time, sudo, env, nice, nohup,
  timeout, xargs, wsl, cmd /c, eval, Invoke-Expression / iex
- shells given a script: bash -c, sh -lc, pwsh -Command, -EncodedCommand, and
  heredocs, echo output or here-strings piped into a shell
- command substitution: $(...) and, in Bash, backticks (not inside single quotes)

It doesn't expand aliases, functions or variables. It's a guard rail for a
cooperative agent, not a sandbox.
"""
from __future__ import annotations

import base64
import binascii
import re
import shlex
from dataclasses import dataclass
from typing import List, Optional

MAX_DEPTH = 5
PUNCT = "();<>|&\n"
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
_REDIRECT = re.compile(r"^(?:<<<|<<|<>|<&|>&|>>|>\||&>>|&>|<|>)$")
_HEREDOC = re.compile(r"(?<![<\d])<<(?!<)(-?)[ \t]*(?:'([^'\n]+)'|\"([^\"\n]+)\"|\\?([A-Za-z_][\w.-]*))")
_HERESTRING = re.compile(r"@(['\"])[ \t]*\r?\n(.*?)\r?\n\1@", re.S)
_DOC_REF = re.compile(r"__PRX_DOC(\d+)__")
_STR_REF = re.compile(r"__PRX_STR(\d+)__")


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
    """Every simple command `text` would run, in order, wrappers unwrapped."""
    if not text or _depth > MAX_DEPTH:
        return []
    pwsh = shell == "powershell"
    docs: List[str] = []
    strings: List[str] = []
    if pwsh:
        text = _HERESTRING.sub(lambda m: f" __PRX_STR{_append(strings, m.group(2))}__ ", text)
    else:
        text = _extract_heredocs(text, docs)
    text, substitutions = _scan(text, pwsh)
    found: List[Command] = []
    for sub in substitutions:
        found += commands(sub, shell, _depth + 1)
    for pipeline in _pipelines(_tokens(text, pwsh), docs, strings):
        for i, cmd in enumerate(pipeline):
            found += _expand(cmd, shell, _depth, pipeline[:i])
    return found


def _append(items, item) -> int:
    items.append(item)
    return len(items) - 1


def _extract_heredocs(text: str, docs: List[str]) -> str:
    """Move heredoc bodies out of the text, leaving `<< __PRX_DOCn__` behind."""
    pos = 0
    while True:
        m = _HEREDOC.search(text, pos)
        if not m:
            return text
        delim = m.group(2) or m.group(3) or m.group(4)
        line_end = text.find("\n", m.end())
        if line_end == -1:
            return text
        body_start = k = line_end + 1
        body_end = rest = len(text)
        while k <= len(text):
            nl = text.find("\n", k)
            line = text[k: nl if nl != -1 else len(text)]
            if (line.lstrip("\t") if m.group(1) else line).rstrip("\r") == delim:
                body_end, rest = k, (nl if nl != -1 else len(text))
                break
            if nl == -1:
                break
            k = nl + 1
        marker = f" << __PRX_DOC{_append(docs, text[body_start:body_end])}__"
        text = text[:m.start()] + marker + text[m.end():line_end] + text[rest:]
        pos = m.start() + len(marker)


def _scan(text: str, pwsh: bool):
    """Drop comments and collect $(...) and backtick bodies, respecting quotes."""
    out, subs = [], []
    esc = "`" if pwsh else "\\"
    i, n, quote = 0, len(text), None
    while i < n:
        c = text[i]
        if quote == "'":
            quote = None if c == "'" else quote
            out.append(c)
            i += 1
            continue
        if c == esc and i + 1 < n:
            out.append(text[i:i + 2])
            i += 2
            continue
        if c == '"':
            quote = None if quote == '"' else '"'
        elif c == "'" and quote is None:
            quote = "'"
        elif c == "#" and quote is None and (i == 0 or text[i - 1] in " \t\n;|&("):
            j = text.find("\n", i)
            i = n if j == -1 else j  # keep the newline: it separates commands
            continue
        elif text.startswith("$(", i) and not text.startswith("$((", i):
            subs.append(text[i + 2:_close_paren(text, i + 1)])
        elif c == "`" and not pwsh:
            j = text.find("`", i + 1)
            if j != -1:
                subs.append(text[i + 1:j])
        out.append(c)
        i += 1
    return "".join(out), subs


def _close_paren(text: str, i: int) -> int:
    depth, quote = 0, None
    while i < len(text):
        c = text[i]
        if quote:
            quote = None if c == quote else quote
        elif c in "'\"":
            quote = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return len(text)


def _tokens(text: str, pwsh: bool) -> List[str]:
    lex = shlex.shlex(text, posix=True, punctuation_chars=PUNCT)
    lex.whitespace = " \t\r"  # newlines separate commands, so they're punctuation
    lex.whitespace_split = True
    lex.commenters = ""  # comments were removed by _scan, which knows where they can start
    if pwsh:
        lex.escape = "`"  # backslashes are path separators in PowerShell
    try:
        return list(lex)
    except ValueError as err:
        raise ParseError(str(err)) from None


def _pipelines(tokens: List[str], docs: List[str], strings: List[str]) -> List[List[Command]]:
    pipelines: List[List[Command]] = []
    pipe: List[Command] = []
    argv: List[str] = []
    stdin: Optional[str] = None

    def resolve(word: str) -> str:
        return _STR_REF.sub(lambda m: strings[int(m.group(1))], word)

    def end_command():
        nonlocal argv, stdin
        if argv or stdin is not None:
            pipe.append(Command(argv, stdin))
        argv, stdin = [], None

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok and set(tok) <= set(PUNCT):
            if _REDIRECT.match(tok):
                target = tokens[i + 1] if i + 1 < len(tokens) else ""
                doc = _DOC_REF.fullmatch(target)
                if tok == "<<" and doc:
                    stdin = docs[int(doc.group(1))]
                elif tok == "<<<":
                    stdin = resolve(target)
                i += 2
                continue
            end_command()
            if tok.strip("()") not in ("|", "|&"):  # anything but a pipe ends the pipeline
                if pipe:
                    pipelines.append(pipe)
                pipe = []
            i += 1
            continue
        argv.append(resolve(tok))
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
        if _ASSIGN.match(argv[0]) or name in KEYWORDS:
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
            if low in ("-c", "-command") or (len(low) > 3 and "-command".startswith(low)):
                script = " ".join(args[i + 1:])
                if script.strip() != "-":
                    return commands(script, lang, depth + 1)
                break  # `-Command -` reads the script from stdin
            if low in ("-e", "-ec") or (len(low) > 3 and "-encodedcommand".startswith(low)):
                return commands(_decode_ps(args[i + 1] if i + 1 < len(args) else ""), lang, depth + 1)
            if low in ("-f", "-file"):
                return [Command(argv, stdin)]  # runs a script file we can't see
            if not a.startswith("-"):
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
    try:
        return base64.b64decode(b64 + "=" * (-len(b64) % 4)).decode("utf-16-le")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return ""
