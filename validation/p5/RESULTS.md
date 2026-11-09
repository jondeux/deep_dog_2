# P5: first default-size live run on the Claude plan (2026-11-09)

One run, approved by the owner ("live default-size run with --usd-cap 3.00"). Labels: **verified** = read from the run's own output,
**inferred** = my reading, not measured. `live1_usage.json` is the run's own usage file (copied from `outputs/`, which is git-ignored).

## Setup
`python run_research.py --prompt <354-char question on intermittent fasting vs continuous calorie restriction> --preset economy --usd-cap 3.00 --thread-id live1`.
Economy = sonnet-5.5 high (supervisor, draft, final writer), haiku-5.5 high (sub-agents), Claude's own WebSearch via haiku-5.5 low. Engine defaults:
30 supervisor iterations, 15 research minutes, 3 researchers and 2 discovery agents at a time. Plan-window guard 0.8, so research stops at 0.70.

## Result (verified)
| | |
|---|---|
| Outcome | `completed`, stopped early by `plan_window_guard` during research; report synthesised by the model |
| Research clock at the stop | 14.4 of 15 minutes (the stop came about 0.6 minute before the time limit) |
| Supervisor iterations | 5 of 30 allowed (about 2.6 minutes each, so the 15-minute window binds long before the iteration cap) |
| Wall time | 965.7 s |
| Cost (list-price estimate) | $2.5382 over 133 calls, 84.6 % of the $3.00 cap |
| By model | sonnet-5.5: 10 calls, $1.6936 (66.7 %). haiku-5.5: 81 calls, $0.1993. haiku-5.5 + WebSearch: 42 calls, $0.6453 |
| Plan window at the end | five-hour 70 %, seven-day 18 % |
| Report | 37,480 characters, 52 distinct citations, Sources section built in code, one writer attempt (no retry) |
| Sources | 51 logged by the observer, 82 curated |
| Tool use | 42 `claude_web_search`, 43 `fetch_urls`, 0 tool errors, 0 protocol repairs, 0 sub-agent failures |
| Governor stop effect | 3 sub-agent model calls refused after the stop; those agents wrapped up with what they had |
| Resume state | `deep-research-memory/live1.json`, 240 KB. Loads and restores offline: 28 supervisor messages, 5 iterations, 82 curated sources, clock 14.4 min |

## What this shows
- The salvage path works on a real run: the stop did not end the run as `partial` with no report (as pilot run 1 did).
- The binding limit was the plan window, not the USD cap. The USD research limit ($2.25) was not the reason for the stop.
- Run size: this is a full-size research phase (the stop was 0.6 minute before the natural end), at about $2.5 and 16 minutes.

## What it does not show
- **The window baseline is unknown.** This run did not record where the five-hour window stood at the first call; the plan is shared with other
  work, including the orchestrating session. **Inferred:** the pilot used 3 to 8 % per $0.55, which would give 14 to 37 % for $2.54, so roughly 33 to 56 %
  was likely already used before the run. Treat "one run uses 70 % of the window" as false until the first reading is on record.
  Since this run the usage file records `util_first` (first reading per window).
- **The final-report call's own cost is not isolated.** Sonnet's 10 calls include brief, draft, 5 supervisor turns and the writer. Since this run the usage
  file records cost by phase (`by_phase`), so the next run measures the reserve directly. The 25 % USD and 0.1 window reserves are still unmeasured guesses.
- **Quality is not measured.** Only the structure of the report was checked (citations resolve, sources listed). No facts were verified, and there is no
  benchmark comparison.
- **Resume was not run live.** The window has not reset, and the $0.46 left of the approved cap would not cover a resumed segment (the supervisor call alone
  carries about 60k tokens of history). A live resume needs a window reset and a new spend approval.
- One run, one question, one preset. No variance, no comparison with the Exa configuration.
