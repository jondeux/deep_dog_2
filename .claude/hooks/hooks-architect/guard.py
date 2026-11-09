#!/usr/bin/env python3
"""PreToolUse guard: one policy (policy.json + .claude/hooks-policy.json) for Bash and the file tools.

Decisions, strongest wins: deny > ask > approval > allow.
  deny      -> JSON permissionDecision "deny"; the reason goes to Claude.
  ask       -> JSON permissionDecision "ask"; the user sees a permission prompt.
  approval  -> allowed only if the user's latest message approved it (approval_mark.py
               recorded it for this session and prompt); otherwise "ask".
  allow     -> no output; the normal permission flow continues.

Fail closed: any internal error exits 1. Register this hook with "onFailure": "block"
(Claude Code v2.1.295+) so a crash or timeout blocks the call instead of letting it through.

Coverage, honestly: this reads the command text. It parses quoting, compound commands,
$(...) and backticks, `bash -c`, wrappers like sudo/env/xargs, and git's global options.
It cannot see what an encoded payload or a freshly written script will do at runtime;
the sandbox, the auto-mode classifier, and VM isolation cover that layer.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True  # keep __pycache__ out of the user's repo
sys.path.insert(0, str(Path(__file__).resolve().parent))
import hookcommon as hc  # noqa: E402

RANK = {"allow": 0, "approval": 1, "ask": 2, "deny": 3}
SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "ash", "fish"}
CODE_RUNNERS = {"python", "python3", "python2", "node", "deno", "bun", "ruby", "perl", "php"}
KEYWORDS = {"if", "then", "else", "elif", "fi", "do", "done", "while", "until", "case", "esac", "!", "{", "}", "(", ")", "time"}
CATASTROPHIC_RAW = {"/", "/*", "~", "~/", "~/*", "$HOME", "${HOME}", "$HOME/", "$HOME/*", ".", "./", "..", "../",
                    "*", "./*", ".*", "../*", "/.*"}
SYSTEM_TOP = {"/etc", "/usr", "/bin", "/sbin", "/var", "/home", "/root", "/opt", "/lib", "/lib64", "/boot", "/srv",
              "/sys", "/proc", "/dev", "/mnt", "/media", "/snap"}
DUMP_PATTERNS = [
    re.compile(r"os\.environ\b(?!\s*(\.get\b|\[|\.setdefault\b|\.pop\b))"),
    re.compile(r"process\.env\b(?!\s*(\.\w|\[|\?\.))"),
    re.compile(r"\bENV\.(to_h|to_a|each|inspect|keys|values)\b|\b(p|puts|pp)\s+ENV\b(?!\[)"),
    re.compile(r"%ENV\b|\bgetenv\(\s*\)|\$_ENV\b(?!\[)|\bDeno\.env\.toObject\b"),
]
GLOB_WORD_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.~$/@{}+-*?[]")
PATHISH = re.compile(r"[\w.~$/@{}+-]*[./~][\w.~$/@{}+-]*")


class Ctx:
    def __init__(self, policy: dict, project: Path, cwd: Path):
        self.policy = policy
        self.paths = policy.get("paths", {})
        self.project = project
        self.cwd = cwd
        self.state = hc.state_dir()
        self.guard_dirs = [hc.HERE] + ([Path(os.environ["CLAUDE_PLUGIN_ROOT"]).resolve()]
                                       if os.environ.get("CLAUDE_PLUGIN_ROOT") else [])
        self.secret_var = re.compile(policy.get("secret_var_regex", r"(KEY|TOKEN|SECRET)"), re.I)
        self.verdicts: list[tuple[str, str, str | None]] = []

    def add(self, decision: str, reason: str, approve: str | None = None) -> None:
        self.verdicts.append((decision, reason, approve))

    def lst(self, key: str) -> list:
        return self.paths.get(key, [])


# ------------------------------------------------------------------ paths

def check_path(ctx: Ctx, raw: str, op: str) -> None:
    """op: read | write | delete. Adds a verdict when the path is restricted."""
    if not raw or raw.startswith("-"):
        return
    expandable = EXPANDABLE is None or raw in EXPANDABLE
    if "{" in raw and "," in raw and expandable:  # bash expands {a,b} before the command runs: check every result
        forms = hc.brace_expand(raw)
        if forms != [raw]:
            for f in forms:
                check_path(ctx, f, op)
            return
    p = hc.expand(raw, ctx.cwd)
    if hc.under(p, ctx.state):
        ctx.add("deny", f"{raw}: hook state is off-limits to tools.")
        return
    allowed = hc.matches(p, ctx.lst("allow"), ctx.project)
    glob_word = hc.has_glob(raw) and expandable  # the shell replaces it with the files it names
    fixed = ctx.lst("zero_access") + ctx.lst("read_only") + ctx.lst("no_delete") + ctx.lst("protected")
    named = hc.glob_expansions(p, fixed, ctx.project) if glob_word else []
    # A glob that names files is judged by those files (`cat .env.exam*` names only the allow-listed .env.example); one that
    # names none yet is still judged by its text, so `cat ~/.ssh/id_*` stays denied where the key does not exist yet.
    if not allowed and not named:
        stub = re.split(r"[*?\[]", raw, maxsplit=1)[0] if glob_word else ""  # `.env` of `.env*`, as before
        hit = hc.matches(p, ctx.lst("zero_access"), ctx.project) or (
            hc.matches(hc.expand(stub, ctx.cwd), ctx.lst("zero_access"), ctx.project) if stub else None)
        if hit:
            ctx.add("deny", f"{raw} matches zero-access pattern {hit!r} (secrets). Use a committed template such as .env.example.")
            return
    names = [p] + named
    for q in named:
        if hc.matches(q, ctx.lst("allow"), ctx.project):
            continue
        hit = hc.matches(q, ctx.lst("zero_access"), ctx.project)
        if hit:
            ctx.add("deny", f"{raw} expands to {q.name!r}, which matches zero-access pattern {hit!r} (secrets).")
            return
    if op == "read":
        return
    for q in names:
        hit = hc.matches(q, ctx.lst("read_only"), ctx.project)
        if hit:
            ctx.add("deny", f"{raw} is read-only ({hit!r}).")
            return
        if op == "delete":
            hit = hc.matches(q, ctx.lst("no_delete"), ctx.project)
            if hit:
                ctx.add("deny", f"{raw} must not be deleted ({hit!r}).")
                return
    for q in names:
        if hc.matches(q, ctx.lst("protected"), ctx.project) or any(hc.under(q, d) for d in ctx.guard_dirs):
            ctx.add("ask", f"{raw} is hook or settings configuration; changing it needs the user's approval.")
            return


def scan_candidates(ctx: Ctx, text: str) -> None:
    """Check every path-like substring of an argument or code string for zero-access hits."""
    seen = set()
    cands = [text]
    for m in PATHISH.finditer(text):
        if text[m.end():m.end() + 1] and text[m.end()] in "*?[":
            # a path cut off by a glob character (".env.exam" of ".env.exam*"): judge the whole word, which check_path
            # resolves to the files it names, not the stub, which would hit `.env.*` although it names only .env.example
            a = m.start()
            while a > 0 and text[a - 1] in GLOB_WORD_CHARS:
                a -= 1
            b = m.end()
            while b < len(text) and text[b] in GLOB_WORD_CHARS:
                b += 1
            cands.append(text[a:b])
        else:
            cands.append(m.group())
    for cand in cands:
        kept = cand.strip("'\"@=:,()[];")  # keep braces so a trailing {a,b} survives
        cand = kept if kept.count("{") == kept.count("}") else cand.strip("'\"@=:,()[]{};")
        if cand and cand not in seen and ("." in cand or "/" in cand or "~" in cand):
            seen.add(cand)
            check_path(ctx, cand, "read")


def is_disposable(ctx: Ctx, raw: str) -> bool:
    p = hc.expand(raw, ctx.cwd)
    if hc.matches(p, ctx.lst("disposable"), ctx.project):
        return True
    return (not hc.under(p, ctx.project) and bool(hc.matches(p, ctx.lst("disposable_roots"), ctx.project))
            and p not in (Path("/tmp"), Path("/var/tmp")))


def is_catastrophic(ctx: Ctx, raw: str) -> bool:
    if raw in CATASTROPHIC_RAW:
        return True
    p = hc.expand(raw, ctx.cwd)
    home = Path(os.path.expanduser("~"))
    if p in (Path("/"), home, ctx.project) or str(p) in SYSTEM_TOP:
        return True
    return hc.under(ctx.project, p)  # an ancestor of the project


def check_delete(ctx: Ctx, targets: list, recursive: bool, source: str) -> None:
    if recursive and not targets:
        ctx.add("ask", f"{source}: recursive delete with targets from stdin or a glob the guard can't see.")
    for t in targets:
        if is_catastrophic(ctx, t):
            ctx.add("deny", f"{source} {t}: deleting this would destroy the project, home, or system.")
            continue
        before = len(ctx.verdicts)
        check_path(ctx, t, "delete")
        if len(ctx.verdicts) > before:
            continue
        if recursive and not is_disposable(ctx, t):
            ctx.add("ask", f"{source} {t}: recursive delete of a non-disposable path.")


# ------------------------------------------------------------------ shell parsing

def strip_heredocs(cmd: str) -> str:
    lines, out, delim = cmd.split("\n"), [], None
    for line in lines:
        if delim is not None:
            if line.strip() == delim:
                delim = None
            continue
        m = re.search(r"<<-?\s*(['\"]?)([A-Za-z_][\w-]*)\1", line)
        if m:
            delim = m.group(2)
            line = line[:m.start()] + line[m.end():]
        out.append(line)
    return "\n".join(out)


def extract_substitutions(cmd: str) -> tuple[str, list]:
    """Pull out $(...), <(...), >(...) and `...` bodies; return (outer, inner bodies)."""
    inner, out, i = [], [], 0
    while i < len(cmd):
        if cmd[i] in "$<>" and cmd[i + 1:i + 2] == "(" and cmd[i + 2:i + 3] != "(":
            depth, j = 1, i + 2
            while j < len(cmd) and depth:
                depth += {"(": 1, ")": -1}.get(cmd[j], 0)
                j += 1
            inner.append(cmd[i + 2:j - 1])
            out.append(" __SUBST__ ")
            i = j
        elif cmd[i] == "`":
            j = cmd.find("`", i + 1)
            j = len(cmd) if j < 0 else j
            inner.append(cmd[i + 1:j])
            out.append(" __SUBST__ ")
            i = j + 1
        else:
            out.append(cmd[i])
            i += 1
    return "".join(out), inner


# Words of the current Bash command that carry an unquoted glob or {a,b} brace: only those does the shell expand.
# None means "not a shell command" (the Read/Glob/Grep tools pass a path or pattern as-is): always expand.
EXPANDABLE: set | None = None
QUOTED = re.compile(r"""'[^']*'|"(?:\\.|[^"\\])*\"""")


