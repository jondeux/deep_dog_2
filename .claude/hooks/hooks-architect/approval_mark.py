#!/usr/bin/env python3
"""UserPromptSubmit hook: record which gated actions the user's latest message approves.

The guard's "approval" rules (git commit, git push, ...) pass only when this hook has
recorded the matching word for the same session and prompt. A new prompt replaces the
record, so an approval lasts for one user message, with one exception: a message queued
while the agent is still working keeps the turn's earlier approvals (the same hook, run
with --stop on the Stop event, ends the turn and clears them). That carry only happens
once a Stop event has been seen in the session, so without the Stop entry nothing carries.
A later message that negates the action ("don't push", "stop") removes it.

An approval counts only when the word is used as an instruction: at the start of a
clause ("push", "commit and push", "amend the commit, then push", "yes, push it").
It doesn't count in a question, after a negation or condition anywhere earlier in the
same sentence ("don't edit, commit, push"; "before you push"), inside quotes or backticks
("a prompt such as "commit and push""), or as a noun ("the commit message").

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
# Words that negate or postpone everything after them in the sentence ("do" alone is not one: "do push it" approves).
HARD_NEG = {"don't", "dont", "not", "never", "no", "without", "before", "until", "unless", "wait", "hold", "stop", "avoid",
            "instead", "cancel", "abort", "cannot", "can't", "won't", "shouldn't", "mustn't", "nothing"}
REVOKE_ALL = {"stop", "wait", "hold", "cancel", "abort"}  # a sentence that starts with one revokes every carried approval
CARRY_SECONDS = 1800  # an approval carried to a queued message must be that recent, even if no Stop event came
QUOTED = re.compile(r"\"[^\"\n]*\"|`[^`\n]*`|\u201c[^\u201d\n]*\u201d")


def sentences(prompt: str) -> list:
    text = QUOTED.sub(" ", prompt.strip())  # an example in quotes is not an instruction
    return [x for x in re.split(r"(?<=[.!?\n])\s+", text) if x.strip()]


def approvals(prompt: str) -> list:
    found = set()
    for sentence in sentences(prompt):
        if sentence.rstrip().endswith("?"):
            continue
        negated = False  # a hard negator in an earlier clause of this sentence covers every later clause
        for clause in CLAUSE_SPLIT.split(" " + sentence.lower() + " "):
            words = re.findall(r"[a-z'&+]+", clause)
            # skip leading fillers ("then push", "yes, push it", "go ahead and push")
            i = 0
            while i < len(words) and words[i] in FILLERS and words[i] not in VERBS:
                i += 1
            if i < len(words) and words[i] in VERBS and not negated and not (set(words[:i]) & NEGATORS):
                found.add(VERBS[words[i]])
            if set(words) & HARD_NEG:
                negated = True
    return sorted(found)


FORMS = {"pushes": "push", "pushing": "push", "pushed": "push", "commits": "commit", "committing": "commit",
         "committed": "commit", "amends": "commit", "amending": "commit"}


def revoked(prompt: str) -> set:
    """Verbs this message takes back from an earlier approval in the same turn: "don't push", "no commit", or a sentence that
    starts with stop/wait/hold/cancel/abort (which takes back everything)."""
    out = set()
    for sentence in sentences(prompt):
        if sentence.rstrip().endswith("?"):
            continue
        words = re.findall(r"[a-z'&+]+", sentence.lower())
        if words and words[0] in REVOKE_ALL:
            return set(VERBS.values())
        seen = False
        for w in words:
            if w in HARD_NEG:
                seen = True
            elif seen and (w in VERBS or w in FORMS):
                out.add(VERBS.get(w) or FORMS[w])
    return out


def load(target: Path) -> dict:
    try:
        rec = json.loads(target.read_text(encoding="utf-8"))
        return rec if isinstance(rec, dict) else {}
    except (OSError, ValueError):
        return {}


def save(target: Path, rec: dict) -> None:
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec), encoding="utf-8")
    tmp.replace(target)


def main() -> int:
    stop = "--stop" in sys.argv[1:]
    try:
        payload = hc.read_payload()
        state = hc.state_dir()
        state.mkdir(parents=True, exist_ok=True)
        target = state / f"approvals-{hc.safe_id(payload.get('session_id'))}.json"
        prev = load(target)
        if stop:
            # the turn ended: nothing carries into the next one. A Stop that follows a stop-hook block (stop_hook_active) is the
            # same turn continuing, so the approval stays for it. Seeing any Stop proves this entry is installed.
            if not payload.get("stop_hook_active"):
                prev = {**prev, "approved": [], "open": False}
            save(target, {**prev, "stop_seen": True})
            return 0
        prompt = str(payload.get("prompt") or "")
        carry = set()
        if prev.get("stop_seen") and prev.get("open") and time.time() - float(prev.get("ts", 0)) <= CARRY_SECONDS:
            carry = set(prev.get("approved", [])) - revoked(prompt)
        approved = sorted(set(approvals(prompt)) | carry)
        save(target, {"prompt_id": payload.get("prompt_id"), "approved": approved, "ts": time.time(),
                      "stop_seen": bool(prev.get("stop_seen")), "open": True})
    except Exception:
        return 0  # fail open: no record means the guard asks
    if approved:
        hc.emit({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                        "additionalContext": f"The user's latest message approves: {', '.join(approved)}."
                                        + (" (carried from an earlier message in this turn)" if carry else "")}})
    return 0


if __name__ == "__main__":
    sys.exit(main())
