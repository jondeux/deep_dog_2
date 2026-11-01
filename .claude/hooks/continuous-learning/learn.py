#!/usr/bin/env python3
"""continuous-learning-architect: turn failed turns into proved, approval-gated candidates.

Propose only. Nothing here edits the live harness except `apply`, which runs only for a
candidate the user already approved in their own words (memory.py candidate resolve).

  learn.py init      --project DIR [--memory-py PATH]   create claude/learner.json and claude/memory/failures.md
  learn.py install   --project DIR                      vendor this script, add the SessionStart hook and /learn
  learn.py ticket    --project DIR --check ID --exit N [--failing T..] [--files F..] [--trace T]
  learn.py ticket    --project DIR --from-trace FILE --format claude|otel --check ID [--dry-run]
  learn.py ingest    --project DIR                      fold the Stop-hook inbox and correction signals into tickets
  learn.py tickets   --project DIR [--json]
  learn.py status    --project DIR                      one line for SessionStart when tickets are open
  learn.py plan      --project DIR [--n N]              one worktree per open signature (at most N); JSON packet
  learn.py prove     --project DIR --ticket F1 --worktree W [--gate-only] [--evidence FILE..] [--eval pass|fail]
  learn.py record    --project DIR --ticket F1 --worktree W --target T --rule R --why W --scope S [--eval pass]
  learn.py apply     --project DIR C3                   apply an approved candidate's patch, then re-prove the rest
  learn.py report    --project DIR [--json]             acceptance and recurrence against the kill criteria

Runs carrying CLAUDE_LEARNER=1 write no tickets and print no status (recursion guard).
Stdlib only. Spec: docs/specs/continuous-learning-agent/.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True  # keep __pycache__ out of the user's repo

MEM = "claude/memory"
FAILURES = f"{MEM}/failures.md"
INBOX = f"{MEM}/failures.inbox.jsonl"
PATCHES = f"{MEM}/patches"
CONFIG = "claude/learner.json"
SIGNALS = ".expertise/signals.jsonl"
COLS = ("id", "kind", "check", "exit", "files", "trace", "date", "last", "count", "signature", "status")
STATUS = re.compile(r"^(open|resolved|stale|candidate C\d+(,C\d+)*)$")
# Always forbidden, whatever claude/learner.json says: guard and approval code, hook registration, the learner itself.
FORBIDDEN = (".claude/hooks/hooks-architect/guard.py", ".claude/hooks/hooks-architect/approval_mark.py",
             ".claude/hooks/hooks-architect/hookcommon.py", ".claude/hooks/hooks-architect/stop_checks.py",
             ".claude/hooks/hooks-architect/policy.json", ".claude/settings.json", ".claude/settings.local.json",
             ".claude/hooks/continuous-learning/**", CONFIG, f"{MEM}/candidates.md", f"{PATCHES}/**",
             "skills/hooks-architect/assets/hooks/guard.py", "skills/hooks-architect/assets/hooks/approval_mark.py",
             "skills/hooks-architect/assets/hooks/hookcommon.py", "skills/hooks-architect/assets/hooks/stop_checks.py",
             "skills/continuous-learning-architect/**")
EVAL_GLOBS = ("skills/*/SKILL.md", "skills/*/references/**", "AGENTS.md", "CLAUDE.md", "**/INSTRUCTION.md", "INSTRUCTION.md")
TEST_GLOBS = ("tests/**", "test/**", "**/test_*.py", "**/*_test.py", "**/*.test.*", "**/*.spec.*")
ASSERTION = re.compile(r"\bassert|\.assert|\bexpect\(|\bshould\b|pytest\.raises|assertRaises")
VERBATIM = 40
FAILING_RES = (re.compile(r"^(?:FAILED|ERROR) (\S+?)(?: - .*)?$", re.M),
               re.compile(r"^(?:FAIL|ERROR): (\S+) \(([\w.]+)\)", re.M))
DEFAULT_CONFIG = {
    "checks": [], "facets": {}, "parallel": 2, "token_budget": 200000, "check_timeout": 900,
    "memory_py": "", "forbidden": [], "eval_globs": list(EVAL_GLOBS), "test_globs": list(TEST_GLOBS),
}
FAILURES_TEMPLATE = """# Failures

Metadata tickets from failed turns (continuous-learning-architect). No transcript or tool-output text.
`learn.py ingest` folds new tickets in; `/learn` drafts and proves candidates for open ones.

<!-- signals-offset: 0 -->

