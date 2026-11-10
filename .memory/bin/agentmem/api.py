"""Frozen verb set (A2) shared by the CLI and the MCP server. Every verb takes a Store and returns JSON-able data
or text. Keep signatures stable: the MCP conformance test pins them."""
import json, os, re, shutil, sys
from pathlib import Path
from .core import TIERS, Store, est_tokens, now
from . import retrieval as R, write as W

VERBS = ("wake", "recall", "remember", "forget", "expand", "note", "capture", "extract", "apply", "compact",
         "reflect", "skills", "ingest", "code_hint", "doctor", "eval")
WIRE_START, WIRE_END = "<!-- agent-memory:start -->", "<!-- agent-memory:end -->"
WIRE_CEILING = 200  # instruction files longer than this get the compact block


STAMP = ".claude/workspace-setup-suite.json"  # which suite version each vendored asset came from (workspace-init detect reads it)


def suite_version(script):
    """Version of the suite plugin this script runs from, or None (a vendored copy, or not from the suite)."""
    for d in list(Path(script).resolve().parents)[:6]:
        for f, ok in ((d / ".claude-plugin" / "plugin.json", lambda j: j.get("name") in ("workspace-setup-suite", "agent-memory")),
                      (d / "suite.json", lambda j: (j.get("marketplace") or {}).get("name") == "jondeux-setup")):
            try:
                j = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(j, dict) and ok(j) and isinstance(j.get("version"), str):
                return j["version"]
    return None


def stamp_version(project, key, script):
    """Record in STAMP that `key` was installed into `project` by this suite version. Skipped when the script is itself a
    copy inside the project (a vendored re-run knows nothing newer)."""
    project, v = Path(project).resolve(), suite_version(script)
    if not v or project in Path(script).resolve().parents:
        return None
    f = project / STAMP
    try:
        data = json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}
    except ValueError:
        data = {}
    vend = data.setdefault("vendored", {}) if isinstance(data, dict) else {}
    if vend.get(key) != v:
        vend[key] = v
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"vendored": dict(sorted(vend.items()))}, indent=2) + "\n", encoding="utf-8")
    return v


def _mem_cmd(store):
    return "python3 " + os.path.relpath(os.path.join(store.root, "bin", "mem.py"))


def _hook_cmd(store):
    """Hook command anchored at the project dir, so it works when a hook runs outside the repo root."""
    return 'python3 "${CLAUDE_PROJECT_DIR}"/' + os.path.relpath(os.path.join(store.root, "bin", "mem.py"))


