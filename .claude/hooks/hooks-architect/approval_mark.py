#!/usr/bin/env python3
"""UserPromptSubmit hook: record which gated actions the user's latest message approves.

The guard's "approval" rules (git commit, git push, ...) pass only when this hook has
recorded the matching word for the same session and prompt. Every new prompt
overwrites the record, so an approval lasts for one user message.

An approval counts only when the word is used as an instruction: at the start of a
clause ("push", "commit and push", "amend the commit, then push", "yes, push it").
It doesn't count in a question, after a negation or condition ("don't push",
"before you push"), or as a noun ("the commit message").

Never blocks the prompt. Fails open: on any error it records nothing, so the guard
falls back to asking the user.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True  # keep __pycache__ out of the user's repo
sys.path.insert(0, str(Path(__file__).resolve().parent))
import hookcommon as hc  # noqa: E402

VERBS = {"push": "push", "commit": "commit", "amend": "commit"}
FILLERS = {"please", "pls", "then", "and", "now", "just", "also", "finally", "ok", "okay", "yes", "yep", "sure",
           "go", "ahead", "git", "you", "can", "could", "may", "&", "+", "so", "next", "after", "that", "that,"}
NEGATORS = {"don't", "dont", "do", "not", "never", "no", "without", "before", "until", "unless", "wait", "hold",
            "stop", "avoid", "instead", "if", "when", "whether", "should", "would", "reply", "say", "type", "i'll",
            "i", "we", "it", "the", "a", "what", "why", "how"}
CLAUSE_SPLIT = re.compile(r"[,;:\n]|\s(?:and|then|but|&|\+)\s|\.(?:\s|$)|!")


def approvals(prompt: str) -> list:
    found = set()
    for sentence in re.split(r"(?<=[.!?\n])\s+", prompt.strip()):
        if sentence.rstrip().endswith("?"):
            continue
        for clause in CLAUSE_SPLIT.split(" " + sentence.lower() + " "):
            words = re.findall(r"[a-z'&+]+", clause)
            # skip leading fillers ("then push", "yes, push it", "go ahead and push")
            i = 0
            while i < len(words) and words[i] in FILLERS and words[i] not in VERBS:
                i += 1
            if i < len(words) and words[i] in VERBS and not (set(words[:i]) & NEGATORS):
                found.add(VERBS[words[i]])
    return sorted(found)


def main() -> int:
    try:
        payload = hc.read_payload()
        approved = approvals(str(payload.get("prompt") or ""))
        state = hc.state_dir()
        state.mkdir(parents=True, exist_ok=True)
        rec = {"prompt_id": payload.get("prompt_id"), "approved": approved, "ts": time.time()}
        target = state / f"approvals-{hc.safe_id(payload.get('session_id'))}.json"
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(rec), encoding="utf-8")
        tmp.replace(target)
    except Exception:
        return 0  # fail open: no record means the guard asks
    if approved:
        hc.emit({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                        "additionalContext": f"The user's latest message approves: {', '.join(approved)}."}})
    return 0


if __name__ == "__main__":
    sys.exit(main())
