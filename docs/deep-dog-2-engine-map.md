# Deep Dog 2 engine map

Read-only map of how the engine runs, from source at commit `e8fc5b8`. Every claim cites `file:line`. Read statically; nothing here was run. "Not read" items are listed at the end so the gaps are explicit.

## 1. Shape: three nested LangGraph graphs

```
run_research()                       integration.py:276
  └─ OUTER graph (research_agent_full.py:288-322)
       clarify_with_user ─▶ write_research_brief ─▶ write_draft_report
            ─▶ supervisor_subgraph ─▶ final_report_generation ─?▶ subtopic_evaluation ─?▶ subtopic_generation ─▶ END
       └─ SUPERVISOR graph (multi_agent_supervisor.py:1028-1033)
            supervisor ⇄ supervisor_tools ─▶ write_final_report
            └─ SUB-AGENT graph, one per Research* tool call (agents/base.py:1601-1627)
                 llm_call ⇄ tool_node ─▶ compress_research ─?▶ report_write ─▶ END
```
`?▶` = conditional edge. Every node reads per-run config, models and prompts from a `RuntimeContext` (`runtime.py:67`), not globals.

## 2. Outer graph (the pipeline)

| Node | What it does | Ref |
|---|---|---|
| `clarify_with_user` | **Stub.** The LLM clarification code is commented out; always routes to `write_research_brief`. | research_agent_scope.py:45-76 |
| `write_research_brief` | One structured (`json_mode`) call → `ResearchQuestion{research_brief, input_language}`. Detects the user's language; `target_language` is set equal to it. | :78-131 |
| `write_draft_report` | One free-text call, 420 s timeout, from the brief + `example_report` (empty). Output = the draft scaffold. Seeds `supervisor_messages` with 3 strings: draft, brief, "ORIGINAL USER QUESTION (verbatim)". | :133-185 |
| `supervisor_subgraph` | The research loop (section 3). | |
| `final_report_generation` | Shim: if `aborted`, raise `RuntimeError`; else optionally write `Final_Report_*.md`; return `final_report`. | research_agent_full.py:70-113 |
| `subtopic_evaluation` / `subtopic_generation` | Optional extra reports, run only if `enable_subtopic_generation` (default **False**, config.py:368). Evaluator picks `GenerateSubtopicReport` / `EndSubtopicEvaluation`; reports are generated in parallel from all notes. | :132-276, route :279-283 |

The draft is a scaffold, not evidence. The final writer rejects a report identical to it (section 5).

## 3. Supervisor loop (the core)

**`supervisor` node** (:330-450): builds one byte-stable system prompt (`lead_researcher_with_multiple_steps_diffusion_double_check_prompt`, formatted with date, concurrency, iteration and time limits, target language, enabled sub-agent list; :296-328). One model call, timeout 420 s, **2 attempts**; fatal errors (quota/auth, :178-196) re-raise, otherwise a failed second attempt becomes a no-tool-call "ending gracefully" message. Increments `research_iterations`.

**`supervisor_tools` node** (:452-865). Order of work each turn:
1. Evaluate exit conditions (:501-540).
2. Run all `think_tool` calls (reflection, no side effects; returns the reflection).
3. Launch **all** sub-agent tool calls in parallel with `asyncio.gather(..., return_exceptions=True)`, each with timeout `subagent_timeout + 30` (:610-682).
4. Merge results: remap each sub-report's local citation codes to global `A{agent_id}-{local}` codes, dedupe by URL/identifier, append new entries to `source_registry` (:695-716).
5. Append a `ToolMessage` per call; every decision tool call must get one (`_complete_unanswered_tool_calls`, :233) or the next API call 400s.
6. Fold `CURRENT PROGRESS: You have been researching for X minutes.` into the last tool message (:824-835), so the history stays append-only.

**Exit to `write_final_report`** when any of: `research_iterations >= supervisor_max_iterations`; the supervisor made no tool calls; elapsed ≥ `strict_timeout_minutes` (= `research_time_max_minutes + 1`, run_config.py:167-173); or `ResearchComplete` called with no research calls and no denoise override (:534-541).

