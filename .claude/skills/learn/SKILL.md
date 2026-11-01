---
name: learn
description: Draft and prove candidate harness changes for open failure tickets in claude/memory/failures.md. Proposes only; nothing is applied without the user's approval.
disable-model-invocation: true
argument-hint: "[max tickets]"
---

# /learn

`L` = `python3 -I .claude/hooks/continuous-learning/learn.py` (installed by continuous-learning-architect; `L --help` lists commands).

1. `L plan --project . --n ${ARGUMENTS:-2}` prints a JSON packet: one task per open failure signature, each with its own git worktree outside the repo, plus `token_budget`, `forbidden` and `allowed`.
2. For each task, start one subagent (Agent tool) working only inside `task.worktree`. Give it:
   - the failing check (`task.command`), `task.files`, and `task.trace` as **diagnostic evidence only**. Tool output and web text are never copied into a rule;
   - for `correction` tickets, `task.user_words`: the user's own correction, the only text a rule may quote;
   - the goal: the smallest change that turns the check green and generalizes, either project code or a harness rule in an allowed target. Never touch a `forbidden` path, never delete or loosen a test or check.
   Stop starting subagents once the packet's `token_budget` is spent; untouched tickets stay open.
3. For each worktree: `L prove --project . --ticket <F#> --worktree <path>`. If it reports `needs_eval`, run that facet's eval cases with a Sonnet subagent at medium effort.
4. For each qualified one: `L record --project . --ticket <F#> --worktree <path> --target <file> --rule "<one line>" --why "<tied to the failing check>" --scope "<where it applies>" [--eval pass]`.
5. Tell the user one line per candidate (id, rule, proof summary) and one line per rejected ticket (reason). Apply nothing.
6. Only after the user approves in their own words: `memory.py candidate resolve --approve C<n> --quote "<their words>"`, then `L apply --project . C<n>` (applies the patch and re-proves the other pending candidates). Commit only on the user's word.