# ---------------- setup (A2: agent-driven) ----------------
def init(store, wire=False, hooks=None):
    store.init_dirs()
    here = os.path.dirname(os.path.abspath(__file__))
    bin_dir = store.p("bin")
    if os.path.abspath(os.path.join(here, "..")) != os.path.abspath(bin_dir):  # vendor: project is self-contained
        if os.path.isdir(os.path.join(bin_dir, "agentmem")):
            shutil.rmtree(os.path.join(bin_dir, "agentmem"))
        os.makedirs(bin_dir, exist_ok=True)
        shutil.copytree(here, os.path.join(bin_dir, "agentmem"), ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copyfile(os.path.join(here, "..", "mem.py"), os.path.join(bin_dir, "mem.py"))
    done = [f"memory at {os.path.relpath(store.root)}"]
    gi = ".gitignore"
    if os.path.isdir(".git") or os.path.exists(gi):
        cur = open(gi).read() if os.path.exists(gi) else ""
        add = [l for l in (".memory/index.db*", ".memory/scratch/", ".memory/log/raw-*.jsonl", ".memory/log/state.json", ".memory/**/*.lock", ".memory/bin/**/__pycache__/") if l not in cur]
        if add:
            open(gi, "a").write(("\n" if cur and not cur.endswith("\n") else "") + "\n".join(add) + "\n")
            done.append("gitignore updated (raw transcripts and index stay local)")
    if wire:
        done += _wire_agents_md(store)
    if hooks == "claude":
        done += _install_claude_hooks(store)
        v = stamp_version(os.getcwd(), "agent-memory", os.path.abspath(__file__))
        if v:
            done.append(f"recorded suite version {v} in {STAMP}")
    wake(store)
    return done


def init_preview(root, wire=False, hooks=None):
    """D18: what `init` would change, as unified diffs, without touching the project. It runs init on a copy of
    only the files init edits, in a temp dir, so nothing else is read or written."""
    import difflib, tempfile
    rel_root = os.path.relpath(os.path.abspath(root))
    if rel_root.startswith(".."):
        raise SystemExit("--dry-run needs the memory root inside the current directory")
    touched = ("AGENTS.md", "CLAUDE.md", ".gitignore", os.path.join(".claude", "settings.json"))
    cwd = os.getcwd()
    before = {f: _read(os.path.join(cwd, f)) if os.path.exists(os.path.join(cwd, f)) else None for f in touched}
    with tempfile.TemporaryDirectory() as tmp:
        for f, text in before.items():
            if text is not None:
                os.makedirs(os.path.dirname(os.path.join(tmp, f)) or tmp, exist_ok=True)
                open(os.path.join(tmp, f), "w", encoding="utf-8").write(text)
        if os.path.isdir(os.path.join(cwd, ".git")):
            os.makedirs(os.path.join(tmp, ".git"))
        os.chdir(tmp)
        try:
            done = init(Store(rel_root), wire=wire, hooks=hooks)
            after = {f: _read(f) if os.path.exists(f) else None for f in touched}
        finally:
            os.chdir(cwd)
    out = ["dry run: nothing was written. init would:"] + [f"  - {d}" for d in done]
    for f in touched:
        if before[f] != after[f]:
            out.append("".join(difflib.unified_diff((before[f] or "").splitlines(True), (after[f] or "").splitlines(True),
                                                    f"a/{f}" if before[f] is not None else "/dev/null", f"b/{f}")))
    return "\n".join(out)


def _read(path):
    return open(path, encoding="utf-8").read() if os.path.exists(path) else ""


def _imports(text, name):
    """Paths of `@...name` import lines (Claude Code CLAUDE.md syntax) in text."""
    return re.findall(r"^[ \t]*@(\S*?" + re.escape(name) + r")[ \t]*$", text, flags=re.M)


def _wire_agents_md(store):
    cmd = _mem_cmd(store)
    agents, claude = _read("AGENTS.md"), _read("CLAUDE.md")
    # A memory-architect index (committed project state + approval queue) owns those jobs; link it, don't duplicate.
    index = next((p for p in _imports(claude, "MEMORY.md") + _imports(agents, "MEMORY.md")
                  if not p.lstrip("./").startswith(".memory/")), None)
    link = (f"\n- Committed project state, decisions and proposed rule changes live in `{index}` (memory-architect). "
            f"Don't copy them into `.memory/`; propose rule changes through its candidate queue." if index else "")
    full = f"""{WIRE_START}
## Memory (agent-memory)
Persistent memory lives in `.memory/` (protocol: the agent-memory SKILL.md). Commands: `{cmd} <verb>`.
- Session start: read `.memory/MEMORY.md` (auto-injected if hooks are on). Resolve listed CONFLICTS / pending extraction first.
- Before acting on conventions, people, projects or earlier decisions: `{cmd} recall "<query>"`.
- Coding tasks: `{cmd} code_hint "<task>"` for related files (Graft, if installed).
- Capture is automatic with hooks; otherwise `{cmd} capture --role user --text "..."` for key user messages.
- After meaningful work: `{cmd} extract` -> answer the packet with JSON -> `{cmd} apply --file x.json`.
- Never hand-edit `.memory/log/`; never approve skills without asking the user; never store secrets.{link}
{WIRE_END}"""
    short_link = f" Project state and rule proposals: `{index}` (memory-architect)." if index else ""
    compact = (f"{WIRE_START}\n## Memory (agent-memory)\n"
               f"Persistent memory lives in `.memory/` (protocol: the agent-memory SKILL.md; commands: `{cmd} <verb>`). Hooks load and "
               f"capture it; run `{cmd} recall \"<query>\"` before acting on earlier decisions, and `{cmd} extract` then `apply` after "
               f"meaningful work.\nNever hand-edit `.memory/log/`, approve skills without asking the user, or store secrets.{short_link}\n"
               f"{WIRE_END}")
    targets = [f for f in ("AGENTS.md", "CLAUDE.md") if os.path.exists(f)] or ["AGENTS.md"]
    if "AGENTS.md" in targets and "CLAUDE.md" in targets and _imports(claude, "AGENTS.md"):
        targets.remove("CLAUDE.md")  # CLAUDE.md is a shim that imports AGENTS.md: one block, loaded once
        if WIRE_START in claude:  # drop a block an earlier --wire left in the shim
            stale = re.sub(r"\n*" + re.escape(WIRE_START) + r".*?" + re.escape(WIRE_END) + r"\n?", "\n", claude, flags=re.S)
            open("CLAUDE.md", "w", encoding="utf-8").write(stale.lstrip("\n") if stale.strip() else "")
    for t in targets:
        txt = open(t, encoding="utf-8").read() if os.path.exists(t) else ""
        base = re.sub(re.escape(WIRE_START) + r".*?" + re.escape(WIRE_END) + r"\n?", "", txt, flags=re.S)
        block = compact if len(base.splitlines()) + len(full.splitlines()) > WIRE_CEILING else full  # D19: stay small in a long file
        if WIRE_START in txt:
            txt = re.sub(re.escape(WIRE_START) + r".*?" + re.escape(WIRE_END), lambda _: block, txt, flags=re.S)
        else:
            txt = (txt.rstrip() + "\n\n" if txt.strip() else "") + block + "\n"
        open(t, "w", encoding="utf-8").write(txt)
    return [f"wired {', '.join(targets)}" + (f" (linked {index})" if index else "")]


def _install_claude_hooks(store):
    old, cmd = _mem_cmd(store), _hook_cmd(store)
    want = {"SessionStart": f"{cmd} wake --hook",
            "Stop": f"{cmd} capture --hook",
            "PreCompact": f"{cmd} capture --hook",
            "SessionEnd": f"{cmd} capture --hook && {cmd} extract --rules --quiet"}  # one JSON value on stdout (D17)
    legacy = {"SessionEnd": f"{cmd} capture --hook && {cmd} extract --rules"}  # printed two objects
    path = os.path.join(".claude", "settings.json")
    os.makedirs(".claude", exist_ok=True)
    cfg = json.load(open(path)) if os.path.exists(path) else {}
    hooks = cfg.setdefault("hooks", {})
    for ev, c in want.items():
        entries = hooks.setdefault(ev, [])
        prevs = {c.replace(cmd, old)} | ({legacy[ev], legacy[ev].replace(cmd, old)} if ev in legacy else set())
        for e in entries:  # re-running init upgrades older relative or two-object entries in place
            for h in e.get("hooks", []):
                if h.get("command") in prevs:
                    h["command"] = c
        if not any(h.get("command") == c for e in entries for h in e.get("hooks", [])):
            entries.append({"hooks": [{"type": "command", "command": c}]})
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")  # keep the final newline other tools expect
    os.replace(tmp, path)
    return [f"Claude Code hooks installed in {path} (SessionStart, Stop, PreCompact, SessionEnd)"]


# ---------------- F8: layered wake-up core ----------------
def _winners(store, kinds):
    best = {}
    for m in store.current():
        if m["kind"] in kinds and store.scope_ok(m):
            k = m.get("key") or m["id"]
            if k not in best or (m["tier_rank"], m["valid_from"]) > (best[k]["tier_rank"], best[k]["valid_from"]):
                best[k] = m
    return list(best.values())


def conflicts(store):
    by = {}
    for m in store.current():
        if m.get("key") and store.scope_ok(m):
            by.setdefault(m["key"], []).append(m)
    return {k: [x["id"] for x in v] for k, v in by.items() if len({x["text"].strip() for x in v}) > 1}


def pending_raw(store):
    return store.db.execute("SELECT COUNT(*) FROM raw WHERE ts > ?", (store.state().get("extracted_until", ""),)).fetchone()[0]


def wake(store, hook=False):
    """L0 identity/pinned, L1 trusted rules+prefs, L2 latest handoff + skills index, then status. Budgeted."""
    store.need()
    budget = store.config["core_budget_tokens"]
    out, used = ["# MEMORY (generated by `mem wake`; do not edit)"], 0

    def add(line):
        nonlocal used
        t = est_tokens(line)
        if used + t > budget:
            return False
        out.append(line); used += t
        return True

    pinned = [m for m in store.current("pinned=1") if store.scope_ok(m)]
    if pinned:
        add("## Pinned")
        for m in sorted(pinned, key=lambda m: -m["tier_rank"]):
            add(f"- {m['text']}  ({m['id']})")
    rules = [m for m in _winners(store, ("rule", "pref")) if m["tier_rank"] >= TIERS["observed"] and not m.get("pinned")]
    rules.sort(key=lambda m: (-m["tier_rank"], -(m.get("importance") or 0.5)))
    add("## Rules and preferences (trusted, highest tier first)")
    for m in rules:
        if not add(f"- [{m['tier']}] {m['text']}  (key={m['key']})"):
            add(f"- (+{len(rules)} more: `mem recall` with kind rule/pref)")
            break
    summ = sorted([m for m in store.current("kind='summary'") if store.scope_ok(m)], key=lambda m: m["valid_from"])
    if summ:
        add("## Last handoff")
        add(f"- {summ[-1]['text'][:600]}  (expand: `mem expand {summ[-1]['id']}`)")
    sk = W.list_skills(store, "approved")
    if sk:
        add("## Skills (open the file only when the task matches)")
        for n, d in sk:
            add(f"- {n}: {d} -> .memory/skills/{n}.md")
    status, local = [], []  # `local` depends on this container's uncommitted raw logs, so it never goes into the file on disk
    pr = pending_raw(store)
    if pr:
        local.append(f"{pr} captured events not yet extracted -> run `mem extract` and answer it (takes a minute)")
    cf = conflicts(store)
    if cf:
        status.append(f"CONFLICTS (ask the user): {', '.join(list(cf)[:5])}")
    props = W.list_skills(store, "proposed")
    if props:
        status.append(f"skill proposals awaiting user approval: {', '.join(n for n, _ in props)}")
    if W.skills_packet(store):
        local.append("enough successful cases for a new skill -> `mem skills packet`")

    def render(lines):
        body = list(out)
        if lines:
            body.append("## Status")
            body += [f"- {x}" for x in lines]
        body.append("- Everything else: `mem recall \"<query>\"` just before acting (budgeted, hybrid search).")
        return "\n".join(body) + "\n"
    text = render(status + local)
    on_disk = render(status)
    path = store.p("MEMORY.md")
    try:
        same = open(path, encoding="utf-8").read() == on_disk
    except OSError:
        same = False
    if not same:  # MEMORY.md is committed in many repos: it holds only what the committed logs say, so a fresh clone and the
        open(path, "w", encoding="utf-8").write(on_disk)  # container that committed it agree (deep_dog_2 audit F1/F7)
    return text


# ---------------- verbs ----------------
def recall(store, query, k=8, kind=None, min_trust=None, as_of=None, layers="memory,kb", budget=None, explain=False, raw=False):
    hits = R.search(store, query, k=k, kinds=kind.split(",") if kind else None, layers=tuple(layers.split(",")),
                    min_trust=min_trust, as_of=as_of, explain=explain)
    if raw:
        return hits
    return R.render(hits, budget or store.config["inject_budget_tokens"], explain=explain) if hits else "no matches"


def note(store, text=None, session=None):
    s = session or store.scope["session"] or "current"
    path = store.p("scratch", re.sub(r"[^A-Za-z0-9_.-]", "_", s) + ".md")
    if text:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"- {now()[11:16]} {text.strip()}\n")
        return f"noted -> {os.path.relpath(path)}"
    return open(path, encoding="utf-8").read() if os.path.exists(path) else "(empty)"


