#!/usr/bin/env python3
"""SessionStart hook: tell Claude what is true right now.

Adds git facts (branch, upstream ahead/behind, uncommitted files, recent commits)
and the contents of the state files listed under "context.files" in the policy.
Static conventions belong in CLAUDE.md/AGENTS.md; this hook carries what changes
between sessions. Re-runs on resume, /clear, and compaction, so the facts stay fresh.

Text is phrased as facts, not commands (the docs warn that imperative,
system-sounding context can trip prompt-injection defenses).
Fails open: on any error it prints nothing.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.dont_write_bytecode = True  # keep __pycache__ out of the user's repo
sys.path.insert(0, str(Path(__file__).resolve().parent))
import hookcommon as hc  # noqa: E402


def git(root: Path, *args: str) -> str:
    p = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=5)
    return p.stdout.strip() if p.returncode == 0 else ""


def git_facts(root: Path) -> list:
    if git(root, "rev-parse", "--is-inside-work-tree") != "true":
        return []
    facts = []
    branch = git(root, "branch", "--show-current") or "(detached HEAD)"
    line = f"Git branch: {branch}"
    counts = git(root, "rev-list", "--left-right", "--count", "@{upstream}...HEAD")
    if counts:
        behind, ahead = counts.split()
        line += f" (ahead {ahead}, behind {behind} of upstream)"
    facts.append(line + ".")
    dirty = [l for l in git(root, "status", "--porcelain").splitlines() if l.strip()]
    if dirty:
        shown = ", ".join(l[3:] for l in dirty[:10])
        more = f" and {len(dirty) - 10} more" if len(dirty) > 10 else ""
        facts.append(f"Uncommitted files ({len(dirty)}): {shown}{more}.")
    else:
        facts.append("Working tree is clean.")
    log = git(root, "log", "--oneline", "-5")
    if log:
        facts.append("Recent commits:\n" + log)
    return facts


def file_facts(root: Path, cfg: dict) -> list:
    out, budget = [], int(cfg.get("max_total_chars", 6000))
    per = int(cfg.get("max_chars_per_file", 1500))
    seen = set()
    for rel in cfg.get("files", []):
        f = root / rel
        if not f.is_file() or f.resolve() in seen:  # policy lists append, so a file named in both layers would print twice
            continue
        seen.add(f.resolve())
        text = f.read_text(encoding="utf-8", errors="replace").strip()
        clipped = text[:per] + (f"\n[... {len(text) - per} more characters in {rel}]" if len(text) > per else "")
        if len(clipped) > budget:
            break
        budget -= len(clipped)
        out.append(f"Contents of {rel}:\n{clipped}")
    return out


def main() -> int:
    try:
        payload = hc.read_payload()
        root = hc.project_dir(payload)
        cfg = hc.load_policy(root).get("context", {})
        facts = (git_facts(root) if cfg.get("git", True) else []) + file_facts(root, cfg)
        facts.append("Hooks active: a PreToolUse guard (denies secrets access and destructive commands; asks before "
                     "discarding work) and an approval gate (git commit and push pass only after the user's latest "
                     "message approves them).")
    except Exception:
        return 0
    hc.emit({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "\n".join(facts)}})
    return 0


if __name__ == "__main__":
    sys.exit(main())
