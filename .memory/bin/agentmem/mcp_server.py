"""Dependency-free MCP server (newline-delimited JSON-RPC 2.0 over stdio) exposing the frozen memory verbs.
stdout carries only protocol messages; diagnostics go to stderr."""
import json, os, subprocess, sys
from . import api, write as W, codegraph

SERVER_INFO = {"name": "agent-memory", "version": "2.0.0"}
DEFAULT_PROTOCOL = "2025-06-18"

FROZEN_TOOLS = ("memory_wake", "memory_recall", "memory_remember", "memory_forget", "memory_expand", "memory_note",
                "memory_capture", "memory_extract", "memory_apply", "memory_compact", "memory_reflect",
                "memory_skills", "memory_code_hint", "memory_doctor")

_S, _I, _N, _B = {"type": "string"}, {"type": "integer"}, {"type": "number"}, {"type": "boolean"}
_SA = {"type": "array", "items": {"type": "string"}}


def _schema(props=None, req=()):
    d = {"type": "object", "properties": props or {}}
    if req:
        d["required"] = list(req)
    return d


def _d(desc, base):
    return dict(base, description=desc)


TOOL_DEFS = {
    "memory_wake": ("Layered always-loaded memory core (identity, trusted rules, last handoff, skills, status). Call at session start.", _schema()),
    "memory_recall": ("Hybrid search over memory and knowledge base; returns a budgeted block. Call before acting on conventions, people, or earlier decisions.",
                      _schema({"query": _S, "k": _I, "kind": _d("comma-separated kinds", _S),
                               "min_trust": {"type": "string", "enum": ["external", "agent", "observed", "operator", "user"]},
                               "as_of": _d("ISO timestamp for time travel", _S), "layers": _d("default 'memory,kb'", _S),
                               "budget": _d("token budget", _I), "explain": _B}, ["query"])),
    "memory_remember": ("Store one memory through the write gate. Tier is stamped by the harness; MCP callers can never claim operator tier.",
                        _schema({"text": _S, "kind": {"type": "string", "enum": ["fact", "rule", "pref", "entity", "artifact"]},
                                 "key": _S, "quote": _d("user's exact words, verified against the captured log", _S),
                                 "entities": _SA, "importance": _N, "ttl_days": _N, "source_ids": _SA, "pinned": _B}, ["text"])),
    "memory_forget": ("Retract a memory (tombstone); purge=true also removes content from the log.",
                      _schema({"id": _S, "reason": _S, "purge": _B}, ["id", "reason"])),
    "memory_expand": ("Original raw events / sources behind a memory or summary.", _schema({"id": _S}, ["id"])),
    "memory_note": ("Session scratchpad: with text appends a note, without text shows it.", _schema({"text": _S, "session": _S})),
    "memory_capture": ("Record one raw event (user/assistant/tool message) in the capture log.",
                       _schema({"role": _S, "text": _S, "session": _S}, ["role", "text"])),
    "memory_extract": ("Extraction packet for the host agent to answer (then call memory_apply); rules=true runs the no-LLM fallback.",
                       _schema({"rules": _B})),
    "memory_apply": ("Apply extracted JSON ({candidates, relations, episode, until}) through the dedup gate and reconcile.",
                     _schema({"payload": {"type": "object"}}, ["payload"])),
    "memory_compact": ("Handoff-summary packet over recent raw events.", _schema({"session": _S, "last": _I})),
    "memory_reflect": ("Consolidation packet for old episodes.", _schema()),
    "memory_skills": ("Procedural memory: packet | list | propose | approve | reject | rollback. "
                      "ASK THE USER FIRST before approve; approve is refused unless arguments include confirmed_by_user=true "
                      "after the user has explicitly said yes.",
                      _schema({"action": {"type": "string", "enum": ["packet", "list", "propose", "approve", "reject", "rollback"]},
                               "name": _S, "evidence": _SA, "description": _S, "body": _S, "reason": _S,
                               "confirmed_by_user": _d("set true only after the user explicitly approved", _B)}, ["action"])),
    "memory_code_hint": ("Related files (or callers of a symbol) for a coding task via Graft/trail-code, if installed.",
                         _schema({"query": _S, "k": _I, "callers": _d("treat query as a symbol", _B)}, ["query"])),
    "memory_doctor": ("Memory health check.", _schema()),
}
assert tuple(TOOL_DEFS) == FROZEN_TOOLS


def _tools_list():
    return [{"name": n, "description": TOOL_DEFS[n][0], "inputSchema": TOOL_DEFS[n][1]} for n in FROZEN_TOOLS]


def _csv(v):
    if isinstance(v, str):
        return [x for x in v.split(",") if x]
    return list(v or [])