def extract(store, rules=False, auto=False):
    if rules:
        stats = {}
        d = W.rule_extract(store, stats)
        skipped = stats.get("events_skipped", 0)
        if skipped:
            print(f"extract --rules: skipped {skipped} of {stats.get('events_seen', 0)} user event(s) with no imperative "
                  f"rule; declarative facts need the LLM packet (`mem extract`)", file=sys.stderr)
        return {"rule_based": [{k: x.get(k) for k in ("action", "id", "tier", "key", "candidate")} for x in d],
                "events_skipped": skipped, "events_seen": stats.get("events_seen", 0),
                "note": "--rules keeps imperative rules only; use `mem extract` (LLM packet) for facts and decisions"}
    pk = W.extract_packet(store)
    if not pk:
        return "nothing to extract"
    if auto:
        payload = W.llm_complete(store, pk)
        if payload is None:
            return "no llm configured (config.llm.url); answer the packet yourself:\n\n" + pk
        return W.apply(store, payload, actor="llm")
    return pk


def doctor(store):
    store.need()
    issues = []
    if not os.path.exists(store.p("index.db")) or store.db.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0 and \
            os.path.getsize(store.p("log", "memories.jsonl")) > 0:
        issues.append("index empty but log has memories -> run `mem reindex`")
    pr = pending_raw(store)
    if pr:
        issues.append(f"{pr} raw events pending extraction")
    bad = []  # trusted memories that came from harness text (hook feedback), stored before capture skipped it
    for m in store.db.execute("SELECT id, key, text, quote, source_ids FROM memories WHERE tier='user' AND forgotten=0 AND valid_to IS NULL"):
        srcs = [x for x in (m["source_ids"] or "").replace("[", "").replace("]", "").replace('"', "").split(",") if x.strip()]
        raws = [r["text"] for r in store.db.execute(f"SELECT text FROM raw WHERE id IN ({','.join('?' * len(srcs))})", srcs)] if srcs else []
        if W.is_harness_text(m["text"]) or W.is_harness_text(m["quote"]) or any(W.is_harness_text(t) for t in raws):
            bad.append(m["id"])
    if bad:
        issues.append(f"{len(bad)} user-tier memories came from hook feedback, not the user: "
                      + "; ".join(f"`mem forget {i} --reason harness-text`" for i in bad[:5]) + (" ..." if len(bad) > 5 else ""))
    for k, ids in conflicts(store).items():
        issues.append(f"conflict on key '{k}': {ids}")
    stale = 0
    import hashlib
    for r in store.db.execute("SELECT path, sha FROM kb GROUP BY path"):
        if os.path.exists(r["path"]):
            if hashlib.sha1(open(r["path"], encoding="utf-8", errors="ignore").read().encode()).hexdigest()[:12] != r["sha"]:
                stale += 1
        else:
            stale += 1
    if stale:
        issues.append(f"{stale} kb file(s) changed or missing since ingest -> `mem ingest`")
    mm = store.p("MEMORY.md")
    if os.path.exists(mm) and est_tokens(open(mm).read()) > store.config["core_budget_tokens"] * 1.2:
        issues.append("MEMORY.md over budget")
    props = W.list_skills(store, "proposed")
    if props:
        issues.append(f"{len(props)} skill proposal(s) awaiting the user")
    emb = R.Embedder(store.config.get("embed"))
    info = {"embeddings": "on" if emb.on else "off (keyword-only; set config.embed for hybrid vectors)",
            "memories": store.db.execute("SELECT COUNT(*) FROM memories WHERE forgotten=0 AND valid_to IS NULL").fetchone()[0],
            "raw_events": store.db.execute("SELECT COUNT(*) FROM raw").fetchone()[0],
            "kb_chunks": store.db.execute("SELECT COUNT(*) FROM kb").fetchone()[0]}
    from . import codegraph
    info["code_graph"] = codegraph.status()
    return {"issues": issues or ["ok"], "info": info}


def evaluate(store, golden_path, k=8):
    """F11: golden set JSONL {"q": "...", "expect": ["substring or id", ...]} -> recall@k, MRR, precision@k."""
    rows = [json.loads(l) for l in open(golden_path, encoding="utf-8") if l.strip()]
    rec, mrr, prec, fails = 0.0, 0.0, 0.0, []
    for r in rows:
        hits = R.search(store, r["q"], k=k)
        exp = r["expect"]
        found = [e for e in exp if any(e == h["id"] or e.lower() in h["text"].lower() for h in hits)]
        rec += len(found) / len(exp)
        rel = [i for i, h in enumerate(hits) if any(e == h["id"] or e.lower() in h["text"].lower() for e in exp)]
        mrr += 1 / (rel[0] + 1) if rel else 0
        prec += len(rel) / max(1, len(hits))
        if len(found) < len(exp):
            fails.append({"q": r["q"], "missing": [e for e in exp if e not in found]})
    n = max(1, len(rows))
    return {"questions": len(rows), f"recall@{k}": round(rec / n, 3), "mrr": round(mrr / n, 3),
            f"precision@{k}": round(prec / n, 3), "failures": fails[:10]}