def note_expandable(cmd: str) -> None:
    if EXPANDABLE is None:
        return
    lex = shlex.shlex(cmd.replace("\n", " ; "), posix=False, punctuation_chars=";&|<>()")
    lex.whitespace_split = True
    lex.commenters = ""
    try:
        words = list(lex)
    except ValueError:
        words = cmd.split()
    for w in words:
        bare = re.sub(r"\\.", "", QUOTED.sub("", w))  # drop quoted parts, then escaped characters
        if hc.has_glob(bare) or ("{" in bare and "," in bare):
            try:
                EXPANDABLE.add("".join(shlex.split(w)))
            except ValueError:
                EXPANDABLE.add(w)


def tokenize(cmd: str) -> list:
    note_expandable(cmd)
    lex = shlex.shlex(cmd.replace("\n", " ; "), posix=True, punctuation_chars=";&|<>()")
    lex.whitespace_split = True
    lex.commenters = ""
    return list(lex)


def segments(tokens: list) -> list:
    """Split into pipelines of commands; redirections become (op, target) pairs."""
    pipelines, pipe, argv, redirs, i = [], [], [], [], 0
    while i < len(tokens):
        t = tokens[i]
        if t in (";", "&&", "||", "&", ";;", "(", ")"):
            pipe.append((argv, redirs)); pipelines.append(pipe); pipe, argv, redirs = [], [], []
        elif t in ("|", "|&"):
            pipe.append((argv, redirs)); argv, redirs = [], []
        elif t and set(t) <= set("<>&") and ("<" in t or ">" in t):
            target = tokens[i + 1] if i + 1 < len(tokens) else ""
            if target.isdigit() or target == "-":
                i += 1
            else:
                redirs.append((">" if ">" in t else "<", target))
                i += 1
        elif t.isdigit() and i + 1 < len(tokens) and set(tokens[i + 1]) <= set("<>&") and tokens[i + 1]:
            pass  # fd number before a redirection
        else:
            argv.append(t)
        i += 1
    pipe.append((argv, redirs))
    pipelines.append(pipe)
    return [[(a, r) for a, r in p if a or r] for p in pipelines]


