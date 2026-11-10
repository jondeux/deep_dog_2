---
title: "The final report is cost-projected before it is written, and a tiny cap now gives the digest, not a model report"
author: Claude
date: 2026-11-10
branch: deep-dog-2-REA-claude-p
cost: { minutes: 21, tokens: 220647 }
id: l-ddebaef0
scope: repo
status: active
src: agent
touches:
  - tests/conftest.py
  - docs/SETUP_SECRETS_IN_CC_CLOUD_ENV.md
  - tests/test_search_guard.py
  - deep_research/search_health.py
  - tests/test_report_budget.py
  - deep_research/report_budget.py
---

## Decided
Before the final-report call the engine projects its cost (input tokens = characters / 2.4, 25k output tokens, the model's own measured price, x1.25) against the room left under the cap or the owner's --report-budget. Over budget it trims per-source text, then returns the findings digest. An earlier stop reason is kept as the cause. Unmeasured models use a default price about 30% above sonnet's, because a higher default made even a $1 cap return the digest.

## Tried and ruled out
- A pessimistic default price (6e-6 per token): broke existing salvage and resume tests, since a history with no earlier calls projected over a $1 budget.
- Driving the whole supervisor subgraph in the new tests: its first node makes its own model call. Test the final-report node directly.

## Watch out
- Tests with a half-cent cap cannot get a model-written report any more (real report calls cost $0.13 to $1.33). The one resume test that relied on this switches the guard off.
- The projection is calibrated on two real runs and has not been run live.

In the code: deep_research/report_budget.py, Governor.report_budget_now and unit_price in providers/claude_code/governor.py, write_final_report in multi_agent_supervisor.py, tests/test_report_budget.py.
