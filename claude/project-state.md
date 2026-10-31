# Project state

Volatile layer: measured facts, known issues, work in flight. Regenerate the snapshot with `bash scripts/state_snapshot.sh` and paste it over the block below; write the other sections by hand.

## Measured snapshot
- **Taken:** 2026-10-31 by hand (state_snapshot.sh needs pytest, which this container lacked)
- **Commit:** ee1db95 on `main`; branch `claude/setup-suite` adds hooks and memory
- **Tests:** not run: `No module named pytest`. The setup script must install `.[dev]`
- **trail:** not installed in this container; `.trail/learnings` holds 4 learnings

## Known issues
- `.claude/` was fully gitignored, so trail's SessionStart hook (`.trail/hook.cjs`) never reached fresh clones. `claude/setup-suite` narrows the ignore to allow `settings.json`, `hooks-policy.json`, and `hooks/`. Run `trail init --yes --no-global` once in a session to register trail's hook alongside the guard.

## In flight
- `claude/setup-suite`: hooks-architect guard and approval gate, plus this memory layer. Pushed; no PR yet.