def strip_wrappers(ctx: Ctx, argv: list) -> list:
    """Remove assignments, sudo/env/timeout/... and control keywords. Flags env dumps."""
    a = list(argv)
    while a:
        head = a[0]
        if head in KEYWORDS or head == "__SUBST__" and len(a) == 1:
            a = a[1:]
        elif re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", head):
            a = a[1:]
        elif os.path.basename(head) in ("sudo", "doas"):
            a = a[1:]
            while a and a[0].startswith("-"):
                a = a[2:] if a[0] in ("-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U") else a[1:]
        elif os.path.basename(head) == "env":
            a = a[1:]
            while a and (a[0].startswith("-") or "=" in a[0]):
                a = a[2:] if a[0] in ("-u", "-C", "-S", "--unset", "--chdir") else a[1:]
            if not a and ctx.policy.get("env_dump", True):
                ctx.add("deny", "env without a command prints every environment variable, including secrets.")
        elif os.path.basename(head) in ("timeout", "nice", "ionice", "stdbuf", "nohup", "command", "builtin",
                                         "exec", "noglob", "nocorrect", "chrt", "taskset", "setsid", "unbuffer"):
            name = os.path.basename(head)
            if name == "command" and len(a) > 1 and a[1] in ("-v", "-V"):
                return []
            a = a[1:]
            while a and a[0].startswith("-"):
                a = a[2:] if a[0] in ("-n", "-c", "-s", "-k", "-p") else a[1:]
            if name == "timeout" and a and re.match(r"^\d+(\.\d+)?[smhd]?$", a[0]):
                a = a[1:]
            if name in ("chrt", "taskset") and a and re.match(r"^[\d,x-]+$", a[0]):
                a = a[1:]
        elif os.path.basename(head) == "xargs":
            a = a[1:]
            while a and a[0].startswith("-"):
                a = a[2:] if a[0] in ("-I", "-n", "-P", "-L", "-d", "-E", "-s", "-a") else a[1:]
            if a:
                a = a + ["__STDIN__"]
        else:
            break
    return a


