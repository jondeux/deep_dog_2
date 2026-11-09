# P2: live contract probes for the `claudecode:` provider (2026-11-09)

Model haiku-5.5, medium effort, one call each, child sessions on the owner's own plan sign-in (`claude` 2.1.321,
`authMethod: oauth_token`) under the allowlisted environment. Scripts: `run_p2.py`, `followup.py`; raw numbers:
`p2_results.json`. **Spend: $0.0294 list-price estimate of the approved $0.05 cap.** Five-hour window 0.29, seven-day 0.12.

| Probe | Result | Evidence |
|---|---|---|
| a. Clean model: no tools, own system prompt | **PASS [V]** | Reply `SQUAWK`; `init.tools == []`; model `claude-haiku-5-5`; `apiKeySource: none` (plan sign-in works with the allowlisted env). Built-in subagent definitions are listed but cannot run without tools. |
| b. Tool-call envelope | **PASS [V]** | 3 parallel calls correct in one reply (`Search`, `Search`, `Calc`) on haiku medium; args valid against the schemas. |
| b2. Schema mode (research-brief shape) | **PASS [V]** | `ResearchQuestion` parsed, `target_language: English`. |
| c. WebSearch headless | **PASS [V]** | Real `tool_use` event `WebSearch {query, mode: standard}` plus a result block; 0 permission denials with `--tools WebSearch --allowedTools WebSearch`. `usage.server_tool_use.web_search_requests` stays 0, so it is NOT a usable counter. **Cost about $0.013 per search call (about 10x a plain call).** |
| d. `httpx` fetch from this container | **PARTIAL [V]** | example.com 200, arxiv.org 200, **en.wikipedia.org 403**. `fetch_urls` will fail on some sites. A WebFetch backend is a possible fallback (not built). |
| e. `CLAUDE_CODE_MAX_OUTPUT_TOKENS` | **PASS [V] (behaviour found)** | The limit is honoured, but exceeding it is an **error**: `is_error: true`, text `API Error: Claude's response exceeded the N output token maximum`, `subtype: success`, exit code 1. The adapter already treats this as a failed call. A 32k-token report was NOT tested (cost); the adapter sets the limit to the role's `max_tokens` (32000 / 40000). |
| f. `rate_limit_event` headless | **PASS [V]** | `five_hour 0.29`, `seven_day 0.12` read from the stream. |
| g. Hooks in child sessions | **PASS [V]** | With the default flags (`--settings {"disableAllHooks": true}`): 0 hooks. Control without it: **7 hooks fired** (your user-level hooks). `--setting-sources` was not needed. |
| i. 3 concurrent async calls | **PASS [V]** | Replies `0 1 2`, 3.4 s total, governor counted 3 calls. |

## Consequences built or noted
- Search backend `claude_web_search` (Claude's own WebSearch, haiku-5.5 low) is implemented on this evidence.
- Budget planning: a search call is about $0.013, so 40 searches is about $0.5 before any reasoning cost.
- Not verified: a full-size report (32k tokens) through the output limit; behaviour when the five-hour window is actually hit (the hard-limit text is detected by hint, not yet observed).
