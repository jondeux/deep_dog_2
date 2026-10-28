"""Deep Dog 2 web UI.

A Gradio front end for ``deep_research.integration.run_research``. It lets a user
pick the models for each role (supervisor, sub-agents, draft writer), the agents
and search engine, and the research budget, then watch progress and read the
cited report.

Credentials: provider keys are typed into the page and passed to the engine as
a per-run ``Credentials`` object. They are never written to disk, never put in
``os.environ`` and never echoed back. A web run NEVER uses the server's own
environment keys (``Credentials(env_fallback=False)``): a provider or search key
that is not typed on the page counts as missing, even if the operator set it as
a Space secret, so visitors can never spend the operator's keys.

The page remembers non-secret settings (and the last finished report) in the
visitor's browser with ``gr.BrowserState``; key boxes, the "Other keys" box and
the LangSmith key are never stored.

Run locally:   python webui/app.py
On a Space:    python app.py --host 0.0.0.0 --port 7860
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import hmac
import json
import os
import re
import shutil
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

# Seconds between progress refreshes of a running research job. Tests lower this in-process
# (tests/conftest.py); nothing in the app reads it from the environment.
_POLL_SECONDS = 0.7

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
    # Likewise no provider/search key may come from the server's environment.
    return Credentials(keys=keys, langsmith_from_env=False, env_fallback=False)


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
    """Key state for one search tool: typed on the page or absent.

    A key set in the server's environment is deliberately not reported (and not used):
    web runs only ever use keys typed on the page.
    """
    if (typed or "").strip():
        return "key entered"
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


# ── Report files ──────────────────────────────────────────────────────────

# Each finished run writes report.md into its own directory under this per-process root,
# yields it to the File component (Gradio copies it into its own cache while serving the
# yielded value), then deletes the directory. Gradio's cache copies are aged out by
# delete_cache. The root is passed to launch(allowed_paths=...) so the file is allowed
# wherever the system temp dir is.
_REPORT_ROOT: Optional[Path] = None
_report_lock = threading.Lock()
CACHE_SWEEP_SECONDS = 3600
CACHE_MAX_AGE_SECONDS = 6 * 3600


def report_root() -> Path:
    global _REPORT_ROOT
    with _report_lock:
        if _REPORT_ROOT is None:
            _REPORT_ROOT = Path(tempfile.mkdtemp(prefix="deepdog_reports_"))
            atexit.register(shutil.rmtree, str(_REPORT_ROOT), True)
        # launch(allowed_paths=...) captured this exact path, so a root removed by a /tmp
        # cleaner is recreated in place rather than replaced by a new one.
        _REPORT_ROOT.mkdir(parents=True, exist_ok=True)
        return _REPORT_ROOT


def _sweep_stale_run_dirs(root: Path, max_age: float = CACHE_SWEEP_SECONDS) -> None:
    """Remove run dirs a disconnected client left behind (the generator never resumed)."""
    cutoff = time.time() - max_age
    for d in root.glob("deepdog_*"):
        try:
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


# ── Settings remembered in the browser ────────────────────────────────────

# gr.BrowserState keeps a small JSON payload in the visitor's localStorage:
#   {"v": 1, "settings": {name: value, ...}, "report": {"text": str, "sources": [[c, t, u]]}}
# NON-SECRET settings only. No key box, no "Other keys" box and no LangSmith key ever
# reaches these functions: they are not among their inputs. Whatever comes back from the
# browser is untrusted and is validated field by field; a bad field is ignored.
SETTINGS_STORAGE_KEY = "deepdog2_settings"
# Not a secret: the payload holds nothing sensitive. A fixed value keeps a visitor's saved
# page readable across server restarts (Gradio otherwise makes a new random one per start).
SETTINGS_STORAGE_SECRET = "deepdog2-non-secret-settings-v1"
SETTINGS_VERSION = 1
STORE_MAX_BYTES = 200_000
STORE_QUESTION_MAX_CHARS = 4000
STORE_MAX_SOURCES = 2000
# run_ui's non-secret controls, in the order save_settings/restore_settings use.
SETTING_NAMES = ("question", "preset", "sup", "sub", "draft", "route_or", "thinking", "minutes",
                 "iters", "searches", "concurrency", "agents", "web_on", "use_tavily", "use_exa",
                 "ls_on", "ls_project", "ls_region")
_MODEL_NAME = re.compile(r"[\w.:/@+\-]{1,120}")
_SLIDER_RANGES = {"minutes": (2, None), "iters": (3, 50), "searches": (1, 10),
                  "concurrency": (1, 6)}


def _whole_number(value, lo, hi):
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, int) and lo <= value <= hi:
        return value
    return None


def _model_list(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 8:
        return None
    if not all(isinstance(v, str) and _MODEL_NAME.fullmatch(v) for v in value):
        return None
    return list(value)


def sanitize_settings(raw) -> dict:
    """The valid fields of a stored settings dict; anything else is left out."""
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    for name in SETTING_NAMES:
        if name not in raw:
            continue
        value = raw[name]
        if name == "question":
            ok = isinstance(value, str)
            value = value[:STORE_QUESTION_MAX_CHARS] if ok else None
        elif name == "preset":
            value = value if isinstance(value, str) and value in PRESETS else None
        elif name in ("sup", "sub", "draft"):
            value = _model_list(value)
        elif name in ("route_or", "web_on", "use_tavily", "use_exa", "ls_on"):
            value = value if isinstance(value, bool) else None
        elif name == "thinking":
            value = value if isinstance(value, str) and value in ("auto", "on", "off") else None
        elif name in _SLIDER_RANGES:
            lo, hi = _SLIDER_RANGES[name]
            value = _whole_number(value, lo, MAX_MINUTES if hi is None else hi)
        elif name == "agents":
            ok = (isinstance(value, list) and value
                  and all(isinstance(v, str) and v in AGENT_CHOICES for v in value))
            value = [a for a in AGENT_CHOICES if a in value] if ok else None
        elif name == "ls_project":
            value = clean_project(value) if isinstance(value, str) else None
        elif name == "ls_region":
            value = value if isinstance(value, str) and value in LANGSMITH_REGIONS else None
        if value is not None:
            out[name] = value
    return out


def _clean_report(raw):
    """A stored {"text", "sources"} dict if well formed and small enough, else None."""
    if not isinstance(raw, dict):
        return None
    text, rows = raw.get("text"), raw.get("sources")
    if not isinstance(text, str) or not text or not isinstance(rows, list):
        return None
    if len(rows) > STORE_MAX_SOURCES:
        return None
    clean = []
    for row in rows:
        if not (isinstance(row, list) and len(row) == 3 and all(isinstance(c, str) for c in row)):
            return None
        clean.append(list(row))
    return {"text": text, "sources": clean}


def build_payload(settings: dict, report) -> dict:
    """Payload for the browser; the report is dropped when the whole thing is too large."""
    payload = {"v": SETTINGS_VERSION, "settings": settings}
    report = _clean_report(report)
    if report is not None:
        with_report = dict(payload, report=report)
        if len(json.dumps(with_report, ensure_ascii=False).encode("utf-8")) <= STORE_MAX_BYTES:
            return with_report
    return payload


def _stored(state):
    """(settings, report) from a stored payload; ({}, None) when it is not one of ours."""
    if not isinstance(state, dict) or state.get("v") != SETTINGS_VERSION:
        return {}, None
    return sanitize_settings(state.get("settings")), _clean_report(state.get("report"))


def save_settings(state, *values):
    """Stored payload after the page's non-secret controls changed (keeps the stored report)."""
    _, report = _stored(state)
    return build_payload(sanitize_settings(dict(zip(SETTING_NAMES, values))), report)


