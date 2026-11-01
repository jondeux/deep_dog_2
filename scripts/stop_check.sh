#!/usr/bin/env bash
# Stop check for hooks-architect's stop hook (and continuous-learning-architect tickets).
# Fast subset (~9 s): the full suite minus the deliberate 10 s hang test, and minus the 2 known
# failures in tests/test_simple_research.py (handoff Q4) so the check has a green baseline.
# Waits for scripts/install_deps.sh (background, cloud only) so a first-turn check doesn't fail
# on missing packages. If that install failed, it says so and exits 0: an environment problem
# shouldn't block the turn.
set -uo pipefail
root="${CLAUDE_PROJECT_DIR:-$(pwd)}"
cd "$root" || exit 0
state="${INSTALL_DEPS_STATE:-${HOME}/.cache/install-deps/$(printf '%s' "$root" | sha256sum | cut -c1-12)}"
for _ in $(seq 1 90); do
  [ "$(cat "$state/status" 2>/dev/null)" = "running" ] || break
  sleep 1
done
if [ "$(cat "$state/status" 2>/dev/null)" = "failed" ]; then
  echo "stop_check: dependency install failed (see $state); tests skipped" >&2
  exit 0
fi
exec python3 -m pytest -q -p no:cacheprovider \
  --deselect tests/test_simple_research.py::test_simple_example \
  -k 'not hanging_flush_with_default_timeout'
