"""Digest ``claude -p --output-format stream-json --verbose`` lines into the few facts the adapter needs.

Event shapes are from recorded real runs (swarm-framework ``validation/fixtures/v1-stream.jsonl``):
``result`` (subtype, result, total_cost_usd, usage, is_error, api_error_status, session_id),
``rate_limit_event`` (``rate_limit_info.unifiedWindows.{five_hour,seven_day}.utilization``)."""
import json
from typing import Optional


class Digest:
    def __init__(self):
        self.result: Optional[dict] = None
        self.util = {"five_hour": None, "seven_day": None}
        self.hooks_seen = 0
        self.session_id: Optional[str] = None
        self.denials = 0

    def feed(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        try:
            e = json.loads(line)
        except ValueError:
            return
        if not isinstance(e, dict):
            return
        t, st = e.get("type"), e.get("subtype")
        if e.get("session_id") and not self.session_id:
            self.session_id = e["session_id"]
        if t == "result":
            self.result = e
        elif t == "rate_limit_event":
            w = (e.get("rate_limit_info") or {}).get("unifiedWindows") or {}
            for k in ("five_hour", "seven_day"):
                u = (w.get(k) or {}).get("utilization")
                if isinstance(u, (int, float)):
                    self.util[k] = u
        elif t == "system" and st == "hook_started":
            self.hooks_seen += 1
        elif t == "system" and st == "permission_denied":
            self.denials += 1

    def feed_all(self, text: str) -> "Digest":
        for line in text.splitlines():
            self.feed(line)
        return self

    @property
    def cost(self) -> float:
        c = (self.result or {}).get("total_cost_usd")
        return float(c) if isinstance(c, (int, float)) else 0.0

    @property
    def text(self) -> str:
        r = (self.result or {}).get("result")
        return r if isinstance(r, str) else ""

    @property
    def ok(self) -> bool:
        r = self.result or {}
        return bool(r) and r.get("subtype") == "success" and not r.get("is_error")

    def usage(self) -> dict:
        u = (self.result or {}).get("usage") or {}
        inp = int(u.get("input_tokens") or 0) + int(u.get("cache_creation_input_tokens") or 0) \
            + int(u.get("cache_read_input_tokens") or 0)
        out = int(u.get("output_tokens") or 0)
        return {"input_tokens": inp, "output_tokens": out, "total_tokens": inp + out}
