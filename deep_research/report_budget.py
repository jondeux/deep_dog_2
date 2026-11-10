"""Plan the final-report call so it fits a dollar budget.

The call's cost grows with what it is sent: the supervisor's history plus the full text of every saved source. A run
with 353 saved sources paid $1.33 for this one call against a $0.75 reserve and ended $0.25 over its $3.00 cap. Before
the call the engine projects its cost (``project``: input tokens and output tokens to dollars, supplied by the cost
governor from the model's own measured price) and, when the projection is over the budget, shortens the per-source text
first. Only if the bare call (history plus findings, no source text) is still too dear does the run fall back to the
findings digest, which needs no model call.

Pure functions: no I/O, no model, no engine imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Sequence

# Measured on two real report calls: 2.40 and 2.47 characters per token (history plus source text).
CHARS_PER_TOKEN = 2.4
# Measured reports were 16.7k and 20.7k output tokens; the writer is capped at 40k. Assume a long one.
OUTPUT_TOKENS = 25_000
# A source cut shorter than this adds nothing the findings do not already say, so its text is dropped instead.
MIN_USEFUL_CHARS = 300
# The dynamic reserve prices the cheapest report worth writing: each source keeps this much text (about an abstract).
TEXT_FLOOR_CHARS = 1_500


@dataclass(frozen=True)
class ReportPlan:
    action: str                       # "full" | "trimmed" | "digest"
    budget_usd: Optional[float]
    projected_usd: Optional[float]    # projected cost of the plan's call (the unchanged call for "digest")
    text_limit: Optional[int] = None  # trimmed: characters of text kept per source (0 = none)
    source_chars_before: int = 0
    source_chars_after: int = 0

    def to_dict(self) -> dict:
        return {"action": self.action, "budget_usd": self.budget_usd,
                "projected_usd": None if self.projected_usd is None else round(self.projected_usd, 4),
                "text_limit": self.text_limit, "source_chars_before": self.source_chars_before,
                "source_chars_after": self.source_chars_after}


def plan_report(budget_usd: Optional[float], project: Callable[[float, float], float], fixed_chars: int,
                source_sizes: Sequence[int]) -> ReportPlan:
    """Decide how to send the final-report call.

    ``project(input_tokens, output_tokens)`` returns dollars. ``fixed_chars`` is everything sent except the source
    text; ``source_sizes`` the characters of text each source would add."""
    sizes = [max(int(s), 0) for s in source_sizes]
    total = sum(sizes)

    def cost(text_chars: int) -> float:
        return project((fixed_chars + text_chars) / CHARS_PER_TOKEN, OUTPUT_TOKENS)

    if budget_usd is None or cost(total) <= budget_usd:
        return ReportPlan("full", budget_usd, cost(total), None, total, total)
    if cost(0) > budget_usd:
        return ReportPlan("digest", budget_usd, cost(total), None, total, 0)

    lo, hi = 0, total                                  # cost is linear in the text: largest total that fits
    while lo < hi:
        mid = (lo + hi + 1) // 2
        lo, hi = (mid, hi) if cost(mid) <= budget_usd else (lo, mid - 1)
    allowed = lo

    lo, hi = 0, max(sizes)                             # largest per-source limit within that total
    while lo < hi:
        mid = (lo + hi + 1) // 2
        lo, hi = (mid, hi) if sum(min(s, mid) for s in sizes) <= allowed else (lo, mid - 1)
    limit = lo if lo >= MIN_USEFUL_CHARS else 0
    kept = sum(min(s, limit) for s in sizes)
    return ReportPlan("trimmed", budget_usd, cost(kept), limit, total, kept)


def trim_text(text: str, limit: int) -> str:
    """Keep the start of a source's text (where abstracts and key results are), marking the cut."""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + " [...text cut to fit the report budget]"


def minimal_report_cost(project: Callable[[float, float], float], fixed_chars: int,
                        source_sizes: Sequence[int]) -> float:
    """Projected cost of the cheapest report worth writing: the history plus ``TEXT_FLOOR_CHARS`` of each source."""
    kept = sum(min(max(int(s), 0), TEXT_FLOOR_CHARS) for s in source_sizes)
    return project((fixed_chars + kept) / CHARS_PER_TOKEN, OUTPUT_TOKENS)