def _call(store, name, a):
    store.need()
    g = a.get
    if name == "memory_wake":
        return api.wake(store)
    if name == "memory_recall":
        return api.recall(store, a["query"], k=int(g("k") or 8), kind=g("kind"), min_trust=g("min_trust"), as_of=g("as_of"),
                          layers=g("layers") or "memory,kb", budget=g("budget"), explain=bool(g("explain")))
    if name == "memory_remember":
        return W.remember(store, a["text"], kind=g("kind") or "fact", key=g("key"), quote=g("quote"),
                          entities=_csv(g("entities")), importance=g("importance") if g("importance") is not None else 0.5,
                          ttl_days=g("ttl_days"), source_ids=_csv(g("source_ids")), pinned=bool(g("pinned")), actor="agent")
    if name == "memory_forget":
        return W.forget(store, a["id"], a["reason"], purge=bool(g("purge")))
    if name == "memory_expand":
        return W.expand(store, a["id"])
    if name == "memory_note":
        return api.note(store, g("text"), g("session"))
    if name == "memory_capture":
        r = store.capture(a["role"], a["text"], session=g("session"))
        return r["id"] if r else "empty"
    if name == "memory_extract":
        return api.extract(store, rules=bool(g("rules")), auto=False)
    if name == "memory_apply":
        if not isinstance(g("payload"), dict):
            raise ValueError("payload must be an object")
        return W.apply(store, a["payload"], actor="agent")
    if name == "memory_compact":
        return W.compact_packet(store, g("session"), int(g("last") or 120)) or "nothing to compact"
    if name == "memory_reflect":
        return W.reflect_packet(store) or "nothing to consolidate yet"
    if name == "memory_skills":
        act, nm = a["action"], g("name")
        if act == "packet":
            return W.skills_packet(store) or "not enough successful cases yet"
        if act == "list":
            return {"approved": W.list_skills(store, "approved"), "proposed": W.list_skills(store, "proposed")}
        if act not in ("propose", "approve", "reject", "rollback"):
            raise ValueError(f"unknown action {act}")
        if act == "approve" and g("confirmed_by_user") is not True:
            raise PermissionError("approve refused: ask the user first, then retry with confirmed_by_user=true")
        if not nm:
            raise ValueError("skill name required")
        if act == "propose":
            return W.skill_propose(store, nm, g("body") or "", _csv(g("evidence")), g("description") or "", allow_auto=False)
        if act == "approve":
            return W.skill_approve(store, nm, by="user")
        if act == "reject":
            return W.skill_reject(store, nm, g("reason") or "")
        return W.skill_rollback(store, nm)
    if name == "memory_code_hint":
        return codegraph.callers(store, a["query"]) if g("callers") else codegraph.hint(store, a["query"], k=int(g("k") or 8))
    if name == "memory_doctor":
        return api.doctor(store)
    raise KeyError(name)


def _text(x):
    return x if isinstance(x, str) else json.dumps(x, indent=1, default=str)


def _tool_result(store, params):
    name, args = params.get("name"), params.get("arguments") or {}
    try:
        if name not in FROZEN_TOOLS:
            raise ValueError(f"unknown tool {name}")
        if not isinstance(args, dict):
            raise ValueError("arguments must be an object")
        missing = [r for r in TOOL_DEFS[name][1].get("required", []) if r not in args]
        if missing:
            raise ValueError("missing required: " + ", ".join(missing))
        return {"content": [{"type": "text", "text": _text(_call(store, name, args))}], "isError": False}
    except (Exception, SystemExit) as e:
        msg = e.code if isinstance(e, SystemExit) and e.code is not None else str(e) or type(e).__name__
        return {"content": [{"type": "text", "text": f"{type(e).__name__}: {msg}"}], "isError": True}


def _handle(store, msg):
    """Return a response dict, or None for notifications."""
    if not isinstance(msg, dict):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}}
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    notif = "id" not in msg
    if notif:
        return None
    ok = lambda r: {"jsonrpc": "2.0", "id": mid, "result": r}
    if method == "initialize":
        return ok({"protocolVersion": (params.get("protocolVersion") if isinstance(params, dict) else None) or DEFAULT_PROTOCOL,
                   "capabilities": {"tools": {}}, "serverInfo": SERVER_INFO})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": _tools_list()})
    if method == "tools/call":
        return ok(_tool_result(store, params if isinstance(params, dict) else {}))
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}


def serve(store):
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            try:
                resp = _handle(store, msg)
            except Exception as e:  # never die on one bad message
                print(f"agent-memory mcp: {e!r}", file=sys.stderr)
                resp = {"jsonrpc": "2.0", "id": msg.get("id") if isinstance(msg, dict) else None,
                        "error": {"code": -32603, "message": str(e)}}
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


def conformance(mem_py_path, root):
    """Launch `mem.py --root ROOT mcp` and exercise the protocol. Returns True or raises AssertionError."""
    p = subprocess.Popen([sys.executable or "python3", mem_py_path, "--root", root, "mcp"], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, encoding="utf-8")
    n = [0]

    def send(method, params=None, notify=False):
        m = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            m["params"] = params
        if not notify:
            n[0] += 1
            m["id"] = n[0]
        p.stdin.write(json.dumps(m) + "\n")
        p.stdin.flush()
        if notify:
            return None
        line = p.stdout.readline()
        assert line, "server closed stdout: " + p.stderr.read()
        r = json.loads(line)
        assert r.get("id") == m["id"], f"id mismatch: {r}"
        return r

    try:
        r = send("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "conf", "version": "0"}})
        assert r["result"]["protocolVersion"] == "2025-03-26" and r["result"]["serverInfo"]["name"] == "agent-memory", r
        send("notifications/initialized", notify=True)
        assert send("ping")["result"] == {}
        names = tuple(t["name"] for t in send("tools/list")["result"]["tools"])
        assert names == FROZEN_TOOLS, names
        text = "conformance probe: the deploy bucket is zebra-quokka-7731"
        r = send("tools/call", {"name": "memory_remember", "arguments": {"text": text, "key": "conformance.probe"}})["result"]
        assert not r["isError"], r
        r = send("tools/call", {"name": "memory_recall", "arguments": {"query": "zebra-quokka-7731 deploy bucket"}})["result"]
        assert not r["isError"] and "zebra-quokka-7731" in r["content"][0]["text"], r
        r = send("tools/call", {"name": "memory_skills", "arguments": {"action": "approve", "name": "x"}})["result"]
        assert r["isError"], r
        r = send("no/such/method")
        assert r.get("error", {}).get("code") == -32601, r
        return True
    finally:
        try:
            p.stdin.close()
            p.wait(timeout=5)
        except Exception:
            p.kill()