**Denoise gate** (:157-175, :526-530): if `ResearchComplete` is called while elapsed < `max_minutes` and the same-turn `think_tool` reflection contains `VERDICT: CONTINUE_RESEARCH`, `ResearchComplete` is **ignored** (answered with an explanatory tool message) and the loop continues. `ResearchComplete` alongside research calls is also ignored.

**Failure policy** (:747-822):
- Fatal error → set `aborted`, end (no report).
- All sub-agents failed with quota/429 → circuit breaker: abort.
- All failed otherwise → errors returned to the supervisor to decide.
- **Usage stop (added for `claudecode:` runs; the only deliberate change to these rules).** A usage-cap stop from the Claude-plan governor (`GovernorStop`, detected by its `governor_stop` marker, not by message text) is not a failure. With findings, the run goes to `write_final_report` instead of aborting, from the `supervisor` node or from `supervisor_tools` (including the all-sub-agents-failed case). With no findings it still aborts. If the cap also leaves no room for the model to write, the report is a labelled digest of the sub-agents' notes (no model call). The state carries `stopped_early` and `report_mode`. Runs without a governor never reach this. The state is also saved for `--resume` (`deep_research/resume.py`): the supervisor re-enters with its saved history, sources and iteration count, and the research clock continues from `stop_elapsed_minutes`; scoping is not repeated (`research_agent_full.build_resume_agent`).
- Other supervisor-level exception: 1st → redo the iteration; 2nd consecutive → **salvage** (write a report anyway) if any findings exist **and** elapsed ≥ `findings_salvage_time_fraction` (0.6) × `max_minutes`, else abort (:213-230, :792-812).

**Supervisor protocol, from the prompt** (prompts_open.py:223-440): each turn starts with `think_tool(purpose="denoise")` comparing the draft and findings, listing High/Open/Residual gaps and ending with `VERDICT: CONTINUE_RESEARCH | READY_TO_CONCLUDE | TIME_LIMIT`. Then optional `discovery=true` sub-agents (hard cap 2 per run, ≤ `max_concurrent_discovery` per turn), then normal research sub-agents (≤ `max_concurrent_researchers`). `ResearchComplete` must be accompanied by a denoise report in the same turn. If more than 40 % of the time budget remains, an early "satisfied" verdict is treated as premature.

**Supervisor tools**: `think_tool`, `ResearchComplete`, and one `Research<Platform>` per enabled agent: Web, General, Reddit, Substack, PubMed, Arxiv, SEC (state_multi_agent_supervisor.py:59-173). Each takes `research_topic`, `discovery`, optional `max_total_reads`. Default enabled: **Web only**.

## 4. Sub-agent loop (one per delegated topic)

State: `researcher_messages`, `search_results`, `articles_read`, `saved_articles`, `search_count`, `final_save_round`, `findings_log` (agents/base.py `ResearcherState`, state_research.py:30).

**Output modes** (:451-456): `sources` (default, config.py:376), `report`, `sources_inline`, `report_inline`. They pick the gather strategy: *curation* (save as you go) or *full_context* (read everything, deliver in the last turn).

**`llm_call`** (:639-835): picks the prompt (`BASE_AGENT_PROMPT` or `DISCOVERY_AGENT_PROMPT`, platform_prompts.py:14/59) plus mode, strategy and platform guidance; model timeout `llm_timeout`. On LLM error it returns an "encountered an error... here is what I have gathered" message with no tool calls, which ends the loop.
- **Tool hiding by budget**: search tools are removed once `search_count >= max_searches`; read tools once total reads hit `max_total_reads`; `batch_save_selected` once saves hit `max_saves`; `set_target_language` once the language is set.
- **Final-turn restriction** (full_context only): `sources` → only save/finish/language tools; `report` → **no tools**, the message is the report.
- **Turn plan / status text** (`_compute_turn_plan`, :520-634): iteration X/max, searches, reads, saves, "MAXED" caps, nudges ("FINAL TURN...", "FINAL SAVE ROUND..."), the read refs and (report_inline) the live source registry. This is folded into the last tool message each turn.
- **Iteration counting** (:494-517): a round counts only if it made at least one non-housekeeping tool call (`set_target_language` and `finish_research` are free).

