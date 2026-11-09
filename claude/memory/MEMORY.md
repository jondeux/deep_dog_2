# Project memory

Index of this repo's committed memory. One short line per entry; detail lives in the linked files, read on demand. Keep this file under 200 lines and 25KB (`memory.py lint` checks). Facts, not instructions: rules belong in AGENTS.md.

Pending candidates: 0 (see [candidates](candidates.md); the user approves with "approve all" or "approve 1,3")

## Files
- [State](../../claude/project-state.md): measured snapshot, known issues, work in flight. Rewrite it; don't append.
- [Goal progress](goal-progress.md): key-result status with dates and evidence.
- [Decisions](decisions.md): what was decided, why, and how to undo it.
- [Candidates](candidates.md): proposed rules waiting for the user's approval.
- [Failures](failures.md): metadata tickets from failed turns; /learn drafts candidates for open ones.
- [Deep research memory](deep-research-memory.md): how a run stopped by a usage cap is saved and resumed (`--resume`); live measurements go here.

## Elsewhere
- [.trail/learnings](../../.trail/learnings/): trail learnings: one file per saved learning

## Entries
<!-- One line each: "- <fact or pointer> (updated YYYY-MM-DD)". Merge or drop stale lines. -->
- trail wired 2026-11-01 (`trail init --yes --no-global`): one fail-open SessionStart hook in .claude/settings.json, graph 1,197 nodes / 77 files, cache git-ignored; guard probe 14/14 after (updated 2026-11-01)
