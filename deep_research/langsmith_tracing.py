"""Opt-in LangSmith tracing for one research run.

When ``RunConfig.langsmith_tracing`` is true and the run's ``Credentials``
carry a ``LANGSMITH_API_KEY``, ``langsmith_scope`` captures the whole
trajectory (supervisor, sub-agents, model and tool calls made through
LangChain/LangGraph) in the caller's own LangSmith project.

Design constraints, all enforced by ``tests/test_langsmith_tracing.py``:

- Per-run and isolated: the scope builds its own ``langsmith.Client`` from the
  run's credentials and enters ``langsmith.tracing_context`` (contextvar
  based), so concurrent runs with different keys/projects never mix. The
  process environment is never read or written by this module, and nothing
  happens at all when the config flag is off, so for command-line use an
  operator's own ``LANGSMITH_TRACING`` environment setup is left alone. In web-UI
  mode (``credentials.langsmith_from_env`` false) an unticked run is explicitly
  run with tracing disabled, so a server-side ``LANGSMITH_TRACING`` cannot trace
  a visitor's run into the operator's account.
- Secret-safe: the API key (and an endpoint carrying userinfo) never goes into
  metadata, tags, run names, logs or trace records.
- Best effort: any tracing failure (bad key, network error, client exception,
  flush hang) is caught, reported once as a short redacted warning, and the
  research run continues. The final flush is bounded.

It is an *async* context manager on purpose: the bounded flush at exit must
not block the event loop that other concurrent runs share.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import sys
import threading
from typing import Any, AsyncIterator, Optional

_log = logging.getLogger(__name__)

API_KEY_NAME = "LANGSMITH_API_KEY"
ENDPOINT_NAME = "LANGSMITH_ENDPOINT"
DEFAULT_PROJECT = "deep-dog-2"
TAG = "deep_dog_2"
FLUSH_TIMEOUT_SECONDS = 10.0
_MAX_WARNING_CHARS = 200


def effective_project(config: Any) -> str:
    """The LangSmith project a run with this config reports to."""
    project = getattr(config, "langsmith_project", None)
    project = project.strip() if isinstance(project, str) else ""
    return project or DEFAULT_PROJECT


def _redact(text: str, secrets: list) -> str:
    """Replace every secret in ``text``, longest first (so a secret that contains a
    shorter one is not left half visible)."""
    for secret in sorted({s for s in secrets if s}, key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    return text


class LangSmithScope:
    """Handle yielded by ``langsmith_scope``. Safe to ignore."""

    def __init__(self) -> None:
        self.active = False
        self.project: Optional[str] = None
        self._root: Any = None
        self._warned = False

    def finish(self, status: str) -> None:
        """Record the run's final status on the root run (best effort)."""
        root = self._root
        if root is None:
            return
        try:
            root.end(outputs={"status": status})
        except Exception:  # noqa: BLE001 - tracing must never affect the run
            _log.debug("LangSmith root run end() failed", exc_info=False)


def _run_metadata(config: Any, run_id: str, extra: Optional[dict]) -> dict:
    meta = {
        "run_id": run_id,
        "profile": getattr(config, "profile", None),
        "supervisor_model_chain": list(getattr(config, "supervisor_model_fallback_chain", []) or []),
        "draft_report_model_chain": list(getattr(config, "draft_report_model_fallback_chain", []) or []),
        "subagent_model_chain": list(getattr(config, "subagent_model_fallback_chain", []) or []),
        "web_search_engine": getattr(config, "web_search_engine", None),
        "thinking_mode": getattr(config, "thinking_mode", None),
    }
    if extra:
        meta.update(extra)
    return meta


def _call_flush(client: Any, timeout: float) -> None:
    """``client.flush()``; pass ``timeout`` where the installed library takes it."""
    try:
        accepts_timeout = "timeout" in inspect.signature(client.flush).parameters
    except (TypeError, ValueError):
        accepts_timeout = False
    if accepts_timeout:
        client.flush(timeout=timeout)
    else:
        client.flush()


async def _flush_bounded(client: Any, timeout: float) -> None:
    """Flush in a daemon thread and wait at most ``timeout`` seconds.

    A dedicated daemon thread (not ``asyncio.to_thread``) so a hung flush can
    neither stall the event loop nor keep ``asyncio.run``/interpreter shutdown
    waiting for the default executor. Raises on timeout or flush error.
    """
    loop = asyncio.get_running_loop()
    done: asyncio.Future = loop.create_future()

    def _settle(error: Optional[BaseException]) -> None:
        if done.done():
            return
        if error is None:
            done.set_result(None)
        else:
            done.set_exception(error)

    def _target() -> None:
        error: Optional[BaseException] = None
        try:
            _call_flush(client, timeout)
        except BaseException as exc:  # noqa: BLE001 - reported to the awaiting task
            error = exc
        with contextlib.suppress(RuntimeError):  # loop already closed
            loop.call_soon_threadsafe(_settle, error)

    threading.Thread(target=_target, name="deep-dog-langsmith-flush", daemon=True).start()
    await asyncio.wait_for(done, timeout)


