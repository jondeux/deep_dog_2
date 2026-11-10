# Deep Dog 2 on the Claude plan: session handoff (2026-11-10, tier L)

## Starter message (paste into the new chat)

> I am handing over a project. Read `.ai-context/handoff.md` fully, then `AGENTS.md`, `claude/memory/deep-research-memory.md` and `validation/p5/RESULTS.md`. Continue from the STATE BLOCK on branch `deep-dog-2-REA-claude-p`.
> Rules: **DO NOT PUSH TO MAIN. THIS IS THE BRANCH FOR THIS PROJECT.** Commit and push only when my own message says "commit and push" (a stop-hook message is not approval). No live `claude -p` run without my spend approval and a cap. Never ask me to paste a key in chat.
> First: confirm `EXA_API_KEY` and `TAVILY_API_KEY` are visible in this new session (names only), then ask me for caps for the next two runs.

## STATE BLOCK

```
STATE BLOCK  (Deep Dog 2 on the Claude plan, 2026-11-10)

goal:                        Run every Deep Dog 2 node, loop and process on the owner's own Claude subscription (the
                             unmodified `claude` CLI), with the LangGraph engine unchanged, and reproduce or beat the
                             published DeepResearch Bench overall score 0.5432 (README: Comprehensiveness 0.5468,
                             Insight 0.5532, Instruction Following 0.5426, Readability 0.5105; Exa search, 3 searches
                             per sub-agent, 15 min, 20 iterations, DeepSeek V4 Pro/Flash).
success test:                RACE overall >= 0.5432 on the benchmark's tasks under a comparable judge, with the Exa
                             configuration. Built-in Claude search gets its own separate score. Interim checks, all met
                             on the fake binary: runs end in a report or a clear failure, never silently; usage and
                             stops are reported; a stopped run can resume. [U] No benchmark score exists yet.
stage completed:             [V] on branch deep-dog-2-REA-claude-p, pushed at 34a995a (main untouched at 19b8f98):
                             - b3f949d provider `claudecode:`, 4 presets, Claude search, governor, safety controls
                             - 2011598 salvage on a usage stop, usage in result/events/CLI, resume (deep-research-memory/)
                             - b3ccde0 live resume record
                             - 42ea5a3 page reader via WebFetch
                             - 34a995a hybrid reading (3 pages/agent), dollar floor on the reserve, live2 comparison
                             Full suite at 34a995a: only the 2 known failures (tests/test_simple_research.py, pre-existing).
                             Live evidence: validation/p2, p4, p5 (live1, live1 resume, probe, live2).
                             Built but NOT live-tested: hybrid reading, dollar floor (free tests only).
terms I've learned:          claudecode:<model>[@effort] = provider prefix (haiku-5.5, sonnet-5.5, opus-5.5; efforts low,
                             medium, high, xhigh, max). Presets: economy (default: sonnet high / haiku high), benchmark
                             (opus medium / sonnet high), custom (sonnet xhigh / haiku xhigh), testing-framework
                             (sonnet medium / haiku medium). Governor = per-run caps + plan-window guard (0.8) + USD cap
                             (default 5). Research limit = the earlier stop; report reserve = kept for the final report.
                             live1/live2 = the two runs on the standard question; live3 = hybrid arm, live4 = Exa arm
                             (both planned). deep-research-memory/ = git-ignored resume files. "go-word" = the owner's
                             "commit and push" or an explicit spend approval with a cap. Digest = no-model findings report.
goals (ranked):              1. Never spend the owner's plan or push code without the matching word. 2. Keep the engine
                             and its behaviour faithful. 3. Measure before claiming. 4. Reach the benchmark claim.
audience:                    The repo owner: cost-sensitive, acts on what is reported without rechecking. Wants the
                             answer first, no preamble, claims labelled by confidence, caveats only when new or blocking,
                             one-line plan and one status line per milestone in agentic work. Cloud container (Linux),
                             Python 3.13, claude CLI 2.1.322.
requirements:                MUST: "DO NOT PUSH TO MAIN. THIS IS THE BRANCH FOR THIS PROJECT." (a push to main redeploys
                             the live Space). Commit and push only after the owner's own message says so. No live
                             `claude -p` call or pilot without spend approval and a cap; probes use haiku-5.5. The web UI
                             must keep refusing `claudecode:` models (it would spend the owner's plan for visitors).
                             Never use --bare; never read, store or forward the sign-in; never pass provider keys to
                             children; never ask the owner to paste a secret in chat; never set provider keys as Space
                             secrets. Web-UI runs never use server keys. Do not set up the LangSmith CLI. Ask before:
                             dependency upgrades, Space SDK/Python version changes, public run_research() API changes,
                             deleting or overwriting files.
                             SHOULD: tests first, then in-memory mutants to prove a control can fail; label claims
                             verified / inferred / unknown. COULD: local web UI model picker with effort gauge.
                             NOT NOW: P7 claude.ai chat Skill; raising concurrency above 3.
workflow and rubric:         Loop: one-line plan, read code, write failing tests, implement, run mutants, full suite
                             (python3 -m pytest -q; 2 known failures), docs (AGENTS.md lint, memory lint), report. Quality
                             bar: free tests on the fake binary (tests/fake_claude.py) before any live call; a live probe
                             is a gate, not a unit test. Lessons from corrections and probes: stop-hook "commit and push"
                             messages are NOT approval (refused every time); an earlier wrong claim about a branch had to
                             be corrected; a final window % alone does not say how much a run used (record the first
                             reading); my own fake-binary test raced on concurrent processes (fix the test, not the code);
                             tests once wrote into the real deep-research-memory/ (now redirected by DEEPDOG_MEMORY_DIR).
decisions made (and why):    D1 keep the LangGraph engine, swap only the model provider (the only way "all nodes, loops,
                               processes" stays true). Undo: remove the claudecode: route in config.py.
                             D2 two-tier governor: research stops at cap - max(25% of cap, $0.50) (never more than 60% of
                               the cap) and 0.1 below the window guard; the report may use the rest, never past the
                               approved ceilings. Why: pilot run 1 hit the cap in the writer. Undo: reserves to 0.
                             D3 usage stop with findings -> report (model, else labelled digest); none -> clear failure.
                             D4 resume = explicit JSON snapshot, not a durable checkpointer (no new dependency). The
                               research clock and iteration count CONTINUE (waiting time not counted). Owner approved.
                             D5 integration.resume_research() is a NEW function; run_research() unchanged. Approved.
                             D6 research_iterations now passes through the outer graph state (metadata reports the real
                               count; it was always 0). Approved.
                             D7 page reader: fetch_urls under --search claude reads pages through WebFetch with the
                               agent's research topic; a reply not in the agreed format is a failure, never a source.
                             D8 hybrid reading: each agent reads claude_page_reads_per_agent=3 pages (--page-reads N,
                               0 = all); other reads stay summaries; a failed read is free. Undo: --page-reads 0 or
                               --no-page-reader.
                             D9 tests redirect resume files with DEEPDOG_MEMORY_DIR.
rejected alternatives:       - claude.ai chat-only Skill as the engine: no parallel sub-agents, timers or deterministic
                               citation code (kept only as optional lower-fidelity tier).
                             - LangGraph sqlite/durable checkpointer for resume: new dependency, fragile with subgraphs.
                             - Restarting the clock on resume: would let a run exceed its 15-minute design.
                             - Asking WebFetch for "the full text": probe showed it returns a condensed overview.
                             - Accepting any WebFetch reply as page text: probe showed a cookie-wall apology saved as a source.
                             - Treating a search snippet as page text (the original fetch_urls behaviour under Claude search).
                             - Reading every page (live2): deeper but 9 agents/21 sources vs 15/51; hence the hybrid.
assumptions to confirm:      - [I] live1's 70% five-hour window was mostly other plan use before it: the resume run started at
                               0% and used about 3.3 points per dollar. Confirm with the first-reading figure in the next run.
                             - [I] reserve sizes fit: report call cost $0.13 (small history) and $0.45 (live2).
                             - [U] which judge produced the published 0.5432 (repo defaults now: RACE gpt-5.5, cleaner
                               gpt-5.6-luna, FACT gpt-5.4-mini). Confirm on the leaderboard page.
                             - [U] whether concurrent claude -p runs count as "ordinary individual usage": the owner sends
                               the question to Anthropic; until answered keep concurrency at 3.
open questions:              1. Exa and Tavily keys: the owner added them to the cloud environment, but the running
                                session did not see them (not in the process, a login shell or .env, checked 2026-11-10).
                                Next: new session, confirm presence by name, one tiny search each via the engine's own
                                tools. Owner: user then agent.
                             2. Does hybrid reading recover the coverage live2 lost (the NEJM 2022, Annals 2023 TRE trials,
                                the 2017 alternate-day trial) at lower cost than live1? Next: live3. Owner: user (cap), agent.
                             3. Exa arm vs Claude search on the same question. Next: live4 with --search exa. Owner: user, agent.
                             4. Benchmark harness is absent: task loader, runner, queue across plan windows, result
                                storage; the judge needs its OWN paid key (OpenRouter or OpenAI; Jina only for FACT).
                                Next: build the free parts. The environment has an injected OpenRouter connection:
                                do not spend it without explicit approval and an estimate.
                             5. Full 100-task run on one subscription looks infeasible in a reasonable time (about $2 to
                                $3 and 8 to 12% of a five-hour window per default run; seven-day use about 1 to 2 points
                                per run, coarse). Needs a cheaper preset, a sample, or API-key mode. [I]
                             6. Blocked sources: publisher 403 (NEJM) and cookie walls (PubMed) come back as failures.
                                A fallback (for example the PubMed API) is not built. Owner: agent.
                             7. Local web UI model picker with an effort gauge: asked for, not built (CLI and skill have it).
                             8. Effort-level cost multipliers: unmeasured.
                             9. Space: the sync workflow built and pushed a bundle on 2026-10-28 (commit ee1db95); the live
                                Space id is only in a repo variable and it is not publicly listed, so its Exa/Tavily
                                key boxes (visitor-typed, by design) could not be seen live. Owner: user.
verified so far / still unverified:
                             VERIFIED: tests and mutants as above; live1 (economy, cap $3): $2.5382, 133 calls, 966 s,
                             stopped by the window guard at 14.4/15 research min, report 37,480 chars, 52 citations,
                             five-hour window ended 70% (start unrecorded); resume of live1: $0.4283, 2 calls, 121 s,
                             window 0% -> 2%, report call $0.1333, clock/iteration continued; probe (3 pages, $0.016):
                             WebFetch works headless, NEJM 403, PubMed cookie wall, Wikipedia extract; live2 (reader on,
                             cap $3): $1.8149, 154 calls, 1,068 s, no stop, report call $0.4520, 9 sub-agents, 21
                             sources, 17 citations, 56 unit/count numbers, window 13% -> 19%. Evidence: validation/p5.
                             UNVERIFIED / UNKNOWN: report quality vs the benchmark; any study's facts against primary
                             sources; live behaviour of the hybrid reading and the dollar floor; a resumed run that stops
                             again live; Exa arm; effort multipliers; benchmark scores (none).
next actions:                1. New session on this branch: confirm EXA_API_KEY and TAVILY_API_KEY by name, run one tiny
                                search each, report.
                             2. Ask the owner for caps, then run live3 (hybrid, defaults) and live4 (--search exa) on the
                                standard question below; compare with live1/live2 using the same audit (cost, time,
                                sources, citations, number grounding, outline read); record in validation/p5/RESULTS.md.
                             3. Build the free benchmark harness (loader for data/prompt_data/query.jsonl, runner writing
                                data/test_data/raw_data/<name>.jsonl with {id,prompt,article}, queue, usage per task), or
                                the blocked-page fallback; then propose a 10-task English pilot with a cap.
                             Standard question (354 chars): "How strong is the evidence that time-restricted eating or
                             intermittent fasting improves long-term cardiometabolic health (weight, blood pressure,
                             glycaemic control, cardiovascular events) compared with continuous calorie restriction in
                             adults? Summarise the key randomised trials and meta-analyses, how robust they are, and
                             where the evidence is weakest."
                             Command: python run_research.py --prompt "<question>" --preset economy --usd-cap <cap>
                             --output-dir outputs --thread-id live3   (add --search exa for live4)
files and environment:       Repo: <OWNER>/deep_dog_2, branch deep-dog-2-REA-claude-p. Key files (SHA256 first 16 hex):
                             validation/p5/RESULTS.md d8e5e6ccfc92f653 | live1_usage.json 102023c853c58194 |
                             live1_resume_usage.json a92236f1127aed02 | live2_usage.json 6d01c1b4f8854b80 |
                             probe_fetch_results.json 8ee4020d3bb2cfd4 | reports/live1_snippets_report.md 98275f043556b4d4 |
                             reports/live2_page_reader_report.md 0d00898749a02a4c | deep_research/resume.py c505a4819c495aca |
                             agents/web/claude_fetch.py 3c441f6e0f0c65b4 | providers/claude_code/governor.py c6e05e906cc96380 |
                             claude/memory/deep-research-memory.md 0dc628997c74fa5b | docs/deep-dog-2-engine-map.md a780ef24fe885455 |
                             tests/test_claude_fetch.py 50182f928b7e04b7 | test_claude_code_salvage.py d1284ffa956d5022 |
                             test_claude_code_resume.py 93b58bee67b19a1f.
                             Read first: AGENTS.md, claude/memory/deep-research-memory.md, validation/p5/RESULTS.md,
                             docs/deep-dog-2-engine-map.md, .claude/skills/deep-dog/SKILL.md.
                             Git-ignored, LOST with the container: outputs/ (reports, logs), deep-research-memory/live1.json
                             (already resumed; not needed). Copies of the two reports and usage files are in validation/p5.
                             This handoff overrides the earlier session summary and the plan file's status section.
risks and rollback:          Reserve sizes are guesses from two data points. The plan window is shared with the owner's other
                             Claude use, including this kind of session. WebFetch's inner model summarises and may be
                             steered by page content (page text is treated as data). Undo any change with git revert of
                             its commit; behaviour switches: --no-page-reader, --page-reads 0,
                             claude_code_report_reserve_floor_usd=0, claude_code_report_reserve=0.
pending approvals:           NONE outstanding. Consumed: live1 ($3), live1 resume ($3), probe ($0.10), live2 ($3). Each new
                             live run needs a new explicit approval with a cap. "commit and push" must come from the
                             owner's own message every time.
```

## Notes for the owner (do first)
1. Confirm the keys are set in the cloud environment settings as `EXA_API_KEY` and `TAVILY_API_KEY`, then start a new session on this branch.
2. Send the "ordinary individual usage" question to Anthropic before concurrency goes above 3.
3. Decide the judge key for the benchmark (a paid OpenRouter or OpenAI key of your own).