def analyze_command(ctx: Ctx, cmd: str, depth: int = 0) -> None:
    if depth > 4:
        ctx.add("ask", "Command nests shells too deeply to analyze.")
        return
    outer, inner = extract_substitutions(strip_heredocs(cmd))
    for body in inner:
        analyze_command(ctx, body, depth + 1)
    try:
        tokens = tokenize(outer)
    except ValueError:
        ctx.add("ask", "Command has unbalanced quotes; the guard can't parse it.")
        scan_candidates(ctx, outer)
        return
    for pipeline in segments(tokens):
        cmds = []
        for argv, redirs in pipeline:
            for op, target in redirs:
                if op == ">":
                    check_path(ctx, target, "write")
                else:
                    scan_candidates(ctx, target)
            a = strip_wrappers(ctx, argv)
            if a:
                analyze_argv(ctx, a, depth)
                cmds.append(a)
        check_pipe_to_shell(ctx, cmds)


def check_pipe_to_shell(ctx: Ctx, cmds: list) -> None:
    decision = ctx.policy.get("pipe_to_shell", "ask")
    if decision == "allow":
        return
    names = [os.path.basename(c[0]) for c in cmds]
    for j, m in enumerate(names):
        if j > 0 and m in SHELLS and "-c" not in cmds[j]:
            src = "a downloaded script" if any(n in ("curl", "wget") for n in names[:j]) else "generated text"
            ctx.add(decision, f"Piping {src} into {m} runs code the guard can't review.")