def store_report(report_text, rows, download, state):
    """Stored payload after a run: remembers the report only when the run completed."""
    if not download or not (report_text or "").strip():
        return gr.skip()
    settings, _ = _stored(state)
    clean = [[str(c) for c in row[:3]] for row in (rows or []) if len(row) >= 3]
    return build_payload(settings, {"text": report_text, "sources": clean})


def restore_settings(state):
    """Control values from the stored payload: SETTING_NAMES order, then report, sources.

    Anything missing or invalid is left as it is (gr.skip()).
    """
    settings, report = _stored(state)
    values = [settings[n] if n in settings else gr.skip() for n in SETTING_NAMES]
    values += [report["text"], report["sources"]] if report else [gr.skip(), gr.skip()]
    return tuple(values)


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
            await asyncio.sleep(_POLL_SECONDS)
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

        root = report_root()
        _sweep_stale_run_dirs(root)
        run_dir = Path(tempfile.mkdtemp(prefix="deepdog_", dir=root))
        try:
            path = run_dir / "report.md"
            path.write_text(result.final_report, encoding="utf-8")
            elapsed = int(time.time() - t0)
            yield out(with_ls(f"Completed in {elapsed // 60}m {elapsed % 60:02d}s."),
                      log, result.final_report, source_rows(result), str(path))
        finally:
            # Gradio has copied the yielded file into its cache by the time this resumes.
            shutil.rmtree(run_dir, ignore_errors=True)
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


# ── Look ──────────────────────────────────────────────────────────────────
# A dark-only page: near-black canvas, a four-step surface ladder, hairline borders and
# one lavender accent (primary button, focus ring, links). No gradients, no shadows,
# no web fonts: the page loads nothing from third parties.

_SANS = ('ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", '
         'Arial, sans-serif')
