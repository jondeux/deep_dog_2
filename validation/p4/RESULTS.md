# P4: end-to-end pilot on the Claude subscription (2026-11-09)

Question: "What are the main benefits and limitations of sodium-ion batteries for home energy storage?"
Preset `testing-framework` (sonnet-5.5 medium supervisor/draft/writer, haiku-5.5 medium sub-agents), search = Claude's
WebSearch (`claude_web_search`, haiku-5.5 low). Script `run_pilot.py`; summaries `pilot_summary.json`; report `pilot_report.md`.

| Run | End state | Seconds | Calls | List-price estimate | Notes |
|---|---|---|---|---|---|
| run1 (`run1_capped/`) cap $0.60, 4 iterations, 3+ sub-agents per turn | `partial`: governor stopped at `usd_cap` ($0.601) at final-report writing | 239 | 39 | $0.601 | The cap fired exactly as designed. 8 delegations. My cap was too low for that size; not a defect. |
| run2 cap $1.00, 3 iterations, 2 concurrent | **`completed`** | 221.5 | 21 | **$0.5513** | 4 delegations; 22,973-char report; 11 inline citations, all present in `## Sources` [V]. |

**Where the cost went (run2, `by_model`)**

| Model | Calls | Cost | Input tokens | Output tokens |
|---|---|---|---|---|
| sonnet-5.5 (supervisor, brief, draft, writer) | 6 | $0.4841 | 105,460 | 18,226 |
| haiku-5.5 (sub-agents) | 11 | $0.0135 | 66,718 | 8,850 |
| haiku-5.5 + WebSearch | 4 | $0.0537 | 24,535 | 6,344 |

- The supervisor/writer side is 88 % of the cost. A cheaper supervisor is the main lever; sub-agents and search are small.
- Per-token cost ratio sonnet : haiku is about 6x (matches swarm-framework m6).
- Plan window: five-hour utilization 0.29 -> 0.37 after run1 and 0.40 after run2; seven-day 0.12 -> 0.14. Hooks seen in children: 0.

**Trace parity [V]**: `run_started`, `config_validated`, `scope_*`, `draft_*`, `supervisor_iteration` x3, `delegation_started`,
`subagent_started/completed`, `source_found/read/saved`, `report_started`, `citations_validated`, `run_completed`: the same grammar as the
offline fake-model run, produced by the unchanged engine.

**Not yet established**
- Quality versus the DeepSeek/Exa baseline: one question is not a benchmark (P6).
- A default-size run (30 iterations, 15 minutes): cost and wall time are not measured. Expect several times this pilot.
- The hard "plan limit reached" reply was not observed (window stayed at 0.40).
- Benchmark claims need `--search exa` to match the published configuration.
