"""Meter for third-party models (``deepinfra:``, ``together:``, ``fireworks:``, ``openrouter:``).

These bill per token on the owner's own key. The meter counts each call's tokens, prices them, and adds the cost to the
run's governor, so ``--usd-cap`` and every guard built on it (dynamic reserve, report-budget guard) see Claude-plan and
third-party spend together. A call that would start past the cap raises ``GovernorStop``, like a Claude-plan call.

Where a call's cost comes from, best first:
1. ``reported``: the provider's own figure (OpenRouter returns ``usage.cost`` when asked).
2. ``estimated``: tokens times a price per million tokens, from ``--price`` / ``hosted_prices_per_mtok``, or, at start-up,
   from the provider's public model list (DeepInfra, OpenRouter, and Together with its key).
Nothing is hard-coded: a model with no price from any of these is refused before the run starts.

It is off unless ``RunConfig.hosted_metering`` is set (the CLI sets it), so the web UI is unaffected.
"""
from __future__ import annotations

import contextlib
import contextvars
import logging
import os
from typing import Optional

from langchain_core.callbacks import BaseCallbackHandler

logger = logging.getLogger(__name__)

PREFIXES = ("openrouter", "deepinfra", "together", "fireworks")
LOOKUP_TIMEOUT = 8.0
_scope: contextvars.ContextVar = contextvars.ContextVar("hosted_meter_governor", default=None)
_lists: dict = {}        # provider -> {model id: (USD per M input, USD per M output)} (in-process cache)


def is_hosted_name(name) -> bool:
    head, sep, rest = str(name).partition(":")
    return bool(sep) and head.lower() in PREFIXES and bool(rest.strip())


def norm_key(name: str) -> str:
    """``DeepInfra:Org/Model`` -> ``deepinfra:Org/Model`` (the prefix is case-insensitive, the id is not)."""
    head, _, rest = str(name).partition(":")
    return f"{head.lower()}:{rest.strip()}"


@contextlib.contextmanager
def meter_scope(governor):
    token = _scope.set(governor)
    try:
        yield governor
    finally:
        _scope.reset(token)


def callbacks_for(model_name: str) -> list:
    """The meter callback for a hosted model being built inside ``meter_scope``; ``[]`` outside it (not metered)."""
    governor = _scope.get()
    if governor is None:
        return []
    key = norm_key(model_name)
    if key not in governor.prices and not key.startswith("openrouter:"):
        raise ValueError(
            f"No price for {key}. Pass --price '{key}=<input USD per million tokens>/<output USD per million tokens>' "
            f"(the provider's pricing page has them), so the spend cap can count this model.")
    return [HostedMeterCallback(key, governor)]


class HostedMeterCallback(BaseCallbackHandler):
    """Stops a call that would start past the cap, and records each finished call's tokens and cost."""

    raise_error = True       # a cap stop must reach the engine, not be logged and swallowed
    run_inline = True

    def __init__(self, model: str, governor):
        self.model, self.governor = model, governor

    def _guard(self) -> None:
        self.governor.check()

    def on_chat_model_start(self, serialized, messages, **kwargs):
        self._guard()

    def on_llm_start(self, serialized, prompts, **kwargs):
        self._guard()

    def on_llm_end(self, response, **kwargs):
        in_tok, out_tok, reported = _usage_of(response)
        price = self.governor.prices.get(self.model)
        if reported is not None:
            cost, source = reported, "reported"
        elif price is not None:
            cost, source = in_tok * price[0] + out_tok * price[1], "estimated"
        else:
            cost, source = 0.0, "unpriced"
            logger.warning("%s: the provider reported no cost and no price is set; this call is not in the cap", self.model)
        self.governor.ingest_external(self.model, cost, in_tok, out_tok, source)


