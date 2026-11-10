"""Readable usage for a run on the Claude plan (costs are the binary's list-price estimate, not a bill)."""
from typing import Optional


def _pct(v: Optional[float]) -> str:
    return "n/a" if v is None else f"{v * 100:.0f}%"


def format_usage(usage: Optional[dict]) -> str:
    """One block of text for the CLI and the skill. Empty string when there is no usage (not a claudecode run)."""
    if not usage:
        return ""
    util = usage.get("util") or {}
    first = usage.get("util_first") or {}
    lim = usage.get("limits") or {}

    def window(key: str) -> str:
        now, start = util.get(key), first.get(key)
        text = _pct(now)
        # Where the window stood at the first call: the plan is shared, so the end value alone is not this run's use.
        if now is not None and start is not None and abs(now - start) >= 0.005:
            text += f" ({_pct(start)} at the first call)"
        return text
    by_model = usage.get("by_model") or {}
    third_party = any(m.get("source") for m in by_model.values())
    on_plan = any(not m.get("source") for m in by_model.values()) or not by_model
    lines = [f"Usage (list-price estimate, not a bill): ${usage.get('est_cost_usd', 0):.4f} over "
             f"{usage.get('calls', 0)} calls"
             + (f"; plan window: 5-hour window {window('five_hour')}, 7-day window {window('seven_day')}" if on_plan else "")]
    if lim.get("usd_cap") is not None:
        lines[0] += f"; cap ${lim['usd_cap']:.2f}, window guard {_pct(lim.get('plan_window_max'))}"
    for model, m in sorted(by_model.items()):
        lines.append(f"  {model}: {m.get('calls', 0)} calls, ${m.get('cost', 0):.4f}"
                     + (f" ({m['source']})" if m.get("source") else "") +
                     f", {m.get('input_tokens', 0)} in / {m.get('output_tokens', 0)} out tokens")
    if third_party:
        lines.append("  third-party rows: reported = the provider's own figure; estimated = tokens x your price per million; "
                     "unpriced = counted in tokens only, not in the cap")
    rep = (usage.get("by_phase") or {}).get("report")
    if rep:
        lines.append(f"  report phase: ${rep.get('cost', 0):.4f} over {rep.get('calls', 0)} calls (the rest was research)")
    if usage.get("stopped"):
        lines.append(f"  stopped: {usage['stopped']}" + (f" (during {usage['stop_phase']})" if usage.get("stop_phase") else ""))
    return "\n".join(lines)
