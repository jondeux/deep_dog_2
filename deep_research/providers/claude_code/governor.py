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


# Output tokens cost this many times an input token for every Claude model measured so far (list price).
OUTPUT_PRICE_RATIO = 5
# Below this many input-equivalent tokens a model's measured price is too noisy to trust.
MIN_CALIBRATION_TOKENS = 10_000
# $ per input-equivalent token assumed for a model with no usable measurement. Measured for sonnet-5.5: 2.8e-6 to
# 3.4e-6 (list price $3 / $15 per million tokens), so this is about 30 % above it. A real run measures its own model
# within its first supervisor turns, so the default only matters for a run that stops before that.
DEFAULT_UNIT_PRICE = 4e-6
# Measured projection error was -8 % to +3 % with the research-phase price; this covers it.
REPORT_COST_MARGIN = 1.25
# Dynamic reserve: with no earlier round to measure, assume each parallel sub-agent costs this share of the cap.
# A guess, set from one live round (3 haiku sub-agents on Tavily raw content cost $0.85 of a $1 cap, far above it);
# the per-call research limit below is what actually keeps a round inside the cap.
FIRST_ROUND_ALLOWANCE_PER_AGENT = 0.05
# Dynamic reserve: the static research limit stays as a backstop, this share of the cap (at least $0.05) below it.
DYNAMIC_BACKSTOP_SHARE = 0.10


