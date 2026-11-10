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

## Page reader on vs off, same question (`live2` vs `live1`, 2026-11-10, owner-approved, cap $3.00)
Same question, economy preset, `--search claude`, defaults; only the page reader differs (`live1` = summaries only, `live2` = reader on). One run each.
Usage: `live2_usage.json`; reports: `reports/`. Figures are **verified** from the usage files and logs; the reading of the reports is my judgement (see limits).
| | live1 (summaries) | live2 (page reader) |
|---|---|---|
| Outcome | stopped early by the window guard at 14.4 min, report written | completed, research clock reached 15.5 min, no stop |
| Cost (list price) | $2.5382, 133 calls | **$1.8149**, 154 calls |
| Wall time | 966 s | 1,068 s |
| Supervisor iterations | 5 | 4 |
| Sub-agents | 15 (about 70 s each) | 9 (185 to 290 s each) |
| Sources logged | 51 | 21 |
| Report | 28,459 chars body, 52 distinct citations | 28,274 chars body, 17 distinct citations |
| Unit/count numbers in the report | 33 | 56 |
| By model | sonnet 10 calls $1.69; haiku 81 calls $0.20; haiku+WebSearch 42 calls $0.65 | sonnet 7 calls $1.18; haiku 53 calls $0.16; WebSearch 19 calls $0.27; **WebFetch 75 calls $0.21** |
| Final-report call | not isolated | **$0.4520** (research $1.3629) |
| Window | five-hour 70 % at the end, start not recorded | five-hour 13 % to 19 %, seven-day 20 % to 21 % |

Reading of the two reports (one reader, the same model family that built the engine, so not independent; no judge, no reference report):
- **Evidence quality is higher with the reader.** The `live2` summary rests on primary figures with intervals (a 2025 BMJ network meta-analysis, 99 trials, 6,582 adults; ADF 1.29 kg more than calorie restriction, 95 % CI; a pooled blood-pressure difference with CI; a 12-month TRE trial), and it says which trials it could not verify.
  `live1` states several effect sizes "press-reported" and says some intervals came only from abstracts or news coverage.
- **Coverage is lower.** `live2` has 17 distinct citations to `live1`'s 52 and omits trials that `live1` covered (the NEJM 2022 and Annals 2023 TRE trials, the 2017 alternate-day fasting trial), saying it could not verify them. The probe showed why: publisher pages return 403 and PubMed returned a cookie wall.
- **Fewer sources because each agent is slower.** Reads take time; 9 agents finished in the same 15-minute window where 15 did before. Cost fell mostly because fewer rounds ran, not because a read is cheap.
- **The number check is not comparable.** `live1` was checked against its full saved state (nothing absent). `live2` saved no resume file, so only the agents' notes and titles were available and 12 of its 56 numbers were not found there. That says the check had less material, not that the numbers are wrong.

What it does not settle: one run each, no variance; no judge; I did not verify any study's facts against primary sources; whether "fewer, deeper sources" scores higher on a benchmark such as RACE is unknown.

Measurements for the reserve and the window:
- **Report call $0.45** (`live2`) and $0.13 (resume of `live1`, a smaller history). The 25 % reserve of a $3 cap ($0.75) holds, but at a $1 cap it would be $0.25, below the $0.45 measured. **Open:** give the reserve a floor in dollars.
- **Window:** `live2` used 6 points of the five-hour window for $1.81 (about 3.3 points per dollar) and 1 point of the seven-day window. That is consistent with `live1`'s 70 % having been mostly a high starting level, and puts a full-size run at roughly 8 to 12 % of the five-hour window.

## Hybrid reading, and a failed Exa arm (`live3`, `live4`, 2026-11-10, owner-approved, $3 cap each)
Same question and economy preset as `live1`/`live2`. Usage: `live3_usage.json`, `live4_usage.json`; reports in `reports/`. Figures are **verified** from the usage files and logs.

### live3: hybrid reading (3 pages per agent), `--search claude`
| | live1 (summaries) | live2 (read all) | **live3 (hybrid)** |
|---|---|---|---|
| Outcome | window-guard stop, report written | completed | **completed, no stop** |
| Cost (list price) | $2.5382 / 133 calls | $1.8149 / 154 | **$2.5559 / 183 calls** |
| Wall time | 966 s | 1,068 s | **1,381 s** |
| Sub-agents | 15 | 9 | **12** (149 to 315 s each) |
| Sources logged | 51 | 21 | **34** |
| Distinct citations | 52 | 17 | **27** |
| Final-report call | not isolated | $0.4520 | **$0.5895** (research $1.9664) |
| Five-hour window | 70 % at end, start unrecorded | 13 % to 19 % | **1 % to 9 %** |
| By model | | | sonnet 8 calls $1.6859; haiku 72 $0.2260; haiku+WebFetch 72 $0.1885; haiku+WebSearch 31 $0.4555 |

