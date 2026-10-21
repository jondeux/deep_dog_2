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
import re
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

import gradio as gr

from deep_research import config as _cfg
from deep_research.cancellation import CancellationToken
from deep_research.integration import RuntimeOptions, run_research, validate_credentials
from deep_research.models import Credentials
from deep_research.run_config import RunConfig

# model_catalog.py sits next to this file: in webui/ when run from a checkout, and
# at the Space root once build_space.sh has staged it. Importing it by its bare name
# gives one module object however this file is started.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import model_catalog  # noqa: E402

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
# One password box per entry, in this order. run_ui takes one explicit parameter
# per entry (key1..keyN, same order) and the click wiring is built from this list.
KEY_FIELDS = [
    ("DEEPSEEK_API_KEY", "DeepSeek"),
    ("OPENROUTER_API_KEY", "OpenRouter"),
    ("OPENAI_API_KEY", "OpenAI"),
    ("GEMINI_API_KEY", "Google Gemini"),
    ("MIMO_API_KEY", "MiMo"),
    ("DEEPINFRA_API_KEY", "DeepInfra"),
    ("TOGETHER_API_KEY", "Together"),
    ("FIREWORKS_API_KEY", "Fireworks"),
    ("TAVILY_API_KEY", "Tavily search"),
    ("EXA_API_KEY", "Exa search"),
]
# Where each box is shown. Model-provider boxes sit under "API keys"; the two
# search-tool boxes sit inside the "Search tools" panel next to their checkboxes.
MODEL_KEY_NAMES = [n for n, _ in KEY_FIELDS if n not in ("TAVILY_API_KEY", "EXA_API_KEY")]
SEARCH_KEY_NAMES = {"tavily": "TAVILY_API_KEY", "exa": "EXA_API_KEY"}
# Hosted providers whose model lists can be fetched live (box name per provider).
CATALOG_KEY_NAMES = [model_catalog.KEY_NAMES[p] for p in model_catalog.PROVIDERS]

# LangSmith tracing (opt-in). The region is a fixed choice mapped to a fixed URL here:
# the visitor never types an endpoint, because a typed URL would make this server send
# requests to any host they name (SSRF). Keys of this dict are the dropdown values.
LANGSMITH_REGIONS = {
    "US": "https://api.smith.langchain.com",
    "EU": "https://eu.api.smith.langchain.com",
}
LANGSMITH_REGION_CHOICES = [(f"{name} ({url})", name) for name, url in LANGSMITH_REGIONS.items()]
LANGSMITH_DEFAULT_REGION = "US"
LANGSMITH_DEFAULT_PROJECT = "deep-dog-2"
LANGSMITH_PROJECT_MAX_CHARS = 80
# The "Other keys" box may not set LANGSMITH_* names (parse_extra_keys drops them), so the
# endpoint can only ever come from LANGSMITH_REGIONS.
# Shorter values are not treated as secrets when scrubbing text (same rule as the engine).
# The floor applies to the whole parsed value, never to a ";"-split piece of one.
MIN_SECRET_CHARS = 6
# The "Other keys" box: only the secret/contact names the engine reads from its credentials
# (URLs, hosts and proxies are deliberately absent), at most this much text and these entries.
# The caps bound the CPU spent scrubbing every poll and every trace record.
EXTRA_KEY_NAME_SHAPE = re.compile(r"[A-Z0-9_]{1,40}")
EXTRA_KEY_NAMES = (
    "REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "REDDIT_USERNAME", "REDDIT_PASSWORD",
    "PUBMED_EMAIL", "SEC_EDGAR_CONTACT_EMAIL", "PERPLEXITY_API_KEY",
    "DEEPSEEK_KEY", "GOOGLE_API_KEY", "ZHIPUAI_API_KEY", "META_API_KEY",
)
EXTRA_KEYS_MAX_CHARS = 4096
EXTRA_KEYS_MAX_ENTRIES = 20

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

