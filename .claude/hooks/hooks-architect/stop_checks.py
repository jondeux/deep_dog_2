#!/usr/bin/env python3
"""Stop hook (opt-in): don't let the turn end on a red test suite.

Set "stop.command" in the policy (empty = disabled). When Claude tries to stop,
this runs the command in the project. On failure it blocks the stop and hands the
tail of the output back to Claude. Loop safety: at most "max_blocks" consecutive
blocks per session (Claude Code's own cap is 8), then it lets the turn end and
tells the user. The counter resets whenever a stop arrives that wasn't caused by
this hook (stop_hook_active is false).

Failure tickets (opt-in, "stop.tickets" = a path such as claude/memory/failures.inbox.jsonl):
when the check still fails after max_blocks AND the previous turn ended green, the give-up
branch appends one metadata line (check id, exit code, failing test ids, changed files,
transcript path, date; no output text) for continuous-learning-architect. It runs no command:
the ids and files were collected when the check first failed. Runs with CLAUDE_LEARNER=1 skip
this hook entirely (no check, no ticket). The first turn of a session has no baseline, so it never writes one.

Anti-gaming (Cole Medin's observed escape): when the suite first fails it hashes the
test files. If the suite later passes only after test files changed, the turn may
end, but the user gets a message naming the changed files to review.

The command runs verbatim through the shell, in the project directory, with the
project's own .venv/venv first on PATH; hooks run in a non-interactive shell with
no ~/.bashrc, so don't rely on aliases.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.dont_write_bytecode = True  # keep __pycache__ out of the user's repo
sys.path.insert(0, str(Path(__file__).resolve().parent))
import hookcommon as hc  # noqa: E402


def project_env(root: Path) -> dict:
    env = os.environ.copy()
    for cand in (".venv", "venv"):
        bindir = root / cand / ("Scripts" if os.name == "nt" else "bin")
        if bindir.is_dir():
            env["VIRTUAL_ENV"] = str(root / cand)
            env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
            break
    return env


def test_hashes(root: Path, globs: list) -> dict:
    out = {}
    for g in globs:
        for p in root.glob(g):
            if p.is_file() and ".git" not in p.parts:
                out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


FAILING_RES = (re.compile(r"^(?:FAILED|ERROR) (\S+?)(?: - .*)?$", re.M),
               re.compile(r"^(?:FAIL|ERROR): (\S+) \(([\w.]+)\)", re.M))


def failing_ids(text: str) -> list:
    """Test ids only (same rules as continuous-learning-architect's learn.py)."""
    ids = set(FAILING_RES[0].findall(text))
    ids |= {f"{cls}.{name}" for name, cls in FAILING_RES[1].findall(text)}
    return sorted(ids)


def changed_files(root: Path) -> list:
    try:
        p = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [l[3:].split(" -> ")[-1] for l in p.stdout.splitlines() if len(l) > 3][:10]


def write_ticket(root: Path, rel: str, rec: dict) -> None:
    try:
        f = (root / rel).resolve()
        if not f.is_relative_to(root):
            return
        f.parent.mkdir(parents=True, exist_ok=True)
        with f.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    except OSError:
        pass  # bookkeeping never changes the stop decision


def main() -> int:
    try:
        payload = hc.read_payload()
        root = hc.project_dir(payload)
        cfg = hc.load_policy(root).get("stop", {})
    except Exception:
        return 0
    command = (cfg.get("command") or "").strip()
    if not command or os.environ.get("CLAUDE_LEARNER") == "1":
        return 0  # learner runs: `learn.py prove` runs the checks itself
    state_file = hc.state_dir() / f"stop-{hc.safe_id(payload.get('session_id'))}.json"
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    if not payload.get("stop_hook_active"):
        state["blocks"] = 0
    max_blocks = int(cfg.get("max_blocks", 3))

    def save():
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps(state), encoding="utf-8")

    tickets = cfg.get("tickets").strip() if isinstance(cfg.get("tickets"), str) else ""
    if state.get("blocks", 0) >= max_blocks:
        state["blocks"] = 0
        if tickets and state.get("last_result") == "green" and os.environ.get("CLAUDE_LEARNER") != "1":
            write_ticket(root, tickets, {"kind": "check", "check": cfg.get("check_id") or "stop",
                                         "exit": state.get("exit", 1), "failing": state.get("failing", []),
                                         "files": state.get("files", []), "trace": payload.get("transcript_path", ""),
                                         "date": dt.date.today().isoformat()})
        state["last_result"] = "red"
        save()
        hc.emit({"systemMessage": f"Stop check: `{command}` still fails after {max_blocks} attempts; the turn ended anyway."})
        return 0
    cwd = root / cfg.get("subdir", "")
    try:
        p = subprocess.run(command, shell=True, cwd=str(cwd), env=project_env(root), capture_output=True, text=True,
                           timeout=int(cfg.get("timeout", 300)))
    except subprocess.TimeoutExpired:
        hc.emit({"systemMessage": f"Stop check: `{command}` timed out; the turn ended without a result."})
        return 0
    except OSError as exc:
        hc.emit({"systemMessage": f"Stop check could not run `{command}`: {exc}"})
        return 0
    globs = cfg.get("test_globs", [])
    if p.returncode != 0:
        if cfg.get("guard_test_edits", True) and "snapshot" not in state:
            state["snapshot"] = test_hashes(root, globs)
        state["blocks"] = state.get("blocks", 0) + 1
        if tickets:
            state.update(exit=p.returncode, failing=failing_ids(p.stdout + p.stderr), files=changed_files(root))
        save()
        tail = (p.stdout + p.stderr)[-int(cfg.get("max_output_chars", 3000)):]
        hc.emit({"decision": "block", "reason": (
            f"The check `{command}` failed (exit {p.returncode}). Fix the code under test; change a test only if it "
            f"is wrong, and say so explicitly in your reply.\n{tail}")})
        return 0
    msg = None
    if "snapshot" in state:
        now = test_hashes(root, globs)
        changed = sorted(k for k in set(now) | set(state["snapshot"]) if now.get(k) != state["snapshot"].get(k))
        if changed:
            msg = f"Stop check passed only after these test files changed: {', '.join(changed[:10])}. Review the test edits."
    state.pop("snapshot", None)
    state["blocks"] = 0
    state["last_result"] = "green"
    save()
    if msg:
        hc.emit({"systemMessage": msg})
    return 0


if __name__ == "__main__":
    sys.exit(main())
