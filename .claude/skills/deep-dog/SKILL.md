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
4. **Spend approval.** Ask the owner to approve before the first run of a session. Never run unapproved.
5. **Preflight.** `python3 -c "from deep_research.providers.claude_code import ensure_ready; print(ensure_ready())"`. If it fails, report the fix it prints.
6. **Run** in the background and poll its output file; do not sleep in a loop:
   `python run_research.py --prompt "<question>" --preset <preset> [overrides] --output-dir outputs`
   The caps `claude_code_usd_cap` ($5) and `claude_code_plan_window_max` (0.8) stop the run before the plan window is exhausted.
7. **Report** the file path, the model line the CLI printed, and whether it ended `completed` or stopped on a cap.

## Never
- Never use `--bare`, read or forward the owner's sign-in, or pass provider keys to the child processes.
- Never expose this to anyone else's requests: it spends the owner's plan. The web UI refuses `claudecode:` models on purpose.