**`tool_node`** (:838-1301): runs the turn's tool calls concurrently under a semaphore (`subagent_max_concurrency`).
- `finish_research` is a signal (not executed) → `compress_research`; `set_target_language`, `log_finding`, `list_saved` are intercepted.
- **Reads**: a per-iteration budget `min(max_reads, max_total_reads − already_read)` is divided across the batch read calls; `fetch_urls` is capped at 3 URLs per call; an exhausted budget returns "Read cap reached" text. Full text captured by a search is reused instead of re-fetching.
- **Searches** past `max_searches` return "Search cap reached".
- **`batch_save_selected`** saves by ref up to `max_saves`.
- Reads execute before saves within one turn.

**Routing**: `should_continue` (:1304) → `compress_research` if `finish_research` was called or no tool calls, else `tool_node`. `route_after_tools` (:1324) → `llm_call`, unless iterations ≥ `max_iterations`; in curation mode, if read items remain unsaved it grants **one** extra "final save round" (restricted to save/list/finish).

**`compress_research`** (:1412-1549): no LLM in `sources` mode. It builds `# Sources: <topic>` with `## n. title`, URL, "Why selected: <reason>". If the agent never saved anything but read something, it auto-selects the most recently read items (:1346). `report_inline` captures the agent's last message and rebuilds `## Sources` in code. `report` mode routes to `report_write` (`generate_report`, subagent_report_write.py:68), which skips when nothing was saved.

**Search/read tools** (agents/shared/tools.py, agents/web/tools.py): `think_tool` (:36), `perplexity_search`, `fetch_urls`, `list_saved`, `log_finding`, `set_target_language`, `finish_research`, `batch_save_selected`; web adds `tavily_search` and `exa_deep_search` (web/tools.py:40, :89). Platform tool sets are in `PLATFORMS` (base.py ~266). `WEB_SEARCH_ENGINE` defaults to `exa` (config.py:709).

## 5. Final report and citations

`write_final_report` (multi_agent_supervisor.py:870-1024):
1. `build_final_registry` (citation_utils.py:286): global `source_registry` entries keep their `A{id}-…` codes; `curated_sources` (sources-mode full text the supervisor never saw) are appended and given fresh `C1, C2…` codes; dedupe by lowercased URL.
2. Build the write instruction from `final_report_write_prompt` (prompts_open.py:747): a registry block (`[code] title (url)` + relevance) and the curated full text.
3. Messages = same system prompt as the supervisor + the full supervisor history (minus a trailing unanswered decision) + that instruction. Reusing the byte-identical prefix is a deliberate prompt-cache optimisation.
4. **Up to 2 attempts.** A report is rejected if empty, identical to the draft, or (Chinese-moderation mode) a refusal. The retry appends a "write the finished report, not the draft" message. Two failures → `RuntimeError`.
5. **`finalize_citations(raw, registry, renumber=True)`** (citation_utils.py:179-283), deterministic code: strip any `<CitationPlanList>` and any writer `## Sources`; keep only inline codes present in the registry; renumber to contiguous `[1..N]` in first-appearance order; rebuild `## Sources` as `[n] title (url)`; out-of-registry codes are dropped from Sources (the body is left as written). If no valid inline citation exists, the body is returned unchanged.

URLs in the final `## Sources` always come from the registry, never from the model.

## 6. Defaults vs the published benchmark run