@contextlib.asynccontextmanager
async def langsmith_scope(
    config: Any,
    credentials: Any,
    run_id: str,
    metadata: Optional[dict] = None,
    *,
    observer: Any = None,
) -> AsyncIterator[LangSmithScope]:
    """Trace everything run inside this block to the caller's LangSmith project.

    A no-op unless ``config.langsmith_tracing`` is true and ``credentials``
    resolve a ``LANGSMITH_API_KEY``. ``observer`` (optional) receives the one
    redacted warning as a trace record in addition to the logging module.
    """
    scope = LangSmithScope()
    if not getattr(config, "langsmith_tracing", False):
        if credentials is not None and getattr(credentials, "langsmith_from_env", True) is False:
            # Web-UI mode: the operator's own LANGSMITH_TRACING/LANGCHAIN_TRACING
            # environment must not trace a visitor's unticked run into the
            # operator's account. (Command-line runs keep the operator's env tracing.)
            try:
                import langsmith
                ctx = langsmith.tracing_context(enabled=False)
            except Exception:  # noqa: BLE001 - never fail the run over tracing
                ctx = contextlib.nullcontext()
            with ctx:
                yield scope
            return
        yield scope
        return

    key = credentials.get(API_KEY_NAME) if credentials is not None else None
    endpoint = credentials.get(ENDPOINT_NAME) if credentials is not None else None
    secrets = [s for s in (key, endpoint if endpoint and "@" in endpoint else None) if s]

    def warn(stage: str, exc: Optional[BaseException] = None) -> None:
        if scope._warned:
            return
        scope._warned = True
        detail = ""
        if exc is not None:
            # Redact the whole text first, then cut: a secret straddling the cut
            # would otherwise leave a readable prefix.
            detail = f" ({type(exc).__name__}: {_redact(str(exc), secrets)[:_MAX_WARNING_CHARS]})"
        message = _redact(f"LangSmith tracing {stage}{detail}; the research run continues.", secrets)
        _log.warning("%s", message)
        if observer is not None:
            with contextlib.suppress(Exception):
                observer.emit_trace("langsmith_warning", phase="run", agent="runner",
                                    message=message)

    if not key:
        warn("requested but LANGSMITH_API_KEY is not set, so tracing is off")
        yield scope
        return

    project = effective_project(config)
    stack = contextlib.ExitStack()
    client: Any = None
    try:
        import langsmith

        # omit_traced_runtime_info: the client otherwise copies the operator's
        # LANGSMITH_*/LANGCHAIN_* environment variables into each run's metadata.
        client_kwargs: dict = {"api_key": key, "omit_traced_runtime_info": True}
        if endpoint:
            client_kwargs["api_url"] = endpoint
        if getattr(credentials, "langsmith_from_env", True) is False:
            # Web-UI mode: an operator LANGSMITH_WORKSPACE_ID must not be sent as the
            # X-Tenant-Id header with a visitor's key. "" is explicit, so the library
            # does not fall back to the environment.
            client_kwargs["workspace_id"] = ""
        client = langsmith.Client(**client_kwargs)
        stack.enter_context(langsmith.tracing_context(
            enabled=True,
            client=client,
            project_name=project,
            tags=[TAG],
            metadata=_run_metadata(config, run_id, metadata),
        ))
        scope._root = stack.enter_context(langsmith.trace(
            f"{TAG}:{run_id}",
            run_type="chain",
            project_name=project,
            client=client,
            inputs={"run_id": run_id},
        ))
        scope.active = True
        scope.project = project
    except Exception as exc:  # noqa: BLE001 - never fail the run over tracing
        warn("could not start", exc)
        scope.active = False
        scope._root = None
        with contextlib.suppress(Exception):
            stack.close()
        stack = contextlib.ExitStack()

    try:
        yield scope
    finally:
        exc_info = sys.exc_info()
        if scope.active:
            try:
                stack.__exit__(*exc_info)
            except Exception as exc:  # noqa: BLE001
                warn("could not close the root run", exc)
            try:
                await _flush_bounded(client, FLUSH_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                warn(f"flush did not finish within {FLUSH_TIMEOUT_SECONDS:g}s; some traces may be missing")
            except Exception as exc:  # noqa: BLE001
                warn("flush failed", exc)
