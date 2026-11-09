"""LangChain chat model that runs one stateless, tool-less ``claude -p`` process per call.

Boundaries (never crossed): the unmodified ``claude`` binary under the owner's own sign-in; no credential is
read, stored or forwarded; children get an allowlisted environment and an empty working directory; no
``--bare``; the plan-window and cost caps in ``Governor`` stop a run before the window is exhausted."""
import asyncio
import json
import os
import shutil
import signal
import subprocess
import tempfile
from typing import Any, List, Optional

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import ConfigDict, Field

from .env import agent_env
from .events import Digest
from .governor import Governor, GovernorStop
from .preflight import ensure_ready
from .runtime import active_governor
from .transcript import ProtocolError, build_prompt, extract_json, parse_envelope, render

# Flags every child gets. The system prompt, the tool list and the model come from the call.
# `disableAllHooks` / `--setting-sources` effects are [U] until the P2 probe; `Digest.hooks_seen` measures them.
DEFAULT_FLAGS = ["--strict-mcp-config", "--no-session-persistence", "--permission-mode", "dontAsk",
                 "--disable-slash-commands", "--settings", json.dumps({"disableAllHooks": True})]
MAX_ARGV_SYSTEM_BYTES = 100_000          # one argv string is capped near 128 KB on Linux; bigger goes through stdin
DEFAULT_SYSTEM = "You are a helpful assistant."
_PLAN_LIMIT_HINTS = ("usage limit", "rate limit", "limit reached", "hit your limit")


class ClaudeCodeCallError(RuntimeError):
    pass


def _scrub(text: str) -> str:
    try:
        from deep_research.secrets import scrub_secrets
        return scrub_secrets(text)
    except Exception:
        return text


def _kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


