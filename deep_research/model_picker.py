"""Model picker: presets and per-role choices for runs on the Claude subscription.

A choice is (model, effort) per role. It is encoded in the model-chain names themselves
(``claudecode:sonnet-5.5@high``), so supervisor, draft writer, sub-agents, per-platform chains and
fallback entries each carry their own effort without new engine plumbing. The final report writer
shares the supervisor's chain (the engine builds it that way, for prompt-cache reasons)."""
from dataclasses import replace
from typing import Optional

from deep_research.providers.claude_code.models import MODEL_IDS, EFFORTS, catalog, validate_effort

# role -> (model, effort). "draft" follows the supervisor unless a preset or override sets it.
PRESETS = {
    "economy":           {"supervisor": ("sonnet-5.5", "high"),   "subagent": ("haiku-5.5", "high")},
    "benchmark":         {"supervisor": ("opus-5.5", "medium"),   "subagent": ("sonnet-5.5", "high")},
    "custom":            {"supervisor": ("sonnet-5.5", "xhigh"),  "subagent": ("haiku-5.5", "xhigh")},
    "testing-framework": {"supervisor": ("sonnet-5.5", "medium"), "subagent": ("haiku-5.5", "medium")},
}
DEFAULT_PRESET = "economy"
ROLES = ("supervisor", "draft", "subagent")


def _name(model: str, effort: Optional[str]) -> str:
    if model not in MODEL_IDS and not model.startswith("claude-"):
        raise ValueError(f"Unknown model {model!r}. Use one of {', '.join(MODEL_IDS)} or a full 'claude-...' id.")
    e = validate_effort(effort)
    return f"claudecode:{model}" + (f"@{e}" if e else "")


def choices(preset: str = DEFAULT_PRESET, **overrides) -> dict:
    """``{role: (model, effort)}`` for a preset with per-role overrides.

    Overrides: ``supervisor=(model, effort)``, ``draft=...``, ``subagent=...``, and
    ``subagent_by_agent={"ResearchReddit": (model, effort), ...}``."""
    key = preset.strip().lower()
    if key not in PRESETS:
        raise ValueError(f"Unknown preset {preset!r}. Use one of: {', '.join(PRESETS)}.")
    out = dict(PRESETS[key])
    out.setdefault("draft", out["supervisor"])
    for role in ROLES:
        if overrides.get(role):
            out[role] = tuple(overrides[role])
    by_agent = overrides.get("subagent_by_agent") or {}
    if by_agent:
        out["subagent_by_agent"] = {k: tuple(v) for k, v in by_agent.items()}
    return out


SEARCH_ENGINES = ("claude", "exa", "tavily", "both")


def apply_preset(config, preset: str = DEFAULT_PRESET, search: str = "claude", **overrides):
    """A copy of ``config`` whose model chains follow the preset (and overrides). Single-entry chains,
    so there is no silent fallback to a different model."""
    if search not in SEARCH_ENGINES:
        raise ValueError(f"Unknown search engine {search!r}. Use one of: {', '.join(SEARCH_ENGINES)}.")
    c = choices(preset, **overrides)
    chain_by_agent = dict(config.subagent_model_chain_by_agent or {})
    for agent, (m, e) in (c.get("subagent_by_agent") or {}).items():
        chain_by_agent[agent] = [_name(m, e)]
    return replace(
        config,
        profile=preset.strip().lower(),
        supervisor_model_fallback_chain=[_name(*c["supervisor"])],
        draft_report_model_fallback_chain=[_name(*c["draft"])],
        subagent_model_fallback_chain=[_name(*c["subagent"])],
        subagent_model_chain_by_agent=chain_by_agent,
        disable_model_fallback=True,
        web_search_engine=search,      # 'claude' = no search key; 'exa' for runs compared with the published benchmark
    )


def describe(preset: str = DEFAULT_PRESET, **overrides) -> str:
    c = choices(preset, **overrides)
    parts = [f"{r}: {c[r][0]} @ {c[r][1]}" for r in ROLES]
    return f"{preset} -> " + "; ".join(parts)


# Relative cost per token, haiku-5.5 = 1. MEASURED: validation/p4 (sonnet $0.484 for 124k tokens vs haiku $0.067 for
# 106k tokens, about 6x) and swarm-framework validation/m6 (sonnet about 6x haiku own-runner, opus at LOW effort about
# 1.8x sonnet, one run per cell). Effort multipliers were NOT measured: do not infer cost from the effort level alone.
RELATIVE_COST = {"haiku-5.5": 1.0, "sonnet-5.5": 6.0, "opus-5.5": 11.0}
COST_BASIS = "measured ratios (p4, m6); one run per cell; effort multiplier unmeasured"


def picker_catalog() -> dict:
    """What a UI or dialog can offer."""
    return {**catalog(), "presets": {k: describe(k) for k in PRESETS}, "roles": list(ROLES), "default": DEFAULT_PRESET,
            "relative_cost": RELATIVE_COST, "relative_cost_basis": COST_BASIS}