- 31 Claude searches, 38 `fetch_urls`, no tool errors or protocol repairs matched in the log. The Exa warning at start is the module default (`WEB_SEARCH_ENGINE=exa`); `--search claude` is the per-run setting, and no Exa call was made.
- Hybrid sits between the other two on sources and citations. It brought the NEJM 2022 trial back; the Annals 2023 trial and the BMJ network meta-analysis are not named in the report. Cost was not lower than `live1`; wall time was the longest of the three.
- Window: about 4.4 points of the five-hour window per dollar, in line with the earlier 3.3 to 5.
- Reserve: report call $0.59 against a 25 % reserve of $0.75 at a $3 cap. Holds at $3; at a $1 cap the dollar floor is what covers it (not run live).
- Limits: one run per arm, no judge, no facts checked against primary sources. My unit-number count (153) used a different pattern than the earlier counts and is not comparable.

### live4: `--search exa`: **INVALID, no search results (do not use as an Exa measurement)**
Run with `EXA_API_KEY=dummy` in the environment, expecting the proxy to inject the real key.
- **All 80 `exa_deep_search` calls returned HTTP 401 `INVALID_API_KEY`**; 0 other Exa errors; 0 sources selected; no `research_data` file written.
- Cause (**verified**, corrected 2026-11-10): the engine's aiohttp session ignores `HTTPS_PROXY` unless `trust_env=True`, so every request went direct to Exa carrying the dummy key and got 401. The proxy does inject the credential: through it, `curl` with `Authorization: Bearer dummy` and with no header both returned 200; only `x-api-key: dummy` passes through unchanged (401). **An earlier version of this note blamed the dummy key itself; that was wrong**, because I had only tested the `x-api-key` form, not the `Authorization: Bearer` form the engine sends. With `trust_env=True` a placeholder `EXA_API_KEY` would have worked too (not run).
- The engine also refuses to enable Exa with an empty key (`deep_research/utils.py`, `agents/base.py`, `integration.py`), and a direct keyless request gets an `x402` payment-required response whose header exceeds aiohttp's 8,190-byte limit (shows as "400 Got more than 8190 bytes").
- Fix (owner chose it: "option 2, proxy injects the key"): `EXA_AUTH_PROXY=1` sends Exa requests with no key header and `trust_env=True`; never honoured when `env_fallback=False` (web UI). `tests/test_exa_proxy_auth.py` (16 tests; 2 in-memory mutants caught). Checked free: the engine's own Exa function returned 2 results with no key set.
- The run still finished: $2.6907 over 130 calls in 822 s, five-hour window 12 % to 21 %, report 26,203 chars (`reports/live4_exa_FAILED_no_sources_report.md`). It was written without retrieved sources, so its 30 citation numbers are **unverified and may come from model memory**. Treat the report as unsupported.
- Spend: $2.69 of the $3 approved. A re-run needs a new approval.
- Open: how to supply Exa access (real key visible as `EXA_API_KEY`, or an engine option that omits the auth header so the proxy can inject it).