def flags_and_args(args: list) -> tuple[list, list]:
    flags, rest, dashdash = [], [], False
    for x in args:
        if dashdash or not x.startswith("-") or x == "-":
            rest.append(x)
        elif x == "--":
            dashdash = True
        else:
            flags.append(x)
    return flags, rest


def analyze_argv(ctx: Ctx, argv: list, depth: int) -> None:
    name = os.path.basename(argv[0])
    args = argv[1:]

    if name in SHELLS and "-c" in args:
        i = args.index("-c")
        if i + 1 < len(args):
            if "__SUBST__" in args[i + 1]:
                ctx.add("ask", f"{name} -c runs code generated at runtime; the guard can't see what it does.")
            analyze_command(ctx, args[i + 1], depth + 1)
        return
    if name in ("eval", "source", ".") and any("__SUBST__" in a for a in args):
        ctx.add("ask", f"{name} runs code generated at runtime; the guard can't see what it does.")
    if name == "eval":
        analyze_command(ctx, " ".join(args), depth + 1)
        return

    for x in args:
        scan_candidates(ctx, x)

    if ctx.policy.get("env_dump", True):
        check_env_dump(ctx, name, args)

    if name in ("rm", "unlink", "shred", "rmdir", "srm"):
        flags, targets = flags_and_args(args)
        recursive = name in ("rm", "srm") and any(f == "--recursive" or (not f.startswith("--") and re.search("[rR]", f)) for f in flags)
        targets = [t for t in targets if t != "__STDIN__"]
        if "__STDIN__" in args and not targets:
            check_delete(ctx, [], recursive, name)
        check_delete(ctx, targets, recursive, name)
    elif name == "find":
        check_find(ctx, args)
    elif name in ("cp", "mv", "install", "ln", "rsync", "scp"):
        _, rest = flags_and_args(args)
        if rest:
            check_path(ctx, rest[-1], "write")
        if name == "mv":
            for src in rest[:-1]:
                check_path(ctx, src, "delete")
    elif name in ("tee", "truncate", "touch", "chmod", "chown", "chgrp", "chattr", "setfacl"):
        _, rest = flags_and_args(args)
        if name in ("chmod", "chown", "chgrp", "chattr", "setfacl") and rest:
            rest = rest[1:]
        for t in rest:
            check_path(ctx, t, "write")
    elif name in ("sed", "perl") and any(f.startswith("-i") or f == "--in-place" or (name == "perl" and re.match(r"^-\w*i", f)) for f in args):
        _, rest = flags_and_args(args)
        has_e = any(f in ("-e", "--expression") for f in args)
        for t in (rest if has_e else rest[1:]):
            check_path(ctx, t, "write")
    elif name == "git":
        check_git(ctx, args)

    check_commands(ctx, name, args)


def check_env_dump(ctx: Ctx, name: str, args: list) -> None:
    if name == "printenv" and not [a for a in args if not a.startswith("-")]:
        ctx.add("deny", "printenv prints every environment variable, including secrets.")
    elif name in ("export", "set", "declare", "typeset") and all(a.startswith("-") for a in args) and \
            (name != "set" or not args) and (name != "export" or not args or args == ["-p"]):
        ctx.add("deny", f"{name} without names lists the environment, including secrets.")
    elif name in CODE_RUNNERS:
        code = " ".join(args)
        if any(p.search(code) for p in DUMP_PATTERNS):
            ctx.add("deny", "This code prints the whole environment, including secrets.")
    elif name in ("echo", "printf", "print"):
        for var in re.findall(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)", " ".join(args)):
            if ctx.secret_var.search(var):
                ctx.add("deny", f"Printing ${var} would expose a secret.")