_MONO = 'ui-monospace, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace'

_THEME_TOKENS = {
    "body_background_fill": "#010102", "body_text_color": "#f7f8f8",
    "body_text_color_subdued": "#8a8f98",
    "background_fill_primary": "#0f1011", "background_fill_secondary": "#141516",
    "border_color_primary": "#23252a", "border_color_accent": "#5e6ad2",
    "color_accent_soft": "#191a1b", "link_text_color": "#828fff",
    "link_text_color_hover": "#a3acff", "link_text_color_active": "#828fff",
    "link_text_color_visited": "#828fff",
    "block_background_fill": "#0f1011", "block_border_color": "#23252a",
    "block_border_width": "1px", "block_shadow": "none", "block_label_background_fill": "#0f1011",
    "block_label_border_color": "#23252a", "block_label_text_color": "#d0d6e0",
    "block_title_text_color": "#d0d6e0", "block_info_text_color": "#8a8f98",
    "panel_background_fill": "#0f1011", "panel_border_color": "#23252a",
    "input_background_fill": "#141516", "input_background_fill_focus": "#141516",
    "input_background_fill_hover": "#141516", "input_border_color": "#34343a",
    "input_border_color_focus": "#5e6ad2", "input_border_color_hover": "#3e3e44",
    "input_shadow": "none", "input_shadow_focus": "0 0 0 2px rgba(94,105,209,0.5)",
    "input_placeholder_color": "#62666d",
    "button_primary_background_fill": "#5e6ad2", "button_primary_background_fill_hover": "#828fff",
    "button_primary_border_color": "#5e6ad2", "button_primary_border_color_hover": "#828fff",
    "button_primary_text_color": "#ffffff", "button_primary_text_color_hover": "#ffffff",
    "button_primary_shadow": "none", "button_primary_shadow_hover": "none",
    "button_secondary_background_fill": "#0f1011",
    "button_secondary_background_fill_hover": "#18191a",
    "button_secondary_border_color": "#34343a", "button_secondary_border_color_hover": "#3e3e44",
    "button_secondary_text_color": "#f7f8f8", "button_secondary_text_color_hover": "#f7f8f8",
    "button_secondary_shadow": "none", "button_secondary_shadow_hover": "none",
    "button_cancel_background_fill": "#0f1011", "button_cancel_background_fill_hover": "#18191a",
    "button_cancel_border_color": "#34343a", "button_cancel_border_color_hover": "#3e3e44",
    "button_cancel_text_color": "#f7f8f8", "button_cancel_text_color_hover": "#f7f8f8",
    "button_cancel_shadow": "none", "button_cancel_shadow_hover": "none",
    "checkbox_background_color": "#141516", "checkbox_background_color_hover": "#18191a",
    "checkbox_background_color_focus": "#141516", "checkbox_background_color_selected": "#5e6ad2",
    "checkbox_border_color": "#3e3e44", "checkbox_border_color_hover": "#62666d",
    "checkbox_border_color_focus": "#5e6ad2", "checkbox_border_color_selected": "#5e6ad2",
    "checkbox_label_background_fill": "#141516", "checkbox_label_background_fill_hover": "#18191a",
    "checkbox_label_background_fill_selected": "#191a1b",
    "checkbox_label_border_color": "#23252a", "checkbox_label_border_color_hover": "#34343a",
    "checkbox_label_border_color_selected": "#5e6ad2",
    "checkbox_label_text_color": "#d0d6e0", "checkbox_label_text_color_selected": "#f7f8f8",
    "checkbox_label_shadow": "none", "slider_color": "#5e6ad2",
    "table_border_color": "#23252a", "table_even_background_fill": "#0f1011",
    "table_odd_background_fill": "#141516", "table_row_focus": "#18191a",
    "table_text_color": "#f7f8f8", "code_background_fill": "#141516",
    "accordion_text_color": "#f7f8f8", "loader_color": "#5e6ad2",
    "shadow_drop": "none", "shadow_drop_lg": "none", "shadow_inset": "none",
    "shadow_spread": "0px", "error_background_fill": "#141516", "error_border_color": "#3e3e44",
    "error_text_color": "#f7f8f8",
}


