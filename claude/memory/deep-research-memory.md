# Deep research memory (runs on the Claude plan)

Facts about how a run that a usage cap stopped early is saved and continued. Rules live in AGENTS.md; this file records
what the code does and what is measured. Run files themselves are in `deep-research-memory/` (git-ignored: they hold the
question and the findings; `DEEPDOG_MEMORY_DIR` moves the folder, and the tests use it so they never write there).

## How it works
- A `claudecode:` run that ends because a cap was reached (USD cap or plan-window guard) still writes its report from the
  findings gathered so far (`write_final_report`; a labelled digest if the model has no budget left). It also saves its resume
  state: the question, the supervisor's history, the sources, the iteration count and the minutes on the research clock.
- The state is plain JSON, `deep-research-memory/<root-id>.json`, one file per chain of runs. The file it replaces is kept
  once as `<root-id>.prev.json`. No credentials and no sign-in are stored. Ids accept letters, digits, `.`, `_`, `-` only.
- Continue with `python run_research.py --resume <root-id>` after the plan window has reset. Scoping (brief, draft) is not
  repeated. The research clock continues from the minute it stopped at, so the waiting time is not counted. The iteration
  budget continues too. Models, search and limits come from the file; only `--usd-cap` and `--plan-window-max` may change.
- A resumed run gets a fresh USD cap, because the cost counter starts at zero. The plan-window guard reads the real window.
- A resumed run that stops again saves a new state under the same root id (`resumes` counts the hops); its thread id is
  `<root-id>-r<n>`. A run that finishes normally saves nothing.
- No file is ever deleted by the code. Clean up `deep-research-memory/` by hand.

## Where it is in the code
`deep_research/resume.py` (format, validation, file sink), `integration.resume_research`, `research_agent_full.build_resume_agent`,
`run_research.py` (`--resume`, `--memory-dir`), events `run_stopped_early` (`resumable`) and `usage_updated`.

## Status
- Verified by free tests on the fake `claude` binary (tests/test_claude_code_resume.py, 13 tests, 5 mutants caught): save on
  stop, JSON round trip, history restored, clock and iteration count continue, chained stops, refusal of bad ids and versions.
- Verified live (2026-11-09/10, `live1`): a real stop saved a resume file, and `--resume live1` in a new process continued the clock (14.4 min) and iteration count (6) and finished with a report. Not verified live: a resumed run that stops again (free test only).
- Not built: waiting for the window reset automatically (`rate_limit_event` carries the reset time; the adapter ignores it).

## Measurements to fill in from live runs
Facts only, one line each with date and run id. Until then the reserve sizes (`claude_code_report_reserve` 0.25,
`claude_code_window_reserve` 0.10) are guesses.
- 2026-11-09, run `live1` (economy, $3 cap, default size): $2.5382 over 133 calls in 966 s; stopped by the plan-window guard at 14.4 of 15 research minutes (iteration 5 of 30); report synthesised, 37,480 chars, 52 citations; plan window ended at five-hour 70 % / seven-day 18 %. Start of the window was not recorded. Details: `validation/p5/RESULTS.md`.
- Since that run the usage file also records `util_first` (window at the first call) and `by_phase` (research vs report cost). The next live run sizes the report reserve from `by_phase`.
- 2026-11-10, `--resume live1` (cap $3): $0.4283 over 2 calls in 121 s; the final-report call cost $0.1333 (62k-token history, sonnet-5.5); window 0 % at the first call, 2 % at the end; report 32,890 chars, 53 citations. Details: `validation/p5/RESULTS.md`.
- 2026-11-10, `live2` (page reader on, cap $3): $1.8149 over 154 calls in 1,068 s, completed with no stop; final-report call $0.4520; window five-hour 13% to 19%, seven-day 20% to 21%. 9 sub-agents and 21 sources against live1's 15 and 51. Report: more primary figures, fewer citations (17 vs 52). Details: `validation/p5/RESULTS.md`.
- Reserve: the report call cost $0.13 (small history) and $0.45 (live2). A 25% reserve covers it at a $3 cap but not at a $1 cap; a dollar floor is an open item.

## Guards added 2026-11-10 (free tests only; not yet run live)
- **Report-cost guard** (`deep_research/report_budget.py`, `Governor.report_budget_now`, `write_final_report`): before the final-report call the engine projects its cost (input tokens = characters / 2.4, 25k output tokens, the model's own measured price from its research calls, x1.25 margin) against the budget = the room left under `--usd-cap`, or `--report-budget USD` (`claude_code_report_budget_usd`) if lower. Over budget: per-source text is shortened first (sources under 300 characters are dropped instead); if even the bare call is too dear the findings digest is returned (stop reason `report_budget`, resumable: `--resume <id> --report-budget <higher>`). The owner's budget also sets how much research leaves behind (reserve = budget, at most 60 % of the cap). The decision is in the usage file as `report_plan`.
- Calibration (two real report calls): $0.589 for 114.9k input and 16.7k output tokens, $1.329 for 289.6k and 20.7k; list price works out near $3 per million input tokens and $15 per million output tokens for sonnet-5.5. Inferred, not run: on `live4b` the guard would have projected about $1.5 against $1.08 of room and trimmed the source text.
- **Search fail-fast guard** (`deep_research/search_health.py`, `RunConfig.search_failure_limit`, default 6, 0 = off): after that many Exa or Tavily failures in a row (HTTP or transport errors; empty results do not count) the search tools stop calling the network and the supervisor, before its next model call, writes a report from the findings it has (stop reason `search_unavailable`, resumable) or fails with a message naming the backend. Why: `live4` made 80 Exa calls that all returned 401 and still ended "completed" with a report from memory ($2.69).
- Claude search (`--search claude`) is not covered by the search guard.
- **Dynamic reserve** (built 2026-11-10, free tests only; default on, `--static-reserve` / `claude_code_dynamic_reserve=False` restores the fixed reserve): before each supervisor turn, `Governor.dynamic_reserve_check` stops research when `spent + next round + projected cheapest useful report >= usd_cap`. The next round is what the previous round cost (10 % of the cap before one is measured); the cheapest useful report is the projection with each source's text cut to 1,500 characters (`report_budget.minimal_report_cost`); an owner `--report-budget` caps that term. Stop reason `report_reserve`, same salvage path as the other stops (a report from the findings, resumable). The old static limit stays as a backstop at 90 % of the cap. Why: `live5` stopped research at $0.52 of a $1 cap and its history (149 sources) projected a $0.92 report against $0.48 of room, so it returned the digest. Not run live: whether a $1 cap now ends in a model-written report is inferred.
- **Mid-round limit** (built 2026-11-10 after `live6t`, free tests only): `live6t` (Tavily, $1 cap) ended at $1.0967, over the cap, because one research round (3 parallel sub-agents, about $0.85) ran between two checks. After each supervisor-turn check the governor now also applies the projected cheapest report to its per-call research limit: calls stop at `cap - cheapest report - concurrency x average research call` (`Governor._dynamic_research_limit`, `limits()["dynamic_research_limit"]`, `stats()["reserve_check"]["research_limit_usd"]`). The report phase is not held to it. The first-round allowance is now 5 % of the cap per parallel sub-agent (a guess: 15 % at the default concurrency of 3). Consequence: at a $1 cap the cheapest report is about $0.57, so research gets only what is left after scoping ($0.24 in `live6t`); the run ends in a thin model-written report or, if that does not fit, the digest, but should no longer pass the cap by a whole round. Calls already in flight when the limit is reached still finish, which the margin covers on average, not as a bound.

