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