| {cols} |
|{seps}|
""".format(cols=" | ".join(COLS), seps="|".join("---" for _ in COLS))


def today() -> str:
    return dt.date.today().isoformat()


def die(msg: str, code: int = 1) -> int:
    print(f"learn.py: {msg}", file=sys.stderr)
    return code


def learner_run() -> bool:
    return os.environ.get("CLAUDE_LEARNER") == "1"


def git(cwd, *args, check=True) -> str:
    p = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    if check and p.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {p.stderr.strip()}")
    return p.stdout


def match(path: str, globs) -> bool:
    for g in globs:
        if fnmatch.fnmatch(path, g) or (g.startswith("**/") and fnmatch.fnmatch(path.rsplit("/", 1)[-1], g[3:])):
            return True
        if g.endswith("/**") and path.startswith(g[:-2]):
            return True
    return False


def config(root: Path) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    f = root / CONFIG
    if f.is_file():
        cfg.update(json.loads(f.read_text(encoding="utf-8")))
    cfg["forbidden"] = list(FORBIDDEN) + list(cfg.get("forbidden") or [])
    return cfg


def failing_ids(text: str) -> list:
    ids = set(FAILING_RES[0].findall(text or ""))
    ids |= {f"{cls}.{name}" for name, cls in FAILING_RES[1].findall(text or "")}
    return sorted(ids)


def signature(rec: dict) -> str:
    extra = rec.get("ehash") if rec.get("kind") == "correction" else ",".join(sorted(rec.get("failing") or []))
    return hashlib.sha256(f"{rec.get('kind')}|{rec.get('check')}|{extra}".encode()).hexdigest()[:12]


# ---------------------------------------------------------------- ticket table
def read_table(root: Path):
    f = root / FAILURES
    text = f.read_text(encoding="utf-8") if f.is_file() else FAILURES_TEMPLATE
    rows = []
    for line in text.splitlines():
        if re.match(r"^\|\s*F\d+\s*\|", line):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) == len(COLS):
                r = dict(zip(COLS, cells))
                r["count"] = int(r["count"] or 0)
                r["exit"] = int(r["exit"]) if re.fullmatch(r"-?\d+", r["exit"]) else ""
                r["files"] = [x for x in r["files"].split(", ") if x]
                rows.append(r)
    m = re.search(r"<!-- signals-offset: (\d+) -->", text)
    return text, rows, int(m.group(1)) if m else 0


def write_table(root: Path, text: str, rows: list, offset: int) -> None:
    head = text.split("\n| id |")[0].rstrip("\n") if "\n| id |" in text else FAILURES_TEMPLATE.split("\n| id |")[0]
    head = re.sub(r"<!-- signals-offset: \d+ -->", f"<!-- signals-offset: {offset} -->", head)
    lines = [head, "", f"| {' | '.join(COLS)} |", "|" + "|".join("---" for _ in COLS) + "|"]
    for r in rows:
        cells = [", ".join(r["files"]) if k == "files" else str(r[k]) for k in COLS]
        lines.append("| " + " | ".join(c.replace("|", "/") for c in cells) + " |")
    new = "\n".join(lines) + "\n"
    f = root / FAILURES
    if not f.is_file() or f.read_text(encoding="utf-8") != new:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(new, encoding="utf-8")


def upsert(rows: list, rec: dict) -> dict:
    sig = signature(rec)
    for r in rows:
        if r["signature"] == sig:
            r["count"] += 1
            r["last"] = rec.get("date") or today()
            if rec.get("exit") not in (None, ""):
                r["exit"] = rec["exit"]
            if r["status"] in ("resolved", "stale"):
                r["status"] = "open"
            return r
    n = max((int(r["id"][1:]) for r in rows), default=0) + 1
    r = {"id": f"F{n}", "kind": rec.get("kind", "check"), "check": rec.get("check", ""),
         "exit": rec.get("exit", ""), "files": list(rec.get("files") or [])[:10], "trace": rec.get("trace", ""),
         "date": rec.get("date") or today(), "last": rec.get("date") or today(), "count": 1,
         "signature": sig, "status": "open"}
    rows.append(r)
    return r


def ingest(root: Path) -> list:
    """Fold the Stop-hook inbox and new correction signals into failures.md. Returns the rows."""
    text, rows, offset = read_table(root)
    before = json.dumps(rows, sort_keys=True), offset
    inbox = root / INBOX
    if inbox.is_file():
        for line in inbox.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict) and rec.get("kind") in ("check", "correction"):
                upsert(rows, rec)
        inbox.write_text("", encoding="utf-8")
    sig_file = root / SIGNALS
    if sig_file.is_file():
        lines = sig_file.read_text(encoding="utf-8").splitlines()
        for n, line in enumerate(lines[offset:], offset + 1):
            try:
                s = json.loads(line)
            except ValueError:
                continue
            words = " ".join(re.findall(r"[a-z0-9']+", str(s.get("excerpt", "")).lower()))
            date = dt.date.fromtimestamp(s["ts"]).isoformat() if isinstance(s.get("ts"), (int, float)) else today()
            upsert(rows, {"kind": "correction", "check": f"correction:p{s.get('pattern', '?')}", "exit": "",
                          "files": [], "trace": f"{SIGNALS}:{n}", "date": date,
                          "ehash": hashlib.sha256(words.encode()).hexdigest()})
        offset = len(lines)
    if (json.dumps(rows, sort_keys=True), offset) != before or not (root / FAILURES).is_file():
        write_table(root, text, rows, offset)
    return rows


def set_status(root: Path, pred, status: str) -> None:
    text, rows, offset = read_table(root)
    for r in rows:
        if pred(r):
            r["status"] = status
    write_table(root, text, rows, offset)


def find_ticket(root: Path, tid: str) -> dict:
    for r in read_table(root)[1]:
        if r["id"] == tid:
            return r
    raise KeyError(f"no ticket {tid} in {FAILURES}")


# ---------------------------------------------------------------- trajectory adapters
def rel(path: str, cwd: str) -> str:
    if cwd and path.startswith(cwd.rstrip("/") + "/"):
        return path[len(cwd.rstrip("/")) + 1:]
    return path


def events_claude(path: Path) -> list:
    """Claude Code transcript JSONL -> ordered ("run", cmd, exit, output) / ("edit", path) events."""
    uses, ev = {}, []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        content = (rec.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for c in content:
            if c.get("type") == "tool_use":
                inp = c.get("input") or {}
                if c.get("name") == "Bash":
                    uses[c.get("id")] = inp.get("command", "")
                elif inp.get("file_path"):
                    ev.append(("edit", rel(inp["file_path"], rec.get("cwd", ""))))
            elif c.get("type") == "tool_result" and c.get("tool_use_id") in uses:
                out = c.get("content")
                if isinstance(out, list):
                    out = "\n".join(x.get("text", "") for x in out if isinstance(x, dict))
                out = str(out or "")
                m = re.match(r"\s*Exit code (\d+)", out)
                code = int(m.group(1)) if m else (1 if c.get("is_error") else 0)
                ev.append(("run", uses.pop(c["tool_use_id"]), code, out))
    return ev


def events_otel(path: Path) -> list:
    """OTLP JSON with GenAI execute_tool spans -> the same events, ordered by start time."""
    data = json.loads(path.read_text(encoding="utf-8"))
    spans = [s for rs in data.get("resourceSpans", []) for ss in rs.get("scopeSpans", []) for s in ss.get("spans", [])]
    ev = []
    for s in sorted(spans, key=lambda s: int(s.get("startTimeUnixNano", 0))):
        a = {x["key"]: next(iter(x.get("value", {}).values()), "") for x in s.get("attributes", [])}
        if a.get("gen_ai.operation.name") != "execute_tool":
            continue
        try:
            args = json.loads(a.get("gen_ai.tool.call.arguments") or "{}")
        except ValueError:
            args = {}
        if a.get("gen_ai.tool.name") == "Bash":
            code = a.get("process.exit.code")
            code = int(code) if str(code).lstrip("-").isdigit() else (1 if s.get("status", {}).get("code") == 2 else 0)
            ev.append(("run", args.get("command", ""), code, str(a.get("gen_ai.tool.call.result", ""))))
        elif args.get("file_path"):
            ev.append(("edit", args["file_path"]))
    return ev


def ticket_from_events(ev: list, check: dict) -> dict | None:
    cmds = [c for c in (check.get("command"), check.get("fast")) if c]
    runs = [i for i, e in enumerate(ev) if e[0] == "run" and any(c in e[1] for c in cmds)]
    if not runs or ev[runs[-1]][2] == 0:
        return None
    greens = [i for i in runs[:-1] if ev[i][2] == 0]
    if not greens:
        return None  # no green baseline in the trajectory
    last = ev[runs[-1]]
    files = sorted({e[1] for e in ev[greens[-1]:runs[-1]] if e[0] == "edit"})[:10]
    return {"kind": "check", "check": check["id"], "exit": last[2], "files": files,
            "failing": failing_ids(last[3]), "date": today()}


# ---------------------------------------------------------------- commands
def cmd_init(a) -> int:
    root = a.project.resolve()
    cfg_file = root / CONFIG
    if not cfg_file.exists():
        cfg = dict(DEFAULT_CONFIG)
        vendored = root / ".claude" / "hooks" / "continuous-learning" / "memory.py"
        found = find_memory_py(root, getattr(a, "memory_py", "") or "")
        cfg["memory_py"] = (".claude/hooks/continuous-learning/memory.py" if vendored.is_file()
                            else str(found) if found else "")
        cfg_file.parent.mkdir(parents=True, exist_ok=True)
        cfg_file.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {CONFIG}: declare checks (id, command, optional fast subset) and facet linters there")
    if not (root / FAILURES).exists():
        write_table(root, FAILURES_TEMPLATE, [], 0)
        print(f"wrote {FAILURES}")
    idx = root / MEM / "MEMORY.md"
    if idx.is_file() and "(failures.md)" not in idx.read_text(encoding="utf-8"):
        s = idx.read_text(encoding="utf-8")
        line = "- [Failures](failures.md): metadata tickets from failed turns; /learn drafts candidates for open ones."
        s = re.sub(r"(- \[Candidates\]\(candidates\.md\)[^\n]*\n)", lambda m: m.group(1) + line + "\n", s, count=1) \
            if "(candidates.md)" in s else s.rstrip("\n") + "\n" + line + "\n"
        idx.write_text(s, encoding="utf-8")
        print(f"linked failures.md from {MEM}/MEMORY.md")
    return 0


def cmd_install(a) -> int:
    root = a.project.resolve()
    here = Path(__file__).resolve()
    dest = root / ".claude" / "hooks" / "continuous-learning"
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(here, dest / "learn.py")
    mp = find_memory_py(root, getattr(a, "memory_py", "") or "")
    if mp and mp.resolve() != (dest / "memory.py").resolve():
        shutil.copyfile(mp, dest / "memory.py")  # a real repo has no skills/ folder to find it in
    cfg_file = root / CONFIG
    if (dest / "memory.py").is_file() and cfg_file.is_file():
        cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
        cfg["memory_py"] = ".claude/hooks/continuous-learning/memory.py"
        cfg_file.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    skill_dir = root / ".claude" / "skills" / "learn"  # project skill = the /learn command
    skill_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(here.parent.parent / "assets" / "learn" / "SKILL.md", skill_dir / "SKILL.md")
    sp = root / ".claude" / "settings.json"
    try:
        s = json.loads(sp.read_text(encoding="utf-8")) if sp.is_file() else {}
    except ValueError:
        return die(f"{sp} is not valid JSON; fix it first (nothing written to it)")
    hook = {"type": "command", "command": "python3",
            "args": ["-I", "${CLAUDE_PROJECT_DIR}/.claude/hooks/continuous-learning/learn.py", "status",
                     "--project", "${CLAUDE_PROJECT_DIR}"], "timeout": 15}
    entries = s.setdefault("hooks", {}).setdefault("SessionStart", [])
    if not any("continuous-learning/learn.py" in json.dumps(e) for e in entries):
        entries.append({"hooks": [hook]})
    sp.write_text(json.dumps(s, indent=2) + "\n", encoding="utf-8")
    pol = root / ".claude" / "hooks-policy.json"
    try:
        p = json.loads(pol.read_text(encoding="utf-8")) if pol.is_file() else {}
    except ValueError:
        return die(f"{pol} is not valid JSON; fix it first")
    stop = p.setdefault("stop", {})
    stop.setdefault("tickets", INBOX)
    stop.setdefault("check_id", a.check_id)
    pol.write_text(json.dumps(p, indent=2) + "\n", encoding="utf-8")
    cmd_init(a)
    written = [".claude/hooks/continuous-learning/learn.py", ".claude/hooks/continuous-learning/memory.py",
               ".claude/skills/learn/SKILL.md", ".claude/settings.json", ".claude/hooks-policy.json", CONFIG, FAILURES]
    ignored = [f for f in written if (root / f).exists() and subprocess.run(
        ["git", "-C", str(root), "check-ignore", "-q", f], capture_output=True).returncode == 0]
    if ignored:
        print("WARNING: these installed files are git-ignored, so they never reach a fresh clone or cloud session: "
              f"{', '.join(ignored)}. Add '!' exceptions to .gitignore (e.g. '!.claude/skills/').")
    print("installed: .claude/hooks/continuous-learning/learn.py, SessionStart status hook, /learn (.claude/skills/learn), "
          f"stop.tickets in .claude/hooks-policy.json (check id {stop['check_id']}). "
          "Stop tickets need hooks-architect's stop hook with a fast stop.command.")
    return 0


def cmd_ticket(a) -> int:
    root = a.project.resolve()
    if learner_run():
        return 0
    if a.from_trace:
        check = next((c for c in config(root)["checks"] if c.get("id") == a.check), None) or {"id": a.check,
                                                                                            "command": a.command or a.check}
        ev = (events_claude if a.format == "claude" else events_otel)(Path(a.from_trace))
        rec = ticket_from_events(ev, check)
        if rec is None:
            return 0
        rec["trace"] = str(a.from_trace)
    else:
        if a.exit is None:
            return die("--exit is required without --from-trace", 2)
        rec = {"kind": "check", "check": a.check, "exit": a.exit, "files": a.files or [], "failing": a.failing or [],
               "trace": a.trace or "", "date": today()}
    if a.dry_run:
        print(json.dumps(rec, sort_keys=True))
        return 0
    (root / INBOX).parent.mkdir(parents=True, exist_ok=True)
    with (root / INBOX).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")
    r = [x for x in ingest(root) if x["signature"] == signature(rec)][0]
    print(f"ticket {r['id']} ({r['check']}, seen {r['count']}x)")
    return 0


def cmd_ingest(a) -> int:
    if learner_run():
        return 0
    rows = ingest(a.project.resolve())
    print(f"{sum(r['status'] == 'open' for r in rows)} open ticket(s)")
    return 0


def cmd_tickets(a) -> int:
    root = a.project.resolve()
    rows = read_table(root)[1] if learner_run() else ingest(root)
    if a.json:
        print(json.dumps(rows, indent=1))
    else:
        for r in rows:
            print(f"{r['id']:4} {r['status']:14} {r['kind']:10} {r['check']:20} x{r['count']}  {r['signature']}")
    return 0


def cmd_status(a) -> int:
    if learner_run():
        return 0
    try:
        rows = ingest(a.project.resolve())
    except Exception:
        return 0  # SessionStart must never fail over bookkeeping
    n = sum(r["status"] == "open" for r in rows)
    if n:
        print(f"Learner: {n} open failure ticket(s) in {FAILURES}. /learn drafts and proves candidates for them "
              "(nothing is applied without the user's approval).")
    return 0


def snapshot(root: Path) -> str:
    """Commit object of the working tree (tracked + untracked, minus ignored) on top of HEAD.
    Uses a temporary index, so the user's index, branch and files are untouched."""
    with tempfile.TemporaryDirectory() as d:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(d) / "index"))
        def g(*args):
            return subprocess.run(["git", "-C", str(root), *args], env=env, capture_output=True, text=True,
                                  check=True).stdout.strip()
        g("read-tree", "HEAD")
        g("add", "-A")
        tree = g("write-tree")
        head = g("rev-parse", "HEAD")
        if tree == g("rev-parse", "HEAD^{tree}"):
            return head
        return subprocess.run(["git", "-C", str(root), "-c", "user.name=learner", "-c", "user.email=learner@localhost",
                               "commit-tree", tree, "-p", head, "-m", "learner snapshot of the working tree"],
                              capture_output=True, text=True, check=True).stdout.strip()