def parse_extra_keys(text: str, *, strict: bool = True) -> dict[str, str]:
    """Parse ``NAME=value`` entries split on ``;`` or newlines (Reddit, PubMed email, ...).

    A value cannot itself contain ``;`` (it ends the entry). ``LANGSMITH_*`` names are always
    ignored. With ``strict`` (the default) text over EXTRA_KEYS_MAX_CHARS, more than
    EXTRA_KEYS_MAX_ENTRIES entries, or a name outside EXTRA_KEY_NAMES raises ``ValueError``
    whose message holds names and counts only, never a value. ``strict=False`` is for
    scrubbing: it truncates to the same caps and accepts any name, so every typed value is
    still hidden.
    """
    text = text or ""
    if len(text) > EXTRA_KEYS_MAX_CHARS:
        if strict:
            raise ValueError(f"Other keys is longer than {EXTRA_KEYS_MAX_CHARS} characters")
        text = text[:EXTRA_KEYS_MAX_CHARS]
    out: dict[str, str] = {}
    entries = 0
    for line in text.replace(";", "\n").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if not (name and value):
            continue
        entries += 1
        if entries > EXTRA_KEYS_MAX_ENTRIES:
            if strict:
                raise ValueError(f"Other keys has more than {EXTRA_KEYS_MAX_ENTRIES} entries")
            break
        if name.startswith("LANGSMITH_"):
            continue
        if strict and name not in EXTRA_KEY_NAMES:
            # Echo the name only when it looks like one: a pasted key containing "="
            # would otherwise come back as the text before its first "=".
            shown = f" {name}" if EXTRA_KEY_NAME_SHAPE.fullmatch(name) else ""
            raise ValueError(f"unsupported key name{shown}")
        out[name] = value
    return out


def clean_project(name) -> str:
    """LangSmith project name: control characters dropped, trimmed, at most 80 chars,
    empty -> the default."""
    text = "".join(ch if ch.isprintable() else " " for ch in str(name or ""))
    text = text.strip()[:LANGSMITH_PROJECT_MAX_CHARS].strip()
    return text or LANGSMITH_DEFAULT_PROJECT


def langsmith_endpoint(region) -> str:
    """Endpoint for a region choice. Raises ValueError for anything but a known region."""
    try:
        return LANGSMITH_REGIONS[region]
    except (KeyError, TypeError):
        raise ValueError("Pick a LangSmith region from the list (US or EU).") from None


def build_config(sup, sub, draft, route_or, thinking, minutes, iters, searches,
                 concurrency, agents, engine, ls_on=False, ls_project="") -> RunConfig:
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
        langsmith_tracing=bool(ls_on),
        langsmith_project=clean_project(ls_project),
    ).finalize()


def build_credentials(key_values: list[str], extra: str, ls_on=False, ls_key="",
                      ls_region=LANGSMITH_DEFAULT_REGION) -> Credentials:
    keys = {name: (value or "").strip()
            for (name, _), value in zip(KEY_FIELDS, key_values) if (value or "").strip()}
    # parse_extra_keys already drops LANGSMITH_* names: LangSmith credentials come only from
    # the Tracing controls (a typed LANGSMITH_ENDPOINT would be a free-text URL).
    keys.update(parse_extra_keys(extra))
    ls_key = (ls_key or "").strip()
    if ls_on and ls_key:
        keys["LANGSMITH_API_KEY"] = ls_key
        keys["LANGSMITH_ENDPOINT"] = langsmith_endpoint(ls_region)
    # The operator's own LANGSMITH_* environment must never trace a visitor's run.
    return Credentials(keys=keys, langsmith_from_env=False)


def typed_secrets(key_values, extra, ls_key="") -> list[str]:
    """Every secret value typed on the page (key boxes, Other keys, LangSmith key)."""
    values = [(v or "").strip() for v in key_values]
    values += list(parse_extra_keys(extra, strict=False).values())
    values.append((ls_key or "").strip())
    return [v for v in values if len(v) >= MIN_SECRET_CHARS]


def scrub(text, secrets) -> str:
    """Replace each secret in ``text`` with ``***``, longest first."""
    text = str(text)
    for secret in sorted(set(secrets), key=len, reverse=True):
        if len(secret) >= MIN_SECRET_CHARS:
            text = text.replace(secret, "***")
    return text


def langsmith_status_line(result) -> str:
    """One line about tracing after a run, from what the engine reported.

    It never says the trace arrived: a failed flush is only a warning in the engine.
    """
    info = (getattr(result, "run_metadata", None) or {}).get("langsmith") or {}
    if not info.get("requested"):
        return ""
    if info.get("active"):
        return f"LangSmith: tracing active, project {info.get('project') or LANGSMITH_DEFAULT_PROJECT}"
    return "LangSmith: tracing was requested but not active"