def build_theme() -> gr.themes.Base:
    """The dark theme. Light and dark values are identical, so the visitor's OS setting
    cannot lighten any part of the page."""
    import inspect
    valid = set(inspect.signature(gr.themes.Base.set).parameters)
    values = {}
    for name, value in _THEME_TOKENS.items():
        for key in (name, name + "_dark"):
            if key in valid:
                values[key] = value
    return gr.themes.Base(
        primary_hue=gr.themes.Color(
            c50="#f0f1fc", c100="#e1e3f8", c200="#c3c7f1", c300="#a3acff", c400="#828fff",
            c500="#5e6ad2", c600="#5e69d1", c700="#4c56b0", c800="#3b4389", c900="#2a3063",
            c950="#1b1f40", name="lavender"),
        neutral_hue=gr.themes.Color(
            c50="#f7f8f8", c100="#d0d6e0", c200="#8a8f98", c300="#62666d", c400="#3e3e44",
            c500="#34343a", c600="#23252a", c700="#191a1b", c800="#141516", c900="#0f1011",
            c950="#010102", name="ink"),
        radius_size=gr.themes.Size(xxs="4px", xs="4px", sm="6px", md="8px", lg="12px",
                                   xl="12px", xxl="16px"),
        font=[f.strip().strip('"') for f in _SANS.split(",")],
        font_mono=[f.strip().strip('"') for f in _MONO.split(",")],
    ).set(**values)


