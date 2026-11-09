"""Code knowledge graph (conditional module) delegated to Graft / Trail-code (trailhq/Graft, npm @trailhq/trail-code).
We do not build our own graph. Rules: telemetry always off (DO_NOT_TRACK=1); never run `trail init` (it edits the
global Claude config and installs hooks); fail open when the tool is absent.
Setup (agent-run, optional):  DO_NOT_TRACK=1 npm install -g @trailhq/trail-code && trail build .   (Node ^20.16 or >=22.3)"""
import json, os, shutil, subprocess

INSTALL = "DO_NOT_TRACK=1 npm install -g @trailhq/trail-code && trail build ."


def _bin():
    return shutil.which("trail") or shutil.which("graft")


def _run(args, timeout=120, parse=True):
    exe = _bin()
    if not exe:
        return None, f"code graph unavailable: install with `{INSTALL}` (optional)"
    env = {**os.environ, "DO_NOT_TRACK": "1"}
    try:
        r = subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout, env=env)
    except Exception as e:
        return None, f"trail failed: {e}"
    if r.returncode:
        return None, f"trail exited {r.returncode}: {(r.stderr or r.stdout)[:300]}"
    if not parse:
        return r.stdout, None
    try:
        return json.loads(r.stdout), None
    except json.JSONDecodeError:
        return None, f"trail returned non-JSON output: {r.stdout[:200]}"


def status():
    exe = _bin()
    return f"trail-code at {exe}" if exe else "trail-code not installed (optional; coding projects only)"


def hint(store, query, k=8, repo=".", refresh=False):
    """Task -> ranked files/symbols (Graft `ask`). Returns text lines for injection."""
    args = ["ask", query, repo, "--json", "-n", str(k)] + ([] if refresh else ["--no-refresh"])
    data, err = _run(args)
    if err:
        return err
    hits = data.get("hits", []) if isinstance(data, dict) else []
    lines = ["[Code graph hints (trail-code)]"]
    for h in hits[:k]:
        snippet = " ".join(str(h.get("snippet", "")).split())[:160]
        lines.append(f"- {h.get('pointer', '?')} {h.get('kind', '')} {h.get('title', '')} score={h.get('score', '')} {snippet}")
    if hits:
        return "\n".join(lines)
    note = data.get("note", "") if isinstance(data, dict) else ""
    return f"no code-graph hits (trail-code{': ' + note if note else ''})"


def callers(store, symbol, repo=".", depth=2):
    """Symbol -> impacted callers (Graft `callers`)."""
    data, err = _run(["callers", symbol, repo, "-d", str(depth), "--json"])
    return err if err else json.dumps(data, indent=1)[:4000]


def build(repo="."):
    data, err = _run(["build", repo], timeout=1800, parse=False)
    return err or "trail graph built"