def worktree_root(root: Path) -> Path:
    h = hashlib.sha256(str(root).encode()).hexdigest()[:8]
    return Path(tempfile.gettempdir()) / f"learner-{h}"


def cmd_plan(a) -> int:
    root = a.project.resolve()
    cfg = config(root)
    rows = ingest(root)
    n = a.n or int(cfg.get("parallel", 2))
    todo = sorted((r for r in rows if r["status"] == "open"), key=lambda r: (-r["count"], int(r["id"][1:])))[:n]
    me = Path(__file__).resolve()
    base = snapshot(root) if todo else ""
    tasks = []
    for r in todo:
        wt = worktree_root(root) / r["signature"]
        if wt.exists():
            git(root, "worktree", "remove", "--force", str(wt), check=False)
            shutil.rmtree(wt, ignore_errors=True)
        wt.parent.mkdir(parents=True, exist_ok=True)
        git(root, "worktree", "add", "-q", "--detach", str(wt), base)
        check = next((c for c in cfg["checks"] if c.get("id") == r["check"]), {})
        t = {"ticket": r["id"], "kind": r["kind"], "check": r["check"], "command": check.get("command", ""),
             "exit": r["exit"], "files": r["files"], "trace": r["trace"], "signature": r["signature"],
             "count": r["count"], "worktree": str(wt), "base": base, "env": {"CLAUDE_LEARNER": "1"},
             "run": f"cd {wt} && CLAUDE_LEARNER=1 claude -p \"<draft prompt from /learn>\"",
             "prove": f"python3 -I {me} prove --project {root} --ticket {r['id']} --worktree {wt} --base {base}"}
        if r["kind"] == "correction" and r["trace"].startswith(SIGNALS + ":"):
            n_line = int(r["trace"].rsplit(":", 1)[1])
            lines = (root / SIGNALS).read_text(encoding="utf-8").splitlines()
            if 0 < n_line <= len(lines):
                t["user_words"] = json.loads(lines[n_line - 1]).get("excerpt", "")
        tasks.append(t)
    print(json.dumps({"tasks": tasks, "token_budget": int(cfg.get("token_budget", 200000)),
                      "forbidden": cfg["forbidden"], "allowed": ["skills", "AGENTS.md/INSTRUCTION.md learned entries",
                      "hooks-policy additions", "setup modules", "memory", "new eval cases", "project code"]}, indent=1))
    return 0


