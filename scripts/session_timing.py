#!/usr/bin/env python3
"""session_timing.py: where did a Claude Code session's time go?

Reads one session transcript (JSONL, as written under ~/.claude/projects/) and
prints: calendar span, waits for the human (short waits vs long gaps), and per
subagent duration. Read-only. Standard library only.

Usage:  python3 scripts/session_timing.py TRANSCRIPT.jsonl [--gap-minutes 30]
        python3 scripts/session_timing.py --self-test

A "wait" is the time from an assistant record to the next human message.
Messages that only carry a system-reminder or tool results are not human
messages. Waits over --gap-minutes are reported as long gaps, not averaged.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from statistics import mean


def _ts(rec):
    raw = rec.get("timestamp")
    if not raw:
        return None
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _text(content) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "\n".join(parts)


def _is_human(rec) -> bool:
    if rec.get("type") != "user" or rec.get("isSidechain") or rec.get("isMeta"):
        return False
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
        return False
    text = _text(content)
    # Strip system-reminder blocks; a message that is only a reminder is not human.
    while "<system-reminder>" in text and "</system-reminder>" in text:
        a = text.index("<system-reminder>")
        b = text.index("</system-reminder>") + len("</system-reminder>")
        text = text[:a] + text[b:]
    return bool(text.strip())


def analyse(records, gap_minutes: float = 30.0) -> dict:
    stamped = [(r, _ts(r)) for r in records if _ts(r)]
    if not stamped:
        return {"span_min": 0.0, "human_messages": 0, "short_waits": [], "long_gaps": [], "agents": {}}
    stamped.sort(key=lambda x: x[1])
    span = (stamped[-1][1] - stamped[0][1]).total_seconds() / 60
    waits, last_assistant = [], None
    for rec, ts in stamped:
        if rec.get("isSidechain"):
            continue
        if rec.get("type") == "assistant":
            last_assistant = ts
        elif _is_human(rec) and last_assistant is not None:
            waits.append((ts - last_assistant).total_seconds() / 60)
            last_assistant = None
    agents: dict[str, list] = {}
    for rec, ts in stamped:
        if rec.get("isSidechain"):
            key = rec.get("agentId") or rec.get("sessionId") or "?"
            lo, hi = agents.get(key, [ts, ts])
            agents[key] = [min(lo, ts), max(hi, ts)]
    return {
        "span_min": round(span, 1),
        "human_messages": sum(1 for r, _ in stamped if _is_human(r)),
        "short_waits": [w for w in waits if w <= gap_minutes],
        "long_gaps": [round(w, 1) for w in waits if w > gap_minutes],
        "agents": {k: round((hi - lo).total_seconds() / 60, 1) for k, (lo, hi) in agents.items()},
    }


def render(res: dict) -> str:
    sw = res["short_waits"]
    lines = [
        f"Calendar span: {res['span_min']} min",
        f"Human messages: {res['human_messages']}",
        f"Short waits: {len(sw)}, mean {mean(sw):.1f} min, total {sum(sw):.1f} min" if sw else "Short waits: none",
        f"Long gaps (min): {res['long_gaps']}" if res["long_gaps"] else "Long gaps: none",
        f"Subagents: {len(res['agents'])}, total {sum(res['agents'].values()):.1f} min",
    ]
    for k, v in sorted(res["agents"].items(), key=lambda kv: -kv[1]):
        lines.append(f"  {k}: {v} min")
    return "\n".join(lines)


def _self_test() -> None:
    def rec(t, ts, **kw):
        return {"type": t, "timestamp": ts, **kw}
    msg = lambda text: {"content": text}
    recs = [
        rec("user", "2026-01-01T10:00:00Z", message=msg("start")),
        rec("assistant", "2026-01-01T10:05:00Z", message=msg("ok")),
        rec("user", "2026-01-01T10:15:00Z", message=msg("go")),
        rec("assistant", "2026-01-01T10:16:00Z", message=msg("done")),
        rec("user", "2026-01-01T10:17:00Z", message=msg("<system-reminder>x</system-reminder>")),
        rec("user", "2026-01-01T12:16:00Z", message=msg("later")),
        rec("assistant", "2026-01-01T10:06:00Z", isSidechain=True, agentId="a1", message=msg("")),
        rec("assistant", "2026-01-01T10:12:00Z", isSidechain=True, agentId="a1", message=msg("")),
    ]
    res = analyse(recs, gap_minutes=30)
    assert res["human_messages"] == 3, res
    assert [round(w) for w in res["short_waits"]] == [10], res
    assert res["long_gaps"] == [120.0], res
    assert res["agents"] == {"a1": 6.0}, res
    print("self-test ok")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("transcript", nargs="?")
    ap.add_argument("--gap-minutes", type=float, default=30.0)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        _self_test()
        return 0
    if not a.transcript:
        ap.error("TRANSCRIPT.jsonl is required (or use --self-test)")
    with open(a.transcript, encoding="utf-8") as fh:
        records = [json.loads(line) for line in fh if line.strip()]
    print(render(analyse(records, a.gap_minutes)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
