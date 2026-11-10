"""Fail-fast guard for a search backend that keeps failing.

A run once ended "completed" with a report written from memory after every one of its 80 Exa searches returned 401:
the sub-agents treat a failed search as an empty one and carry on. This module counts failures per run. After
``RunConfig.search_failure_limit`` failures in a row (default 6; 0 turns the guard off) it trips: the search tools stop
calling the network, and the supervisor either writes a report from the findings it already has (stop reason
``search_unavailable``, resumable) or, with no findings, fails with a message that names the backend.

Only an HTTP or transport failure counts. A successful search that returns nothing is not a failure.
State lives on the active run, so concurrent runs never share a streak; outside a run nothing is tracked.
"""

from __future__ import annotations

import threading
from typing import Iterable, Optional

_MAX_DETAIL = 200


class SearchBackendUnavailable(RuntimeError):
    """A search backend failed ``search_failure_limit`` times in a row and the run has no findings to fall back on."""


class SearchHealth:
    def __init__(self, limit: int = 6, secrets: Iterable[str] = ()):
        self.limit = max(int(limit or 0), 0)
        self._secrets = sorted({s for s in secrets if isinstance(s, str) and len(s) >= 6}, key=len, reverse=True)
        self._lock = threading.Lock()
        self.consecutive = 0
        self.successes = 0
        self.failures = 0
        self.tripped: Optional[str] = None

    def _clean(self, detail: str) -> str:
        text = " ".join(str(detail or "").split())
        for secret in self._secrets:                  # redact before cutting, so a key can't straddle the cut
            text = text.replace(secret, "***")
        return text[:_MAX_DETAIL]

    def record(self, provider: str, ok: bool, detail: str = "") -> None:
        with self._lock:
            if ok:
                self.consecutive = 0
                self.successes += 1
                return
            self.consecutive += 1
            self.failures += 1
            if self.limit and self.consecutive >= self.limit and self.tripped is None:
                last = self._clean(detail)
                self.tripped = (f"{provider} search failed {self.consecutive} times in a row"
                                + (f" (last error: {last})" if last else "")
                                + ". Check the search key or network access; the run was stopped instead of "
                                  "continuing without search results.")


def _health() -> Optional[SearchHealth]:
    from deep_research.runtime import active_runtime
    rt = active_runtime()
    if rt is None:
        return None
    health = getattr(rt, "_search_health", None)
    if health is None:
        keys = getattr(rt.credentials, "keys", None) or {}
        health = SearchHealth(getattr(rt.config, "search_failure_limit", 6), keys.values())
        setattr(rt, "_search_health", health)
    return health


def record_search(provider: str, ok: bool, detail: str = "") -> None:
    health = _health()
    if health is not None:
        health.record(provider, ok, detail)


def search_unavailable() -> Optional[str]:
    """The message once the active run's search backend has tripped the guard, else None."""
    health = _health()
    return health.tripped if health is not None else None


UNAVAILABLE_TOOL_TEXT = ("SEARCH BACKEND UNAVAILABLE: searches have failed repeatedly in this run, so no more "
                         "searches will be run. Do not search again; call finish_research with what you have.")