# ---------------------------------------------------------------- proof
PROJECT_ROOT: Path | None = None  # set by main(): its .venv goes first on PATH, as in stop_checks.py


def run_check(cmd: str, cwd: Path, timeout: int):
    env = dict(os.environ, CLAUDE_LEARNER="1")
    for cand in (".venv", "venv"):
        bindir = (PROJECT_ROOT or cwd) / cand / "bin"
        if bindir.is_dir():
            env["VIRTUAL_ENV"] = str(bindir.parent)
            env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
            break
    try:
        p = subprocess.run(cmd, shell=True, cwd=str(cwd), env=env, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr)
    except subprocess.TimeoutExpired:
        return 124, "timeout"


BOOKKEEPING = (FAILURES, INBOX, PATCHES)  # learner state, never part of a candidate


def start_commit(wt: Path) -> str:
    """The commit a worktree was created at: the oldest entry of its own HEAD reflog."""
    lines = git(wt, "reflog", "--format=%H", check=False).split()
    return lines[-1] if lines else ""


def changes(wt: Path, base: str) -> tuple:
    git(wt, "add", "-A")
    spec = ["--", "."] + [f":(exclude){p}" for p in BOOKKEEPING]
    status = [l.split("\t") for l in git(wt, "diff", "--cached", "--name-status", "--no-renames", base, *spec).splitlines()
              if l]
    patch = git(wt, "diff", "--cached", "--binary", base, *spec)
    return [(s[0], s[-1]) for s in status], patch