def _usage_of(response) -> tuple:
    """``(input tokens, output tokens, provider-reported cost or None)`` of a LangChain ``LLMResult``."""
    usage = dict((getattr(response, "llm_output", None) or {}).get("token_usage") or {})
    message = None
    try:
        message = response.generations[0][0].message
    except (AttributeError, IndexError, TypeError):
        pass
    if message is not None:
        usage = {**dict((getattr(message, "response_metadata", None) or {}).get("token_usage") or {}), **usage}
        meta = getattr(message, "usage_metadata", None) or {}
    else:
        meta = {}
    in_tok = int(meta.get("input_tokens") or usage.get("prompt_tokens") or 0)
    out_tok = int(meta.get("output_tokens") or usage.get("completion_tokens") or 0)
    cost = usage.get("cost")
    try:
        cost = None if cost is None else float(cost)
    except (TypeError, ValueError):
        cost = None
    return in_tok, out_tok, cost


# ── price lookup (start-up only; the engine itself stays offline) ─────────────────────────────────────────────────
def _fetch_list(provider: str) -> dict:
    """``{model id: (in, out) USD per million tokens}`` from the provider's model list; {} when unavailable."""
    if provider in _lists:
        return _lists[provider]
    import requests
    urls = {"deepinfra": "https://api.deepinfra.com/v1/openai/models", "openrouter": "https://openrouter.ai/api/v1/models",
            "together": "https://api.together.ai/v1/models"}      # together: price format NOT verified (key needed)
    out: dict = {}
    if provider in urls:
        headers = {}
        if provider == "together" and os.environ.get("TOGETHER_API_KEY"):
            headers["Authorization"] = "Bearer " + os.environ["TOGETHER_API_KEY"]
        try:
            r = requests.get(urls[provider], headers=headers, timeout=LOOKUP_TIMEOUT)
            if r.status_code == 200:
                data = r.json()
                items = data.get("data", []) if isinstance(data, dict) else data
                for item in items if isinstance(items, list) else []:
                    price = _price_of(provider, item)
                    if price and item.get("id"):
                        out[str(item["id"])] = price
        except Exception as e:                          # a failed lookup only means "no price found"
            logger.info("price lookup for %s failed: %s", provider, type(e).__name__)
    _lists[provider] = out
    return out


def _price_of(provider: str, item: dict) -> Optional[tuple]:
    try:
        if provider == "deepinfra":
            p = (item.get("metadata") or {}).get("pricing") or {}
            return float(p["input_tokens"]), float(p["output_tokens"])
        if provider == "openrouter":
            p = item.get("pricing") or {}
            return float(p["prompt"]) * 1e6, float(p["completion"]) * 1e6
        if provider == "together":
            p = item.get("pricing") or {}
            return float(p["input"]), float(p["output"])
    except (KeyError, TypeError, ValueError):
        return None
    return None


def lookup_price(model_name: str) -> Optional[tuple]:
    head, _, rest = str(model_name).partition(":")
    return _fetch_list(head.lower()).get(rest.strip())


def hosted_models(config) -> list:
    """Every third-party model named in the config's chains, in order, without duplicates."""
    names = (list(config.supervisor_model_fallback_chain) + list(config.draft_report_model_fallback_chain)
             + list(config.subagent_model_fallback_chain))
    for chain in (config.subagent_model_chain_by_agent or {}).values():
        names += list(chain)
    return list(dict.fromkeys(norm_key(n) for n in names if is_hosted_name(n)))


def resolve_prices(config, lookup=lookup_price) -> tuple:
    """``(prices per million tokens, problems)`` for the config's third-party models: the owner's ``--price`` values
    first, then the provider's public list. A model with no price and no provider-reported cost is a problem."""
    prices = {norm_key(k): tuple(v) for k, v in (config.hosted_prices_per_mtok or {}).items()}
    problems = []
    for key in hosted_models(config):
        if key in prices:
            continue
        found = lookup(key)
        if found:
            prices[key] = tuple(found)
        elif not key.startswith("openrouter:"):          # OpenRouter reports each call's cost itself
            problems.append(f"No price for {key}: pass --price '{key}=<input USD per million tokens>/<output USD per million "
                            f"tokens>' (see the provider's pricing page).")
    return prices, problems
