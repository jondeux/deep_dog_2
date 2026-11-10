#!/usr/bin/env python3
"""A fake `claude` binary for free tests. It never touches the network.

Controlled through DEEPDOG_CC_FAKE_* variables (the child allowlist passes the DEEPDOG_CC_ prefix):
  DEEPDOG_CC_FAKE_SCRIPT  JSON file: list of responses, consumed in order (the last one repeats)
  DEEPDOG_CC_FAKE_STATE   counter file; DEEPDOG_CC_FAKE_LOG  JSONL log of every call (argv, stdin, pid, times)
A response: {"text", "cost", "util": {"five_hour", "seven_day"}, "is_error", "subtype", "api_error_status",
             "delay", "hooks", "no_result", "exit"}. Event shapes follow recorded real runs
(swarm-framework validation/fixtures/v1-stream.jsonl)."""
import fcntl
import json
import os
import sys
import time

E = os.environ.get


def main(argv):
    if len(argv) > 1 and argv[1] == "--version":
        print("2.1.319 (Claude Code)")
        return 0
    if argv[1:3] == ["auth", "status"]:
        print(json.dumps({"loggedIn": not E("DEEPDOG_CC_FAKE_LOGGED_OUT"), "authMethod": "oauth_token"}))
        return 0
    prompt = sys.stdin.read()
    script = json.load(open(E("DEEPDOG_CC_FAKE_SCRIPT")))
    state = E("DEEPDOG_CC_FAKE_STATE")
    with open(state + ".lock", "w") as lock:       # concurrent calls (parallel page reads) must each get their own index
        fcntl.flock(lock, fcntl.LOCK_EX)
        n = int(open(state).read() or 0) if os.path.exists(state) else 0
        open(state, "w").write(str(n + 1))
    r = script[min(n, len(script) - 1)]
    t0 = time.time()
    with open(E("DEEPDOG_CC_FAKE_LOG"), "a") as f:
        f.write(json.dumps({"i": n, "argv": argv[1:], "stdin": prompt, "pid": os.getpid(), "cwd": os.getcwd(),
                            "env_names": sorted(k for k in os.environ if k.startswith(("DEEPDOG", "ANTHROPIC", "OPENAI"))),
                            "start": t0}) + "\n")
    time.sleep(float(r.get("delay", 0)))
    sid = "fake-session"
    out = [{"type": "system", "subtype": "init", "session_id": sid}]
    for _ in range(int(r.get("hooks", 0))):
        out.append({"type": "system", "subtype": "hook_started", "session_id": sid})
    if r.get("util"):
        out.append({"type": "rate_limit_event", "session_id": sid, "rate_limit_info": {"unifiedWindows": {
            k: {"utilization": v} for k, v in r["util"].items()}}})
    if not r.get("no_result"):
        out.append({"type": "result", "subtype": r.get("subtype", "success"), "is_error": bool(r.get("is_error")),
                    "result": r.get("text", ""), "total_cost_usd": r.get("cost", 0.001), "session_id": sid,
                    "stop_reason": "end_turn", "api_error_status": r.get("api_error_status"),
                    "usage": {"input_tokens": 5, "cache_creation_input_tokens": 100,
                              "cache_read_input_tokens": 200, "output_tokens": 50}})
    for e in out:
        print(json.dumps(e), flush=True)
    with open(E("DEEPDOG_CC_FAKE_LOG"), "a") as f:
        f.write(json.dumps({"i": n, "pid": os.getpid(), "end": time.time()}) + "\n")
    return int(r.get("exit", 0))


if __name__ == "__main__":
    sys.exit(main(sys.argv))
