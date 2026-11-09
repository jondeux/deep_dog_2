"""Messages <-> text for a stateless ``claude -p`` call, and the tool-call envelope.

Deep Dog's engine runs its own tools. Claude therefore never executes them: the model RETURNS tool calls
as JSON and the engine (LangGraph) runs every loop, budget and gate itself. Three reply modes:
  plain  : no tools, no schema -> the reply text is the message
  tools  : reply is one JSON object {"content": str, "tool_calls": [{"name", "args"}]}
  schema : reply is one JSON object that validates against a JSON Schema
A malformed reply is repaired once by the caller, then surfaced as ``ProtocolError`` (never dropped silently)."""
import json
import uuid
from typing import Any, List, Optional, Tuple

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage


class ProtocolError(ValueError):
    """The model's reply did not follow the reply format."""


def text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                parts.append(str(b.get("text") or b.get("content") or ""))
            else:
                parts.append(str(b))
        return "".join(parts)
    return "" if content is None else str(content)


def render(messages: List[BaseMessage]) -> Tuple[str, str]:
    """``(system_text, transcript)``. System messages become the system prompt; the rest a tagged transcript."""
    system, lines = [], []
    for m in messages:
        if isinstance(m, SystemMessage):
            system.append(text_of(m.content))
        elif isinstance(m, HumanMessage):
            lines.append("[user]\n" + text_of(m.content))
        elif isinstance(m, AIMessage):
            body = text_of(m.content)
            if body.strip():
                lines.append("[assistant]\n" + body)
            calls = getattr(m, "tool_calls", None) or []
            if calls:
                lines.append("[assistant tool_calls]\n" + json.dumps(
                    [{"id": c.get("id"), "name": c.get("name"), "args": c.get("args")} for c in calls],
                    ensure_ascii=False))
        elif isinstance(m, ToolMessage):
            lines.append(f"[tool result name={getattr(m, 'name', '')} id={m.tool_call_id}]\n" + text_of(m.content))
        else:
            lines.append(f"[{getattr(m, 'type', 'message')}]\n" + text_of(m.content))
    return "\n\n".join(system), "\n\n".join(lines)


def tool_block(specs: list) -> str:
    out = []
    for s in specs:
        f = s.get("function", s)
        out.append(json.dumps({"name": f.get("name"), "description": f.get("description", ""),
                               "parameters": f.get("parameters", {})}, ensure_ascii=False))
    return "\n".join(out)


def build_prompt(transcript: str, specs: list, schema: Optional[dict], retry_note: str = "") -> str:
    head = "Below is the conversation so far, as tagged blocks. Write the next assistant turn.\n\n<conversation>\n" \
           + transcript + "\n</conversation>\n\n"
    if specs:
        tail = (
            "You can call these tools (one JSON object per line: name, description, parameters as JSON Schema):\n"
            + tool_block(specs) + "\n\n"
            "Reply with ONE JSON object and nothing else:\n"
            '{"content": "<your text, may be empty>", "tool_calls": [{"name": "<tool name>", "args": {...}}]}\n'
            "Use an empty tool_calls list when you are not calling a tool. You may call several independent "
            "tools in one reply. Only call tools listed above, with arguments that follow their schema.")
    elif schema is not None:
        tail = ("Reply with ONE JSON object that validates against this JSON Schema, and nothing else:\n"
                + json.dumps(schema, ensure_ascii=False))
    else:
        tail = "Reply with the assistant's next message only, as plain text."
    return head + tail + ("\n\n" + retry_note if retry_note else "")


def extract_json(text: str) -> Any:
    """First JSON value in ``text`` (code fences and surrounding prose tolerated)."""
    s = (text or "").strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else ""
        s = s.rsplit("```", 1)[0]
    dec = json.JSONDecoder()
    for i, ch in enumerate(s):
        if ch in "{[":
            try:
                val, _ = dec.raw_decode(s[i:])
                return val
            except ValueError:
                continue
    raise ProtocolError("reply contained no JSON object")


def _check_args(name: str, args: Any, params: dict) -> None:
    if not isinstance(args, dict):
        raise ProtocolError(f"tool {name!r}: args must be an object")
    try:
        import jsonschema
    except ImportError:
        jsonschema = None
    if jsonschema is not None and params:
        try:
            jsonschema.validate(args, params)
        except jsonschema.ValidationError as e:
            raise ProtocolError(f"tool {name!r}: arguments do not match the schema: {e.message}") from None
        return
    for req in (params or {}).get("required", []):
        if req not in args:
            raise ProtocolError(f"tool {name!r}: missing required argument {req!r}")


def parse_envelope(text: str, specs: list) -> Tuple[str, List[dict]]:
    """``(content, tool_calls)`` from a tools-mode reply, validated against the bound tool schemas."""
    obj = extract_json(text)
    if not isinstance(obj, dict):
        raise ProtocolError("reply JSON is not an object")
    content = obj.get("content", "")
    calls = obj.get("tool_calls", [])
    if not isinstance(calls, list):
        raise ProtocolError("tool_calls must be a list")
    by_name = {(s.get("function", s)).get("name"): (s.get("function", s)).get("parameters", {}) for s in specs}
    out = []
    for c in calls:
        if not isinstance(c, dict) or "name" not in c:
            raise ProtocolError("each tool call needs a name")
        name = c["name"]
        if name not in by_name:
            raise ProtocolError(f"unknown tool {name!r}; listed tools: {', '.join(by_name)}")
        args = c.get("args", {}) or {}
        _check_args(name, args, by_name[name])
        out.append({"id": "call_" + uuid.uuid4().hex[:12], "name": name, "args": args, "type": "tool_call"})
    return (content if isinstance(content, str) else text_of(content)), out
