#!/usr/bin/env bash
# state_snapshot.sh: print the measurable part of the project state document.
#
# Measures, never recalls: commit, working-tree state, test baseline (passed
# count + exact failing test IDs), trail check conflicts and learnings count.
# Output is a Markdown block to paste into claude/project-state.md (the
# judgement parts of that document, such as known issues, deferred work and
# decisions, are written by hand).
#
# Usage (from anywhere):  bash scripts/state_snapshot.sh
# Cost: one full test run (about two minutes). Read-only: changes no files.
# Exit status: 0 when the snapshot was produced, even if tests fail (a failing
# test is part of the state); non-zero only if the repo cannot be found.

set -uo pipefail

repo="$(git -C "$(dirname "$0")" rev-parse --show-toplevel 2>/dev/null)" || {
  echo "state_snapshot: not inside a git repository" >&2; exit 2; }
cd "$repo" || exit 2

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

commit="$(git rev-parse --short HEAD)"
branch="$(git rev-parse --abbrev-ref HEAD)"
subject="$(git log -1 --format=%s)"
changed="$(git status --porcelain | wc -l | tr -d ' ')"
if [ "$changed" = "0" ]; then tree="clean"; else tree="$changed uncommitted file(s)"; fi

# Tests. "-o addopts=" drops the repo's own -q so the summary line prints.
python3 -m pytest -o addopts= -q -rf -p no:cacheprovider >"$tmp/pytest.txt" 2>&1
summary="$(grep -E '^[=]* ?[0-9]+ (passed|failed)|^[0-9]+ (passed|failed)|no tests ran' "$tmp/pytest.txt" | tail -1 | sed 's/=//g; s/^ *//; s/ *$//')"
[ -n "$summary" ] || summary="(no summary line found; see a manual pytest run)"
failures="$(grep -E '^FAILED ' "$tmp/pytest.txt" | sed -E 's/^FAILED ([^ ]+).*/\1/' | sort -u)"

# Trail. trail check only inspects changed files, so a clean tree reports none.
if command -v trail >/dev/null 2>&1; then
  trail check >"$tmp/trail.txt" 2>&1
  conflicts="$(grep -E '^✗ ' "$tmp/trail.txt" | sed 's/^✗ //')"
  graph="$(trail status 2>/dev/null | grep -i 'code map' | tr -s ' ' | sed 's/^ *//')"
else
  conflicts=""; graph="trail not installed"
fi
learnings="$(ls .trail/learnings/*.md 2>/dev/null | wc -l | tr -d ' ')"

echo "## Measured snapshot"
echo "- **Taken:** $(date -u +%Y-%m-%dT%H:%MZ) by scripts/state_snapshot.sh"
echo "- **Commit:** $commit on \`$branch\`: $subject"
echo "- **Working tree:** $tree"
echo "- **Tests:** $summary"
if [ -n "$failures" ]; then
  echo "- **Failing test IDs** (pre-existing for a session that starts here):"
  echo "$failures" | sed 's/^/  - `/; s/$/`/'
else
  echo "- **Failing test IDs:** none"
fi
if [ "$changed" = "0" ]; then
  echo "- **trail check:** not applicable on a clean tree (it inspects changed files only)"
elif [ -n "$conflicts" ]; then
  echo "- **trail check conflicts** (classify each as real or keyword match):"
  echo "$conflicts" | sed 's/^/  - /'
else
  echo "- **trail check:** no conflicts"
fi
echo "- **Trail graph:** ${graph:-unknown}"
echo "- **Trail learnings:** $learnings"
