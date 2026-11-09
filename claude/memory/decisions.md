# Decisions

One entry per decision that code and git history can't explain on their own.

<!-- ## D<n> <title> (2026-10-31)
- decided: ...
- why: ...
- rejected: ...
- undo: ... -->

## Continuous-learning loop installed (2026-11-01)
- decided: continuous-learning-architect installed. The Stop check runs `scripts/stop_check.sh` (fast subset, ~9 s; max 2 blocks). When it is still red and the previous turn ended green, it writes a metadata ticket to `claude/memory/failures.inbox.jsonl`. `/learn` drafts and proves fixes; candidates land in `candidates.md` and are applied only after the user approves them in their own words.
- deselected: the 2 known failures in `tests/test_simple_research.py` (expects `scripts.simple_research.QUESTION`, which no longer exists). They are excluded from the check so it has a green baseline; they are still failing and still need a fix.
- undo: clear `stop.tickets` in `.claude/hooks-policy.json` and remove the continuous-learning SessionStart entry in `.claude/settings.json` (learner off); set `stop.command` to "" (Stop check off).

## Resume continues the research clock; resume API is additive (2026-11-09)
- decided: a run resumed after a usage stop (`--resume`) keeps the research clock where it stopped (waiting time not counted) and its iteration count, so the total stays within the 15-minute / iteration design. `integration.resume_research()` is a new function; `run_research()` is unchanged. `research_iterations` now passes through the outer graph state, so `run_metadata["research_iterations"]` reports the real count (it was always 0).
- approved by the owner on 2026-11-09 ("clock", "API surface", "side effect").
- undo: restore `start_time` to `time.time()` in `deep_research/resume.py::restore_inputs`; remove `resume_research` and `build_resume_agent`.
