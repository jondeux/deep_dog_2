---
name: deep-dog
description: Run a Deep Dog 2 research job (a cited report) on the owner's own Claude plan, with a model and reasoning-effort picker per role. Use when the owner asks for deep research, a cited report, or "/deep-dog <question>". Personal use only; never approves its own spend.
---

# /deep-dog

Runs `python run_research.py` with every LLM call and the web search going through the unmodified `claude` CLI on the
owner's own plan. Nothing here uses provider API keys.

## Steps
1. **Question.** Use the owner's question. If none was given, ask for it.
2. **Pick models (one question dialog, recommended option first).** Presets (supervisor / sub-agents):
   - `economy` (default): sonnet-5.5 high / haiku-5.5 high
   - `benchmark`: opus-5.5 medium / sonnet-5.5 high
   - `custom`: sonnet-5.5 xhigh / haiku-5.5 xhigh
   - `testing-framework`: sonnet-5.5 medium / haiku-5.5 medium (cheapest of the four)
   Offer per-role overrides (`--supervisor-model/--supervisor-effort`, same for `--draft-` and `--subagent-`). Efforts: low, medium, high, xhigh, max.
   Search: `--search claude` (default, no key) or `exa` / `tavily` / `both` (the owner's own keys, set outside chat).
3. **Show the cost gauge before running.** Relative cost per token: haiku 1x, sonnet about 6x, opus about 11x (measured, one run per cell;
   the effort level's own multiplier is not measured). Measured reference: a small run (3 supervisor iterations, 4 sub-agents,
   `testing-framework`) cost about $0.55 list price and used about 3 to 8 % of the five-hour window (`validation/p4`).
   A default-size run (30 iterations) will cost more. State it as an estimate.
4. **Spend approval.** Ask the owner to approve a USD cap (list price) before the first run of a session and pass it as `--usd-cap`. Never run unapproved.
5. **Preflight.** `python3 -c "from deep_research.providers.claude_code import ensure_ready; print(ensure_ready())"`. If it fails, report the fix it prints.
6. **Run** in the background and poll its output file; do not sleep in a loop:
   `python run_research.py --prompt "<question>" --preset <preset> --usd-cap <approved> [overrides] --output-dir outputs`
   Two ceilings are never exceeded by a call that starts: the USD cap (`--usd-cap`, default $5) and the plan-window guard
   (`--plan-window-max`, default 0.8). Research stops earlier, at 75 % of the cap and 0.1 below the guard, and that reserve is
   kept for writing the final report. So a run that hits a limit still ends in a report.
7. **Report** the file path, the model line the CLI printed, and the `Usage (list-price estimate...)` block: cost, calls, plan-window
   use, cost per model (also saved as `usage_<timestamp>.json` next to the report). Say plainly how it ended:
   - `completed`: no cap was hit.
   - `STOPPED EARLY` with a synthesised report: research was cut short by a cap; the report covers only what was gathered.
   - `STOPPED EARLY` with status `partial`: no budget was left to write a report, so the file is a labelled digest of the
     sub-agents' findings (no model wrote it).
   - A run that stopped before any findings fails with a clear message (no report is invented); the usage file is still written.
8. **Resume.** A stopped run prints `RESUME STATE saved: ...` and a `--resume <id>` command. After the plan window has reset, and with the
   owner's spend approval for a new cap, run `python run_research.py --resume <id> --usd-cap <approved> --output-dir outputs`. The research clock
   and iteration count continue; the saved models and search are reused. The state files are in `deep-research-memory/` (git-ignored); never
   delete or edit them without asking.

## Never
- Never use `--bare`, read or forward the owner's sign-in, or pass provider keys to the child processes.
- Never expose this to anyone else's requests: it spends the owner's plan. The web UI refuses `claudecode:` models on purpose.
