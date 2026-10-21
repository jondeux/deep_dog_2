"""Deep Dog 2 web UI.

A Gradio front end for ``deep_research.integration.run_research``. It lets a user
pick the models for each role (supervisor, sub-agents, draft writer), the agents
and search engine, and the research budget, then watch progress and read the
cited report.

Credentials: provider keys are typed into the page and passed to the engine as
a per-run ``Credentials`` object. They are never written to disk, never put in
``os.environ`` and never echoed back. The engine still falls back to the
server's environment for any key not typed in, so do not set provider keys as
Space secrets unless you want every visitor to spend them.

Run locally:   python webui/app.py
On a Space:    python app.py --host 0.0.0.0 --port 7860
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import os
import tempfile
import time
from pathlib import Path

import gradio as gr

from deep_research import config as _cfg
from deep_research.cancellation import CancellationToken
from deep_research.integration import RuntimeOptions, run_research, validate_credentials
from deep_research.models import Credentials
from deep_research.run_config import RunConfig

# ZeroGPU Spaces refuse to start unless some function is decorated with
# @spaces.GPU. This engine never uses a GPU, so the marker is a no-op that is
# never called. `spaces` exists only on Hugging Face; elsewhere this is skipped.
try:
    import spaces

    @spaces.GPU
    def _zerogpu_marker() -> None:
        return None
except ImportError:
    pass

# ZeroGPU Spaces refuse to start unless the app defines at least one
# @spaces.GPU function. The engine never uses a GPU, so this is a no-op that only
# satisfies that startup check. Off ZeroGPU the `spaces` package is absent or inert.
try:
    import spaces

    @spaces.GPU
    def _zerogpu_startup_check() -> None:
        return None
except Exception:  # not on Spaces, or `spaces` not installed
    pass

# ── Choices ───────────────────────────────────────────────────────────────

# Names the engine is known to resolve. The model boxes accept any name, so this
# list is a convenience, not a limit; an unknown name fails at run time with the
# engine's own "cannot determine provider" error.
_MAP = getattr(_cfg, "_OPENROUTER_MODEL_MAP_BY_NAME", {}) or {}
MODEL_CHOICES = sorted(set(_MAP) | {"gemini-3-flash-preview", "gpt-5.2"})
AGENT_CHOICES = list(_cfg.KNOWN_AGENT_NAMES)
KEY_FIELDS = [
    ("DEEPSEEK_API_KEY", "DeepSeek"),
    ("OPENROUTER_API_KEY", "OpenRouter"),
    ("OPENAI_API_KEY", "OpenAI"),
    ("GEMINI_API_KEY", "Google Gemini"),
    ("MIMO_API_KEY", "MiMo"),
    ("EXA_API_KEY", "Exa search"),
    ("TAVILY_API_KEY", "Tavily search"),
]

# Operator limits, set as Space variables. They cap what a visitor can request.
MAX_MINUTES = int(os.environ.get("UI_MAX_MINUTES", "30"))
MAX_CONCURRENT_RUNS = int(os.environ.get("UI_MAX_CONCURRENT_RUNS", "2"))

# The first preset mirrors the README's published benchmark profile.
PRESETS = {
    "Benchmark profile (Pro supervisor, Flash sub-agents)": dict(
        sup=["deepseek-v4-pro"], sub=["deepseek-v4-flash"], draft=["deepseek-v4-flash"],
        minutes=15, iters=20, searches=3, engine="exa"),
    "Quick and cheap (Flash everywhere)": dict(
        sup=["deepseek-v4-flash"], sub=["deepseek-v4-flash"], draft=["deepseek-v4-flash"],
        minutes=8, iters=12, searches=3, engine="exa"),
}
DEFAULT_PRESET = "Quick and cheap (Flash everywhere)"

# One running research per browser session.
_TOKENS: dict[str, CancellationToken] = {}
_RUNNING: set[str] = set()


# ── Helpers ───────────────────────────────────────────────────────────────

def parse_extra_keys(text: str) -> dict[str, str]:
    """Parse ``NAME=value`` entries split on ``;`` or newlines (Reddit, PubMed email, ...)."""
    out: dict[str, str] = {}
    for line in (text or "").replace(";", "\n").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if name and value:
            out[name] = value
    return out


def build_config(sup, sub, draft, route_or, thinking, minutes, iters, searches,
                 concurrency, agents, engine) -> RunConfig:
    minutes = min(float(minutes), float(MAX_MINUTES))
    return RunConfig(
        supervisor_model_fallback_chain=list(sup),
        subagent_model_fallback_chain=list(sub),
        draft_report_model_fallback_chain=list(draft),
        route_via_openrouter=bool(route_or),
        thinking_mode=thinking,
        research_time_min_minutes=min(5.0, minutes),
        research_time_max_minutes=minutes,
        supervisor_max_iterations=int(iters),
        subagent_max_searches=int(searches),
        subagent_max_concurrency=int(concurrency),
        enabled_agents=list(agents),
        web_search_engine=engine,
        # A web session keeps nothing on the server.
        output_mode="none",
        log_mode="none",
        save_report_to_file=False,
        save_subagent_reports_to_file=False,
        enable_source_log=False,
        enable_research_trace=False,
        enable_subtopic_generation=False,
    ).finalize()


def build_credentials(key_values: list[str], extra: str) -> Credentials:
    keys = {name: (value or "").strip()
            for (name, _), value in zip(KEY_FIELDS, key_values) if (value or "").strip()}
    keys.update(parse_extra_keys(extra))
    return Credentials(keys=keys)


def format_event(ev, t0: float) -> str:
    """One readable progress line. Events carry metadata only, never reasoning."""
    secs = max(0, int(time.time() - t0))
    who = ev.agent or ev.platform or ""
    bits = []
    for k, v in list((ev.payload or {}).items())[:3]:
        text = str(v).replace("\n", " ")
        bits.append(f"{k}={text[:80]}")
    parts = [f"[{secs // 60:02d}:{secs % 60:02d}]", ev.type]
    if who:
        parts.append(f"· {who}")
    if ev.iteration is not None:
        parts.append(f"· iter {ev.iteration}")
    if bits:
        parts.append("· " + ", ".join(bits))
    return " ".join(parts)


def source_rows(result) -> list[list[str]]:
    rows = []
    for e in (result.source_registry or []):
        rows.append([str(e.get("code") or ""), str(e.get("title") or ""),
                     str(e.get("url") or e.get("identifier") or "")])
    return rows


def preflight_text(cfg: RunConfig, creds: Credentials) -> str:
    check = validate_credentials(cfg, creds)
    if check.ok:
        return ""
    return ("Missing keys for this setup: **" + ", ".join(sorted(set(check.missing_required)))
            + "**. Add them under *API keys*, or pick models and a search engine you have keys for.")


# ── The run ───────────────────────────────────────────────────────────────

async def run_ui(question, sup, sub, draft, route_or, thinking, minutes, iters, searches,
                 concurrency, agents, engine, key1, key2, key3, key4, key5, key6, key7, extra,
                 request: gr.Request = None):
    # Explicit parameters, not *args: Gradio only injects the request into a plain
    # annotated parameter. One key parameter per entry in KEY_FIELDS.
    key_values = [key1, key2, key3, key4, key5, key6, key7]
    sid = request.session_hash if request else "local"
    empty = ("", "", [], None)  # log, report, sources, download

    def out(status, *tail):
        return (status, *(tail if tail else empty))

    question = (question or "").strip()
    if not question:
        yield out("Enter a research question first.")
        return
    if not (sup and sub and draft):
        yield out("Each model role needs at least one model.")
        return
    if not agents:
        yield out("Pick at least one agent.")
        return
    if sid in _RUNNING:
        yield out("A run is already in progress in this session. Stop it first.")
        return

    try:
        cfg = build_config(sup, sub, draft, route_or, thinking, minutes, iters, searches,
                           concurrency, agents, engine)
        creds = build_credentials(key_values, extra)
    except Exception as exc:  # config validation errors are user-facing
        yield out(f"Settings problem: {exc}")
        return

    problem = preflight_text(cfg, creds)
    if problem:
        yield out(problem)
        return

    token = CancellationToken()
    _TOKENS[sid] = token
    _RUNNING.add(sid)
    t0 = time.time()
    lines: list[str] = []

    def sink(ev):
        lines.append(format_event(ev, t0))

    task = asyncio.create_task(run_research(
        question, config=cfg, credentials=creds,
        runtime=RuntimeOptions(event_sink=sink, cancellation=token, console_enabled=False)))
    try:
        while not task.done():
            await asyncio.sleep(0.7)
            elapsed = int(time.time() - t0)
            yield out(f"Running… {elapsed // 60}m {elapsed % 60:02d}s",
                      "\n".join(lines[-300:]), "", [], None)
        try:
            result = task.result()
        except Exception as exc:
            yield out(f"The run failed: {type(exc).__name__}: {exc}", "\n".join(lines[-300:]),
                      "", [], None)
            return

        log = "\n".join(lines[-300:])
        if result.status != "completed" or not result.final_report:
            why = result.failure or "no report was produced"
            yield out(f"Run {result.status}: {why}", log, result.final_report or "", [], None)
            return

        path = Path(tempfile.mkdtemp(prefix="deepdog_")) / "report.md"
        path.write_text(result.final_report, encoding="utf-8")
        elapsed = int(time.time() - t0)
        yield out(f"Completed in {elapsed // 60}m {elapsed % 60:02d}s.",
                  log, result.final_report, source_rows(result), str(path))
    finally:
        if not task.done():
            task.cancel()
        _RUNNING.discard(sid)
        _TOKENS.pop(sid, None)


def stop_run(request: gr.Request):
    token = _TOKENS.get(request.session_hash if request else "local")
    if token:
        token.cancel("Stopped by user")
        return "Stopping after the current step…"
    return "Nothing is running."


def apply_preset(name):
    p = PRESETS[name]
    return (p["sup"], p["sub"], p["draft"], p["minutes"], p["iters"], p["searches"], p["engine"])


# ── Layout ────────────────────────────────────────────────────────────────

def build_demo() -> gr.Blocks:
    p = PRESETS[DEFAULT_PRESET]
    with gr.Blocks(title="Deep Dog 2") as demo:
        gr.Markdown(
            "# Deep Dog 2\nAsk a question. A supervisor plans the research, platform agents "
            "gather sources, and you get a cited report. Keys you type here are used for this "
            "run only and are not stored.")
        question = gr.Textbox(label="Research question", lines=3,
                              placeholder="What are the main benefits and limitations of "
                                          "sodium-ion batteries for home energy storage?")
        with gr.Row():
            run_btn = gr.Button("Run research", variant="primary")
            stop_btn = gr.Button("Stop", variant="stop")

        with gr.Accordion("Models", open=True):
            preset = gr.Dropdown(list(PRESETS), value=DEFAULT_PRESET, label="Preset")
            gr.Markdown("Each role takes an ordered list: the first is tried first, the rest are "
                        "fallbacks. Pick from the list or type any model name the engine supports.")
            with gr.Row():
                sup = gr.Dropdown(MODEL_CHOICES, value=p["sup"], multiselect=True,
                                  allow_custom_value=True, label="Supervisor")
                sub = gr.Dropdown(MODEL_CHOICES, value=p["sub"], multiselect=True,
                                  allow_custom_value=True, label="Sub-agents")
                draft = gr.Dropdown(MODEL_CHOICES, value=p["draft"], multiselect=True,
                                    allow_custom_value=True, label="Draft and brief")
            with gr.Row():
                route_or = gr.Checkbox(False, label="Route supported models through OpenRouter")
                thinking = gr.Radio(["auto", "on", "off"], value="auto", label="Thinking mode")

        with gr.Accordion("Agents, search and budget", open=False):
            agents = gr.CheckboxGroup(AGENT_CHOICES, value=["ResearchWeb"], label="Agents")
            engine = gr.Radio(["exa", "tavily", "both"], value=p["engine"],
                              label="Web search engine")
            with gr.Row():
                minutes = gr.Slider(2, MAX_MINUTES, value=p["minutes"], step=1,
                                    label="Research time limit (minutes)")
                iters = gr.Slider(3, 50, value=p["iters"], step=1, label="Supervisor iterations")
            with gr.Row():
                searches = gr.Slider(1, 10, value=p["searches"], step=1,
                                     label="Searches per sub-agent")
                concurrency = gr.Slider(1, 6, value=3, step=1, label="Parallel sub-agents")

        key_boxes = []
        with gr.Accordion("API keys (this run only)", open=False):
            with gr.Row():
                for name, label in KEY_FIELDS[:4]:
                    key_boxes.append(gr.Textbox(label=label, type="password", max_lines=1))
            with gr.Row():
                for name, label in KEY_FIELDS[4:]:
                    key_boxes.append(gr.Textbox(label=label, type="password", max_lines=1))
            extra = gr.Textbox(label="Other keys, NAME=value separated by semicolons",
                               type="password", max_lines=1,
                               placeholder="REDDIT_CLIENT_ID=...;PUBMED_EMAIL=you@example.com")

        status = gr.Markdown()
        with gr.Tabs():
            with gr.Tab("Report"):
                report = gr.Markdown(label="Report")
                download = gr.File(label="Download report (.md)")
            with gr.Tab("Sources"):
                sources = gr.Dataframe(headers=["Code", "Title", "URL"], interactive=False,
                                       wrap=True)
            with gr.Tab("Progress"):
                log = gr.Textbox(label="Events", lines=16, max_lines=16, autoscroll=True,
                                 interactive=False)

        preset.change(apply_preset, preset, [sup, sub, draft, minutes, iters, searches, engine])
        inputs = [question, sup, sub, draft, route_or, thinking, minutes, iters, searches,
                  concurrency, agents, engine, *key_boxes, extra]
        run_btn.click(run_ui, inputs, [status, log, report, sources, download],
                      show_progress="hidden", api_name=False)
        stop_btn.click(stop_run, None, status, api_name=False)
    demo.queue(default_concurrency_limit=MAX_CONCURRENT_RUNS)
    return demo


def _auth():
    """Optional shared-password gate, set with the ACCESS_PASSWORD Space secret."""
    password = os.environ.get("ACCESS_PASSWORD", "")
    if not password:
        return None
    return lambda user, pw: hmac.compare_digest(str(pw), password)


def main() -> None:
    ap = argparse.ArgumentParser()
    # Spaces (Gradio SDK) sets GRADIO_SERVER_NAME / GRADIO_SERVER_PORT; honour them.
    ap.add_argument("--host", default=os.environ.get("GRADIO_SERVER_NAME", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("GRADIO_SERVER_PORT", "7860")))
    args = ap.parse_args()
    # ssr_mode=False: server-side rendering needs Node, which the slim image lacks.
    build_demo().launch(server_name=args.host, server_port=args.port, auth=_auth(),
                        theme=gr.themes.Soft(), footer_links=["gradio"], ssr_mode=False)


if __name__ == "__main__":
    main()
