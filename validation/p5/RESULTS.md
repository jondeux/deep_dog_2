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
- **Resume was not run live at this point.** It was, the next day: see the last section.
- One run, one question, one preset. No variance, no comparison with the Exa configuration.

## Live resume of `live1` (2026-11-10, owner-approved, `--resume live1 --usd-cap 3.00`)
Usage file: `live1_resume_usage.json`. All **verified** from the run's own output unless marked.
| | |
|---|---|
| Restore | Log: "Resuming run live1 (stopped by plan_window_guard, 14.399 research minutes in)". The first supervisor line read "Iteration 6 \| 14.4 min elapsed": the iteration count and research clock continued, scoping was not repeated |
| Outcome | `completed`, no further stop; no new resume file (a normal finish saves nothing, `live1.json` unchanged) |
| Cost | $0.4283 over 2 calls (both sonnet-5.5): one supervisor call $0.2949, then the final-report call **$0.1333** |
| Time | 121 s |
| Window | five-hour 2 % at the end, **0 % at the first call** (window had reset); seven-day 19 %, 18 % at the first call |
| Report | 32,890 characters, 53 distinct citations, Sources section, from the restored history of 28 messages |

What this settles and does not:
- **Resume works live**: save on a real stop, load in a new process, continue clock and iteration count, finish with a report.
- **Final-report call cost: $0.13 once, on a 62k-token history.** The 25 % USD reserve of a $3 cap is $0.75, so it looks generous for sonnet-5.5.
  One data point, one preset (not opus, not a larger history): not a basis for lowering it yet.
- **Window baseline**: this run started from 0 %, and two sonnet calls ($0.43) used 2 %. That is about 5 % per dollar, so $2.54 would be roughly 12 %, not 70 %.
  **Inferred:** the 70 % of the first run was mostly use of the shared plan by other work before and during it. Rates vary by model mix, so treat it as a rough check.
- Not tested: a resume that itself stops again live (chained stop is covered by the free test only).

## Page reader probe (2026-11-10, owner-approved: 3 pages, haiku-5.5, hard cap $0.10; spent $0.016 in total)
Script `probe_fetch.py`; results `probe_fetch_results_v1.json` (first reader) and `probe_fetch_results.json` (fixed reader). **Verified** from those files.
| Page | v1 (asked for "the full text") | v2 (research topic passed, strict reply format) |
|---|---|---|
| nejm.org (NEJM trial) | HTTP 403, reported as text | same: clean failure text, $0.0005 |
| pubmed.ncbi.nlm.nih.gov | **false success**: the model's apology about a cookie notice was returned as page content | clean failure: "only a cookie-requirement notice", $0.0006 |
| en.wikipedia.org | 2,045 chars of a "condensed overview"; the tool said it read 100,000 of 145,479 characters | 1,854 chars, an extract focused on the topic with quotes; it states the article gives no enrolment counts; $0.0072, 19 s |

What the probe settles:
- **WebFetch works headless on the plan sign-in** and passed a page that plain `httpx` got a 403 for earlier (Wikipedia).
- **WebFetch is not a raw-text fetcher.** Its own small model answers a prompt about the page, so "give me the full text" returns an overview.
  The reader therefore passes the agent's research topic and returns a focused extract. Much richer than a 200-character search summary, but not the whole page.
- **Two fixes came from the probe**: the topic is passed in, and a reply that is not in the agreed format is a failure (never a source).
- **Still blocked:** publisher pages behind a 403 (NEJM) and pages behind a cookie wall (PubMed). The agent is told why and tries another source.
  Not tested: how much this raises report quality or run cost (about $0.0006 to $0.007 per page here; a full run would make dozens of reads).