### live4b: `--search exa` with `EXA_AUTH_PROXY=1` (2026-11-10, owner-approved, cap $3.00): **over the cap**
Usage: `live4b_usage.json`; report: `reports/live4b_exa_report.md`. **Verified** from the usage file and log.
| | live1 | live2 | live3 | **live4b (Exa)** |
|---|---|---|---|---|
| Outcome | window stop | completed | completed | **completed, no stop** |
| Cost (list price) | $2.54 | $1.81 | $2.56 | **$3.25 (cap $3.00: over by $0.25)** |
| Calls / wall time | 133 / 966 s | 154 / 1,068 s | 183 / 1,381 s | **79 / 1,183 s** |
| Sub-agents | 15 | 9 | 12 | **14** |
| Sources saved / distinct citations | 51 / 52 | 21 / 17 | 34 / 27 | **353 / 67** |
| Report | 28.5k body | 28.3k | 34.2k | **42.1k body (56.0k with Sources)** |
| Final-report call | n/a | $0.45 | $0.59 | **$1.33** (research $1.92) |
| Five-hour window | 70 % end | 13 to 19 % | 1 to 9 % | **25 to 35 %** (about 3.1 points per dollar) |
- Exa worked: 30 `exa_deep_search` calls, 0 errors; 37 `fetch_urls`. By model: sonnet 10 calls $2.87; haiku 69 calls $0.38 (no WebSearch or WebFetch lines: search ran through Exa).
- Coverage: the BMJ network meta-analysis (named 10 times) and the Annals 2023 trial appear, as do NEJM 2022, Trepanowski, Liu, Lowe, Sundfør and Cochrane. Only the count of citations and trial names was checked; no facts were verified against primary sources, no judge, one run.
- **Cap overrun:** research stopped by itself at $1.92 (under the $2.25 research limit). The final-report call then cost $1.33 on a history holding 353 sources, against a reserve of $0.75 (25 % of the cap), so the run ended at $3.25. The governor lets a call start while spend is under the cap and cannot predict its cost, so a large history can push one call past the ceiling. This is the second measurement (after live2's $0.45) that the 25 % reserve does not scale with the size of the findings. Not fixed; open. Candidate fixes: estimate the report call from the history size before starting it and trim or digest sources when the projection exceeds the cap; or size the reserve per search backend.

### Guards built after `live4` and `live4b` (2026-11-10; free tests only, **not yet run live**)
- **Search fail-fast guard** (`deep_research/search_health.py`, `search_failure_limit`, default 6, 0 = off): 6 failed Exa or Tavily searches in a row stop the searching; the supervisor writes a report from the findings it has (`search_unavailable`, resumable) or fails naming the backend. It would have stopped `live4` after its first round instead of after 80 failed searches. Claude search is not covered.
- **Report-cost guard** (`deep_research/report_budget.py`, `--report-budget USD`, `claude_code_report_budget_usd`): projects the final-report call from the history size and the model's own measured price (x1.25 margin), trims per-source text to fit the room left under the cap (or the owner's budget if lower), and returns the findings digest if even the bare call is too dear. Calibration points: $0.589 (114.9k in, 16.7k out tokens) and $1.329 (289.6k in, 20.7k out). **Inferred, not run:** on `live4b` this would have projected about $1.5 against $1.08 of room and trimmed the source text, ending near the cap instead of $0.25 over it. First live run with the guard on is still owed.

### live5: both guards on, `--search exa`, cap $1.00 (2026-11-10, owner-approved)
Usage: `live5_usage.json`; digest: `reports/live5_exa_digest_report.md`. **Verified** from the usage file and log.
- Outcome: `partial`, stopped by `usd_cap` during research (research limit $0.50 = the $0.50 dollar floor on the reserve), then the **report-cost guard chose the digest**: budget $0.483 (the room left), projected $0.916, so no report call was made. Total **$0.5167 over 33 calls in 350 s: under the $1.00 cap** (live4b overran its $3.00 cap by $0.25). Window five-hour 51 % to 53 %. Resume state saved (`--resume live5`).
- Exa: 13 searches, 0 errors, 149 sources saved (220,616 characters of source text), 17 `fetch_urls`. The search fail-fast guard did not fire (nothing failed), so it is still unexercised live.
- The guard worked as designed but the result is a digest, not a report: at a $1 cap the fixed $0.50 reserve is too small for a history with 149 Exa sources. Even with all source text dropped, the bare call projects about $0.5 or more (25k assumed output tokens alone are about $0.40 at the measured price of $2.6e-6 per token). Not measured: what the report call would really have cost (it was not made).
- Open (not built): make the research limit dynamic (stop research when spend plus the projected report cost reaches the cap) so a small cap still ends in a model-written report, or lower the assumed output size for small caps. A digest at $1 is the guard doing its job; the reserve is what is too small.

### Dynamic reserve built after `live5` (2026-11-10; free tests only, **not yet run live**)
Research now stops when `spent + next round + projected cheapest useful report >= usd_cap` (stop reason `report_reserve`), instead of at a fixed 25 % of the cap (at least $0.50). `--static-reserve` restores the old rule. `tests/test_dynamic_reserve.py`: 18 tests, 6 in-memory mutants caught. **Inferred, not run:** at a $1 cap with Exa, research would stop after about one or two rounds with a small history and end in a trimmed, model-written report instead of the digest. A live run (new cap needed) is the test; whether a thin report beats the digest of 149 sources is a quality question the tests cannot answer.


