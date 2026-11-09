"""Run Deep Dog's LLM calls through the unmodified ``claude`` CLI on the owner's own Claude plan.

Model names: ``claudecode:<model>[@<effort>]`` (see ``models.py``). Personal use only: this provider must
not be exposed to other people's requests (the public web UI refuses it)."""
from typing import Optional

from .chat_model import ClaudeCodeCallError, ClaudeCodeChatModel
from .governor import Governor, GovernorStop
from .models import EFFORTS, MODEL_IDS, PREFIX, catalog, is_claudecode, parse_name, validate_effort
from .phase import current_phase, report_scope
from .preflight import ClaudeCodeUnavailable, ensure_ready
from .runtime import active_governor, governor_scope
from .transcript import ProtocolError
from .usage import format_usage


def build_chat_model(model_name: str, *, temperature: float = 0, max_tokens: Optional[int] = None,
                     reasoning_effort: Optional[str] = None) -> ClaudeCodeChatModel:
    """The chat model for a ``claudecode:`` name. Raises ``ClaudeCodeUnavailable`` (with the fix) if the
    binary is missing or not signed in. ``temperature`` is accepted for signature parity and ignored."""
    model_id, effort = parse_name(model_name, default_effort=reasoning_effort)
    info = ensure_ready()
    return ClaudeCodeChatModel(model_id=model_id, effort=effort, max_tokens=max_tokens, binary=info["binary"],
                               governor=active_governor())


__all__ = ["ClaudeCodeCallError", "ClaudeCodeChatModel", "ClaudeCodeUnavailable", "EFFORTS", "Governor",
           "GovernorStop", "MODEL_IDS", "PREFIX", "ProtocolError", "active_governor", "build_chat_model",
           "catalog", "current_phase", "ensure_ready", "format_usage", "governor_scope", "is_claudecode",
           "parse_name", "report_scope", "validate_effort"]
