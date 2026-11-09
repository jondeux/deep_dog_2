"""Which stretch of a run a ``claude -p`` call belongs to: "research" (the default) or "report".

A ContextVar, like the governor scope, so it follows the asyncio task that makes the call."""
import contextlib
import contextvars

_phase_var: contextvars.ContextVar = contextvars.ContextVar("claude_code_phase", default="research")


def current_phase() -> str:
    return _phase_var.get()


@contextlib.contextmanager
def report_scope():
    """Calls inside may use the reserve the governor kept back for writing the final report."""
    token = _phase_var.set("report")
    try:
        yield
    finally:
        _phase_var.reset(token)
