"""Model names, ids and effort levels for the ``claudecode:`` provider.

Name grammar: ``claudecode:<model>[@<effort>]``, e.g. ``claudecode:sonnet-5.5@high``.
The effort suffix is per chain entry, so supervisor, draft writer, sub-agents, per-platform
chains and fallback entries can each carry their own level.
"""
from typing import Optional, Tuple

PREFIX = "claudecode:"
# Ids as used by swarm-framework (verified live there on a plan sign-in).
MODEL_IDS = {
    "haiku-5.5": "claude-haiku-5-5",
    "sonnet-5.5": "claude-sonnet-5-5",
    "opus-5.5": "claude-opus-5-5",
}
# Levels accepted by the installed `claude --effort` (checked with `claude --help`, 2.1.319).
EFFORTS = ("low", "medium", "high", "xhigh", "max")


def is_claudecode(name) -> bool:
    return isinstance(name, str) and name.lower().startswith(PREFIX)


def validate_effort(effort: Optional[str]) -> Optional[str]:
    if effort is None or str(effort).strip() == "":
        return None
    e = str(effort).strip().lower()
    if e not in EFFORTS:
        raise ValueError(f"Invalid effort {effort!r}. Use one of: {', '.join(EFFORTS)}.")
    return e


def parse_name(name: str, default_effort: Optional[str] = None) -> Tuple[str, Optional[str]]:
    """``(claude model id, effort)`` for a ``claudecode:`` name. An ``@effort`` suffix wins over ``default_effort``."""
    if not is_claudecode(name):
        raise ValueError(f"Not a claudecode model name: {name!r}")
    rest = name[len(PREFIX):].strip()
    model, _, suffix = rest.partition("@")
    model = model.strip()
    if not model:
        raise ValueError(f"Model name {name!r} has no model after the 'claudecode:' prefix.")
    key = model.lower()
    if key in MODEL_IDS:
        model_id = MODEL_IDS[key]
    elif key.startswith("claude-"):
        model_id = model
    else:
        raise ValueError(
            f"Unknown Claude model {model!r}. Use one of {', '.join(MODEL_IDS)} or a full 'claude-...' id.")
    return model_id, validate_effort(suffix) if suffix.strip() else validate_effort(default_effort)


def catalog() -> dict:
    """What a picker can offer: model names and effort levels."""
    return {"models": list(MODEL_IDS), "efforts": list(EFFORTS)}