def langsmith_hint(ls_on, ls_key) -> str:
    """Short state line under the Tracing controls. Never prints the key."""
    if not ls_on:
        return "Tracing is off."
    if (ls_key or "").strip():
        return "Tracing is on for the next run; key entered."
    return "Tracing is on but no LangSmith API key is entered yet."


def engine_from_selection(web_on, use_tavily, use_exa) -> str:
    """Search-tool checkboxes -> the engine's ``web_search_engine`` value.

    Both ticked is "both", one is that engine, none (or the master switch off) is
    "none": the engine accepts any string, exposes no search tool for it and asks for
    no search key, so "none" turns web search off.
    """
    if not web_on:
        return "none"
    if use_tavily and use_exa:
        return "both"
    if use_tavily:
        return "tavily"
    if use_exa:
        return "exa"
    return "none"


def engine_to_selection(engine: str) -> tuple[bool, bool, bool]:
    """Inverse of engine_from_selection: (web_on, use_tavily, use_exa)."""
    engine = (engine or "").lower()
    return (engine in ("tavily", "exa", "both"),
            engine in ("tavily", "both"), engine in ("exa", "both"))


def search_key_status(name: str, typed: str) -> str:
    """Key state for one search tool: typed on the page, set on the server, or absent."""
    if (typed or "").strip():
        return "key entered"
    if os.environ.get(name):
        return "server key available"
    return "no key"


def format_event(ev, t0: float, secrets=()) -> str:
    """One readable progress line. Events carry metadata only, never reasoning.

    Typed secrets are scrubbed from each full value BEFORE it is cut to 80 chars, so a
    key straddling the cut cannot leave a readable prefix.
    """
    secs = max(0, int(time.time() - t0))
    who = ev.agent or ev.platform or ""
    bits = []
    for k, v in list((ev.payload or {}).items())[:3]:
        text = scrub(str(v).replace("\n", " "), secrets)
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


def missing_keys(cfg: RunConfig, creds: Credentials) -> list[str]:
    """Names (never values) of the keys this setup still needs."""
    check = validate_credentials(cfg, creds)
    return [] if check.ok else sorted(set(check.missing_required))


def preflight_text(cfg: RunConfig, creds: Credentials) -> str:
    missing = missing_keys(cfg, creds)
    if not missing:
        return ""
    return ("Missing keys for this setup: **" + ", ".join(missing)
            + "**. Add them under *API keys* or *Search tools*, or pick models and search "
            "tools you have keys for.")


def live_status(sup, sub, draft, route_or, agents, web_on, use_tavily, use_exa, *key_and_extra):
    """Search-tool key states plus a Ready / Missing keys line for the current page state.

    Takes the same key boxes as run_ui (KEY_FIELDS order, then the extra-keys box, then
    optionally ls_on, ls_key, ls_project, ls_region). Returns (tavily state, exa state,
    summary). Only key NAMES are ever printed.
    """
    n = len(KEY_FIELDS)
    key_values = list(key_and_extra[:n])
    extra = key_and_extra[n] if len(key_and_extra) > n else ""
    ls = list(key_and_extra[n + 1:n + 5])
    ls_on, ls_key, ls_project, ls_region = ls + [False, "", "", LANGSMITH_DEFAULT_REGION][len(ls):]
    by_name = {name: value for (name, _), value in zip(KEY_FIELDS, key_values)}
    tavily = search_key_status("TAVILY_API_KEY", by_name.get("TAVILY_API_KEY", ""))
    exa = search_key_status("EXA_API_KEY", by_name.get("EXA_API_KEY", ""))
    engine = engine_from_selection(web_on, use_tavily, use_exa)
    try:
        if not (sup and sub and draft):
            return tavily, exa, "Each model role needs at least one model."
        cfg = build_config(sup, sub, draft, route_or, "auto", 5, 5, 3, 1, agents or [], engine,
                           ls_on, ls_project)
        missing = missing_keys(cfg, build_credentials(key_values, extra, ls_on, ls_key, ls_region))
    except Exception as exc:  # config validation errors are user-facing
        return tavily, exa, "Settings problem: " + scrub(
            exc, typed_secrets(key_values, extra, ls_key))
    note = "" if engine != "none" else " (web search is off)"
    if missing:
        return tavily, exa, "Missing keys: " + ", ".join(missing) + note
    return tavily, exa, "Ready" + note


