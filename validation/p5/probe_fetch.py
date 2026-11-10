"""Live probe: does Claude's WebFetch return real page text, headless, on the plan sign-in? (3 pages, haiku-5.5, cap $0.10.)

Run from the repo root: python validation/p5/probe_fetch.py   Output: validation/p5/probe_fetch_results.json
Owner approved the spend on 2026-11-10 (hard cap $0.10; the governor stops starting calls once it is reached).
v1 (no topic, "full text" requested) is in probe_fetch_results_v1.json; this version passes a research topic."""
import asyncio
import json
import time
from pathlib import Path

from deep_research import model_picker as mp
from deep_research.agents.web.claude_fetch import claude_fetch_page
from deep_research.models import Credentials, ModelFactory
from deep_research.run_config import RunConfig
from deep_research.runtime import RuntimeContext, runtime_scope

URLS = ["https://www.nejm.org/doi/10.1056/NEJMoa2114833",
        "https://pubmed.ncbi.nlm.nih.gov/37364268",
        "https://en.wikipedia.org/wiki/Intermittent_fasting"]


FOCUS = "How many adults were enrolled, and what weight and cardiometabolic results did the time-restricted eating trials report?"


async def main():
    cfg = mp.apply_preset(RunConfig(output_mode="none", log_mode="none", claude_code_usd_cap=0.10,
                                    claude_code_report_reserve=0.0), "testing-framework").finalize()
    factory = ModelFactory(cfg, Credentials({}))
    rt = RuntimeContext(run_id="probe", thread_id="probe", config=cfg, models=factory, console_enabled=False)
    rows = []
    with runtime_scope(rt):
        for url in URLS:
            before = factory.governor.cost
            t0 = time.time()
            title, body = await claude_fetch_page(url, focus=FOCUS)
            rows.append({"url": url, "title": title, "chars": len(body), "failed": body.startswith("Failed to fetch"),
                         "seconds": round(time.time() - t0, 1), "cost_usd": round(factory.governor.cost - before, 4),
                         "head": body[:400], "tail": body[-200:]})
            print(json.dumps({k: rows[-1][k] for k in ("url", "chars", "failed", "seconds", "cost_usd")}))
    out = {"usage": factory.usage(), "pages": rows}
    Path(__file__).with_name("probe_fetch_results.json").write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print("total cost", out["usage"]["est_cost_usd"], "calls", out["usage"]["calls"])


asyncio.run(main())