class ClaudeCodeChatModel(BaseChatModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    model_id: str
    effort: Optional[str] = None
    max_tokens: Optional[int] = None
    tool_specs: list = Field(default_factory=list)
    response_schema: Optional[dict] = None
    binary: Optional[str] = None
    governor: Any = Field(default=None, exclude=True)
    extra_flags: list = Field(default_factory=lambda: list(DEFAULT_FLAGS))
    # Claude Code's own tools this model may use inside the call (e.g. ["WebSearch"] for the search backend).
    # Empty (the default) means NO tools: the engine runs its own tools. [V] WebSearch headless: validation/p2.
    native_tools: list = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "claude-code"

    @property
    def _identifying_params(self) -> dict:
        return {"model_id": self.model_id, "effort": self.effort}

    # ── LangChain surface ─────────────────────────────────────────────
    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        specs = [convert_to_openai_tool(t) for t in tools]
        return self.model_copy(update={"tool_specs": specs, "response_schema": None})

    def with_structured_output(self, schema, *, include_raw: bool = False, method: Optional[str] = None, **kwargs):
        is_model = hasattr(schema, "model_json_schema")
        json_schema = schema.model_json_schema() if is_model else dict(schema)
        bound = self.model_copy(update={"response_schema": json_schema, "tool_specs": []})

        def parse(msg: AIMessage):
            obj = extract_json(msg.content if isinstance(msg.content, str) else str(msg.content))
            return schema.model_validate(obj) if is_model else obj
        return bound | RunnableLambda(parse)

    # ── process plumbing ──────────────────────────────────────────────
    def _gov(self) -> Governor:
        return self.governor or active_governor()

    def _argv(self, system: str) -> list:
        binary = self.binary or ensure_ready()["binary"]
        argv = [binary, "-p", "--model", self.model_id, "--output-format", "stream-json", "--verbose"]
        if self.effort:
            argv += ["--effort", self.effort]
        argv += ["--system-prompt", system or DEFAULT_SYSTEM, "--tools", ",".join(self.native_tools)]
        argv += list(self.extra_flags)
        if self.native_tools:                       # variadic flag: keep it last
            argv += ["--allowedTools", *self.native_tools]
        return argv

    def _env(self) -> dict:
        extra = {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": self.max_tokens} if self.max_tokens else None
        return agent_env(extra=extra)

    def _split_system(self, system: str, prompt: str):
        if system and len(system.encode("utf-8")) > MAX_ARGV_SYSTEM_BYTES:
            return "Follow the instructions in the [system] block of the conversation.", \
                   "[system]\n" + system + "\n\n" + prompt
        return system, prompt

    def _finish(self, d: Digest, rc: Optional[int], stderr: str) -> Digest:
        gov = self._gov()
        gov.ingest(d, model=self.model_id + ("+" + ",".join(self.native_tools) if self.native_tools else ""))
        if d.result is None:
            raise ClaudeCodeCallError(f"claude -p exited {rc} without a result: {_scrub(stderr[-300:])}")
        if not d.ok:
            r = d.result
            status = r.get("api_error_status")
            text = (r.get("result") or "") if isinstance(r.get("result"), str) else ""
            if status == 429 or any(h in text.lower() for h in _PLAN_LIMIT_HINTS):
                gov.stopped = "plan_limit_reached"
                raise GovernorStop("plan_limit_reached", _scrub(text[:200]) or f"api_error_status={status}")
            raise ClaudeCodeCallError(
                f"claude -p failed (subtype={r.get('subtype')}, api_error_status={status}): {_scrub(text[:300])}")
        return d

    def _run_sync(self, system: str, prompt: str) -> Digest:
        gov = self._gov()
        system, prompt = self._split_system(system, prompt)
        gov.check()
        gov.acquire()
        cwd = tempfile.mkdtemp(prefix="ddcc-")
        try:
            gov.check()
            proc = subprocess.Popen(self._argv(system), cwd=cwd, env=self._env(), stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                    start_new_session=True)
            try:
                out, err = proc.communicate(prompt, timeout=gov.timeout_s)
            except subprocess.TimeoutExpired:
                _kill_group(proc.pid)
                proc.communicate()
                raise TimeoutError(f"claude -p timed out after {gov.timeout_s:.0f}s") from None
            except BaseException:
                _kill_group(proc.pid)
                raise
            return self._finish(Digest().feed_all(out), proc.returncode, err)
        finally:
            shutil.rmtree(cwd, ignore_errors=True)
            gov.release()

    async def _run_async(self, system: str, prompt: str) -> Digest:
        gov = self._gov()
        system, prompt = self._split_system(system, prompt)
        gov.check()
        await gov.acquire_async()
        cwd = tempfile.mkdtemp(prefix="ddcc-")
        proc = None
        try:
            gov.check()
            proc = await asyncio.create_subprocess_exec(
                *self._argv(system), cwd=cwd, env=self._env(), stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
            try:
                out, err = await asyncio.wait_for(proc.communicate(prompt.encode("utf-8")), gov.timeout_s)
            except asyncio.TimeoutError:
                _kill_group(proc.pid)
                await proc.wait()
                raise TimeoutError(f"claude -p timed out after {gov.timeout_s:.0f}s") from None
            except BaseException:                       # cancellation: never leave the process group running
                _kill_group(proc.pid)
                try:                                    # reap it too, or a zombie outlives the run
                    await asyncio.wait_for(asyncio.shield(proc.wait()), 2.0)
                except BaseException:
                    pass
                raise
            return self._finish(Digest().feed_all(out.decode("utf-8", "replace")), proc.returncode,
                                err.decode("utf-8", "replace"))
        finally:
            shutil.rmtree(cwd, ignore_errors=True)
            gov.release()

    # ── reply interpretation (one repair retry, then a protocol error) ─
    def _interpret(self, text: str):
        if self.tool_specs:
            return parse_envelope(text, self.tool_specs)
        if self.response_schema is not None:
            obj = extract_json(text)
            try:
                import jsonschema
                jsonschema.validate(obj, self.response_schema)
            except ImportError:
                pass
            except jsonschema.ValidationError as e:
                raise ProtocolError(f"reply does not match the schema: {e.message}") from None
            return json.dumps(obj, ensure_ascii=False), []
        return text, []

    def _result(self, content: str, calls: list, d: Digest) -> ChatResult:
        r = d.result or {}
        msg = AIMessage(content=content, tool_calls=calls, usage_metadata=d.usage(),
                        response_metadata={"model": self.model_id, "finish_reason": r.get("stop_reason"),
                                           "cost_usd": d.cost, "session_id": d.session_id})
        return ChatResult(generations=[ChatGeneration(message=msg)],
                          llm_output={"cost_usd": d.cost, "session_id": d.session_id})

    @staticmethod
    def _retry_note(err: Exception, text: str) -> str:
        return (f"Your previous reply was rejected: {err}. It was:\n{text[:1500]}\n"
                "Reply again with ONE valid JSON object only, following the format above.")

    def _generate(self, messages: List[BaseMessage], stop=None, run_manager=None, **kwargs) -> ChatResult:
        system, transcript = render(messages)
        note = ""
        for attempt in (1, 2):
            d = self._run_sync(system, build_prompt(transcript, self.tool_specs, self.response_schema, note))
            try:
                content, calls = self._interpret(d.text)
                return self._result(content, calls, d)
            except ProtocolError as e:
                if attempt == 2:
                    raise
                note = self._retry_note(e, d.text)
        raise AssertionError("unreachable")

    async def _agenerate(self, messages: List[BaseMessage], stop=None, run_manager=None, **kwargs) -> ChatResult:
        system, transcript = render(messages)
        note = ""
        for attempt in (1, 2):
            d = await self._run_async(system, build_prompt(transcript, self.tool_specs, self.response_schema, note))
            try:
                content, calls = self._interpret(d.text)
                return self._result(content, calls, d)
            except ProtocolError as e:
                if attempt == 2:
                    raise
                note = self._retry_note(e, d.text)
        raise AssertionError("unreachable")
