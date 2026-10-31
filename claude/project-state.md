# Project state

Volatile layer: measured facts, known issues, work in flight. Regenerate the snapshot with `bash scripts/state_snapshot.sh` and paste it over the block below; write the other sections by hand.

## Measured snapshot
- **Taken:** 2026-10-31T23:36Z by scripts/state_snapshot.sh
- **Commit:** 2ce13fc on `claude/setup-suite` (plus the install_deps hook, committed after)
- **Tests:** 2 failed, 1239 passed in 109.78s
- **Failing test IDs** (already failing; `scripts/simple_research.py` has no `QUESTION` attribute, which the test expects):
  - `tests/test_simple_research.py::test_simple_example[completed-0]`
  - `tests/test_simple_research.py::test_simple_example[partial-1]`
- **trail check:** no conflicts. **Trail learnings:** 4

## Known issues
- `test_simple_example` x2: the test patches `simple_research.QUESTION`, which the script no longer defines.
- trail isn't installed in the container until the environment's setup script carries the `trail` module; then run `trail init --yes --no-global` once to register trail's SessionStart hook next to the guard.

## In flight
- `claude/setup-suite`: guard and approval hooks, this memory layer, and `scripts/install_deps.sh` (SessionStart, cloud only; installs `.[dev]` + `webui/requirements-ui.txt` in the background, about 36 s when cold, then skipped while inputs are unchanged).
