"""Shared helpers for the hooks-architect hook scripts (stdlib only).

Each hook adds its own directory to sys.path explicitly and imports this module,
so the scripts can run under `python3 -I` (isolated mode: no cwd, no user site,
no PYTHON* env vars on the import path).

Policy = policy.json next to the scripts, merged with the optional project
override .claude/hooks-policy.json (dicts merge, lists append, scalars replace;
"disable" lists rule ids to switch off).
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
OVERRIDE = Path(".claude") / "hooks-policy.json"

SECRETISH = re.compile(
    r"(sk-[\w-]{10,}|gh[pousr]_\w{20,}|github_pat_\w{20,}|xox[abprs]-[\w-]{10,}|AKIA[0-9A-Z]{16}"
    r"|(?i:bearer)\s+[\w.~+/-]{16,}=*|(?i:password|passwd|token|secret|api[_-]?key)\s*[:=]\s*\S+)"
)


class PolicyError(Exception):
    pass


def read_payload() -> dict:
    data = json.load(sys.stdin)
    if not isinstance(data, dict):
        raise ValueError("hook input is not a JSON object")
    return data


def project_dir(payload: dict) -> Path:
    return Path(os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or os.getcwd()).resolve()


def state_dir() -> Path:
    """Where hooks keep per-session state. The guard treats it as zero-access."""
    if os.environ.get("CLAUDE_HOOKS_STATE_DIR"):
        return Path(os.environ["CLAUDE_HOOKS_STATE_DIR"]).resolve()
    if os.environ.get("CLAUDE_PLUGIN_DATA"):
        return (Path(os.environ["CLAUDE_PLUGIN_DATA"]) / "state").resolve()
    uid = os.getuid() if hasattr(os, "getuid") else "u"
    return (Path(tempfile.gettempdir()) / f"claude-hooks-architect-{uid}").resolve()


def safe_id(value) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(value or "none"))[:80]


def _merge(base, extra):
    if isinstance(base, dict) and isinstance(extra, dict):
        out = dict(base)
        for k, v in extra.items():
            out[k] = _merge(base[k], v) if k in base else v
        return out
    if isinstance(base, list) and isinstance(extra, list):
        return base + extra
    return extra


def load_policy(project: Path) -> dict:
    base_file = HERE / "policy.json"
    try:
        policy = json.loads(base_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PolicyError(f"cannot read {base_file}: {exc}") from exc
    override = project / OVERRIDE
    if override.exists():
        try:
            policy = _merge(policy, json.loads(override.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            raise PolicyError(f"cannot read {override}: {exc}") from exc
    if not isinstance(policy, dict):
        raise PolicyError("policy is not a JSON object")
    return policy


def enabled(rules: list, policy: dict) -> list:
    off = set(policy.get("disable", []))
    return [r for r in rules if r.get("id") not in off]


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")


# ---------------------------------------------------------------- path matching

def expand(raw: str, base: Path) -> Path:
    """Resolve a path token the way a shell would, without touching the filesystem."""
    s = raw.strip()
    home, tmp = os.path.expanduser("~"), tempfile.gettempdir()
    for var, val in (("${HOME}", home), ("$HOME", home), ("${TMPDIR}", tmp), ("$TMPDIR", tmp)):
        if s == var or s.startswith(var + "/"):
            s = val + s[len(var):]
            break
    s = os.path.expanduser(s)
    p = Path(s) if os.path.isabs(s) else base / s
    return Path(os.path.normpath(str(p)))


def _variants(pattern: str, project: Path) -> list:
    """Absolute glob(s) for a policy pattern, or [] for basename-only patterns."""
    if "/" not in pattern.rstrip("/"):
        return []
    if pattern.startswith("~") or pattern.startswith("$HOME"):
        pat = os.path.expanduser(pattern.replace("$HOME", "~", 1))
    elif os.path.isabs(pattern):
        pat = pattern
    elif pattern.startswith("$TMPDIR"):
        pat = tempfile.gettempdir() + pattern[len("$TMPDIR"):]
    else:
        pat = str(project / pattern)
    pat = pat.rstrip("/")
    out = [pat]
    if pat.endswith("/**"):
        out.append(pat[:-3])
    return out


def matches(path: Path, patterns: list, project: Path) -> str | None:
    """Return the first pattern that matches `path`, or None.

    - "name" or "*.ext" (no slash): matches the basename anywhere.
    - "dir/file", "dir/**": relative to the project root.
    - "/abs/**", "~/x/**", "$TMPDIR/**": absolute.
    """
    s = str(path)
    base = path.name
    for pat in patterns:
        if "/" not in pat.rstrip("/"):
            if fnmatch.fnmatchcase(base, pat):
                return pat
            continue
        for v in _variants(pat, project):
            if fnmatch.fnmatchcase(s, v):
                return pat
    return None


def under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
