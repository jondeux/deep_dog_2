"""Command-line switches for ``run_research.py``: one table that drives the parser, the config builder, ``--help``
and the manual (``docs/DEEP_DOG_RESEARCH_MANUAL.md`` is checked against ``OPTIONS`` by a test).

Three ways to set a ``RunConfig`` field, weakest first:

1. ``--config run.json``: a JSON object of RunConfig fields.
2. A named switch (``--time-max 20``, ``--agents ResearchWeb,ResearchReddit`` ...): the common knobs.
3. ``--set field=value`` (repeatable): any RunConfig field, type-coerced from the dataclass. Unknown names are refused.

Models are picked by role (``--supervisor-model`` ...) from a preset. A model spec is a Claude-plan name
(``sonnet-5.5``, ``opus-5.5@high``) or a third-party id (``deepinfra:<id>``, ``openrouter:<slug>``, ``together:<id>``,
``fireworks:<id>``). Keys for third-party backends come from the environment only; nothing here accepts one.
"""
from __future__ import annotations

import argparse
import json
import os
import typing
from dataclasses import dataclass, fields, replace
from difflib import get_close_matches
from pathlib import Path
from typing import Any, Optional

from deep_research import config as _cfg
from deep_research import model_picker
from deep_research.run_config import RunConfig


class CliError(ValueError):
    """A bad switch value; ``run_research.py`` turns it into a parser error."""


@dataclass(frozen=True)
class Option:
    flag: str                        # "--time-max"
    field: str                       # RunConfig field it sets
    kind: str                        # int | float | str | bool | choice | list | flag_false
    group: str
    help: str
    choices: tuple = ()
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    exclusive_min: bool = False
    metavar: Optional[str] = None


G_BUDGET, G_AGENTS, G_SEARCH, G_OUTPUT, G_MODEL, G_LS, G_COST = (
    "effort budget", "agents", "search", "output", "behaviour", "LangSmith", "cost and plan limits")