### live6t: Tavily, $1 cap, dynamic reserve on, `--plan-window-max 0.95` (2026-11-10, owner-approved; **over the cap**)
Usage: `live6t_usage.json`; digest: `reports/live6t_tavily_digest_report.md`. **Verified** from the usage file and log. Two earlier attempts the same hour (Exa, then Tavily with `--plan-window-max 0.85`) stopped at their first call ($0.0106 each) because the five-hour window was at 78 to 79 % and the research limit is the ceiling minus 0.10.
- Outcome: `partial`, stop reason `report_reserve`, report mode digest (25,750 characters), resumable (`--resume live6t`). **Cost $1.0967 over 21 calls in 285 s: $0.097 over the $1.00 cap.** Window five-hour 79 % to 83 % (about 3.6 points per dollar), seven-day 34 % to 35 %.
- Tavily worked through the proxy with the placeholder key: 8 searches, 0 errors, 9 `fetch_urls`, 586,563 characters of source text. Unverified: whether the results used the injected key or Tavily's keyless tier (check the dashboard credit count).
- **Why the dynamic reserve did not save the run:** it checks once per supervisor turn. The first check came after the first research round, and that round alone cost about $0.85 (3 parallel haiku sub-agents reading 1.12 M input tokens of Tavily raw content) against an assumed next-round allowance of $0.10. The governor's per-call limit (the 90 % backstop, $0.90) only blocks calls that start after spend crosses it, so calls already in flight carried the run past $1.00. At the check, spent $1.10 + $0.10 + the cheapest useful report $0.57 exceeded the cap, so research stopped, but the report budget was $0.00 and the guard chose the digest.
- Conclusion: the report guard and the stop rule behaved as built; the **ceiling was still breached**, by a research round. My earlier "known limit" note (one costly round can eat the reserve) is exactly what happened. Not fixed. Candidate fix: after each supervisor turn, the governor also applies the projected cheapest report cost to its per-call research limit (stop calls when spend >= cap minus that projection), and the first-round allowance scales with the number of parallel sub-agents.

### Mid-round limit built after `live6t` (2026-11-10; free tests only, **not yet run live**)
After each supervisor-turn check the governor applies the projected cheapest report to its per-call research limit (cap minus that projection minus concurrency x average research call), and the first-round allowance scales with the parallel sub-agents (5 % of the cap each). `tests/test_dynamic_reserve.py` has 9 new tests (24 in the file), 5 more in-memory mutants caught. A replay of `live6t`'s numbers in a test shows research calls refused near $0.35 instead of running to $1.10. **Inferred, not run:** at a $1 cap with Tavily this leaves very little research (the cheapest report is about $0.57), so the likely result is a thin report or the digest, under the cap. Whether $1 is a useful cap for this setup is open; $2 to $3 is where earlier runs produced real reports. A live run needs a new cap.


### live7b: Tavily, $2 cap, all guards on, default window ceiling (2026-11-10, owner-approved) **first clean run under the guards**
Usage: `live7b_usage.json`; report: `reports/live7b_tavily_report.md`. **Verified** from the usage file and log. (An earlier `live7` attempt stopped at its first call, window at 87 %; this run started with the window at 0 %.)
- Outcome: `completed`, a **model-written report** (34,064 characters, 17 distinct citations), stop reason `report_reserve`, resumable. **Cost $1.8279 over 22 calls in 373 s: under the $2.00 cap** (research $1.2002, report call $0.6277). Window five-hour 0 % to 14 %, seven-day 36 % to 37 %.
- Dynamic reserve: the check after the first round read spent $1.20 + next round $0.30 + cheapest report $0.57 >= $2.00, so research stopped with $0.80 of room. The mid-round limit (cap minus report minus in-flight margin = $1.26) was not reached.
- Report guard: budget $0.80, projected $0.80 after trimming source text 694,367 to 210,454 characters (16,888 per source); actual call **$0.6277** (129,761 input and 14,536 output tokens): the projection was about 27 % above the real cost, on the safe side.
- Tavily: 8 searches, 0 errors, 8 `fetch_urls`. Unverified: injected key or keyless tier (check the dashboard credit count).
- Coverage by name: BMJ network meta-analysis 31 mentions, Cochrane 20, Liu (NEJM 2022) 15, Lowe 4, NEJM 2, Annals 2. Not checked: any study's facts against primary sources; no judge.
- What this shows: at a $2 cap the three guards work together (stop with room, trim to fit, write the report, stay under the cap). What it does not show: the fail-fast search guard firing (nothing failed), or behaviour at $1 (live6t) with the fixes; one run, one question.
