---
name: deep-dog-Research
description: Run a Deep Dog 2 research job (a cited report) on the owner's own Claude plan, with a model and reasoning-effort picker per role, optional third-party models (deepinfra, openrouter, together, fireworks) and every engine setting as a switch. Use when the owner asks for deep research, a cited report, or types the /deep-dog-Research command followed by a question. Personal use only; never approves its own spend.
---

# /deep-dog-Research

Runs `python run_research.py` with LLM calls and web search going through the unmodified `claude` CLI on the owner's own plan
by default, with no provider keys. Any role can instead use a third-party model on the owner's own key from the environment.

## Steps
1. **Question.** Use the owner's question. If none was given, ask for it. A strong question names the topic, the time frame,
   what to cover, what to avoid and the shape of the answer; see `docs/DEEP_DOG_RESEARCH_MANUAL.md` (section 2) and offer to
   tighten a thin one before spending anything.
2. **Choose the setup with the question dialog** (recommended option first, at most 4 questions per dialog).
   - **Round 1, always:**
     1. Backend mix: all Claude plan (default, no keys) / Claude supervisor with third-party sub-agents / all third-party.
     2. Depth: `testing-framework` (cheapest) / `economy` (default) / `benchmark` / `custom`, or "customize".
     3. Search: `claude` (default, no key) / `tavily` / `exa` / `both` (own keys, set outside chat; cloud proxy setup in
        `docs/SETUP_SECRETS_IN_CC_CLOUD_ENV.md`).
     4. USD cap (see step 4).
   - **Round 2, only if the owner picks "customize":** any of agents/platforms (`--agents`, `--general-platforms`), models per
     role and per agent (`--supervisor-model`, `--draft-model`, `--subagent-model`, `--agent-model`, `--*-effort`, `--*-fallback`),
     limits (`--time-max`, `--supervisor-iterations`, `--parallel-research`, `--searches-per-agent`, `--reads-per-agent`), output
     (`--language`, `--output-mode`, `--no-trace`), and anything else through `--set FIELD=VALUE`. Ask only what they chose.
   - **Third-party models** are `deepinfra:<id>`, `openrouter:<slug>`, `together:<id>`, `fireworks:<id>`, billed per token on the
     owner's own key from the environment: never ask for a key in chat and never put one on a command line. `--list-models` shows
     which keys are set (names only). If a model has no price in the provider's public list, ask for `--price MODEL=IN/OUT`
     (USD per million tokens).
   - Efforts apply to Claude-plan models only: low, medium, high, xhigh, max. Presets (supervisor / sub-agents):
     `economy` sonnet-5.5 high / haiku-5.5 high; `benchmark` opus-5.5 medium / sonnet-5.5 high; `custom` sonnet-5.5 xhigh /
     haiku-5.5 xhigh; `testing-framework` sonnet-5.5 medium / haiku-5.5 medium. The draft writer follows the supervisor unless set.
   - With `--search claude`, agents read pages through Claude's WebFetch, which returns an extract focused on the agent's topic, not the whole page (probe: $0.0005 to $0.007 per page; publisher 403s and cookie walls come back as failures). Each agent reads 3 pages that way (`--page-reads N`; its other reads stay search summaries, which keeps breadth; 0 = no limit, slower and deeper). `--no-page-reader` keeps the thinner search-summary-only arm.
3. **Show the setup and the cost gauge before running.** Build the command, run it once with `--show-config` (spends nothing; prints
   the effective settings, which keys are set, and any problem a real run would refuse on) and show the owner the models, limits and
   cap. Relative cost per Claude-plan token: haiku 1x, sonnet about 6x, opus about 11x (measured, one run per cell; the effort
   level's own multiplier is not measured). Third-party cost is tokens times the provider's price, shown per model after the run.
   Measured reference: a small run (3 supervisor iterations, 4 sub-agents, `testing-framework`) cost about $0.55 list price and
   used about 3 to 8 % of the five-hour window (`validation/p4`). A default-size run (30 iterations) will cost more. State it as an estimate.
4. **Spend approval.** Ask the owner to approve a USD cap (list price; it covers Claude-plan and third-party spend together) before
   the first run of a session and pass it as `--usd-cap`. Never run unapproved.
5. **Preflight** (when any role uses the Claude plan). `python3 -c "from deep_research.providers.claude_code import ensure_ready; print(ensure_ready())"`. If it fails, report the fix it prints.
6. **Run** in the background and poll its output file; do not sleep in a loop:
   `python run_research.py --prompt "<question>" --preset <preset> --usd-cap <approved> [switches] --output-dir outputs`
   (`python run_research.py --help` lists every switch; the manual has them grouped.)
   Two ceilings: the USD cap (`--usd-cap`, default $5) and the plan-window guard (`--plan-window-max`, default 0.8). No call
   starts past either, but calls already running finish, so neither is a hard bound (`live6t` ended $0.097 over a $1 cap).
   Research stops earlier so the final report can still be written:
   - **Cost (dynamic reserve, the default):** before each supervisor turn, research stops when spend + the next round + the
     projected cheapest useful report reaches the cap; between turns, calls stop at the cap minus that report minus a margin
     for calls in flight. `--static-reserve` restores the old fixed reserve (25 % of the cap, at least $0.50).
   - **Window:** 0.1 below the guard. The plan window is shared with everything else on the plan: if the first reading is already
     above that limit, the run stops at its first call (about $0.01). Do not raise `--plan-window-max` without the owner's say-so.
   - **Report guard:** before the final-report call its cost is projected; over budget, the per-source text is trimmed, and if
     even the bare call is too dear the findings digest is returned. `--report-budget USD` sets the most that call may cost
     (default: what is left under the cap). A small cap is a real limit: at $1 the cheapest report projects near $0.57.
   - **Search guard:** after `search_failure_limit` (default 6) failed Exa or Tavily searches in a row the run stops searching
     and writes from its findings, or fails naming the backend. It does not cover `--search claude`.
7. **Report** the file path, the model line the CLI printed, and the `Usage (list-price estimate...)` block: cost, calls, plan-window
   use, cost per model (also saved as `usage_<timestamp>.json` next to the report). Say plainly how it ended:
   - `completed`: no cap was hit.
   - `STOPPED EARLY` with a synthesised report: research was cut short by a cap; the report covers only what was gathered.
   - `STOPPED EARLY` with status `partial`: no budget was left to write a report, so the file is a labelled digest of the
     sub-agents' findings (no model wrote it).
   - A run that stopped before any findings fails with a clear message (no report is invented); the usage file is still written.
   - The stop reason names the cause: `usd_cap`, `plan_window_guard`, `report_reserve` (kept for the report), `report_budget` (the
     report did not fit its budget: digest), `search_unavailable` (search kept failing).
8. **Resume.** A stopped run prints `RESUME STATE saved: ...` and a `--resume <id>` command. After the plan window has reset, and with the
   owner's spend approval for a new cap, run `python run_research.py --resume <id> --usd-cap <approved> --output-dir outputs` (only `--usd-cap`, `--report-budget` and `--plan-window-max` may change). The research clock
   and iteration count continue; the saved models and search are reused. The state files are in `deep-research-memory/` (git-ignored); never
   delete or edit them without asking.

## Never
- Never use `--bare`, read or forward the owner's sign-in, or pass provider keys to the child processes.
- Never ask for, print or accept an API key in chat or as a flag; keys live in the environment settings only.
- Never expose this to anyone else's requests: it spends the owner's plan. The web UI refuses `claudecode:` models on purpose.