CSS = """
:root, .dark, body, .gradio-container {
  color-scheme: dark;
  --dd-canvas: #010102; --dd-s1: #0f1011; --dd-s2: #141516; --dd-s3: #18191a; --dd-s4: #191a1b;
  --dd-line: #23252a; --dd-line-2: #34343a; --dd-line-3: #3e3e44;
  --dd-ink: #f7f8f8; --dd-ink-2: #d0d6e0; --dd-ink-3: #8a8f98; --dd-ink-4: #62666d;
  --dd-accent: #5e6ad2; --dd-accent-hover: #828fff;
  --dd-ring: 0 0 0 2px rgba(94, 105, 209, 0.5);
  --font: __SANS__; --font-mono: __MONO__;
  --body-background-fill: var(--dd-canvas); --body-text-color: var(--dd-ink);
  --background-fill-primary: var(--dd-s1); --background-fill-secondary: var(--dd-s2);
  --block-background-fill: var(--dd-s1); --block-border-color: var(--dd-line);
  --border-color-primary: var(--dd-line); --input-background-fill: var(--dd-s2);
  --input-border-color: var(--dd-line-2); --body-text-color-subdued: var(--dd-ink-3);
  --block-label-text-color: var(--dd-ink-2); --block-title-text-color: var(--dd-ink-2);
  --neutral-50: #f7f8f8; --neutral-100: #d0d6e0; --neutral-200: #8a8f98;
  --shadow-drop: none; --shadow-drop-lg: none; --shadow-spread: 0px;
}
html, body { background: var(--dd-canvas) !important; color: var(--dd-ink); }
.gradio-container {
  background: var(--dd-canvas) !important; font-family: var(--font) !important;
  max-width: 1120px !important; margin: 0 auto !important;
  padding: 32px clamp(12px, 4vw, 24px) 64px !important;
  font-size: 16px; line-height: 1.5; letter-spacing: -0.05px;
}
.gradio-container *, .gradio-container *::before, .gradio-container *::after {
  box-shadow: none; text-shadow: none;
}
.gradio-container, .gradio-container .gap, .gradio-container .form { overflow-x: clip; }
.gradio-container :focus-visible { outline: 2px solid rgba(94, 105, 209, 0.7); outline-offset: 1px; }

/* Headings and prose */
.gradio-container h1 {
  font-size: 40px; line-height: 1.15; font-weight: 600; letter-spacing: -1px;
  color: var(--dd-ink); margin: 0 0 8px; border: 0;
}
.gradio-container h2 {
  font-size: 22px; line-height: 1.25; font-weight: 500; letter-spacing: -0.4px;
  color: var(--dd-ink); margin: 0;
}
.gradio-container .prose, .gradio-container .prose p, .gradio-container .md {
  color: var(--dd-ink-2); font-size: 16px; line-height: 1.5;
}
.gradio-container .prose h1, .gradio-container .prose h2, .gradio-container .prose h3,
.gradio-container .prose h4, .gradio-container .prose strong { color: var(--dd-ink); }
.dd-section .prose h1 { font-size: 28px; letter-spacing: -0.6px; }
.gradio-container .prose h3 { font-size: 20px; font-weight: 500; letter-spacing: -0.2px; }
.gradio-container a, .gradio-container .prose a { color: var(--dd-accent-hover); text-decoration: none; }
.gradio-container a:hover { text-decoration: underline; }
.gradio-container .prose code, .gradio-container .prose pre {
  font-family: var(--font-mono); font-size: 13px; background: var(--dd-s2);
  color: var(--dd-ink-2); border: 1px solid var(--dd-line); border-radius: 6px;
}
.gradio-container .prose pre { padding: 12px; overflow-x: auto; }
.gradio-container .prose pre code { border: 0; background: transparent; }
.gradio-container .prose table { display: block; overflow-x: auto; border-collapse: collapse;
  border: 1px solid var(--dd-line) !important; max-width: 100%; width: max-content; }
.gradio-container .prose th, .gradio-container .prose td {
  word-break: normal; overflow-wrap: normal; min-width: 6ch;
  border: 1px solid var(--dd-line); padding: 8px 12px; color: var(--dd-ink-2);
}
.gradio-container .prose th { background: var(--dd-s2); color: var(--dd-ink); font-weight: 500; }
.gradio-container .prose blockquote {
  border-left: 2px solid var(--dd-line-3); color: var(--dd-ink-3); padding-left: 12px;
}
.gradio-container .prose hr { border-color: var(--dd-line); }
.dd-eyebrow p, .dd-eyebrow { font-size: 13px !important; font-weight: 500;
  letter-spacing: 0.4px; text-transform: uppercase; color: var(--dd-ink-3) !important; }
.dd-lead, .dd-lead p { color: var(--dd-ink-3) !important; font-size: 16px; max-width: 70ch; }

/* Sections: one panel per step */
.dd-section {
  background: var(--dd-s1); border: 1px solid var(--dd-line); border-radius: 12px;
  padding: 24px; gap: 16px; margin-top: 24px; min-width: 0 !important; max-width: 100%;
  overflow-x: clip;
}
.dd-section > .block, .dd-section > .column { min-width: 0 !important; max-width: 100%; contain: inline-size; }
.dd-section .table-wrap, .dd-section .table-container { max-width: 100%; overflow-x: auto; }
.dd-section > .block, .dd-section .block { box-shadow: none; }
.dd-section .block.padded:not(.dd-keep) { background: transparent; }
.dd-section .form { background: transparent; border: 0; }
.dd-lead.block, .dd-lead { background: transparent !important; border: 0 !important; padding: 0 !important; }
.dd-head { background: transparent !important; border: 0 !important; padding: 0 !important; }
.dd-head h2 { margin-bottom: 0; }
.dd-head .prose { margin: 0; }

/* Blocks, labels */
.gradio-container .block { border-radius: 12px; border-color: var(--dd-line); }
.gradio-container .dd-section .block { background: transparent; border-color: transparent; padding: 0; }
.gradio-container .dd-section .row { gap: 16px; }
.gradio-container label > span, .gradio-container span[data-testid="block-info"],
.gradio-container .block-title, .gradio-container .block-label {
  color: var(--dd-ink-2) !important; font-size: 14px; font-weight: 500; letter-spacing: 0;
}
.gradio-container .block-info, .gradio-container .info { color: var(--dd-ink-3) !important; font-size: 13px; }

/* Inputs: 8px radius, surface-2 fill, hairline, lavender ring on focus */
.gradio-container input[type="text"], .gradio-container input[type="password"],
.gradio-container input[type="number"], .gradio-container textarea,
.gradio-container select {
  background: var(--dd-s2) !important; color: var(--dd-ink) !important;
  border: 1px solid var(--dd-line-2) !important; border-radius: 8px !important;
  font-family: var(--font); font-size: 16px; padding: 8px 12px; min-height: 44px;
}
.gradio-container textarea::placeholder, .gradio-container input::placeholder {
  color: var(--dd-ink-4) !important; opacity: 1;
}
.gradio-container input:focus, .gradio-container textarea:focus {
  border-color: var(--dd-accent) !important; box-shadow: var(--dd-ring) !important; outline: none;
}
.gradio-container .wrap-inner { background: var(--dd-s2); border-radius: 8px; color: var(--dd-ink); }
.gradio-container .wrap-inner { border: 1px solid var(--dd-line-2); min-height: 44px; }
.gradio-container .wrap-inner:focus-within { border-color: var(--dd-accent); box-shadow: var(--dd-ring); }
.gradio-container .wrap-inner input { border: 0 !important; box-shadow: none !important;
  background: transparent !important; min-height: 32px; }
.gradio-container .token {
  background: var(--dd-s4); color: var(--dd-ink-2); border: 1px solid var(--dd-line-2);
  border-radius: 6px; font-size: 13px;
}
.gradio-container .token-remove { background: transparent; color: var(--dd-ink-3); }
.gradio-container .secondary-wrap, .gradio-container .icon-wrap svg { color: var(--dd-ink-3); }
ul.options, .gradio-container ul.options, body > ul.options {
  background: var(--dd-s3) !important; border: 1px solid var(--dd-line-3) !important;
  border-radius: 8px !important; color: var(--dd-ink) !important; box-shadow: none !important;
}
ul.options li, .gradio-container ul.options li { color: var(--dd-ink-2) !important; min-height: 40px; }
ul.options li:hover, ul.options li.active, ul.options li.selected {
  background: var(--dd-s4) !important; color: var(--dd-ink) !important;
}
ul.options li .inner-item, ul.options li span { color: inherit; }

/* Buttons: 8px radius, 14px/500 labels */
.gradio-container button { font-family: var(--font); letter-spacing: 0; }
.gradio-container button.lg, .gradio-container button.md, .gradio-container button.sm {
  border-radius: 8px; font-size: 14px; font-weight: 500; min-height: 44px; padding: 8px 16px;
}
.gradio-container button.primary {
  background: var(--dd-accent) !important; border-color: var(--dd-accent) !important;
  color: #ffffff !important;
}
.gradio-container button.primary:hover { background: var(--dd-accent-hover) !important;
  border-color: var(--dd-accent-hover) !important; }
.gradio-container button.primary:active { background: #5e69d1 !important; }
.gradio-container button.stop, .gradio-container button.secondary {
  background: var(--dd-s1) !important; border: 1px solid var(--dd-line-2) !important;
  color: var(--dd-ink) !important;
}
.gradio-container button.stop:hover, .gradio-container button.secondary:hover {
  background: var(--dd-s3) !important; border-color: var(--dd-line-3) !important;
}
.dd-actions { gap: 12px !important; flex-wrap: nowrap !important; }
.dd-actions > * { min-width: 0 !important; }
.dd-actions button { width: 100%; min-width: 0 !important; }
.dd-actions > :first-child { flex: 2 1 0; }
.dd-actions > :last-child { flex: 1 1 0; }
.dd-actions button.primary { min-height: 48px; font-size: 15px; }

/* Accordions: nested panels on surface-2 */
.gradio-container .dd-section .block.dd-accordion, .dd-accordion {
  background: var(--dd-s2) !important; border: 1px solid var(--dd-line) !important;
  border-radius: 12px !important; padding: 0 !important; overflow: hidden;
}
.dd-accordion > .label-wrap, .dd-accordion .label-wrap {
  color: var(--dd-ink) !important; font-size: 16px; font-weight: 500; min-height: 48px;
  padding: 12px 16px; background: transparent;
}
.dd-accordion .label-wrap:hover { background: var(--dd-s3); }
.dd-accordion .label-wrap svg, .dd-accordion .label-wrap .icon { color: var(--dd-ink-3); }
.dd-accordion > div:not(.label-wrap) { padding: 4px 16px 16px; }
.dd-accordion .block, .dd-accordion .form { background: transparent; }

/* Checkboxes and radios as selectable chips */
.gradio-container label.svelte-1mhtq7j, .gradio-container .wrap label { color: var(--dd-ink-2); }
.gradio-container input[type="checkbox"], .gradio-container input[type="radio"] {
  accent-color: var(--dd-accent); width: 18px; height: 18px; border-color: var(--dd-line-3);
  background-color: var(--dd-s2);
}
.gradio-container input[type="checkbox"]:checked, .gradio-container input[type="radio"]:checked {
  background-color: var(--dd-accent); border-color: var(--dd-accent);
}
.gradio-container .wrap label.checkbox-label, .gradio-container label.checkbox-label,
.gradio-container fieldset label { min-height: 44px; align-items: center; }
.gradio-container input[type="range"] { accent-color: var(--dd-accent); }
.gradio-container input[type="number"] {
  height: 32px !important; min-height: 32px !important; padding: 4px 8px !important;
  font-size: 14px; line-height: 1.2; width: 72px; text-align: center;
}
.gradio-container .slider_input_container, .gradio-container .head { align-items: center; }

/* Status line and progress log */
.dd-status, .dd-status p {
  font-size: 14px; color: var(--dd-ink-2) !important; background: var(--dd-s2);
  border: 1px solid var(--dd-line); border-radius: 8px; padding: 8px 12px; margin: 0;
  min-height: 20px;
}
.dd-status:not(:has(p)) { display: none; }
.dd-status p { border: 0; padding: 0; background: transparent; }
.dd-log textarea {
  font-family: var(--font-mono) !important; font-size: 13px !important; line-height: 1.5;
  color: var(--dd-ink-2) !important; background: var(--dd-canvas) !important;
  border-color: var(--dd-line) !important; min-height: 220px;
}

/* Row of checkbox + key box + state: bottom-aligned so the three line up */
.dd-keyrow { align-items: flex-end !important; }
.dd-keyrow .prose { padding-bottom: 10px; white-space: nowrap; }
.dd-keyrow > .form { flex-direction: row !important; align-items: flex-end !important;
  flex: 3 1 0 !important; gap: 16px; flex-wrap: nowrap !important; }
.dd-keyrow > .form > :first-child { flex: 0 0 140px !important; min-width: 140px !important; }
.dd-keyrow > .form > :last-child { flex: 1 1 auto !important; min-width: 0 !important; }
.dd-keyrow > .form > :first-child { align-self: center; padding-bottom: 0; }
.gradio-container button.sm { white-space: nowrap; }

/* File and dataframe */
.dd-file .upload-container, .dd-file button.center { min-height: 88px !important; height: auto !important;
  max-height: 120px; padding: 12px; background: var(--dd-s2); border-color: var(--dd-line-2); }
.dd-file .upload-container .wrap { height: auto !important; min-height: 0 !important; width: auto !important;
  background: transparent; color: var(--dd-ink-3); }
.dd-file .wrap.default.hide, .dd-file .wrap.hide { display: none; }
.dd-file { min-height: 0 !important; }
.gradio-container [data-testid="file"], .gradio-container .file-preview-holder,
.gradio-container .file-preview, .gradio-container table.file-preview {
  background: var(--dd-s2); color: var(--dd-ink-2);
}
.gradio-container .file-preview td, .gradio-container .file-preview a { color: var(--dd-ink-2); }
.gradio-container .file-preview .download a { color: var(--dd-accent-hover); }
.gradio-container .table-wrap, .gradio-container .table-container {
  background: var(--dd-s1); border: 1px solid var(--dd-line); border-radius: 8px;
}
.gradio-container table { color: var(--dd-ink-2); }
.gradio-container thead th, .gradio-container th {
  background: var(--dd-s2) !important; color: var(--dd-ink) !important; font-weight: 500;
}
.gradio-container tbody td { color: var(--dd-ink-2); background: var(--dd-s1); }
.gradio-container tbody tr:nth-child(even) td { background: var(--dd-s2); }
.gradio-container .cell-wrap, .gradio-container .cell-menu-button { color: inherit; }

/* Footer and settings links */
footer, .gradio-container footer { color: var(--dd-ink-3) !important; border-color: var(--dd-line); }
footer button, footer a { color: var(--dd-ink-3) !important; }
.toast-wrap .toast-body { background: var(--dd-s3); border: 1px solid var(--dd-line-3); color: var(--dd-ink); }
.dd-hidden-label label { display: none; }

/* Phone: one column, comfortable tap targets, no horizontal overflow. Gradio prefixes
   every selector inside a media query, so these use plain class names only. */
@media (max-width: 768px) {
  h1 { font-size: 32px; letter-spacing: -0.8px; }
  .dd-section { padding: 16px; margin-top: 16px; }
  .row:not(.dd-actions) { flex-direction: column !important; align-items: stretch !important; }
  .row:not(.dd-actions) > * { width: 100% !important; min-width: 0 !important; flex: 1 1 auto !important; }
  .dd-accordion > div:not(.label-wrap) { padding: 4px 12px 12px; }
  .dd-keyrow .prose { padding-bottom: 0; }
  .dd-keyrow > .form { flex-direction: column !important; align-items: stretch !important; gap: 8px; }
  .dd-keyrow > .form > * { flex: 0 0 auto !important; min-width: 0 !important; width: 100% !important; }
}
""".replace("__SANS__", _SANS).replace("__MONO__", _MONO)

