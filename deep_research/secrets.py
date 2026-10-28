"""Credential lookup for tools and search providers.

Tools historically read API keys from ``os.getenv`` at call time. Under a
per-run runtime (API), keys must come from the run's ``Credentials`` without
mutating ``os.environ``. ``get_secret`` resolves a secret from the active
runtime's credentials first, then falls back to the process environment — so
standalone/CLI behaviour is unchanged.

A run whose ``Credentials`` has ``env_fallback=False`` (the public web UI) never
reads the process environment: ``get_secret`` returns only what that run holds.
"""

from __future__ import annotations

import os
from typing import Optional


def _active_credentials():
    try:
        from deep_research.runtime import get_runtime
        return get_runtime().credentials
    except Exception:
        return None


def env_fallback_allowed() -> bool:
    """False when the active run must use only its own typed keys (web UI)."""
    creds = _active_credentials()
    return getattr(creds, "env_fallback", True) is not False


def get_secret(env_name: str) -> Optional[str]:
    creds = _active_credentials()
    if creds is not None:
        try:
            value = creds.get(env_name)
        except Exception:
            value = None
        if value:
            return value
        if getattr(creds, "env_fallback", True) is False:
            return None
    return os.getenv(env_name)


def scrub_secrets(text) -> str:
    """``text`` with every secret held by the active run replaced by ``***``.

    Longest first, for values of at least 6 characters. For log lines that
    carry raw provider exception text, which can echo a key.
    """
    text = str(text)
    creds = _active_credentials()
    values = [v for v in (getattr(creds, "keys", None) or {}).values()
              if isinstance(v, str) and len(v) >= 6]
    for secret in sorted(set(values), key=len, reverse=True):
        text = text.replace(secret, "***")
    return text