def norm(s: str) -> str:
    return " ".join(s.split())


def evidence_text(root: Path, row: dict, files) -> str:
    out = []
    for f in files or []:
        out.append(Path(f).read_text(encoding="utf-8", errors="replace"))
    tr = Path(row.get("trace") or "")
    if row.get("kind") == "check" and tr.is_file():
        try:
            ev = events_claude(tr) if tr.suffix == ".jsonl" else events_otel(tr)
            out += [e[3] for e in ev if e[0] == "run"]
        except (ValueError, OSError):
            pass
    return norm("\n".join(out))


def policy_weakened(before, after) -> bool:
    """True if anything present before is missing or changed after, or 'disable' grew."""
    if isinstance(before, dict):
        if not isinstance(after, dict):
            return True
        if len(after.get("disable", []) or []) > len(before.get("disable", []) or []):
            return True
        return any(k not in after or policy_weakened(v, after[k]) for k, v in before.items())
    if isinstance(before, list):
        return not isinstance(after, list) or any(x not in after for x in before)
    return before != after


def gate(root: Path, wt: Path, base: str, cfg: dict, row: dict, evidence_files) -> tuple:
    changed, patch = changes(wt, base)
    if not changed:
        return "no changes in the worktree", changed, patch
    for st, path in changed:
        if match(path, cfg["forbidden"]):
            return f"forbidden target: {path}", changed, patch
        if match(path, cfg["test_globs"]):
            if st == "D":
                return f"deletes a test file: {path}", changed, patch
            removed = [l for l in git(wt, "diff", "--cached", "-U0", base, "--", path).splitlines()
                       if l.startswith("-") and not l.startswith("---")]
            if any(ASSERTION.search(l) for l in removed):
                return f"weakens a test (removes an assertion): {path}", changed, patch
        if path.endswith("hooks-policy.json") and st == "M":
            try:
                old = json.loads(git(root, "show", f"{base}:{path}"))
                new = json.loads((wt / path).read_text(encoding="utf-8"))
            except (ValueError, RuntimeError, OSError):
                return f"unreadable policy change: {path}", changed, patch
            if policy_weakened(old, new):
                return f"weakens the hooks policy (only additions allowed, never 'disable'): {path}", changed, patch
    ev = evidence_text(root, row, evidence_files)
    if ev:
        added = [norm(l[1:]) for l in patch.splitlines() if l.startswith("+") and not l.startswith("+++")]
        for line in added:
            for i in range(0, max(len(line) - VERBATIM + 1, 0)):
                if line[i:i + VERBATIM] in ev:
                    return "copies tool output or fetched text verbatim; write the rule from the failing check", changed, patch
    return "", changed, patch