OPTIONS: tuple = (
    # ── effort budget ──
    Option("--time-min", "research_time_min_minutes", "float", G_BUDGET, "shortest research window, minutes", minimum=0.5, metavar="MIN"),
    Option("--time-max", "research_time_max_minutes", "float", G_BUDGET, "longest research window, minutes (research then goes to the report)", minimum=0.5, metavar="MIN"),
    Option("--salvage-fraction", "findings_salvage_time_fraction", "float", G_BUDGET, "share of the time limit after which findings are salvaged into a report", minimum=0, maximum=1, metavar="F"),
    Option("--supervisor-iterations", "supervisor_max_iterations", "int", G_BUDGET, "most supervisor turns", minimum=1, metavar="N"),
    Option("--supervisor-timeout", "supervisor_timeout_seconds", "int", G_BUDGET, "seconds one supervisor turn may take", minimum=1, metavar="SEC"),
    Option("--parallel-research", "supervisor_max_concurrent_research", "int", G_BUDGET, "sub-agents the supervisor may run at once", minimum=1, metavar="N"),
    Option("--parallel-discovery", "supervisor_max_concurrent_discovery", "int", G_BUDGET, "discovery sub-agents at once", minimum=1, metavar="N"),
    Option("--subagent-iterations", "subagent_max_iterations", "int", G_BUDGET, "most turns per sub-agent", minimum=1, metavar="N"),
    Option("--searches-per-agent", "subagent_max_searches", "int", G_BUDGET, "most searches per sub-agent", minimum=1, metavar="N"),
    Option("--reads-per-agent", "subagent_max_reads", "int", G_BUDGET, "most page reads per sub-agent turn", minimum=1, metavar="N"),
    Option("--total-reads", "subagent_max_total_reads", "int", G_BUDGET, "most page reads per sub-agent in all", minimum=1, metavar="N"),
    Option("--saves-per-agent", "subagent_max_saves", "int", G_BUDGET, "most sources a sub-agent may save", minimum=1, metavar="N"),
    Option("--subagent-concurrency", "subagent_max_concurrency", "int", G_BUDGET, "tool calls a sub-agent runs at once", minimum=1, metavar="N"),
    Option("--subagent-timeout", "subagent_timeout_seconds", "int", G_BUDGET, "seconds one sub-agent may take", minimum=1, metavar="SEC"),
    Option("--llm-timeout", "llm_timeout", "int", G_BUDGET, "seconds one model call may take", minimum=1, metavar="SEC"),
    Option("--tool-timeout", "tool_timeout", "int", G_BUDGET, "seconds one tool call may take", minimum=1, metavar="SEC"),
    # ── agents ──
    Option("--agents", "enabled_agents", "list", G_AGENTS, "agents the supervisor may call, comma separated (see --list-agents)", metavar="A,B"),
    Option("--general-platforms", "general_agent_platforms", "list", G_AGENTS, "platforms ResearchGeneral may use, comma separated (empty = all)", metavar="P,Q"),
    # ── search ──
    Option("--search", "web_search_engine", "choice", G_SEARCH, "web search backend: claude (Claude's own search, no key), exa, tavily, both",
           choices=("claude", "exa", "tavily", "both")),
    Option("--exa-max-chars", "exa_search_max_chars", "int", G_SEARCH, "characters of page text Exa returns per result", minimum=1, metavar="N"),
    Option("--fetch-max-chars", "fetch_url_max_chars", "int", G_SEARCH, "characters a page read may return", minimum=1, metavar="N"),
    Option("--search-failure-limit", "search_failure_limit", "int", G_SEARCH, "stop the run after N failed Exa or Tavily searches in a row (0 = off)", minimum=0, metavar="N"),
    Option("--no-page-reader", "claude_page_reader", "flag_false", G_SEARCH,
           "with --search claude, do not read pages through WebFetch: agents get the search summary only (the cheaper, thinner arm)"),
    Option("--page-reads", "claude_page_reads_per_agent", "int", G_SEARCH,
           "with --search claude: pages each agent reads through WebFetch (default 3; its other reads stay search summaries; 0 = no limit, the slower, deeper arm)",
           minimum=0, metavar="N"),
    # ── output ──
    Option("--output-mode", "output_mode", "choice", G_OUTPUT, "where outputs go", choices=("file", "db", "both", "none")),
    Option("--log-mode", "log_mode", "choice", G_OUTPUT, "where logs go", choices=("file", "db", "both")),
    Option("--language", "target_language_fallback", "str", G_OUTPUT, "language the report is written in", metavar="LANG"),
    Option("--subtopics", "enable_subtopic_generation", "bool", G_OUTPUT, "generate sub-topics before research"),
    Option("--trace", "enable_research_trace", "bool", G_OUTPUT, "write the research trace document"),
    Option("--source-log", "enable_source_log", "bool", G_OUTPUT, "log every source"),
    Option("--save-subagent-reports", "save_subagent_reports_to_file", "bool", G_OUTPUT, "save each sub-agent's report to a file"),
    Option("--subagent-output-mode", "subagent_output_mode", "str", G_OUTPUT, "what sub-agents hand back", metavar="MODE"),
    Option("--discovery-output-mode", "discovery_output_mode", "str", G_OUTPUT, "what discovery agents hand back", metavar="MODE"),
    # ── behaviour ──
    Option("--thinking", "thinking_mode", "choice", G_MODEL, "model thinking mode", choices=("auto", "on", "off")),
    Option("--route-openrouter", "route_via_openrouter", "bool", G_MODEL, "route supported (non-Claude-plan) models through OpenRouter"),
    # ── LangSmith ──
    Option("--langsmith", "langsmith_tracing", "bool", G_LS, "trace the run in LangSmith (the key comes from LANGSMITH_API_KEY in the environment)"),
    Option("--langsmith-project", "langsmith_project", "str", G_LS, "LangSmith project name (default deep-dog-2)", metavar="NAME"),
    # ── cost and plan limits ──
    Option("--usd-cap", "claude_code_usd_cap", "float", G_COST,
           "list-price cap in USD for the whole run (default 5.0), Claude-plan and third-party spend together. Research stops when "
           "spend plus the projected final report reaches it (--static-reserve: at 75%% of it)", minimum=0, exclusive_min=True, metavar="USD"),
    Option("--report-budget", "claude_code_report_budget_usd", "float", G_COST,
           "most the final-report call may cost, in USD (default: whatever is left under --usd-cap). The engine projects the call's cost "
           "first: over budget it shortens the per-source text, and if even that is too dear it returns the findings digest, which "
           "--resume can finish with a higher budget", minimum=0, exclusive_min=True, metavar="USD"),
    Option("--static-reserve", "claude_code_dynamic_reserve", "flag_false", G_COST,
           "keep a fixed share of --usd-cap (25%%, at least $0.50) for the final report instead of stopping research when spend plus "
           "the projected report reaches the cap"),
    Option("--plan-window-max", "claude_code_plan_window_max", "float", G_COST,
           "never exceed this share of the Claude plan window, 0.1 to 1 (default 0.8). Research stops 0.1 below it, the rest is kept "
           "for the final report", minimum=0.1, maximum=1, metavar="F"),
)

