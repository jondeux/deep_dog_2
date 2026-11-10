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
