#!/usr/bin/env python3
"""Repo-committed project memory: discover, init, lint, and approval-gated candidates.

Native auto memory (~/.claude/projects/<project>/memory/) is machine-local, so cloud
containers lose it. This keeps the durable layer in the repo instead: a MEMORY.md index
(loaded at session start by a CLAUDE.md @import) plus topic files read on demand.

Usage:
  memory.py discover  --project DIR [--json]
  memory.py init      --project DIR [--dir claude/memory] [--state PATH] [--via import|hook] [--link PATH ...] [--dry-run]
  memory.py lint      --project DIR [--dir claude/memory] [--json]
  memory.py candidate add     --project DIR --target T --rule R --why W --evidence E --scope S
  memory.py candidate list    --project DIR [--json]
  memory.py candidate resolve --project DIR (--approve IDS | --reject IDS) --quote "<user's own words>"

IDS is "all" or a comma list such as "1,3" (C prefix optional). resolve refuses unless
--quote is the user's instruction naming those ids ("approve 1,3", "go ahead with C1 and C3",
"reject 2", "leave out C2").
Exit codes: 0 ok, 1 lint errors or refused action, 2 usage error.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

sys.dont_write_bytecode = True  # keep __pycache__ out of the user's repo

ASSETS = Path(__file__).resolve().parent.parent / "assets" / "memory"
TOPICS = ("MEMORY.md", "state.md", "goal-progress.md", "decisions.md", "candidates.md")
MAX_LINES, MAX_BYTES = 200, 25_000          # what auto memory loads; we hold the index to the same budget
WARN_LINES, WARN_BYTES = 160, 20_000
STATE_STALE_DAYS, ENTRY_STALE_DAYS = 30, 180
HOOK_CLIP = 1500                            # hooks-architect session_context.py per-file clip
FIELDS = ("rule", "why", "evidence", "scope")

SECRETS = [
    (r"\bghp_[A-Za-z0-9]{36}\b|\bgithub_pat_[A-Za-z0-9_]{22,}", "GitHub token"),
    (r"\bsk-ant-[A-Za-z0-9_-]{20,}", "Anthropic key"),
    (r"\bsk-[A-Za-z0-9]{32,}\b", "API key"),
    (r"\bAKIA[0-9A-Z]{16}\b", "AWS access key"),
    (r"\bxox[abprs]-[A-Za-z0-9-]{10,}", "Slack token"),
    (r"\bhf_[A-Za-z0-9]{30,}\b", "Hugging Face token"),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "private key"),
    (r"(?i)\b(password|passwd|secret|token|api[_-]?key)\s*[:=]\s*['\"]?[A-Za-z0-9/+_.-]{12,}", "credential assignment"),
]
LINK = re.compile(r"\]\(([^)\s#]+)(?:#[^)]*)?\)")
HEAD = re.compile(r"^### (C\d+) · (pending|approved|rejected)\b(.*)$", re.M)
APPROVE_WORDS = r"approve|go ahead with|go ahead on|go with|apply|accept|yes to"
REJECT_WORDS = r"reject|leave out|drop|skip|decline"
NEG = re.compile(r"(?i)\b(don'?t|do not|not|never|no)\b[^.,;]{0,20}\b(approve|reject|go ahead|apply|accept|drop)")


def today() -> str:
    return dt.date.today().isoformat()


def die(msg: str, code: int = 1) -> int:
    print(f"memory.py: {msg}", file=sys.stderr)
    return code


def secret_hits(text: str) -> list:
    return [name for pat, name in SECRETS if re.search(pat, text)]


def strip_code(text: str) -> str:
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    return re.sub(r"`[^`\n]*`", "", text)


def imports(text: str) -> set:
    return set(re.findall(r"(?:^|\s)@([^\s`]+)", strip_code(text)))


def rel_dir(a: argparse.Namespace) -> str:
    return a.dir.strip("/").replace("\\", "/")


# ---------------------------------------------------------------- discover
def auto_memory_dir(root: Path) -> Path:
    slug = re.sub(r"[^A-Za-z0-9-]", "-", str(root.resolve()))
    return Path.home() / ".claude" / "projects" / slug / "memory"


def discover(root: Path, d: str) -> dict:
    found = []

    def add(kind, rel, note=""):
        found.append({"kind": kind, "path": rel, "note": note})

    for rel, kind in (("AGENTS.md", "agents-md"), ("CLAUDE.md", "claude-md"), (".claude/CLAUDE.md", "claude-md"),
                      ("CLAUDE.local.md", "claude-local-md"), (".claude/rules", "rules"),
                      (".trail/learnings", "trail-learnings"), ("claude/project-state.md", "project-state"),
                      (".claude/agent-memory", "agent-memory"), (".memory/MEMORY.md", "agent-memory-store"),
                      (f"{d}/MEMORY.md", "memory-index")):
        if (root / rel).exists():
            note = ""
            if (root / rel).is_dir():
                note = f"{sum(1 for _ in (root / rel).rglob('*.md'))} markdown files"
            add(kind, rel, note)
    settings = root / ".claude" / "settings.json"
    if settings.is_file():
        try:
            s = json.loads(settings.read_text(encoding="utf-8"))
            for k in ("autoMemoryEnabled", "autoMemoryDirectory"):
                if k in s:
                    add("auto-memory-setting", ".claude/settings.json", f"{k}={s[k]!r}")
        except ValueError:
            add("settings-invalid", ".claude/settings.json", "not valid JSON")
    am = auto_memory_dir(root)
    if (am / "MEMORY.md").is_file():
        n = len((am / "MEMORY.md").read_text(encoding="utf-8", errors="replace").splitlines())
        add("auto-memory-local", str(am), f"{n} index lines on this machine only; lost with the container")
    return {"sources": found, "index_loaded": loaded_via(root, d) is not None}


def loaded_via(root: Path, d: str):
    target = f"{d}/MEMORY.md"
    for rel in ("CLAUDE.md", ".claude/CLAUDE.md"):
        f = root / rel
        if f.is_file() and target in imports(f.read_text(encoding="utf-8", errors="replace")):
            return "import"
    pol = root / ".claude" / "hooks-policy.json"
    if pol.is_file():
        try:
            if target in json.loads(pol.read_text(encoding="utf-8")).get("context", {}).get("files", []):
                return "hook"
        except ValueError:
            pass
    return None


# ---------------------------------------------------------------- init
def links_for(root: Path, d: str, extra: list, skip: str = "") -> list:
    depth = "../" * len(Path(d).parts)
    out = []
    for rel, label in ((".trail/learnings", "trail learnings: one file per saved learning"),
                       ("claude/project-state.md", "older project-state snapshot"),
                       (".claude/agent-memory", "subagent memory (memory: project)"),
                       (".memory", "agent-memory store: captured sessions and recall (`mem recall`)")):
        if rel == ".memory" and not (root / rel / "MEMORY.md").is_file():
            continue
        if (root / rel).exists() and rel != skip:
            out.append(f"- [{rel}]({depth}{rel}{'/' if (root / rel).is_dir() else ''}): {label}")
    for rel in extra:
        rel = rel.strip("/")
        out.append(f"- [{rel}]({depth}{rel}{'/' if (root / rel).is_dir() else ''}): linked source")
    return out


def cmd_init(a) -> int:
    root, d = a.project.resolve(), rel_dir(a)
    if Path(d).parts and Path(d).parts[0] in (".claude", ".git"):
        return die(f"{d} is under a protected path (.claude/ or .git/): every memory write would prompt. "
                   "Use a directory such as claude/memory.")
    mem, plan = root / d, []
    state = (a.state or f"{d}/state.md").strip("/")
    for name in TOPICS:
        dest = root / state if name == "state.md" else mem / name
        if not dest.exists():
            text = (ASSETS / name).read_text(encoding="utf-8").replace("{{DATE}}", today())
            if name == "MEMORY.md":
                el = links_for(root, d, a.link or [], skip=state)
                text = text.replace("{{ELSEWHERE}}", "\n".join(el) if el else "- (none found)")
                text = text.replace("](state.md)", f"]({'../' * len(Path(d).parts)}{state})" if a.state else "](state.md)")
            plan.append(("write", dest, text))
    if a.via == "import":
        cm = root / "CLAUDE.md"
        line = f"@{d}/MEMORY.md"
        if cm.is_file():
            cur = cm.read_text(encoding="utf-8")
            if f"{d}/MEMORY.md" not in imports(cur):
                last = cur.rstrip("\n").splitlines()[-1].strip() if cur.strip() else ""
                gap = "\n\n" if last and not last.startswith("@") else "\n"  # not glued to a list or paragraph
                plan.append(("write", cm, cur.rstrip("\n") + gap + line + "\n"))
        else:
            head = "@AGENTS.md\n" if (root / "AGENTS.md").is_file() else ""
            plan.append(("write", cm, head + line + "\n"))
    else:
        pf = root / ".claude" / "hooks-policy.json"
        pol = json.loads(pf.read_text(encoding="utf-8")) if pf.is_file() else {}
        files = pol.setdefault("context", {}).setdefault("files", [])
        if f"{d}/MEMORY.md" not in files:
            files.insert(0, f"{d}/MEMORY.md")
            plan.append(("write", pf, json.dumps(pol, indent=2) + "\n"))
    for _, path, text in plan:
        rel = path.relative_to(root)
        if a.dry_run:
            print(f"would write {rel}")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"wrote {rel}")
    if not plan:
        print("nothing to do: memory already set up")
    if a.state:
        sf = root / state
        if sf.is_file() and not re.search(r"(?im)^(?:-\s*)?(?:\*\*)?(?:measured|taken):?(?:\*\*)?:?\s*\d{4}-\d{2}-\d{2}",
                                         sf.read_text(encoding="utf-8")):
            print(f"note: the adopted state file {state} has no 'measured: YYYY-MM-DD' line, so lint will warn "
                  "'stale'; add the line when you re-measure it")
    return 0


# ---------------------------------------------------------------- lint
def parse_candidates(text: str) -> list:
    out, heads = [], list(HEAD.finditer(text))
    for i, m in enumerate(heads):
        body = text[m.end(): heads[i + 1].start() if i + 1 < len(heads) else len(text)]
        fields = dict(re.findall(r"^- (\w+): (.+)$", body, re.M))
        tgt = re.search(r"target: ([^·]+)", m.group(3))
        out.append({"id": m.group(1), "status": m.group(2), "target": tgt.group(1).strip() if tgt else "",
                    "fields": fields, "start": m.start(), "end": m.end()})
    return out


def lint(root: Path, d: str) -> list:
    F = []

    def f(level, code, path, msg):
        F.append({"level": level, "code": code, "path": path, "message": msg})

    mem, idx = root / d, root / d / "MEMORY.md"
    if not idx.is_file():
        f("error", "index-missing", f"{d}/MEMORY.md", "No memory index. Run: memory.py init --project .")
        return F
    text = idx.read_text(encoding="utf-8", errors="replace")
    n, b = len(text.splitlines()), len(text.encode("utf-8"))
    if n > MAX_LINES or b > MAX_BYTES:
        f("error", "index-over-limit", f"{d}/MEMORY.md",
          f"{n} lines / {b} bytes; keep under {MAX_LINES} lines and {MAX_BYTES} bytes. Move detail into topic files.")
    elif n > WARN_LINES or b > WARN_BYTES:
        f("warning", "index-near-limit", f"{d}/MEMORY.md", f"{n} lines / {b} bytes; merge or drop stale entries.")
    for i, line in enumerate(text.splitlines(), 1):
        if len(line) > 300:
            f("warning", "long-entry", f"{d}/MEMORY.md:{i}", "Index entries should be one short line; move detail out.")
    linked = set()
    for ref in LINK.findall(strip_code(text)):
        if re.match(r"^[a-z]+:", ref):
            continue
        tgt = (mem / ref).resolve()
        linked.add(tgt)
        if not tgt.exists():
            f("error", "broken-link", f"{d}/MEMORY.md", f"Link target {ref} does not exist.")
    for p in sorted(mem.rglob("*.md")):
        if p.name != "MEMORY.md" and p.resolve() not in linked:
            f("warning", "orphan", str(p.relative_to(root)), "Not linked from MEMORY.md, so no session will find it.")
    for p in sorted(mem.rglob("*.md")):
        hits = secret_hits(p.read_text(encoding="utf-8", errors="replace"))
        if hits:
            f("error", "secret", str(p.relative_to(root)), f"Looks like a secret ({', '.join(hits)}). Remove it and rotate it.")
    sm = re.search(r"\[State\]\(([^)\s#]+)", text)
    st = (mem / sm.group(1)).resolve() if sm else mem / "state.md"
    srel = str(st.relative_to(root)) if st.is_relative_to(root) else str(st)
    if st.is_file():
        m = re.search(r"(?im)^(?:-\s*)?(?:\*\*)?(?:measured|taken):?(?:\*\*)?:?\s*(\d{4}-\d{2}-\d{2})",
                      st.read_text(encoding="utf-8"))
        if not m:
            f("warning", "stale", srel, "No 'measured: YYYY-MM-DD' line; state can't be dated.")
        elif (dt.date.today() - dt.date.fromisoformat(m.group(1))).days > STATE_STALE_DAYS:
            f("warning", "stale", srel, f"Measured {m.group(1)}; re-measure before relying on it.")
    for i, line in enumerate(text.splitlines(), 1):
        m = re.search(r"\(updated (\d{4}-\d{2}-\d{2})\)", line)
        if m and (dt.date.today() - dt.date.fromisoformat(m.group(1))).days > ENTRY_STALE_DAYS:
            f("warning", "stale", f"{d}/MEMORY.md:{i}", f"Entry last updated {m.group(1)}; confirm or drop it.")
    cf = mem / "candidates.md"
    if cf.is_file():
        for c in parse_candidates(cf.read_text(encoding="utf-8")):
            missing = [k for k in FIELDS if k not in c["fields"]]
            if missing:
                f("error", "candidate-malformed", f"{d}/candidates.md", f"{c['id']} lacks {', '.join(missing)}.")
            if c["status"] != "pending" and "approval" not in c["fields"]:
                f("error", "approval-without-quote", f"{d}/candidates.md",
                  f"{c['id']} is {c['status']} with no recorded user quote. Resolve only via 'candidate resolve'.")
    ff = mem / "failures.md"
    if ff.is_file():
        for i, line in enumerate(ff.read_text(encoding="utf-8").splitlines(), 1):
            if re.match(r"^\|\s*F\d+\s*\|", line):
                cells = [c.strip() for c in line.strip().strip("|").split("|")]
                if len(cells) != 11 or not re.fullmatch(r"open|resolved|stale|candidate C\d+(,C\d+)*", cells[-1]):
                    f("error", "failures-malformed", f"{d}/failures.md:{i}",
                      "Ticket rows need 11 cells and a status of open, resolved, stale or 'candidate C<n>'. "
                      "Let continuous-learning-architect's learn.py write this table.")
    via = loaded_via(root, d)
    if via is None:
        f("warning", "not-loaded", f"{d}/MEMORY.md",
          f"Nothing loads the index at session start. Add '@{d}/MEMORY.md' to CLAUDE.md (memory.py init does).")
    elif via == "hook" and len(text) > HOOK_CLIP:
        f("warning", "hook-clipped", f"{d}/MEMORY.md",
          f"Loaded by session_context.py, which clips files at {HOOK_CLIP} characters; use --via import.")
    ag = root / "AGENTS.md"
    if ag.is_file() and f"{d}/MEMORY.md" in imports(ag.read_text(encoding="utf-8", errors="replace")):
        f("warning", "import-in-agents-md", "AGENTS.md",
          "Other agents read '@' imports as literal text. Put the import in the CLAUDE.md shim instead.")
    return F


def cmd_lint(a) -> int:
    F = lint(a.project.resolve(), rel_dir(a))
    errors = sum(x["level"] == "error" for x in F)
    if a.json:
        print(json.dumps({"findings": F, "errors": errors}, indent=2))
    else:
        for x in F:
            print(f"{x['level']:7} {x['code']:24} {x['path']}: {x['message']}")
        print(f"{errors} error(s), {len(F) - errors} warning(s)")
    return 1 if errors else 0


# ---------------------------------------------------------------- candidates
def cand_file(a) -> Path:
    return a.project.resolve() / rel_dir(a) / "candidates.md"


def refresh_count(a, text: str) -> None:
    idx = a.project.resolve() / rel_dir(a) / "MEMORY.md"
    if idx.is_file():
        n = sum(c["status"] == "pending" for c in parse_candidates(text))
        s = idx.read_text(encoding="utf-8")
        idx.write_text(re.sub(r"Pending candidates: \d+", f"Pending candidates: {n}", s), encoding="utf-8")


def cmd_add(a) -> int:
    cf = cand_file(a)
    if not cf.is_file():
        return die("no candidates.md; run memory.py init first")
    vals = {"rule": a.rule, "why": a.why, "evidence": a.evidence, "scope": a.scope}
    missing = [k for k, v in vals.items() if not (v or "").strip()]
    if missing:
        return die(f"missing --{', --'.join(missing)}: every candidate needs rule, why, evidence, scope", 2)
    hits = secret_hits(" ".join([a.target, *vals.values()]))
    if hits:
        return die(f"refused: candidate text looks like it contains a secret ({', '.join(hits)})")
    text = cf.read_text(encoding="utf-8")
    nums = [int(c["id"][1:]) for c in parse_candidates(text)]
    cid = f"C{max(nums, default=0) + 1}"
    block = (f"\n### {cid} · pending · target: {a.target.strip()} · proposed {today()}\n"
             + "".join(f"- {k}: {' '.join(v.split())}\n" for k, v in vals.items()))
    text = text.rstrip("\n") + "\n" + block
    cf.write_text(text, encoding="utf-8")
    refresh_count(a, text)
    print(f"added {cid} (pending). Mention it to the user in one line; apply nothing until they approve.")
    return 0


def cmd_list(a) -> int:
    cf = cand_file(a)
    cs = parse_candidates(cf.read_text(encoding="utf-8")) if cf.is_file() else []
    pending = [{"id": c["id"], "target": c["target"], "rule": c["fields"].get("rule", "")}
               for c in cs if c["status"] == "pending"]
    if a.json:
        print(json.dumps({"pending": pending}, indent=2))
    else:
        for c in pending:
            print(f"{c['id']} → {c['target']}: {c['rule']}")
        print(f"{len(pending)} pending" + (' (reply "approve all" or "approve 1,3")' if pending else ""))
    return 0


def parse_ids(s: str) -> object:
    s = s.strip().lower()
    if s == "all":
        return "all"
    ids = [int(x) for x in re.findall(r"\d+", s)]
    return set(ids) if ids else None


def cmd_resolve(a) -> int:
    verb, ids_s = ("approve", a.approve) if a.approve else ("reject", a.reject)
    want = parse_ids(ids_s)
    if want is None:
        return die("IDS must be 'all' or numbers such as 1,3", 2)
    q = (a.quote or "").strip()
    verbs = APPROVE_WORDS if verb == "approve" else REJECT_WORDS
    m = re.search(rf"(?i)\b(?:{verbs})\s+(?:with\s+|to\s+)?(all|c?\d+(?:\s*(?:,|and|&)?\s*c?\d+)*)", q)
    if not m or NEG.search(q):
        return die(f"refused: --quote must be the user's own instruction naming the ids, e.g. \"{verb} 1,3\", "
                   f"\"go ahead with C1 and C3\", or \"{verb} all\"")
    said = parse_ids(m.group(1))
    if said != "all" and (want == "all" or not want <= said):
        return die(f"refused: the quote \"{q}\" doesn't cover {ids_s}")
    cf = cand_file(a)
    text = cf.read_text(encoding="utf-8")
    cs = [c for c in parse_candidates(text) if c["status"] == "pending"
          and (want == "all" or int(c["id"][1:]) in want)]
    if not cs:
        return die("no matching pending candidates")
    status = "approved" if verb == "approve" else "rejected"
    for c in reversed(cs):  # edit from the end so offsets stay valid
        head = text[c["start"]:c["end"]].replace("· pending ·", f"· {status} {today()} ·", 1)
        nxt = text.find("\n### ", c["end"])
        body_end = len(text) if nxt == -1 else nxt + 1
        body = text[c["end"]:body_end].rstrip("\n") + f'\n- approval: "{q}"\n' + ("\n" if nxt != -1 else "")
        text = text[:c["start"]] + head + body + text[body_end:]
    cf.write_text(text, encoding="utf-8")
    refresh_count(a, text)
    for c in cs:
        if status == "approved":
            print(f"{c['id']} approved → apply to {c['target']}: {c['fields'].get('rule', '')}")
        else:
            print(f"{c['id']} rejected")
    if status == "approved":
        print("Apply each approved rule to its target as one dated line, then re-run the target's linter.")
    return 0


# ---------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--project", type=Path, default=Path("."))
        p.add_argument("--dir", default="claude/memory", help="memory directory, relative to the project")
        return p

    p = common(sub.add_parser("discover"))
    p.add_argument("--json", action="store_true")
    p = common(sub.add_parser("init"))
    p.add_argument("--via", choices=("import", "hook"), default="import")
    p.add_argument("--state", help="adopt an existing state file (relative to the project) instead of <dir>/state.md")
    p.add_argument("--link", action="append", help="extra memory source to link from the index")
    p.add_argument("--dry-run", action="store_true")
    p = common(sub.add_parser("lint"))
    p.add_argument("--json", action="store_true")
    cand = sub.add_parser("candidate").add_subparsers(dest="ccmd", required=True)
    p = common(cand.add_parser("add"))
    for k in ("target", *FIELDS):
        p.add_argument(f"--{k}", default="")
    p = common(cand.add_parser("list"))
    p.add_argument("--json", action="store_true")
    p = common(cand.add_parser("resolve"))
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--approve")
    g.add_argument("--reject")
    p.add_argument("--quote", default="")
    a = ap.parse_args()

    if a.cmd == "discover":
        rep = discover(a.project.resolve(), rel_dir(a))
        if a.json:
            print(json.dumps(rep, indent=2))
        else:
            for s in rep["sources"]:
                print(f"{s['kind']:20} {s['path']}  {s['note']}")
            print(f"index loaded at session start: {'yes' if rep['index_loaded'] else 'no'}")
        return 0
    if a.cmd == "init":
        return cmd_init(a)
    if a.cmd == "lint":
        return cmd_lint(a)
    return {"add": cmd_add, "list": cmd_list, "resolve": cmd_resolve}[a.ccmd](a)


if __name__ == "__main__":
    sys.exit(main())