BY_DEST = {o.field: o for o in OPTIONS}
# A resumed run keeps its saved models, search and limits; only these may change.
RESUME_FIELDS = frozenset({"claude_code_usd_cap", "claude_code_report_budget_usd", "claude_code_plan_window_max"})
ROLES = model_picker.ROLES
_ROLE_FIELDS = {"supervisor": "supervisor_model_fallback_chain", "draft": "draft_report_model_fallback_chain",
                "subagent": "subagent_model_fallback_chain"}


def _dest(o: Option) -> str:
    return "opt_" + o.flag.lstrip("-").replace("-", "_")


# ── parser ────────────────────────────────────────────────────────────────────
def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Every model, setting and inspection switch. The input, output-folder and resume switches stay in run_research.py."""
    picker = parser.add_argument_group(
        "models",
        "A model is a Claude-plan name (haiku-5.5, sonnet-5.5, opus-5.5, optionally @low|medium|high|xhigh|max) run through the "
        "unmodified `claude` CLI on YOUR OWN plan (personal use only), or a third-party id "
        f"({', '.join(p + ':<id>' for p in model_picker.HOSTED_PREFIXES)}) billed per token on your own key from the environment. "
        "Roles you do not set follow the preset; roles can mix backends.")
    picker.add_argument("--preset", help="economy (default when any model flag is used), benchmark, custom, testing-framework")
    for role in ROLES:
        picker.add_argument(f"--{role}-model", metavar="MODEL", help=f"model for the {role} role"
                            + (" (default: the supervisor's)" if role == "draft" else ""))
        picker.add_argument(f"--{role}-effort", metavar="LEVEL", help=f"reasoning effort for the {role} role (Claude-plan models)")
        picker.add_argument(f"--{role}-fallback", metavar="M1,M2",
                            help=f"fallback models for the {role} role, tried in order if the first fails (default: none)")
    picker.add_argument("--agent-model", action="append", default=[], metavar="AGENT=MODEL",
                        help="model for one platform agent, repeatable, e.g. ResearchReddit=haiku-5.5@low (see --list-agents)")
    picker.add_argument("--price", action="append", default=[], metavar="MODEL=IN/OUT",
                        help="price in USD per million tokens of a third-party model, repeatable, e.g. "
                             "deepinfra:deepseek-ai/DeepSeek-V3=0.38/0.89. Needed when the engine has no price for it")
    picker.add_argument("--list-presets", action="store_true", help="show the presets and exit")
    picker.add_argument("--list-models", action="store_true", help="show the models, prefixes and which keys are set (names only) and exit")
    picker.add_argument("--list-agents", action="store_true", help="show the agents and platforms and exit")

    groups: dict = {}
    for o in OPTIONS:
        g = groups.get(o.group)
        if g is None:
            g = groups[o.group] = parser.add_argument_group(o.group)
        kw: dict = {"dest": _dest(o), "default": None, "help": o.help}
        if o.kind in ("bool",):
            g.add_argument(o.flag, action=argparse.BooleanOptionalAction, **kw)
        elif o.kind == "flag_false":
            kw.pop("default")
            g.add_argument(o.flag, action="store_true", default=None, **kw)
        else:
            if o.metavar:
                kw["metavar"] = o.metavar
            if o.kind == "choice":
                kw["choices"] = o.choices
            elif o.kind in ("int", "float"):
                kw["type"] = int if o.kind == "int" else float
            g.add_argument(o.flag, **kw)

    gen = parser.add_argument_group("any other setting")
    gen.add_argument("--set", dest="set_items", action="append", default=[], metavar="FIELD=VALUE",
                     help="set any RunConfig field, repeatable (numbers, true/false, none, comma lists or JSON). Beats the switches above")
    gen.add_argument("--config", dest="config_file", metavar="FILE", help="JSON object of RunConfig fields (the switches and --set override it)")
    gen.add_argument("--show-config", action="store_true", help="print the settings this run would use (no secrets) and exit; spends nothing")


# ── coercion ──────────────────────────────────────────────────────────────────
def _field_types() -> dict:
    return typing.get_type_hints(RunConfig)


def _unwrap(tp):
    if typing.get_origin(tp) is typing.Union:
        args = [a for a in typing.get_args(tp) if a is not type(None)]
        return args[0] if len(args) == 1 else tp, type(None) in typing.get_args(tp)
    return tp, False


def coerce_value(name: str, raw: Any) -> Any:
    """``raw`` (a string from the command line or a JSON value) as the type of RunConfig field ``name``."""
    types = _field_types()
    if name not in types:
        hint = get_close_matches(name, list(types), n=3)
        raise CliError(f"unknown setting {name!r}" + (f"; did you mean {', '.join(hint)}?" if hint else ""))
    tp, optional = _unwrap(types[name])
    if not isinstance(raw, str):
        return raw
    text = raw.strip()
    if optional and text.lower() in ("none", "null", ""):
        return None
    origin = typing.get_origin(tp) or tp
    try:
        if origin is bool:
            low = text.lower()
            if low in ("1", "true", "yes", "on"):
                return True
            if low in ("0", "false", "no", "off"):
                return False
            raise ValueError
        if origin is int:
            return int(text)
        if origin is float:
            return float(text)
        if origin is list:
            if text.startswith("["):
                return list(json.loads(text))
            return [p.strip() for p in text.split(",") if p.strip()]
        if origin is dict:
            value = json.loads(text)
            if not isinstance(value, dict):
                raise ValueError
            return value
        return text
    except (ValueError, json.JSONDecodeError):
        raise CliError(f"{name}: cannot read {raw!r} as {getattr(origin, '__name__', origin)}") from None


def parse_set(items) -> dict:
    out: dict = {}
    for item in items or []:
        name, sep, raw = str(item).partition("=")
        if not sep or not name.strip():
            raise CliError(f"--set expects FIELD=VALUE, got {item!r}")
        name = name.strip()
        out[name] = coerce_value(name, raw)
    return out


def load_config_file(path) -> dict:
    p = Path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise CliError(f"--config file not found: {p}") from None
    except (OSError, json.JSONDecodeError) as e:
        raise CliError(f"--config {p}: not a JSON object ({e})") from None
    if not isinstance(data, dict):
        raise CliError(f"--config {p}: expected a JSON object of RunConfig fields")
    return {k: coerce_value(k, v) for k, v in data.items()}


def _check_range(o: Option, value) -> None:
    if o.kind not in ("int", "float") or value is None:
        return
    lo, hi = o.minimum, o.maximum
    bad = (lo is not None and (value <= lo if o.exclusive_min else value < lo)) or (hi is not None and value > hi)
    if bad:
        bound = (f"above {lo:g}" if o.exclusive_min else f"at least {lo:g}") if lo is not None else ""
        if hi is not None:
            bound = (bound + " and " if bound else "") + f"at most {hi:g}"
        raise CliError(f"{o.flag} must be {bound}")


def named_settings(args) -> dict:
    """RunConfig fields set by the named switches (validated)."""
    out: dict = {}
    for o in OPTIONS:
        value = getattr(args, _dest(o), None)
        if value is None:
            continue
        if o.kind == "flag_false":
            value = False if value else None
            if value is None:
                continue
        elif o.kind == "list":
            value = [p.strip() for p in str(value).split(",") if p.strip()]
            if o.field == "enabled_agents":
                unknown = [a for a in value if a not in _cfg.KNOWN_AGENT_NAMES]
                if unknown:
                    raise CliError(f"unknown agent {', '.join(unknown)}; known: {', '.join(_cfg.KNOWN_AGENT_NAMES)}")
            elif o.field == "general_agent_platforms":
                unknown = [a for a in value if a not in _cfg.KNOWN_PLATFORM_KEYS]
                if unknown:
                    raise CliError(f"unknown platform {', '.join(unknown)}; known: {', '.join(_cfg.KNOWN_PLATFORM_KEYS)}")
        _check_range(o, value)
        out[o.field] = value
    return out


def collect_settings(args) -> dict:
    """config file < named switches < --set."""
    merged: dict = {}
    if getattr(args, "config_file", None):
        merged.update(load_config_file(args.config_file))
    merged.update(named_settings(args))
    merged.update(parse_set(getattr(args, "set_items", None)))
    return merged


# ── models ────────────────────────────────────────────────────────────────────
def _chain(spec_list: str) -> list:
    return [model_picker.chain_name(s) for s in str(spec_list).split(",") if s.strip()]


def wants_models(args) -> bool:
    return bool(args.preset or args.agent_model or any(
        getattr(args, f"{r}_{k}", None) for r in ROLES for k in ("model", "effort", "fallback")))


def model_overrides(args, preset: str) -> dict:
    """Per-role ``(model, effort)`` overrides for ``model_picker.apply_preset``."""
    base = model_picker.choices(preset)
    out: dict = {}
    for role in ROLES:
        m, e = getattr(args, f"{role}_model"), getattr(args, f"{role}_effort")
        if not (m or e):
            continue
        cur = base.get(role) or base["supervisor"]
        if m:
            model, spec_effort = model_picker.split_spec(m)
            if model_picker.is_hosted(model):
                if e:
                    raise CliError(f"--{role}-effort applies only to Claude-plan models, not to {model}")
                out[role] = (model, None)
            else:
                out[role] = (model, e or spec_effort or cur[1])
        else:
            if model_picker.is_hosted(cur[0]):
                raise CliError(f"--{role}-effort applies only to Claude-plan models")
            out[role] = (cur[0], e)
    if "supervisor" in out and "draft" not in out:
        out["draft"] = out["supervisor"]           # the report writer follows the supervisor unless told otherwise
    by_agent: dict = {}
    for item in args.agent_model or []:
        name, sep, spec = str(item).partition("=")
        name = name.strip()
        if not sep or not spec.strip():
            raise CliError(f"--agent-model expects AGENT=MODEL, got {item!r}")
        if name not in _cfg.KNOWN_AGENT_NAMES:
            raise CliError(f"unknown agent {name!r}; known: {', '.join(_cfg.KNOWN_AGENT_NAMES)}")
        model, spec_effort = model_picker.split_spec(spec)
        by_agent[name] = (model, spec_effort or (None if model_picker.is_hosted(model) else base["subagent"][1]))
    if by_agent:
        out["subagent_by_agent"] = by_agent
    return out


def parse_prices(items) -> dict:
    """``--price deepinfra:org/model=0.38/0.89`` -> {"deepinfra:org/model": [0.38, 0.89]} (USD per million tokens)."""
    from deep_research.providers.hosted_meter import is_hosted_name, norm_key
    out: dict = {}
    for item in items or []:
        name, sep, rate = str(item).rpartition("=")
        low, slash, high = rate.partition("/")
        try:
            pin, pout = float(low), float(high)
            if not sep or not slash or pin < 0 or pout < 0:
                raise ValueError
        except ValueError:
            raise CliError(f"--price expects MODEL=IN/OUT in USD per million tokens, got {item!r}") from None
        if not is_hosted_name(name):
            raise CliError(f"--price names a third-party model ({', '.join(model_picker.HOSTED_PREFIXES)}:<id>), got {name!r}")
        out[norm_key(name)] = [pin, pout]
    return out


def apply_fallbacks(cfg: RunConfig, args) -> RunConfig:
    """Append ``--<role>-fallback`` models to the role chains; fallback is then on."""
    changes: dict = {}
    for role, fld in _ROLE_FIELDS.items():
        extra = getattr(args, f"{role}_fallback", None)
        if extra:
            changes[fld] = list(getattr(cfg, fld)) + _chain(extra)
    if changes:
        changes["disable_model_fallback"] = False
        cfg = replace(cfg, **changes)
    return cfg


# ── the whole config ──────────────────────────────────────────────────────────
def build_config(args, snapshot: Optional[dict] = None) -> Optional[RunConfig]:
    """The RunConfig a run should use, or None when no switch asked for anything (engine defaults).

    ``snapshot``: a run being resumed. Only the cost and window limits may change; anything else is refused."""
    settings = collect_settings(args)
    prices = parse_prices(getattr(args, "price", None))
    if prices:
        settings["hosted_prices_per_mtok"] = {**(settings.get("hosted_prices_per_mtok") or {}), **prices}
    wants = wants_models(args) or bool(settings)

    if snapshot is not None:
        if wants_models(args) or getattr(args, "price", None) or set(settings) - RESUME_FIELDS:
            raise CliError("--resume keeps the saved models, search and limits; only --usd-cap, --report-budget and "
                           "--plan-window-max may change")
        if not settings:
            return None
        from deep_research.resume import config_from_snapshot
        return replace(config_from_snapshot(snapshot), **settings)

    if not wants:
        return None
    cfg = RunConfig()
    if wants_models(args):
        preset = args.preset or model_picker.DEFAULT_PRESET
        try:
            overrides = model_overrides(args, preset)
            cfg = model_picker.apply_preset(cfg, preset, search=settings.get("web_search_engine") or "claude", **overrides)
            cfg = apply_fallbacks(cfg, args)
        except ValueError as e:
            raise CliError(str(e)) from None
        print("Models: " + model_picker.describe(preset, **overrides))
    cfg = replace(cfg, **settings) if settings else cfg
    from deep_research.providers.hosted_meter import hosted_models
    if hosted_models(cfg) and not cfg.hosted_metering:
        cfg = replace(cfg, hosted_metering=True)          # third-party spend counts against --usd-cap
    return cfg


def prepare_hosted(cfg: RunConfig, lookup=None, strict: bool = True) -> tuple:
    """``(config, problems)``: third-party models get their prices (``--price``, else the provider's public list) and
    their keys checked (names only). ``strict`` raises ``CliError`` on a problem. Network: one GET per provider."""
    from deep_research.providers import hosted_meter as hm
    if not hm.hosted_models(cfg):
        return cfg, []
    prices, problems = hm.resolve_prices(cfg, lookup or hm.lookup_price)
    for name, info in key_report(cfg).items():
        if hm.is_hosted_name(name) and not info["present"]:
            problems.append(f"{name} needs {', '.join(info['needs'])} in the environment (never in chat or on the command line).")
    if problems and strict:
        raise CliError("\n".join(problems))
    return replace(cfg, hosted_metering=True, hosted_prices_per_mtok={k: list(v) for k, v in prices.items()}), problems


# ── inspection ────────────────────────────────────────────────────────────────
def _chains(cfg: RunConfig) -> list:
    names = (list(cfg.supervisor_model_fallback_chain) + list(cfg.draft_report_model_fallback_chain)
             + list(cfg.subagent_model_fallback_chain))
    for chain in (cfg.subagent_model_chain_by_agent or {}).values():
        names += list(chain)
    return list(dict.fromkeys(str(n) for n in names))


def key_report(cfg: RunConfig) -> dict:
    """Env-var names the chain models need and whether each is set (never the values)."""
    out: dict = {}
    for name in _chains(cfg):
        try:
            need = _cfg.model_required_keys(name, bool(cfg.route_via_openrouter))
        except Exception:
            need = []
        out[name] = {"needs": need, "present": all(bool(os.environ.get(k)) for k in need) if need else True}
    return out


def show_config(cfg: RunConfig, lookup=None) -> str:
    cfg, problems = prepare_hosted(cfg, lookup, strict=False)
    out = {"settings": cfg.finalize().to_dict(), "keys": key_report(cfg)}
    if problems:
        out["problems"] = problems          # a real run would refuse to start with these
    return json.dumps(out, indent=2, ensure_ascii=False, default=str)


def models_report() -> str:
    from deep_research.providers.claude_code.models import EFFORTS, MODEL_IDS
    lines = ["Claude-plan models (your own `claude` sign-in; no key): " + ", ".join(MODEL_IDS) + "  efforts: " + ", ".join(EFFORTS),
             "  use as: sonnet-5.5   sonnet-5.5@high   claudecode:opus-5.5@medium", "",
             "Third-party models (billed per token on your own key; key names below, values never shown):"]
    for prefix in model_picker.HOSTED_PREFIXES:
        row = _cfg._HOSTED_PROVIDERS.get(prefix)
        keys = list(row.env_keys) if row else ["(not configured in this build)"]
        state = "set" if row and all(os.environ.get(k) for k in keys) else "not set"
        lines.append(f"  {prefix}:<model id>   key {', '.join(keys)}: {state}")
    lines += ["", "Presets: " + ", ".join(model_picker.PRESETS), "A role you do not set follows the preset; the draft writer follows the supervisor."]
    return "\n".join(lines)


def agents_report() -> str:
    cfg = RunConfig()
    return "\n".join([
        "Agents (--agents): " + ", ".join(_cfg.KNOWN_AGENT_NAMES), "  on by default: " + ", ".join(cfg.enabled_agents),
        "General-agent platforms (--general-platforms): " + ", ".join(_cfg.KNOWN_PLATFORM_KEYS) + "  (empty = all)"])


def option_table() -> list:
    """(flag, field, group, help, default) rows, the source of the manual's switch table."""
    base = RunConfig()
    return [(o.flag, o.field, o.group, o.help.replace("%%", "%"), getattr(base, o.field)) for o in OPTIONS]


MANUAL_BEGIN, MANUAL_END = "<!-- switches:begin (generated by deep_research/cli_options.py; do not edit) -->", "<!-- switches:end -->"


def manual_switch_tables() -> str:
    """The manual's switch reference as Markdown, one table per group, straight from ``OPTIONS``."""
    def show(v):
        return "" if v is None else ("on" if v is True else "off" if v is False else ", ".join(map(str, v)) if isinstance(v, list) else str(v))
    out, seen = [], []
    for o in OPTIONS:
        if o.group not in seen:
            seen.append(o.group)
    base = RunConfig()
    for group in seen:
        out += [f"**{group[0].upper() + group[1:]}**", "", "| Switch | Sets | Default | What it does |", "|---|---|---|---|"]
        for o in OPTIONS:
            if o.group != group:
                continue
            flag = {"bool": f"`{o.flag}` / `--no-{o.flag[2:]}`"}.get(o.kind, f"`{o.flag}" + (f" {o.metavar}" if o.metavar else "") + "`")
            out.append(f"| {flag} | `{o.field}` | {show(getattr(base, o.field))} | {o.help.replace('%%', '%')} |")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def sync_manual(path, write: bool = False) -> bool:
    """True when the manual's generated block matches ``OPTIONS``; ``write`` rewrites the block to match."""
    text = Path(path).read_text(encoding="utf-8")
    a, b = text.index(MANUAL_BEGIN) + len(MANUAL_BEGIN), text.index(MANUAL_END)
    fresh = "\n\n" + manual_switch_tables() + "\n"
    if text[a:b] == fresh:
        return True
    if write:
        Path(path).write_text(text[:a] + fresh + text[b:], encoding="utf-8")
    return False


if __name__ == "__main__":                       # python -m deep_research.cli_options docs/DEEP_DOG_RESEARCH_MANUAL.md
    import sys
    print("manual switch tables " + ("already current" if sync_manual(sys.argv[1], write=True) else "rewritten"))