# ── The run ───────────────────────────────────────────────────────────────

async def run_ui(question, sup, sub, draft, route_or, thinking, minutes, iters, searches,
                 concurrency, agents, web_on, use_tavily, use_exa,
                 key1, key2, key3, key4, key5, key6, key7, key8, key9, key10, extra,
                 ls_on=False, ls_key="", ls_project="", ls_region=LANGSMITH_DEFAULT_REGION,
                 request: gr.Request = None):
    # Explicit parameters, not *args: Gradio only injects the request into a plain
    # annotated parameter. One key parameter per entry in KEY_FIELDS, same order.
    key_values = [key1, key2, key3, key4, key5, key6, key7, key8, key9, key10]
    engine = engine_from_selection(web_on, use_tavily, use_exa)
    sid = request.session_hash if request else "local"
    empty = ("", "", [], None)  # log, report, sources, download
    secrets = typed_secrets(key_values, extra, ls_key)

    def safe(text) -> str:
        """Text for the page: any typed key value in it becomes ***."""
        return scrub(text, secrets)

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
                           concurrency, agents, engine, ls_on, ls_project)
        creds = build_credentials(key_values, extra, ls_on, ls_key, ls_region)
    except Exception as exc:  # config validation errors are user-facing
        yield out(safe(f"Settings problem: {exc}"))
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
        lines.append(format_event(ev, t0, secrets))

    task = asyncio.create_task(run_research(
        question, config=cfg, credentials=creds,
        runtime=RuntimeOptions(event_sink=sink, cancellation=token, console_enabled=False)))
    try:
        while not task.done():
            await asyncio.sleep(0.7)
            elapsed = int(time.time() - t0)
            yield out(f"Running… {elapsed // 60}m {elapsed % 60:02d}s",
                      safe("\n".join(lines[-300:])), "", [], None)
        try:
            result = task.result()
        except Exception as exc:
            yield out(safe(f"The run failed: {type(exc).__name__}: {exc}"),
                      safe("\n".join(lines[-300:])), "", [], None)
            return

        log = safe("\n".join(lines[-300:]))
        ls_line = langsmith_status_line(result)
        with_ls = (lambda msg: f"{msg}\n\n{ls_line}") if ls_line else (lambda msg: msg)
        if result.status != "completed" or not result.final_report:
            why = result.failure or "no report was produced"
            yield out(with_ls(safe(f"Run {result.status}: {why}")), log,
                      result.final_report or "", [], None)
            return

        path = Path(tempfile.mkdtemp(prefix="deepdog_")) / "report.md"
        path.write_text(result.final_report, encoding="utf-8")
        elapsed = int(time.time() - t0)
        yield out(with_ls(f"Completed in {elapsed // 60}m {elapsed % 60:02d}s."),
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
    return (p["sup"], p["sub"], p["draft"], p["minutes"], p["iters"], p["searches"],
            *engine_to_selection(p["engine"]))


# Per-session refresh throttle: session_hash -> (time of last refresh, choices, ...).
REFRESH_MIN_INTERVAL = 3.0
REFRESH_THROTTLED_TEXT = "Please wait a few seconds before refreshing again"
_REFRESH_SESSIONS_MAX = 1024
_REFRESH_LAST: dict[str, tuple[float, Optional[list[str]]]] = {}
_refresh_lock = threading.Lock()


def _refresh_gate(sid):
    """(allowed, previous choices). Records the attempt time when it is allowed."""
    if not sid:
        return True, None
    now = model_catalog._now()
    with _refresh_lock:
        last = _REFRESH_LAST.get(sid)
        if last is not None and now - last[0] < REFRESH_MIN_INTERVAL:
            return False, last[1]
        _REFRESH_LAST[sid] = (now, last[1] if last else None)
        while len(_REFRESH_LAST) > _REFRESH_SESSIONS_MAX:
            del _REFRESH_LAST[next(iter(_REFRESH_LAST))]
    return True, None


def refresh_models(key_deepinfra, key_together, key_fireworks, request: gr.Request = None):
    """Re-fetch the hosted providers' model lists with the keys typed on the page.

    Returns new choices for the three role dropdowns (their selected values are left
    alone, so no value is returned) and a status line. No key is ever returned. One
    session can refresh at most once per REFRESH_MIN_INTERVAL seconds; a refresh inside
    that window makes no outbound request and returns the previous choices.
    """
    sid = getattr(request, "session_hash", None) if request is not None else None
    allowed, previous = _refresh_gate(sid)
    if not allowed:
        # No previous list yet (first fetch still running): leave the dropdowns alone.
        choices = gr.update(choices=previous) if previous else gr.update()
        return choices, choices, choices, REFRESH_THROTTLED_TEXT
    typed = dict(zip(model_catalog.PROVIDERS, (key_deepinfra, key_together, key_fireworks)))
    results = model_catalog.fetch_all(typed)
    merged = model_catalog.merged_choices(MODEL_CHOICES, results)
    if sid:
        with _refresh_lock:
            _REFRESH_LAST[sid] = (_REFRESH_LAST.get(sid, (model_catalog._now(),))[0], merged)
    choices = gr.update(choices=merged)
    return choices, choices, choices, "Model lists: " + model_catalog.status_line(results)


# ── Layout ────────────────────────────────────────────────────────────────

def build_demo() -> gr.Blocks:
    p = PRESETS[DEFAULT_PRESET]
    web_on0, tavily_on0, exa_on0 = engine_to_selection(p["engine"])
    initial_choices = model_catalog.merged_choices(MODEL_CHOICES, model_catalog.fallback_results())
    key_labels = dict(KEY_FIELDS)
    boxes: dict[str, gr.Textbox] = {}

    def key_box(name: str) -> gr.Textbox:
        boxes[name] = gr.Textbox(label=key_labels[name], type="password", max_lines=1)
        return boxes[name]

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
                        "fallbacks. Pick from the list or type any model name the engine supports. "
                        "DeepInfra, Together and Fireworks models are listed as "
                        "`deepinfra:<id>`, `together:<id>` and `fireworks:<id>`; enter their key "
                        "below to load the live list.")
            with gr.Row():
                sup = gr.Dropdown(initial_choices, value=p["sup"], multiselect=True,
                                  allow_custom_value=True, label="Supervisor")
                sub = gr.Dropdown(initial_choices, value=p["sub"], multiselect=True,
                                  allow_custom_value=True, label="Sub-agents")
                draft = gr.Dropdown(initial_choices, value=p["draft"], multiselect=True,
                                    allow_custom_value=True, label="Draft and brief")
            with gr.Row():
                refresh_btn = gr.Button("Refresh model lists", size="sm", scale=0)
                models_status = gr.Markdown(
                    "Model lists: " + model_catalog.status_line(model_catalog.fallback_results()))
            with gr.Row():
                route_or = gr.Checkbox(False, label="Route supported models through OpenRouter")
                thinking = gr.Radio(["auto", "on", "off"], value="auto", label="Thinking mode")

        with gr.Accordion("Search tools", open=True):
            web_on = gr.Checkbox(web_on0, label="Web search on (master switch)")
            with gr.Row():
                use_tavily = gr.Checkbox(tavily_on0, label="Tavily", scale=1)
                key_box("TAVILY_API_KEY")
                tavily_state = gr.Markdown(search_key_status("TAVILY_API_KEY", ""))
            with gr.Row():
                use_exa = gr.Checkbox(exa_on0, label="Exa", scale=1)
                key_box("EXA_API_KEY")
                exa_state = gr.Markdown(search_key_status("EXA_API_KEY", ""))
            ready = gr.Markdown()

        with gr.Accordion("Agents and budget", open=False):
            agents = gr.CheckboxGroup(AGENT_CHOICES, value=["ResearchWeb"], label="Agents")
            with gr.Row():
                minutes = gr.Slider(2, MAX_MINUTES, value=p["minutes"], step=1,
                                    label="Research time limit (minutes)")
                iters = gr.Slider(3, 50, value=p["iters"], step=1, label="Supervisor iterations")
            with gr.Row():
                searches = gr.Slider(1, 10, value=p["searches"], step=1,
                                     label="Searches per sub-agent")
                concurrency = gr.Slider(1, 6, value=3, step=1, label="Parallel sub-agents")

        with gr.Accordion("API keys (this run only)", open=False):
            with gr.Row():
                for name in MODEL_KEY_NAMES[:4]:
                    key_box(name)
            with gr.Row():
                for name in MODEL_KEY_NAMES[4:]:
                    key_box(name)
            extra = gr.Textbox(label="Other keys, NAME=value separated by semicolons",
                               type="password", max_lines=1,
                               info=("Allowed names: " + ", ".join(EXTRA_KEY_NAMES)
                                     + f". At most {EXTRA_KEYS_MAX_ENTRIES} entries and "
                                     f"{EXTRA_KEYS_MAX_CHARS} characters. A value cannot "
                                     "contain a semicolon (it ends the entry)."),
                               placeholder="REDDIT_CLIENT_ID=...;PUBMED_EMAIL=you@example.com")
        key_boxes = [boxes[name] for name, _ in KEY_FIELDS]  # run_ui's key1..keyN order

        with gr.Accordion("Tracing (LangSmith)", open=False):
            ls_on = gr.Checkbox(False, label="Trace this run in LangSmith")
            gr.Markdown(
                "**Privacy:** tracing sends the prompts, search results and the final report of "
                "this run to your LangSmith account, into the project named below. It is off "
                "unless you tick the box. Your LangSmith key is used for this run only and is "
                "not stored.")
            with gr.Row():
                ls_key = gr.Textbox(label="LangSmith API key", type="password", max_lines=1)
                ls_region = gr.Dropdown(LANGSMITH_REGION_CHOICES, value=LANGSMITH_DEFAULT_REGION,
                                        allow_custom_value=False, filterable=False,
                                        label="LangSmith region")
            ls_project = gr.Textbox(label="LangSmith project", value=LANGSMITH_DEFAULT_PROJECT,
                                    max_length=LANGSMITH_PROJECT_MAX_CHARS, max_lines=1)
            ls_state = gr.Markdown(langsmith_hint(False, ""))
        ls_boxes = [ls_on, ls_key, ls_project, ls_region]  # run_ui's ls_* order

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

        preset.change(apply_preset, preset,
                      [sup, sub, draft, minutes, iters, searches, web_on, use_tavily, use_exa],
                      api_name=False)

        # Live model lists: the button, or leaving a hosted-provider key box.
        catalog_keys = [boxes[name] for name in CATALOG_KEY_NAMES]
        refresh_outputs = [sup, sub, draft, models_status]
        refresh_btn.click(refresh_models, catalog_keys, refresh_outputs,
                          show_progress="hidden", api_name="refresh_models",
                          api_visibility="undocumented")
        for box in catalog_keys:
            box.blur(refresh_models, catalog_keys, refresh_outputs, show_progress="hidden",
                     trigger_mode="always_last", api_name=False)

        # Live search-tool key states and the Ready / Missing keys line.
        status_inputs = [sup, sub, draft, route_or, agents, web_on, use_tavily, use_exa,
                         *key_boxes, extra, *ls_boxes]
        status_outputs = [tavily_state, exa_state, ready]
        status_kwargs = dict(show_progress="hidden", trigger_mode="always_last")
        for box in (*key_boxes, extra, ls_key, ls_project):
            box.blur(live_status, status_inputs, status_outputs, api_name=False, **status_kwargs)
        for control in (sup, sub, draft, route_or, agents, web_on, use_tavily, use_exa,
                        ls_on, ls_region):
            control.change(live_status, status_inputs, status_outputs, api_name=False,
                           **status_kwargs)
        # The Tracing panel's own one-line state (never shows the key).
        for control in (ls_on, ls_key):
            event = control.change if control is ls_on else control.blur
            event(langsmith_hint, [ls_on, ls_key], ls_state, api_name=False,
                  show_progress="hidden")
        demo.load(live_status, status_inputs, status_outputs, api_name="search_status",
                  api_visibility="undocumented", show_progress="hidden")

        inputs = [question, sup, sub, draft, route_or, thinking, minutes, iters, searches,
                  concurrency, agents, web_on, use_tavily, use_exa, *key_boxes, extra, *ls_boxes]
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
    # Bytes, not str: compare_digest raises TypeError on non-ASCII str (a 500 on /login).
    expected = password.encode("utf-8")
    return lambda user, pw: hmac.compare_digest(str(pw).encode("utf-8"), expected)


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
