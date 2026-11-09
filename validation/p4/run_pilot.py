#!/usr/bin/env python3
"""P4 pilot: one small end-to-end Deep Dog run entirely on the Claude subscription.

Preset testing-framework (sonnet-5.5 medium supervisor/draft/writer, haiku-5.5 medium sub-agents), search = Claude's
WebSearch. Small budgets on purpose. Hard stops: claude_code_usd_cap (list-price estimate) and the plan-window guard."""
import asyncio, dataclasses, json, os, re, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import deep_research.integration as integration
from deep_research import model_picker as mp
from deep_research.events import EventCollector
from deep_research.models import Credentials, ModelFactory
from deep_research.run_config import RunConfig

QUESTION = "What are the main benefits and limitations of sodium-ion batteries for home energy storage?"
OUT = os.path.dirname(__file__)
CAP = float(os.environ.get("PILOT_USD_CAP", "1.00"))

base = RunConfig(output_mode="none", log_mode="none", save_report_to_file=False, save_subagent_reports_to_file=False,
                 enable_source_log=False, enable_subtopic_generation=False, enable_research_trace=True)
cfg = mp.apply_preset(base, "testing-framework")
cfg = dataclasses.replace(
    cfg, research_time_min_minutes=1, research_time_max_minutes=3, supervisor_max_iterations=3,
    subagent_max_iterations=2, subagent_max_searches=2, subagent_max_total_reads=3, subagent_max_reads=3,
    subagent_max_saves=3, supervisor_max_concurrent_research=2, supervisor_max_concurrent_discovery=1,
    claude_code_usd_cap=CAP, enabled_agents=["ResearchWeb"]).finalize()

created = []


class F(ModelFactory):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        created.append(self)


integration.ModelFactory = F
collector, trace = EventCollector(), []


async def main():
    t0 = time.time()
    res = await integration.run_research(
        QUESTION, config=cfg,
        runtime=integration.RuntimeOptions(run_id="p4", thread_id="p4", event_sink=collector,
                                           trace_sink=lambda r: trace.append(r) if True else None),
        credentials=Credentials({}))
    return res, time.time() - t0

try:
    res, secs = asyncio.run(main())
    err = None
except Exception as e:          # report honestly, keep what we have
    res, secs, err = None, 0.0, f"{type(e).__name__}: {str(e)[:300]}"

gov = created[0].governor.stats() if created else {}
types = collector.types()
report = (res.final_report if res else "") or ""
cites = sorted({int(m) for m in re.findall(r"\[(\d{1,3})\]", report.split("## Sources")[0])})
sources = re.findall(r"^\[(\d+)\] .*\((https?://[^)]+)\)", report, flags=re.M)
summary = {
    "status": res.status if res else "error", "error": err or (res.failure if res else None), "seconds": round(secs, 1),
    "governor": gov, "event_types": sorted(set(types)), "event_count": len(types),
    "order_ok": types[:1] == ["run_started"] and types[-1:] in (["run_completed"], ["run_failed"]),
    "report_chars": len(report), "inline_citations": cites, "sources_listed": len(sources),
    "citations_all_in_sources": bool(cites) and set(cites) <= {int(n) for n, _ in sources},
    "iterations": types.count("supervisor_iteration"), "delegations": types.count("delegation_started"),
}
open(os.path.join(OUT, "pilot_report.md"), "w").write(report)
json.dump(summary, open(os.path.join(OUT, "pilot_summary.json"), "w"), indent=1)
json.dump(types, open(os.path.join(OUT, "pilot_events.json"), "w"))
print(json.dumps(summary, indent=1))
