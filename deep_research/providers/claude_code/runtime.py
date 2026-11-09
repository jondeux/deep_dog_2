"""Per-run governor scope (same ContextVar pattern as ``config.credential_scope``)."""
import contextlib
import contextvars
from typing import Optional

from .governor import Governor

_governor_var: contextvars.ContextVar = contextvars.ContextVar("claude_code_governor", default=None)
_default: Optional[Governor] = None


@contextlib.contextmanager
def governor_scope(governor: Governor):
    token = _governor_var.set(governor)
    try:
        yield governor
    finally:
        _governor_var.reset(token)


def active_governor() -> Governor:
    """The run's governor, or a process-wide default for standalone ``get_model`` use."""
    global _default
    g = _governor_var.get()
    if g is not None:
        return g
    if _default is None:
        _default = Governor()
    return _default