class Governor:
    """Two tiers. ``usd_cap`` and ``plan_window_max`` are the ceilings the owner approved and are never
    exceeded by a call this governor lets start. Research stops earlier, at ``usd_cap * (1 - report_reserve)``
    and at ``plan_window_max - window_reserve``, so the last stretch is kept for writing the final report
    (calls inside ``report_scope()``). A run that hits a research stop can then still end in a report."""

    def __init__(self, plan_window_max: float = 0.8, usd_cap: Optional[float] = 5.0,
                 max_concurrent: int = 3, timeout_s: float = 600.0,
                 report_reserve: float = 0.25, window_reserve: float = 0.1, report_reserve_floor_usd: float = 0.0,
                 report_budget_usd: Optional[float] = None, dynamic_reserve: bool = False):
        self.plan_window_max = plan_window_max
        # Stop research when spend + the next round + the projected report reaches the cap (see dynamic_reserve_check).
        self.dynamic_reserve = bool(dynamic_reserve)
        self._report_floor: Optional[float] = None    # cheapest useful report, as of the last dynamic check
        self._last_check_cost: Optional[float] = None
        self._round_cost = 0.0
        self.reserve_check: Optional[dict] = None
        self.usd_cap = usd_cap
        # The most the owner lets the final-report call cost. None: whatever is left under the cap.
        self.report_budget_usd = None if report_budget_usd is None else max(float(report_budget_usd), 0.0)
        self.report_plan: Optional[dict] = None     # what the report-cost guard decided (see report_budget.py)
        self.report_reserve = min(max(float(report_reserve), 0.0), 0.9)
        self.report_reserve_floor_usd = max(float(report_reserve_floor_usd), 0.0)
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
        usd_research = None
        if self.usd_cap is not None:
            # The report call has a dollar cost of its own (measured $0.45 at a $3 cap), so a percentage alone is too
            # little at a small cap: reserve the larger of the two, but never more than 60% of the cap.
            reserve = min(max(self.usd_cap * self.report_reserve, self.report_reserve_floor_usd), self.usd_cap * 0.6)
            if self.dynamic_reserve:                      # the projection decides; this limit is only a backstop
                reserve = min(max(self.usd_cap * DYNAMIC_BACKSTOP_SHARE, 0.05), self.usd_cap * 0.6)
            elif self.report_budget_usd is not None:      # the owner's budget decides what research leaves behind
                reserve = min(self.report_budget_usd, self.usd_cap * 0.6)
            usd_research = round(self.usd_cap - reserve, 6)
        return {"usd_cap": self.usd_cap, "usd_research_limit": usd_research,
                "dynamic_research_limit": self._dynamic_research_limit(),
                "report_budget_usd": self.report_budget_usd, "dynamic_reserve": self.dynamic_reserve,
                "plan_window_max": self.plan_window_max,
                "plan_window_research_limit": round(self.plan_window_max - self.window_reserve, 6)}

    def _dynamic_research_limit(self) -> Optional[float]:
        """Where research CALLS stop once a dynamic check has priced the report: the cap minus the cheapest useful
        report minus a margin for the calls already in flight (the average research call times the concurrency).
        None before the first check, without a cap, or in fixed-reserve mode."""
        if not self.dynamic_reserve or self.usd_cap is None or self._report_floor is None:
            return None
        ph = self.by_phase.get("research") or {}
        avg = ph.get("cost", 0.0) / ph["calls"] if ph.get("calls") else 0.0
        return round(self.usd_cap - self._report_floor - self.max_concurrent * avg, 6)

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
            if phase != "report":                       # mid-round: the report must still fit under the cap
                dyn = self._dynamic_research_limit()
                if dyn is not None and (usd is None or dyn < usd):
                    usd = dyn
            if usd is not None and self.cost >= usd:
                self._trip("usd_cap", phase)
                raise GovernorStop("usd_cap",
                                   f"list-price estimate ${self.cost:.4f} >= limit ${usd:.2f} "
                                   f"(cap ${self.usd_cap:.2f})", phase)

    # ── final-report cost guard ───────────────────────────────────────────
    def report_budget_now(self) -> Optional[float]:
        """Dollars the final-report call may spend: the room left under the cap, or the owner's budget if lower."""
        with self._lock:
            room = None if self.usd_cap is None else max(self.usd_cap - self.cost, 0.0)
        if self.report_budget_usd is None:
            return room
        return self.report_budget_usd if room is None else min(self.report_budget_usd, room)

    def unit_price(self, model_id: Optional[str]) -> float:
        """Measured $ per input-equivalent token (output counts OUTPUT_PRICE_RATIO times) for ``model_id``."""
        with self._lock:
            m = dict(self.by_model.get(model_id or "", {}))
        eq = m.get("input_tokens", 0) + OUTPUT_PRICE_RATIO * m.get("output_tokens", 0)
        if eq >= MIN_CALIBRATION_TOKENS and m.get("cost", 0) > 0:
            return m["cost"] / eq
        return DEFAULT_UNIT_PRICE

    def project_report_cost(self, model_id: Optional[str], input_tokens: float, output_tokens: float) -> float:
        return REPORT_COST_MARGIN * self.unit_price(model_id) * (input_tokens + OUTPUT_PRICE_RATIO * output_tokens)

    def dynamic_reserve_check(self, min_report_cost: float) -> Optional[str]:
        """Should research stop now? Returns the reason, or None to carry on.

        Stops when ``spent + next round + the cheapest useful report >= usd_cap``. The next round is what the last
        one cost (the spend since the previous check), or a small default before any round was measured. The owner's
        ``report_budget_usd`` caps the report term."""
        if not self.dynamic_reserve or self.usd_cap is None:
            return None
        with self._lock:
            spent = self.cost
            if self._last_check_cost is None:
                self._round_cost = max(self.usd_cap * FIRST_ROUND_ALLOWANCE_PER_AGENT * self.max_concurrent, 0.05)
            elif spent > self._last_check_cost:
                self._round_cost = spent - self._last_check_cost
            self._last_check_cost = spent
            report = float(min_report_cost)
            if self.report_budget_usd is not None:
                report = min(report, self.report_budget_usd)
            need = spent + self._round_cost + report
            stop = need >= self.usd_cap
            self._report_floor = report
            self.reserve_check = {"spent_usd": round(spent, 4), "next_round_usd": round(self._round_cost, 4),
                                  "report_usd": round(report, 4), "cap_usd": self.usd_cap, "stopped": stop,
                                  "research_limit_usd": round(self.usd_cap - report, 4)}
            if not stop:
                return None
            return (f"spent ${spent:.2f} + next round about ${self._round_cost:.2f} + the cheapest useful report "
                    f"about ${report:.2f} reaches the ${self.usd_cap:.2f} cap")

    def report_estimator(self, model_id: Optional[str]) -> "ReportEstimator":
        return ReportEstimator(self, model_id)

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
                    "by_phase": {k: dict(v) for k, v in self.by_phase.items()}, "limits": self.limits(),
                    "report_plan": dict(self.report_plan) if self.report_plan else None,
                    "reserve_check": dict(self.reserve_check) if self.reserve_check else None}

    # concurrency gate (thread-safe, loop-agnostic)
    def acquire(self) -> None:
        self._sem.acquire()

    async def acquire_async(self) -> None:
        while not self._sem.acquire(blocking=False):
            await asyncio.sleep(0.05)

    def release(self) -> None:
        self._sem.release()


class ReportEstimator:
    """What the engine needs to keep the final-report call inside its budget, without importing the provider."""

    def __init__(self, governor: Governor, model_id: Optional[str]):
        self._g, self._model_id = governor, model_id

    def budget_usd(self) -> Optional[float]:
        return self._g.report_budget_now()

    def project(self, input_tokens: float, output_tokens: float) -> float:
        return self._g.project_report_cost(self._model_id, input_tokens, output_tokens)

    def record(self, plan: dict) -> None:
        self._g.report_plan = dict(plan)

    def reserve_stop(self, fixed_chars: int, source_sizes) -> Optional[str]:
        """Dynamic reserve: the reason research should stop now (spend + next round + cheapest useful report >= cap)."""
        from deep_research.report_budget import minimal_report_cost
        return self._g.dynamic_reserve_check(minimal_report_cost(self.project, fixed_chars, source_sizes))
