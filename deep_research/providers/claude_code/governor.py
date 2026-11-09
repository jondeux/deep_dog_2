"""Run governor: caps and plan-window guard for ``claude -p`` calls (subscription use).

Mirrors swarm-framework's meter rules: sum each process's cumulative ``total_cost_usd`` (every call here
is its own process, so the sum is over calls), stop on the plan window (``rate_limit_event`` utilization)
before it is exhausted, never evade it. Cost is the list-price estimate the binary reports, not a bill."""
import asyncio
import threading
from typing import Optional

from .events import Digest


class GovernorStop(RuntimeError):
    """A cap was reached. The message carries ``RESOURCE_EXHAUSTED`` so the engine classifies it as fatal
    (no retry, clean abort) instead of retrying into the limit."""

    def __init__(self, reason: str, detail: str):
        super().__init__(f"RESOURCE_EXHAUSTED: claude-code governor stop ({reason}): {detail}")
        self.reason = reason


class Governor:
    def __init__(self, plan_window_max: float = 0.8, usd_cap: Optional[float] = 5.0,
                 max_concurrent: int = 3, timeout_s: float = 600.0):
        self.plan_window_max = plan_window_max
        self.usd_cap = usd_cap
        self.max_concurrent = max(1, int(max_concurrent))
        self.timeout_s = float(timeout_s)
        self._lock = threading.Lock()
        self._sem = threading.BoundedSemaphore(self.max_concurrent)
        self.cost = 0.0
        self.calls = 0
        self.hooks_seen = 0
        self.util = {"five_hour": None, "seven_day": None}
        self.stopped: Optional[str] = None
        self.by_model: dict = {}

    def check(self) -> None:
        """Raise ``GovernorStop`` BEFORE spawning when a cap is already reached."""
        with self._lock:
            for k, v in self.util.items():
                if v is not None and v >= self.plan_window_max:
                    self.stopped = "plan_window_guard"
                    raise GovernorStop("plan_window_guard",
                                       f"{k} utilization {v:.2f} >= limit {self.plan_window_max:.2f}")
            if self.usd_cap is not None and self.cost >= self.usd_cap:
                self.stopped = "usd_cap"
                raise GovernorStop("usd_cap", f"list-price estimate ${self.cost:.4f} >= cap ${self.usd_cap:.2f}")

    def ingest(self, d: Digest, model: Optional[str] = None) -> None:
        with self._lock:
            self.calls += 1
            self.cost += d.cost
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

    def stats(self) -> dict:
        with self._lock:
            return {"calls": self.calls, "est_cost_usd": round(self.cost, 6), "util": dict(self.util),
                    "stopped": self.stopped, "hooks_seen": self.hooks_seen,
                    "by_model": {k: dict(v) for k, v in self.by_model.items()}}

    # concurrency gate (thread-safe, loop-agnostic)
    def acquire(self) -> None:
        self._sem.acquire()

    async def acquire_async(self) -> None:
        while not self._sem.acquire(blocking=False):
            await asyncio.sleep(0.05)

    def release(self) -> None:
        self._sem.release()