FORCE_DARK_JS = """() => {
  const dark = () => { document.documentElement.classList.add('dark');
                       document.body.classList.add('dark'); };
  dark();
  new MutationObserver(dark).observe(document.body, {attributes: true, attributeFilter: ['class']});
}"""
FORCE_DARK_HEAD = ('<meta name="color-scheme" content="dark">'
                   '<meta name="theme-color" content="#010102">')


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

    with gr.Blocks(title="Deep Dog 2",
                   delete_cache=(CACHE_SWEEP_SECONDS, CACHE_MAX_AGE_SECONDS)) as demo:
        # Non-secret settings and the last report, kept in the visitor's browser.
        saved = gr.BrowserState(None, storage_key=SETTINGS_STORAGE_KEY,
                                secret=SETTINGS_STORAGE_SECRET)
        gr.Markdown("# Deep Dog 2", elem_classes="dd-head")
        gr.Markdown(
            "Ask a question. A supervisor plans the research, platform agents gather sources, "
            "and you get a cited report. Keys you type here are used for this run only and "
            "are not stored.", elem_classes="dd-lead")

        # 1. Setup
        with gr.Column(elem_classes="dd-section", elem_id="dd-setup"):
            gr.Markdown("## 1. Setup", elem_classes="dd-head")
            question = gr.Textbox(label="Research question", lines=3,
                                  placeholder="What are the main benefits and limitations of "
                                              "sodium-ion batteries for home energy storage?")
            with gr.Row(elem_classes="dd-actions"):
                run_btn = gr.Button("Run research", variant="primary")
                stop_btn = gr.Button("Stop", variant="stop")

            with gr.Accordion("Models", open=True, elem_classes="dd-accordion"):
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

            with gr.Accordion("Search tools", open=True, elem_classes="dd-accordion"):
                web_on = gr.Checkbox(web_on0, label="Web search on (master switch)")
                with gr.Row(elem_classes="dd-keyrow"):
                    use_tavily = gr.Checkbox(tavily_on0, label="Tavily", scale=1)
                    key_box("TAVILY_API_KEY")
                    tavily_state = gr.Markdown(search_key_status("TAVILY_API_KEY", ""))
                with gr.Row(elem_classes="dd-keyrow"):
                    use_exa = gr.Checkbox(exa_on0, label="Exa", scale=1)
                    key_box("EXA_API_KEY")
                    exa_state = gr.Markdown(search_key_status("EXA_API_KEY", ""))
                ready = gr.Markdown()

            with gr.Accordion("Agents and budget", open=False, elem_classes="dd-accordion"):
                agents = gr.CheckboxGroup(AGENT_CHOICES, value=["ResearchWeb"], label="Agents")
                with gr.Row():
                    minutes = gr.Slider(2, MAX_MINUTES, value=p["minutes"], step=1,
                                        label="Research time limit (minutes)")
                    iters = gr.Slider(3, 50, value=p["iters"], step=1,
                                      label="Supervisor iterations")
                with gr.Row():
                    searches = gr.Slider(1, 10, value=p["searches"], step=1,
                                         label="Searches per sub-agent")
                    concurrency = gr.Slider(1, 6, value=3, step=1, label="Parallel sub-agents")

            with gr.Accordion("API keys (this run only)", open=False, elem_classes="dd-accordion"):
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

            with gr.Accordion("Tracing (LangSmith)", open=False, elem_classes="dd-accordion"):
                ls_on = gr.Checkbox(False, label="Trace this run in LangSmith")
                gr.Markdown(
                    "**Privacy:** tracing sends the prompts, search results and the final report "
                    "of this run to your LangSmith account, into the project named below. It is "
                    "off unless you tick the box. Your LangSmith key is used for this run only "
                    "and is not stored.")
                with gr.Row():
                    ls_key = gr.Textbox(label="LangSmith API key", type="password", max_lines=1)
                    ls_region = gr.Dropdown(LANGSMITH_REGION_CHOICES,
                                            value=LANGSMITH_DEFAULT_REGION,
                                            allow_custom_value=False, filterable=False,
                                            label="LangSmith region")
                ls_project = gr.Textbox(label="LangSmith project", value=LANGSMITH_DEFAULT_PROJECT,
                                        max_length=LANGSMITH_PROJECT_MAX_CHARS, max_lines=1)
                ls_state = gr.Markdown(langsmith_hint(False, ""))
        ls_boxes = [ls_on, ls_key, ls_project, ls_region]  # run_ui's ls_* order

        # 2. Progress: always on the page, between the setup and the results.
        with gr.Column(elem_classes="dd-section", elem_id="dd-progress"):
            gr.Markdown("## 2. Progress", elem_classes="dd-head")
            status = gr.Markdown(elem_classes="dd-status")
            log = gr.Textbox(label="Events", lines=10, max_lines=10, autoscroll=True,
                             interactive=False, elem_classes="dd-log")

        # 3. Report and sources
        with gr.Column(elem_classes="dd-section", elem_id="dd-results"):
            gr.Markdown("## 3. Report and Sources", elem_classes="dd-head")
            report = gr.Markdown(label="Report")
            download = gr.File(label="Download report (.md)", elem_classes="dd-file")
            gr.Markdown("### Sources", elem_classes="dd-head")
            sources = gr.Dataframe(headers=["Code", "Title", "URL"], interactive=False,
                                   wrap=True, type="array")

        # .input, not .change: restoring a saved page sets this box from code and must not
        # re-apply the preset over the restored model and budget values.
        preset.input(apply_preset, preset,
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
                      show_progress="hidden", api_name=False).then(
            store_report, [report, sources, download, saved], saved,
            show_progress="hidden", api_name=False)

        # Remember non-secret settings in the browser; restore them (and the last report)
        # on page load. The key boxes and the LangSmith key are not inputs here.
        setting_boxes = [question, preset, sup, sub, draft, route_or, thinking, minutes, iters,
                         searches, concurrency, agents, web_on, use_tavily, use_exa, ls_on,
                         ls_project, ls_region]
        for control in setting_boxes:
            control.change(save_settings, [saved, *setting_boxes], saved, api_name=False,
                           show_progress="hidden", trigger_mode="always_last")
        demo.load(restore_settings, saved, [*setting_boxes, report, sources],
                  api_name=False, show_progress="hidden")
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
                        theme=build_theme(), css=CSS, js=FORCE_DARK_JS, head=FORCE_DARK_HEAD,
                        footer_links=["gradio"], ssr_mode=False,
                        allowed_paths=[str(report_root())])


if __name__ == "__main__":
    main()