def check_find(ctx: Ctx, args: list) -> None:
    roots = []
    for a in args:
        if a.startswith("-") or a in ("(", "!", ")"):
            break
        roots.append(a)
    roots = roots or ["."]
    deleting = "-delete" in args
    for i, a in enumerate(args):
        if a in ("-exec", "-execdir", "-ok", "-okdir") and i + 1 < len(args) and \
                os.path.basename(args[i + 1]) in ("rm", "shred", "unlink", "srm"):
            deleting = True
    if not deleting:
        return
    names = [args[i + 1] for i, a in enumerate(args) if a in ("-name", "-iname") and i + 1 < len(args)]
    if names and all(hc.matches(Path(n), ctx.lst("disposable"), ctx.project) or
                     any(n == d for d in ctx.lst("disposable")) for n in names):
        return
    for r in roots:
        if r in ("/", "~", "$HOME", "${HOME}") or hc.expand(r, ctx.cwd) in (Path("/"), Path(os.path.expanduser("~"))):
            ctx.add("deny", f"find {r} ... deletes across the whole system or home directory.")
        elif not is_disposable(ctx, r):
            before = len(ctx.verdicts)
            check_path(ctx, r, "delete")
            if len(ctx.verdicts) == before:
                ctx.add("ask", f"find {r} ... deletes files; confirm the filter is right.")


GIT_VALUE_OPTS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--super-prefix", "--config-env"}


def parse_git(args: list) -> tuple[str | None, list]:
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in GIT_VALUE_OPTS else 1
    return (args[i], args[i + 1:]) if i < len(args) else (None, [])


def git_rule_hits(rule: dict, rest: list) -> bool:
    flags, pos = flags_and_args(rest)
    short = "".join(f[1:] for f in flags if not f.startswith("--"))
    positive = [k for k in ("any", "any_short", "prefix_any", "first_any", "force", "colon_refspec", "dashdash_paths") if k in rule]
    hit = not positive
    if "any" in rule and any(t in rule["any"] for t in rest):
        hit = True
    if "any_short" in rule and any(c in short for c in rule["any_short"]):
        hit = True
    if "prefix_any" in rule and any(t.startswith(p) for t in rest for p in rule["prefix_any"]):
        hit = True
    if "first_any" in rule and pos and pos[0] in rule["first_any"]:
        hit = True
    if rule.get("force") and ("--force" in flags or "f" in short or any(p.startswith("+") for p in pos)):
        hit = True
    if rule.get("colon_refspec") and any(p.startswith(":") and len(p) > 1 for p in pos):
        hit = True
    if rule.get("dashdash_paths") and "--" in rest and rest.index("--") < len(rest) - 1:
        hit = True
    if "unless_any" in rule and any(t in rule["unless_any"] for t in rest):
        return False
    if "unless_first" in rule and pos and pos[0] in rule["unless_first"]:
        return False
    if rule.get("unless_staged_only") and ({"--staged", "-S"} & set(flags)) and not ({"--worktree", "-W"} & set(flags)):
        return False
    return hit


def check_git(ctx: Ctx, args: list) -> None:
    sub, rest = parse_git(args)
    if not sub:
        return
    for rule in hc.enabled(ctx.policy.get("git", []), ctx.policy):
        subs = rule["sub"] if isinstance(rule["sub"], list) else [rule["sub"]]
        if sub in subs and git_rule_hits(rule, rest):
            ctx.add(rule["decision"], f"git {sub}: {rule.get('reason', '')} [{rule['id']}]", rule.get("approve"))
            if rule["decision"] in ("deny", "ask"):
                return  # a stronger rule already decides; skip approval rules below it


def check_commands(ctx: Ctx, name: str, args: list) -> None:
    joined = " ".join(a for a in args if a != "__STDIN__")
    for rule in hc.enabled(ctx.policy.get("commands", []), ctx.policy):
        if "cmd" in rule and name not in rule["cmd"]:
            continue
        if "cmd_regex" in rule and not re.search(rule["cmd_regex"], name):
            continue
        if "args_regex" in rule and not re.search(rule["args_regex"], joined):
            continue
        ctx.add(rule["decision"], f"{name}: {rule.get('reason', '')} [{rule['id']}]", rule.get("approve"))


