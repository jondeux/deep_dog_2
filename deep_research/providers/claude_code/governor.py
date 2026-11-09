"""Run governor: caps and plan-window guard for ``claude -p`` calls (subscription use).

Mirrors swarm-framework's meter rules: sum each process's cumulative ``total_cost_usd`` (every call here
is its own process, so the sum is over calls), stop on the plan window (``rate_limit_event`` utilization)
before it is exhausted, never evade it. Cost is the list-price estimate the binary reports, not a bill."""
import asyncio
import threading
from typing import Optional

from .events import Digest
from .phase import current_phase


class GovernorStop(RuntimeError):
    """A cap was reached. The message carries ``RESOURCE_EXHAUSTED`` so the engine classifies it as fatal
    (no retry into the limit). ``governor_stop`` and ``reason`` let the engine tell a usage stop from a real
    failure without importing this module, so it can salvage a report from the findings gathered so far."""

    governor_stop = True

    def __init__(self, reason: str, detail: str, phase: str = "research"):
        super().__init__(f"RESOURCE_EXHAUSTED: claude-code governor stop ({reason}): {detail}")
        self.reason = reason
        self.phase = phase


class Governor:
    """Two tiers. ``usd_cap`` and ``plan_window_max`` are the ceilings the owner approved and are never
    exceeded by a call this governor lets start. Research stops earlier, at ``usd_cap * (1 - report_reserve)``
    and at ``plan_window_max - window_reserve``, so the last stretch is kept for writing the final report
    (calls inside ``report_scope()``). A run that hits a research stop can then still end in a report."""

    def __init__(self, plan_window_max: float = 0.8, usd_cap: Optional[float] = 5.0,
                 max_concurrent: int = 3, timeout_s: float = 600.0,
                 report_reserve: float = 0.25, window_reserve: float = 0.1):
        self.plan_window_max = plan_window_max
        self.usd_cap = usd_cap
        self.report_reserve = min(max(float(report_reserve), 0.0), 0.9)
        self.window_reserve = min(max(float(window_reserve), 0.0), max(plan_window_max - 0.05, 0.0))
        self.max_concurrent = max(1, int(max_concurrent))
        self.timeout_s = float(timeout_s)
        self._lock = threading.Lock()
        self._sem = threading.BoundedSemaphore(self.max_concurrent)
        self.cost = 0.0
        self.calls = 0
        self.hooks_seen = 0
        self.util = {"five_hour": None, "seven_day": None}
        self.util_first = {"five_hour": None, "seven_day": None}    # first reading per window: where the run started
        self.stopped: Optional[str] = None
        self.stop_phase: Optional[str] = None
        self.by_model: dict = {}
        self.by_phase: dict = {}      # cost of research calls vs final-report calls (sizes the report reserve)

    def limits(self) -> dict:
        usd_research = None if self.usd_cap is None else round(self.usd_cap * (1 - self.report_reserve), 6)
        return {"usd_cap": self.usd_cap, "usd_research_limit": usd_research,
                "plan_window_max": self.plan_window_max,
                "plan_window_research_limit": round(self.plan_window_max - self.window_reserve, 6)}

    def check(self, phase: Optional[str] = None) -> None:
        """Raise ``GovernorStop`` BEFORE spawning when a limit is already reached.

        ``phase`` defaults to the current scope: "research" (the early limits) or "report" (the ceilings)."""
        phase = phase or current_phase()
        lim = self.limits()
        window = lim["plan_window_max"] if phase == "report" else lim["plan_window_research_limit"]
        usd = lim["usd_cap"] if phase == "report" else lim["usd_research_limit"]
        with self._lock:
            for k, v in self.util.items():
                if v is not None and v >= window:
                    self._trip("plan_window_guard", phase)
                    raise GovernorStop("plan_window_guard",
                                       f"{k} utilization {v:.2f} >= limit {window:.2f}", phase)
            if usd is not None and self.cost >= usd:
                self._trip("usd_cap", phase)
                raise GovernorStop("usd_cap",
                                   f"list-price estimate ${self.cost:.4f} >= limit ${usd:.2f} "
                                   f"(cap ${self.usd_cap:.2f})", phase)

    def _trip(self, reason: str, phase: str) -> None:
        if self.stopped is None:
            self.stopped, self.stop_phase = reason, phase

    def ingest(self, d: Digest, model: Optional[str] = None) -> None:
        with self._lock:
            self.calls += 1
            self.cost += d.cost
            ph = self.by_phase.setdefault(current_phase(), {"calls": 0, "cost": 0.0})
            ph["calls"] += 1
            ph["cost"] = round(ph["cost"] + d.cost, 6)
            if model:
                m = self.by_model.setdefault(model, {"calls": 0, "cost": 0.0, "input_tokens": 0, "output_tokens": 0})
                u = d.usage()
                m["calls"] += 1
                m["cost"] = round(m["cost"] + d.cost, 6)
                m["input_tokens"] += u["input_tokens"]
                m["output_tokens"] += u["output_tokens"]
            self.hooks_seen += d.hooks_seen
            for k, v in d.util.items():
                if v is not None:
                    self.util[k] = v
                    if self.util_first.get(k) is None:
                        self.util_first[k] = v

    def stats(self) -> dict:
        with self._lock:
            return {"calls": self.calls, "est_cost_usd": round(self.cost, 6), "util": dict(self.util),
                    "util_first": dict(self.util_first), "stopped": self.stopped, "stop_phase": self.stop_phase, "hooks_seen": self.hooks_seen,
                    "by_model": {k: dict(v) for k, v in self.by_model.items()},
                    "by_phase": {k: dict(v) for k, v in self.by_phase.items()}, "limits": self.limits()}

    # concurrency gate (thread-safe, loop-agnostic)
    def acquire(self) -> None:
        self._sem.acquire()

    async def acquire_async(self) -> None:
        while not self._sem.acquire(blocking=False):
            await asyncio.sleep(0.05)

    def release(self) -> None:
        self._sem.release()