def prove(root: Path, tid: str, wt: Path, gate_only=False, evidence=None, eval_result=None, base=None) -> dict:
    cfg = config(root)
    row = find_ticket(root, tid)
    base = base or start_commit(wt) or snapshot(root)
    v = {"ticket": tid, "signature": row["signature"], "qualified": False, "refused": False, "stale": False,
         "reason": "", "before": None, "after": None, "regression": {}, "preexisting": [], "facets": [],
         "needs_eval": False, "changed": []}
    reason, changed, patch = gate(root, wt, base, cfg, row, evidence)
    v["changed"] = [p for _, p in changed]
    if reason:
        v.update(refused=bool(changed), reason=reason)
        return v
    if gate_only:
        v["reason"] = "passed the refusal gate"
        return v
    timeout = int(cfg.get("check_timeout", 900))
    checks = {c["id"]: c for c in cfg["checks"] if c.get("id") and c.get("command")}
    tmp = Path(tempfile.mkdtemp(prefix="learner-base-"))
    git(root, "worktree", "add", "-q", "--detach", str(tmp / "b"), base)
    basewt = tmp / "b"
    try:
        target = checks.get(row["check"]) if row["kind"] == "check" else None
        if row["kind"] == "check" and not target:
            v["reason"] = f"check {row['check']} is not declared in {CONFIG}"
            return v
        if target:
            v["before"], out = run_check(target["command"], basewt, timeout)
            if v["before"] == 0:
                v.update(stale=True, reason=f"check {row['check']} is already green at {base[:8]}")
                set_status(root, lambda r: r["id"] == tid, "stale")
                return v
            v["after"], out = run_check(target["command"], wt, timeout)
            if v["after"] != 0:
                v["reason"] = f"check {row['check']} still red (exit {v['after']})"
                return v
        for cid, c in checks.items():
            if target and cid == target["id"]:
                continue
            code, _ = run_check(c["command"], wt, timeout)
            v["regression"][cid] = code
            if code != 0:
                if run_check(c["command"], basewt, timeout)[0] != 0:
                    v["preexisting"].append(cid)
                else:
                    v["reason"] = f"check {cid} turned red (exit {code})"
                    return v
        for prefix, cmds in (cfg.get("facets") or {}).items():
            if any(p == prefix or p.startswith(prefix.rstrip("/") + "/") or fnmatch.fnmatch(p, prefix)
                   for p in v["changed"]):
                for cmd in cmds:
                    code, _ = run_check(cmd, wt, timeout)
                    v["facets"].append({"command": cmd, "exit": code})
                    if code != 0:
                        v["reason"] = f"facet check failed: {cmd} (exit {code})"
                        return v
        v["needs_eval"] = any(match(p, cfg["eval_globs"]) for p in v["changed"])
        if v["needs_eval"] and eval_result == "fail":
            v["reason"] = "the target facet's eval cases failed"
            return v
        v["qualified"] = True
        v["reason"] = "qualified" + ("; run the target facet's eval cases (Sonnet, medium) before recording"
                                     if v["needs_eval"] and eval_result != "pass" else "")
        return v
    finally:
        git(root, "worktree", "remove", "--force", str(basewt), check=False)
        shutil.rmtree(tmp, ignore_errors=True)


