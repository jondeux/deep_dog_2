"""CLI over the frozen verb set. `mem <verb> -h` for details."""
import argparse, json, os, sys
from .core import Store
from . import api, write as W, retrieval as R, codegraph, okf


def _out(x):
    if isinstance(x, str):
        print(x)
    else:
        print(json.dumps(x, indent=1, ensure_ascii=False, default=str))


def _payload(a):
    raw = open(a.file, encoding="utf-8").read() if a.file else sys.stdin.read()
    return json.loads(raw)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="mem", description="Portable three-layer agent memory (see SKILL.md).")
    ap.add_argument("--root", default=None, help="the store (default: MEM_ROOT, else the .memory that holds this script or sits "
                    "above the working directory; `init` uses ./.memory)")
    sp = ap.add_subparsers(dest="verb", required=True)
    p = sp.add_parser("init", help="create .memory/, vendor the scripts, optionally wire AGENTS.md and hooks")
    p.add_argument("--wire", action="store_true"); p.add_argument("--hooks", choices=["claude"])
    p.add_argument("--dry-run", action="store_true", help="show the diffs init would make, write nothing")
    p = sp.add_parser("wake", help="layered always-loaded core (writes MEMORY.md)"); p.add_argument("--hook", action="store_true")
    p = sp.add_parser("recall", help="hybrid search + budgeted injection block")
    p.add_argument("query"); p.add_argument("--k", type=int, default=8); p.add_argument("--kind")
    p.add_argument("--min-trust", choices=["external", "agent", "observed", "operator", "user"]); p.add_argument("--as-of")
    p.add_argument("--layers", default="memory,kb"); p.add_argument("--budget", type=int); p.add_argument("--explain", action="store_true")
    p.add_argument("--json", action="store_true")
    p = sp.add_parser("remember", help="store one memory through the write gate (tier is stamped by the harness)")
    p.add_argument("text"); p.add_argument("--kind", default="fact", choices=["fact", "rule", "pref", "entity", "artifact"])
    p.add_argument("--key"); p.add_argument("--quote", help="user's exact words; verified against the captured log")
    p.add_argument("--entities", help="comma-separated"); p.add_argument("--importance", type=float, default=0.5)
    p.add_argument("--ttl-days", type=float); p.add_argument("--source-ids"); p.add_argument("--derived-from")
    p.add_argument("--pin", action="store_true"); p.add_argument("--operator", action="store_true",
                   help="human curator writing (only honoured from an interactive terminal)")
    p = sp.add_parser("forget", help="retract (tombstone); --purge removes content from the log"); p.add_argument("id")
    p.add_argument("--reason", required=True); p.add_argument("--purge", action="store_true")
    p = sp.add_parser("verify", help="record a verification signal (OKF verified[{by,at}])"); p.add_argument("id")
    p.add_argument("--by", default="human:user")
    p = sp.add_parser("pin", help="pin/unpin a memory into the wake-up core"); p.add_argument("id"); p.add_argument("--off", action="store_true")
    p = sp.add_parser("expand", help="original raw events / sources behind a memory or summary"); p.add_argument("id")
    p = sp.add_parser("note", help="session scratchpad; no text = show"); p.add_argument("text", nargs="?"); p.add_argument("--session")
    p = sp.add_parser("capture", help="record raw events (F2)")
    p.add_argument("--hook", action="store_true", help="read a Claude Code hook payload from stdin")
    p.add_argument("--transcript"); p.add_argument("--role", default="user"); p.add_argument("--text"); p.add_argument("--session")
    p = sp.add_parser("extract", help="extraction packet for the host agent; --rules = no-LLM fallback; --auto = API model")
    p.add_argument("--rules", action="store_true"); p.add_argument("--auto", action="store_true")
    p.add_argument("--quiet", action="store_true", help="run, but print nothing (used by the SessionEnd hook, whose stdout must be one JSON value)")
    p = sp.add_parser("apply", help="apply extracted JSON (file or stdin) through dedup gate + reconcile"); p.add_argument("--file")
    p = sp.add_parser("compact", help="handoff-summary packet over raw events (expandable)"); p.add_argument("--session"); p.add_argument("--last", type=int, default=120)
    sp.add_parser("reflect", help="consolidation packet for old episodes")
    p = sp.add_parser("skills", help="procedural memory: packet | list | propose | approve | reject | rollback")
    p.add_argument("action", choices=["packet", "list", "propose", "approve", "reject", "rollback"]); p.add_argument("name", nargs="?")
    p.add_argument("--evidence"); p.add_argument("--description", default=""); p.add_argument("--file"); p.add_argument("--body")
    p.add_argument("--reason", default=""); p.add_argument("--by", default="user")
    p = sp.add_parser("ingest", help="index project docs/code into the knowledge base"); p.add_argument("paths", nargs="*")
    p = sp.add_parser("code_hint", help="related files for a coding task via Graft/trail-code (optional)")
    p.add_argument("query"); p.add_argument("--k", type=int, default=8); p.add_argument("--callers", action="store_true", help="treat query as a symbol")
    sp.add_parser("doctor", help="health check")
    p = sp.add_parser("eval", help="golden-set retrieval eval"); p.add_argument("golden"); p.add_argument("--k", type=int, default=8)
    p = sp.add_parser("export", help="export current memories"); p.add_argument("--okf", required=True, help="output dir (OKF v0.2 bundle)")
    p.add_argument("--history", action="store_true")
    sp.add_parser("reindex", help="rebuild index.db from the JSONL source of truth")
    sp.add_parser("mcp", help="run the MCP server over stdio")
    sp.add_parser("selftest", help="end-to-end tests in a temp dir")
    a = ap.parse_args(argv)
    init_root = a.root or os.environ.get("MEM_ROOT", ".memory")  # init creates a store where it is told to; the other verbs look for one
    st = Store(init_root if a.verb == "init" else a.root)
    v = a.verb
    if v == "init":
        if a.dry_run:
            return _out(api.init_preview(init_root, wire=a.wire, hooks=a.hooks))
        return _out(api.init(st, wire=a.wire, hooks=a.hooks))
    if v == "selftest":
        from . import selftest
        return selftest.run()
    if v == "mcp":
        from . import mcp_server
        return mcp_server.serve(st)
    st.need()
    if v == "wake":
        txt = api.wake(st, hook=a.hook)
        if a.hook:  # SessionStart: also pick up rule-based extraction from the previous session
            W.rule_extract(st)
            txt = api.wake(st)
        return _out(txt)
    if v == "recall":
        return _out(api.recall(st, a.query, k=a.k, kind=a.kind, min_trust=a.min_trust, as_of=a.as_of, layers=a.layers,
                               budget=a.budget, explain=a.explain, raw=a.json))
    if v == "remember":
        actor = "operator" if a.operator and sys.stdin.isatty() and sys.stdout.isatty() else "agent"
        return _out(W.remember(st, a.text, kind=a.kind, key=a.key, quote=a.quote,
                               entities=[e for e in (a.entities or "").split(",") if e], importance=a.importance,
                               ttl_days=a.ttl_days, source_ids=[s for s in (a.source_ids or "").split(",") if s],
                               pinned=a.pin, actor=actor, derived_from=[s for s in (a.derived_from or "").split(",") if s]))
    if v == "forget":
        return _out(W.forget(st, a.id, a.reason, purge=a.purge))
    if v == "verify":
        if not st.get(a.id):
            raise SystemExit(f"unknown id {a.id}")
        return _out(st.emit({"op": "verify", "id": a.id, "by": a.by}))
    if v == "pin":
        return _out(st.emit({"op": "pin", "id": a.id, "value": 0 if a.off else 1}))
    if v == "expand":
        return _out(W.expand(st, a.id))
    if v == "note":
        return _out(api.note(st, a.text, a.session))
    if v == "capture":
        if a.hook:
            try:
                payload = json.loads(sys.stdin.read() or "{}")
            except json.JSONDecodeError:
                payload = {}
            return _out({"captured": W.capture_hook(st, payload)})
        if a.transcript:
            return _out({"captured": W.capture_transcript(st, a.transcript, a.session)})
        if not a.text:
            raise SystemExit("--text, --transcript or --hook required")
        r = st.capture(a.role, a.text, session=a.session)
        return _out(r["id"] if r else "empty")
    if v == "extract":
        res = api.extract(st, rules=a.rules, auto=a.auto)
        return None if a.quiet else _out(res)
    if v == "apply":
        return _out(W.apply(st, _payload(a)))
    if v == "compact":
        return _out(W.compact_packet(st, a.session, a.last) or "nothing to compact")
    if v == "reflect":
        return _out(W.reflect_packet(st) or "nothing to consolidate yet")
    if v == "skills":
        if a.action == "packet":
            return _out(W.skills_packet(st) or "not enough successful cases yet")
        if a.action == "list":
            return _out({"approved": W.list_skills(st, "approved"), "proposed": W.list_skills(st, "proposed")})
        if not a.name:
            raise SystemExit("skill name required")
        if a.action == "propose":
            body = open(a.file, encoding="utf-8").read() if a.file else (a.body or "")
            return _out(W.skill_propose(st, a.name, body, (a.evidence or "").split(","), a.description))
        if a.action == "approve":
            return _out(W.skill_approve(st, a.name, by=a.by))
        if a.action == "reject":
            return _out(W.skill_reject(st, a.name, a.reason))
        return _out(W.skill_rollback(st, a.name))
    if v == "ingest":
        return _out(R.ingest(st, a.paths))
    if v == "code_hint":
        return _out(codegraph.callers(st, a.query) if a.callers else codegraph.hint(st, a.query, k=a.k))
    if v == "doctor":
        return _out(api.doctor(st))
    if v == "eval":
        return _out(api.evaluate(st, a.golden, k=a.k))
    if v == "export":
        return _out(okf.export(st, a.okf, include_history=a.history))
    if v == "reindex":
        st.reindex()
        return _out("reindexed from log/ (run `mem ingest` to rebuild the knowledge base)")


if __name__ == "__main__":
    main()
