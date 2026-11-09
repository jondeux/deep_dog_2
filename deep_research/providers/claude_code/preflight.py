"""Readiness check for the ``claude`` binary: found, runs, signed in. Never reads credentials."""
import json
import subprocess
import tempfile
from typing import Optional

from .env import BIN_VAR, OPT_IN_VAR, agent_env, resolve_binary


class ClaudeCodeUnavailable(RuntimeError):
    pass


_cache: dict = {}


def ensure_ready(binary: Optional[str] = None, *, force: bool = False) -> dict:
    """``{"binary", "version", "auth_method"}``, or raise ``ClaudeCodeUnavailable`` with the fix.

    The sign-in check runs in the SAME allowlisted environment the children get, so a sign-in that
    depends on a stripped variable is caught here, not in the middle of a run."""
    binary = binary or resolve_binary()
    if not binary:
        raise ClaudeCodeUnavailable(
            f"The 'claude' binary was not found on PATH. Install Claude Code, or set {BIN_VAR} to its path.")
    if binary in _cache and not force:
        return _cache[binary]
    env = agent_env()
    with tempfile.TemporaryDirectory(prefix="ddcc-pre-") as cwd:
        try:
            v = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=30, env=env, cwd=cwd)
            a = subprocess.run([binary, "auth", "status"], capture_output=True, text=True, timeout=30,
                               env=env, cwd=cwd, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise ClaudeCodeUnavailable(f"could not run {binary!r}: {type(e).__name__}") from None
    status = {}
    try:
        status = json.loads(a.stdout)
    except ValueError:
        pass
    if not status.get("loggedIn"):
        raise ClaudeCodeUnavailable(
            "Claude Code is not signed in for child processes. Sign in with `claude` in this environment. "
            f"If your sign-in lives in a variable the allowlist strips, name it in {OPT_IN_VAR} (your own choice).")
    info = {"binary": binary, "version": (v.stdout or "").strip(), "auth_method": status.get("authMethod")}
    _cache[binary] = info
    return info