def cmd_prove(a) -> int:
    root = a.project.resolve()
    try:
        v = prove(root, a.ticket, Path(a.worktree).resolve(), a.gate_only, a.evidence, a.eval, a.base)
    except (KeyError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(json.dumps({"qualified": False, "refused": False, "reason": str(exc)}))
        return 1
    print(json.dumps(v, indent=1))
    if a.gate_only:
        return 1 if v["refused"] or v["reason"].startswith("no changes") else 0
    return 0 if v["qualified"] else 1


# ---------------------------------------------------------------- queue integration
HEAD_RE = re.compile(r"^### (C\d+) · (\w+)(?: (\d{4}-\d{2}-\d{2}))? · (.*)$", re.M)


def candidates(root: Path) -> list:
    f = root / MEM / "candidates.md"
    text = f.read_text(encoding="utf-8") if f.is_file() else ""
    heads = list(HEAD_RE.finditer(text))
    out = []
    for i, m in enumerate(heads):
        body = text[m.end(): heads[i + 1].start() if i + 1 < len(heads) else len(text)]
        fields = dict(re.findall(r"^- (\w+): (.+)$", body, re.M))
        out.append({"id": m.group(1), "status": m.group(2), "date": m.group(3) or "", "fields": fields,
                    "end": m.end() + len(body)})
    return out


def find_memory_py(root: Path, configured: str = "") -> Path | None:
    rel = Path("memory-architect") / "scripts" / "memory.py"
    if configured and not Path(configured).is_absolute():
        configured = str(root / configured)  # vendored copies are recorded repo-relative
    cands = [configured, Path(__file__).resolve().parent / "memory.py", Path(__file__).resolve().parents[2] / rel, root / "skills" / rel, root / ".claude" / "skills" / rel,
             Path.home() / ".claude" / "skills" / rel]
    cands += sorted((Path.home() / ".claude" / "plugins").glob(f"**/{rel.as_posix()}"))
    return next((Path(c) for c in cands if c and Path(c).is_file()), None)


def memory_py(root: Path, cfg: dict) -> Path:
    p = find_memory_py(root, cfg.get("memory_py") or "")
    if not p:
        raise RuntimeError("memory.py not found; set memory_py in claude/learner.json (memory-architect's scripts/memory.py)")
    return p


def cmd_record(a) -> int:
    root = a.project.resolve()
    wt = Path(a.worktree).resolve()
    cfg = config(root)
    try:
        base = a.base or start_commit(wt) or snapshot(root)
        v = prove(root, a.ticket, wt, False, a.evidence, a.eval, base)
        mp = memory_py(root, cfg)
    except (KeyError, RuntimeError) as exc:
        return die(str(exc))
    if not v["qualified"]:
        print(json.dumps(v, indent=1))
        return die(f"not recorded: {v['reason']}")
    if v["needs_eval"] and a.eval != "pass":
        return die("not recorded: skill or instruction text changed; run its eval cases, then pass --eval pass")
    nxt = f"C{max((int(c['id'][1:]) for c in candidates(root)), default=0) + 1}"
    patch_rel = f"{PATCHES}/{nxt}.patch"
    _, patch = changes(wt, base)
    regress = ", ".join(f"{k} green" for k, c in v["regression"].items() if c == 0) or "no other checks"
    pre = f"; pre-existing red: {', '.join(v['preexisting'])}" if v["preexisting"] else ""
    exits = f"{v['before']} -> {v['after']}" if v["before"] is not None else "n/a (correction)"
    evidence = (f"learner: {a.ticket} {v['signature']}; {find_ticket(root, a.ticket)['check']} exit {exits}; "
                f"regression: {regress}{pre}; patch {patch_rel}")
    r = subprocess.run([sys.executable, "-I", str(mp), "candidate", "add", "--project", str(root), "--target", a.target,
                        "--rule", a.rule, "--why", a.why, "--evidence", evidence, "--scope", a.scope],
                       capture_output=True, text=True)
    if r.returncode or f"added {nxt}" not in r.stdout:
        return die(f"memory.py candidate add failed: {r.stderr or r.stdout}")
    (root / PATCHES).mkdir(parents=True, exist_ok=True)
    (root / patch_rel).write_text(patch, encoding="utf-8")

    def add_cand(row):
        return row["id"] == a.ticket

    text, rows, off = read_table(root)
    for row in rows:
        if add_cand(row):
            prev = row["status"][len("candidate "):].split(",") if row["status"].startswith("candidate ") else []
            row["status"] = "candidate " + ",".join(prev + [nxt])
    write_table(root, text, rows, off)
    print(f"recorded {nxt} (pending) for {a.ticket}; patch {patch_rel}. Tell the user in one line; apply nothing "
          "until they approve it in their own words.")
    return 0


def reprove_pending(root: Path) -> list:
    """Re-prove pending learner candidates against HEAD plus the working-tree changes. Returns report lines."""
    out = []
    pend = [c for c in candidates(root) if c["status"] == "pending" and c["fields"].get("evidence", "").startswith("learner:")
            and "stale" not in c["fields"]]
    if not pend:
        return out
    tmp = Path(tempfile.mkdtemp(prefix="learner-reprove-"))
    try:
        base = snapshot(root)  # HEAD plus the working tree, so it includes the patch just applied
        for c in pend:
            m = re.match(r"learner: (F\d+) (\w+);.*patch (\S+)$", c["fields"]["evidence"])
            reason = ""
            if not m or not (root / m.group(3)).is_file():
                reason = "patch file missing"
            else:
                wt = tmp / c["id"]
                git(root, "worktree", "add", "-q", "--detach", str(wt), base)
                ap = subprocess.run(["git", "-C", str(wt), "apply", "--binary", str(root / m.group(3))],
                                    capture_output=True, text=True)
                if ap.returncode:
                    reason = "patch no longer applies"
                else:
                    try:
                        v = prove(root, m.group(1), wt, base=base)
                        reason = "" if v["qualified"] else v["reason"]
                    except (KeyError, RuntimeError) as exc:
                        reason = str(exc)
                git(root, "worktree", "remove", "--force", str(wt), check=False)
            if reason:
                mark_stale(root, c["id"], reason)
                out.append(f"{c['id']}: stale ({reason}); recommend rejecting it")
            else:
                out.append(f"{c['id']}: still qualifies")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return out


def mark_stale(root: Path, cid: str, reason: str) -> None:
    f = root / MEM / "candidates.md"
    text = f.read_text(encoding="utf-8")
    c = next(x for x in candidates(root) if x["id"] == cid)
    insert = f"- stale: {today()} {reason}\n"
    body_end = c["end"]
    prefix = text[:body_end].rstrip("\n") + "\n"
    f.write_text(prefix + insert + text[body_end:].lstrip("\n") if text[body_end:].strip() else prefix + insert,
                 encoding="utf-8")


def cmd_apply(a) -> int:
    root = a.project.resolve()
    c = next((x for x in candidates(root) if x["id"] == a.candidate), None)
    if not c:
        return die(f"no candidate {a.candidate}")
    if c["status"] != "approved":
        return die(f"{a.candidate} is {c['status']}; only an approved candidate is applied "
                   "(memory.py candidate resolve with the user's own words)")
    m = re.search(r"patch (\S+)$", c["fields"].get("evidence", ""))
    if not m or not (root / m.group(1)).is_file():
        return die(f"{a.candidate} has no learner patch; apply it by hand")
    r = subprocess.run(["git", "-C", str(root), "apply", "--binary", str(root / m.group(1))], capture_output=True, text=True)
    if r.returncode:
        return die(f"patch does not apply cleanly: {r.stderr.strip()}")
    set_status(root, lambda row: row["status"].startswith("candidate ") and a.candidate in row["status"][10:].split(","),
               "resolved")
    print(f"applied {a.candidate} ({m.group(1)}) to the working tree; review and commit on the user's word.")
    for line in reprove_pending(root):
        print(line)
    return 0


def cmd_report(a) -> int:
    root = a.project.resolve()
    learner = sorted((c for c in candidates(root) if c["fields"].get("evidence", "").startswith("learner:")),
                     key=lambda c: int(c["id"][1:]))
    window = learner[-20:]
    approved = [c for c in window if c["status"] == "approved"]
    decided = [c for c in window if c["status"] in ("approved", "rejected")]
    acc = len(approved) / len(decided) if decided else None
    rows = {r["signature"]: r for r in read_table(root)[1]}
    recurred = []
    for c in approved:
        m = re.match(r"learner: F\d+ (\w+)", c["fields"]["evidence"])
        r = rows.get(m.group(1)) if m else None
        if r and c["date"] and r["last"] > c["date"]:
            recurred.append(c["id"])
    rec_rate = len(recurred) / len(approved) if approved else None
    kill_acc = len(window) >= 20 and acc is not None and acc < 0.30
    kill_rec = len(approved) >= 5 and rec_rate is not None and rec_rate >= 0.5
    rep = {"window": len(window), "decided": len(decided), "approved": len(approved), "acceptance": acc,
           "recurred": recurred, "recurrence": rec_rate, "kill": kill_acc or kill_rec,
           "why": [w for w, k in (("acceptance under 30% over 20 candidates", kill_acc),
                                  ("half or more of approved signatures recurred", kill_rec)) if k]}
    if a.json:
        print(json.dumps(rep, indent=1))
    else:
        pct = "n/a" if acc is None else f"{acc:.0%}"
        print(f"learner candidates: {len(window)} in window, {len(decided)} decided, acceptance {pct}; "
              f"recurred after approval: {', '.join(recurred) or 'none'}")
        print("KILL: " + "; ".join(rep["why"]) if rep["kill"] else "kill criteria: not met")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sp = ap.add_subparsers(dest="cmd", required=True)

    def sub(name, fn):
        p = sp.add_parser(name)
        p.add_argument("--project", type=Path, default=Path("."))
        p.set_defaults(fn=fn)
        return p

    p = sub("init", cmd_init)
    p.add_argument("--memory-py", default="")
    p = sub("install", cmd_install)
    p.add_argument("--check-id", default="stop")
    p.add_argument("--memory-py", default="")
    p = sub("ticket", cmd_ticket)
    p.add_argument("--check", required=True)
    p.add_argument("--exit", type=int)
    p.add_argument("--failing", nargs="*")
    p.add_argument("--files", nargs="*")
    p.add_argument("--trace")
    p.add_argument("--from-trace")
    p.add_argument("--format", choices=["claude", "otel"], default="claude")
    p.add_argument("--command", help="check command when the check isn't declared in claude/learner.json")
    p.add_argument("--dry-run", action="store_true")
    sub("ingest", cmd_ingest)
    p = sub("tickets", cmd_tickets)
    p.add_argument("--json", action="store_true")
    sub("status", cmd_status)
    p = sub("plan", cmd_plan)
    p.add_argument("--n", type=int)
    for name, fn in (("prove", cmd_prove), ("record", cmd_record)):
        p = sub(name, fn)
        p.add_argument("--ticket", required=True)
        p.add_argument("--worktree", required=True)
        p.add_argument("--evidence", nargs="*", default=[])
        p.add_argument("--eval", choices=["pass", "fail"])
        p.add_argument("--base", help="commit the worktree started from (plan prints it); default: read from the worktree's reflog")
        if name == "prove":
            p.add_argument("--gate-only", action="store_true")
        else:
            for k in ("target", "rule", "why", "scope"):
                p.add_argument(f"--{k}", required=True)
    p = sub("apply", cmd_apply)
    p.add_argument("candidate")
    p = sub("report", cmd_report)
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    global PROJECT_ROOT
    PROJECT_ROOT = a.project.resolve()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