def check_powershell(ctx: Ctx, cmd: str) -> None:
    scan_candidates(ctx, cmd)
    if re.search(r"(?i)\b(Remove-Item|rm|del|rd|rmdir)\b.*-Recurse", cmd):
        ctx.add("ask", "PowerShell recursive delete.")
    if re.search(r"(?i)\b(Get-ChildItem|gci|dir|ls)\s+env:|\[Environment\]::GetEnvironmentVariables\(\)", cmd):
        ctx.add("deny", "This lists the whole environment, including secrets.")


# ------------------------------------------------------------------ decision

def approved(payload: dict, word: str, policy: dict) -> bool:
    f = hc.state_dir() / f"approvals-{hc.safe_id(payload.get('session_id'))}.json"
    try:
        state = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if word not in state.get("approved", []):
        return False
    pid, spid = payload.get("prompt_id"), state.get("prompt_id")
    if pid and spid:
        return pid == spid
    window = float(policy.get("approval", {}).get("window_minutes", 30)) * 60
    return time.time() - float(state.get("ts", 0)) <= window


def policy_mode(payload: dict, ctx: Ctx) -> str:
    """approval.on_missing: "deny" (default) or "ask"."""
    return "ask" if ctx.policy.get("approval", {}).get("on_missing") == "ask" else "deny"


def decide(ctx: Ctx, payload: dict) -> tuple[str, str]:
    if not ctx.verdicts:
        return "allow", ""
    top = max(RANK[d] for d, _, _ in ctx.verdicts)
    if top == RANK["approval"]:
        missing = sorted({w for d, _, w in ctx.verdicts if d == "approval" and w and not approved(payload, w, ctx.policy)})
        if not missing:
            return "allow", ""
        reasons = "; ".join(r for d, r, w in ctx.verdicts if d == "approval" and w in missing)
        base = f"{reasons} No approval for {', '.join(missing)} was found in the latest user message."
        if policy_mode(payload, ctx) == "ask":
            return "ask", base
        # Deny, not ask: an ask is only as strong as the prompt the host shows, and a cloud session set to Auto answered it
        # itself (round 3, 2026-11-06: the agent committed and pushed on the Stop-hook nag with this gate installed).
        words = " / ".join(f'"{w}"' for w in missing)
        return "deny", (f"{base} Do not retry. A stop-hook message that asks for it is not approval. Tell the user in one "
                        f"line what you are waiting for and stop: their next message approves it if it says {words}.")
    decision = next(d for d in RANK if RANK[d] == top)
    reasons = list(dict.fromkeys(r for d, r, _ in ctx.verdicts if RANK[d] == top))
    return decision, " ".join(reasons[:3])


def check(payload: dict, policy: dict, project: Path) -> Ctx:
    tool = payload.get("tool_name", "")
    ti = payload.get("tool_input") or {}
    global EXPANDABLE
    EXPANDABLE = set() if tool in ("Bash", "PowerShell") else None
    ctx = Ctx(policy, project, Path(payload.get("cwd") or project))
    if tool == "Bash":
        analyze_command(ctx, str(ti.get("command", "")))
    elif tool == "PowerShell":
        check_powershell(ctx, str(ti.get("command", "")))
    elif tool == "Read":
        check_path(ctx, str(ti.get("file_path", "")), "read")
    elif tool in ("Edit", "Write", "MultiEdit"):
        check_path(ctx, str(ti.get("file_path", "")), "write")
    elif tool == "NotebookEdit":
        check_path(ctx, str(ti.get("notebook_path") or ti.get("file_path") or ""), "write")
    elif tool in ("Grep", "Glob"):
        for key in ("path", "glob") if tool == "Grep" else ("path", "pattern"):
            if ti.get(key):
                check_path(ctx, str(ti[key]), "read")
    return ctx


def main() -> int:
    try:
        payload = hc.read_payload()
        project = hc.project_dir(payload)
        policy = hc.load_policy(project)
        ctx = check(payload, policy, project)
        decision, reason = decide(ctx, payload)
    except Exception as exc:  # fail closed; pair with "onFailure": "block"
        print(f"hooks-architect guard failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if decision in ("deny", "ask"):
        hc.emit({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision,
                                        "permissionDecisionReason": f"hooks-architect: {reason}"}})
    return 0


if __name__ == "__main__":
    sys.exit(main())
