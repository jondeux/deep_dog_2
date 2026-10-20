---
title: Fresh clone of the fork has .trail/ but no trail hooks or graph
author: Claude
date: 2026-10-20
branch: main
cost: { minutes: 21, tokens: 335729 }
touches:
  - README.md
---

## Decided
- A folder called .trail/ is not proof that trail is set up. Check two things: .claude/settings.json mentions .trail/hook.cjs, and `trail status` reports the code map in sync.
- If the hook is missing, run `trail init --yes --no-global`. If only the graph is missing or stale, run `trail build`.

## Tried and ruled out
- Treating "the .trail/ folder exists" as "already set up". Commit 674c017 tracks only .trail/.gitignore, README.md and hook.cjs. The repo's .gitignore excludes .claude/ (line 31) and .trail/.gitignore excludes cache/, so a fresh clone gets neither the hooks nor the graph.
- Using `trail init --dry-run --no-global` to preview a local-only init. The dry run still lists the ~/.claude writes, so it doesn't show what --no-global will skip.

## Watch out
- An early `trail init` ran in /home/claude rather than the repo and left .claude/, .trail/ and graft/ there. Run trail only from the repo root.
- `trail --help` doesn't list `viz` or `mcp`. `trail viz --export <dir>` writes a standalone index.html, so put it outside the repo.
