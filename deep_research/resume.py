"""Resume state (the ``deep-research-memory`` folder): continue a run that a usage cap cut short.

A run on the Claude plan can stop early when a cap or the plan-window guard is reached. Its report is still
written from the findings so far (see the engine map), and the state needed to keep researching is saved here as
plain JSON. ``resume_research`` (integration.py) feeds it back into the supervisor, so the run picks up where it
stopped: same history, same sources, same iteration count, and the research clock continues from the minute it
stopped at (the time spent waiting for the window to reset is not counted).

The file holds the question, the findings and the model settings. It holds no credentials and no sign-in."""
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, messages_from_dict, messages_to_dict

from deep_research.run_config import RunConfig

SNAPSHOT_VERSION = 1
ARTIFACT_NAME = "resume_state"
DEFAULT_DIR = "deep-research-memory"
_ID_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")
_INTERRUPTED = ("This round was interrupted by a usage limit before its sub-agents finished, so nothing came back. "
                "If the topic is still needed, delegate it again.")


def _check_id(value: Any) -> str:
    if not isinstance(value, str) or not _ID_OK.match(value) or ".." in value:
        raise ValueError(f"Invalid resume id {value!r}: use letters, digits, '.', '_' or '-' only.")
    return value


def _messages(items) -> list:
    return [m if not isinstance(m, str) else HumanMessage(content=m) for m in (items or [])]


def close_open_tool_calls(messages) -> list:
    """Answer every tool call that has no result, right after its assistant message, so the history stays valid."""
    msgs = list(messages)
    out, i = [], 0
    while i < len(msgs):
        m = msgs[i]
        out.append(m)
        i += 1
        calls = getattr(m, "tool_calls", None) if isinstance(m, AIMessage) else None
        if not calls:
            continue
        answered = set()
        while i < len(msgs) and isinstance(msgs[i], ToolMessage):
            answered.add(msgs[i].tool_call_id)
            out.append(msgs[i])
            i += 1
        for tc in calls:
            if tc.get("id") and tc["id"] not in answered:
                out.append(ToolMessage(content=_INTERRUPTED, name=tc.get("name", ""), tool_call_id=tc["id"]))
    return out


def build_snapshot(prompt: str, cfg: RunConfig, state: dict, *, run_id: str, thread_id: str, reason: str,
                   elapsed_minutes: Optional[float] = None, report_mode: Optional[str] = None,
                   root_id: Optional[str] = None, resumes: int = 0) -> dict:
    """JSON-safe snapshot of a run that stopped early. ``state`` is the outer graph's last state."""
    if elapsed_minutes is None:
        started = float(state.get("start_time") or 0.0)
        elapsed_minutes = max(0.0, (time.time() - started) / 60.0) if started > 0 else 0.0
    return {
        "version": SNAPSHOT_VERSION,
        "root_id": _check_id(root_id or thread_id),
        "run_id": run_id,
        "thread_id": thread_id,
        "resumes": int(resumes),
        "prompt": prompt,
        "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "stopped_early": reason,
        "report_mode": report_mode,
        "elapsed_minutes": round(float(elapsed_minutes), 3),
        "config": cfg.to_dict(),
        "state": {
            "messages": messages_to_dict([HumanMessage(content=prompt)]),
            "research_brief": state.get("research_brief") or "",
            "draft_report": state.get("draft_report") or "",
            "target_language": state.get("target_language"),
            "input_language": state.get("input_language"),
            "research_iterations": int(state.get("research_iterations") or 0),
            "supervisor_messages": messages_to_dict(_messages(state.get("supervisor_messages"))),
            "notes": [n if isinstance(n, str) else str(n) for n in (state.get("notes") or [])],
            "curated_sources": list(state.get("curated_sources") or []),
            "source_registry": list(state.get("source_registry") or []),
        },
    }


def _validate(snapshot: Any) -> dict:
    if not isinstance(snapshot, dict):
        raise ValueError("Resume state must be a JSON object.")
    if snapshot.get("version") != SNAPSHOT_VERSION:
        raise ValueError(f"Resume state version {snapshot.get('version')!r} is not supported "
                         f"(this build reads version {SNAPSHOT_VERSION}).")
    if not isinstance(snapshot.get("state"), dict) or not isinstance(snapshot.get("config"), dict):
        raise ValueError("Resume state is missing its 'state' or 'config'.")
    if not isinstance(snapshot.get("prompt"), str) or not snapshot["prompt"].strip():
        raise ValueError("Resume state has no question (prompt).")
    _check_id(snapshot.get("root_id"))
    return snapshot


def restore_inputs(snapshot: dict) -> dict:
    """The outer-graph state to start from. The research clock continues from ``elapsed_minutes``."""
    snap = _validate(snapshot)
    st = snap["state"]
    elapsed = max(0.0, float(snap.get("elapsed_minutes") or 0.0))
    return {
        "messages": messages_from_dict(st.get("messages") or []),
        "research_brief": st.get("research_brief") or "",
        "draft_report": st.get("draft_report") or "",
        "target_language": st.get("target_language"),
        "input_language": st.get("input_language"),
        "research_iterations": int(st.get("research_iterations") or 0),
        "supervisor_messages": close_open_tool_calls(messages_from_dict(st.get("supervisor_messages") or [])),
        "notes": list(st.get("notes") or []),
        "curated_sources": list(st.get("curated_sources") or []),
        "source_registry": list(st.get("source_registry") or []),
        "start_time": time.time() - elapsed * 60.0,
        "config_snapshot": snap["config"],
    }


def config_from_snapshot(snapshot: dict) -> RunConfig:
    return RunConfig.from_dict(_validate(snapshot)["config"])


def snapshot_path(directory, root_id: str) -> Path:
    return Path(directory) / f"{_check_id(root_id)}.json"


def save_snapshot(directory, snapshot: dict) -> Path:
    """Write ``<dir>/<root_id>.json`` (the latest state of that run's chain). The file it replaces is kept
    once, as ``<root_id>.prev.json``."""
    snap = _validate(snapshot)
    path = snapshot_path(directory, snap["root_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.replace(path.with_name(f"{snap['root_id']}.prev.json"))
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(snap, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    tmp.replace(path)
    return path


def load_snapshot(directory, root_id: str) -> dict:
    path = snapshot_path(directory, root_id)
    if not path.is_file():
        raise ValueError(f"No resume file for {root_id!r} in {Path(directory)}.")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"Resume file {path} is not valid JSON: {e}") from None
    return _validate(data)


class FileSnapshotSink:
    """An artifact sink (``RuntimeOptions.artifact_sink``) that keeps only the resume state, on disk."""

    def __init__(self, directory):
        self.directory = Path(directory)
        self.path: Optional[Path] = None

    def store(self, name: str, data: Any) -> None:
        if name == ARTIFACT_NAME and data:
            self.path = save_snapshot(self.directory, data)