| Setting | Default now | Benchmark run (README:70) | Ref |
|---|---|---|---|
| Research window | min 5, max 15 min; hard stop at max+1 | 15 min | config.py:281-282; run_config.py:173 |
| Supervisor iterations | 30 | 20 | config.py:291 |
| Searches per sub-agent | 3 | 3 (Exa) | config.py:302 |
| Sub-agent iterations / reads per iter / total reads / saves | 5 / 10 / 25 / 10 | not stated | config.py:295-301, 338 |
| Concurrency | 3 research, 2 discovery, 3 sub-agent tools | not stated | config.py:303, 311-312 |
| Timeouts | sub-agent 600 s, supervisor call 420 s | not stated | config.py:325, 329 |
| Salvage fraction | 0.6 | not stated | config.py:288 |
| Models | `deepseek-v4-flash` all roles | V4 Pro supervisor, V4 Flash sub-agents | config.py:274, 412, 427 |
| Search engine | `exa` | Exa | config.py:709 |
| Sub-agent output mode | `sources` | not stated | config.py:376 |
| Prompt set | `OPEN` (only supported) | — | config.py:278 |

The README calls the benchmark run a "historical reproducibility profile" (:80). Settings marked "not stated" cannot be confirmed from the repo.

## 7. Prompt inventory (verbatim sources for a port)

`prompts_open.py` (1176 lines): `transform_messages_into_research_topic_human_msg_prompt` :69, `lead_researcher_with_multiple_steps_diffusion_double_check_prompt` :223, `compress_research_system_prompt` :441, `compress_discovery_system_prompt` :515, `discovery_agent_prompt` :584, `final_report_write_prompt` :747, `draft_report_generation_prompt` :894, `SUBTOPIC_EVALUATION_PROMPT` :1030, `SUBTOPIC_GENERATION_PROMPT` :1086. `example_report` is empty (:1176).
`platform_prompts.py` (454 lines): `BASE_AGENT_PROMPT` :14, `DISCOVERY_AGENT_PROMPT` :59, gathering/strategy blocks :107-300, per-platform tool guidance :300-381, `CURATION_TOOL_GUIDANCE` :381, `REPORT_GENERATION_PROMPT` :399.

## 8. What a claude.ai port can and cannot copy

| Part | Port status |
|---|---|
| Prompts (section 7), brief → draft-first scaffold, denoise protocol and verdicts, gap taxonomy | Copy as skill text. Faithful. |
| `finalize_citations`, `build_final_registry`, code remapping | Deterministic code (471 lines). Faithful only if the claude.ai sandbox can run bundled Python (unverified); otherwise approximated by instruction. |
| Loop control: exit conditions, denoise gate, ResearchComplete rules, discovery cap | Expressible as instructions; enforced by the model, not by code. Approximate. |
| Budgets: per-agent search/read/save caps, per-iteration read budgets, tool hiding | Instruction-only; no hard enforcement. Approximate. |
| Parallel sub-agents, semaphores, wall-clock timers (15 min + 1), per-call timeouts | No equivalent in a single chat turn sequence. Not reproducible; needs a stated substitute. |
| Failure policy: retries, circuit breaker, salvage fraction, fallback model chains | No equivalent. Not reproducible. |
| Prompt-cache-stable history, structured outputs, observer/trace events | Engine internals; not part of behaviour. Out of scope. |
| Isolation: each sub-agent has its own context and sees only its topic | A single chat holds everything in one context. Behavioural difference to decide on. |
| `sources` mode: the supervisor sees only titles and reasons; the full text goes straight to the final writer | The chat model would see everything unless the port restricts it. Behavioural difference to decide on. |

## 9. Not read yet (so this map is incomplete here)

- Contents of the prompts (only structure and headings of the lead prompt were read).
- Platform tool internals for Reddit, PubMed, arXiv, SEC, Substack, and `exa_deep_search` / `tavily_search` parameters.
- `llm_repair_citations` / `ensure_report_citations` (citation_utils.py:371-471): where they are called is unverified.
- `tool_node` lines 946-1300 beyond the dispatch rules listed above; `model` fallback behaviour (`models.py`); the observer/event schema.
