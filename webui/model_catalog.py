"""Live model lists for the hosted providers (DeepInfra, Together, Fireworks).

Given the API key typed on the page, fetch the provider's model list on the
server and return engine-ready names (``deepinfra:<id>``, ``together:<id>``,
``fireworks:<id>``). When no key is typed or the fetch fails, a small curated
fallback list is used instead.

Safety rules kept here:
* The key is only ever sent, in an ``Authorization`` header, to the fixed base
  URL for its provider. Redirects are not followed, so it cannot be forwarded.
* The key is never logged and never appears in a returned value. Every message
  shown to a user is a fixed string; exception text is never used.
* Nothing is written to disk. The in-memory cache is keyed by a SHA-256 of the
  key (never the key itself) and entries expire after ``CACHE_TTL_SECONDS`` (failures after
  ``FAILURE_TTL_SECONDS``, so a bad key or a down provider is not re-asked on every click).

UNVERIFIED payload shapes: the parser below was written without a live call to
any of the three providers. OpenAI-style ``{"data": [{"id": ...}]}`` is expected
from all three, a bare JSON list is also accepted (Together has been reported to
return one), and the ``type`` / ``kind`` / ``model_type`` field used to drop
non-chat models is a guess at the field names. If a provider's real payload
differs, the result is an empty or partial list, and the page falls back to the
curated list; nothing breaks.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Iterable, Optional

import httpx

log = logging.getLogger(__name__)

# Fixed base URL per provider. Tests patch this mapping; nothing reads it from
# the environment, so a visitor cannot redirect the server's requests.
BASE_URLS: dict[str, str] = {
    "deepinfra": "https://api.deepinfra.com/v1/openai",
    "together": "https://api.together.ai/v1",
    "fireworks": "https://api.fireworks.ai/inference/v1",
}

# Env-var name of each provider's key (matches deep_research/config.py).
KEY_NAMES: dict[str, str] = {
    "deepinfra": "DEEPINFRA_API_KEY",
    "together": "TOGETHER_API_KEY",
    "fireworks": "FIREWORKS_API_KEY",
}

LABELS: dict[str, str] = {
    "deepinfra": "DeepInfra",
    "together": "Together",
    "fireworks": "Fireworks",
}

PROVIDERS: tuple[str, ...] = tuple(BASE_URLS)

# Only ids that appear in .env.example / the engine docs.
FALLBACK_MODELS: dict[str, list[str]] = {
    "deepinfra": ["deepinfra:deepseek-ai/DeepSeek-V3"],
    "together": ["together:openai/gpt-oss-120b"],
    "fireworks": ["fireworks:accounts/fireworks/models/llama-v3p3-70b-instruct"],
}

TIMEOUT_SECONDS = 8.0          # whole request budget per provider (<= 10 s)
MAX_RESPONSE_BYTES = 5_000_000  # larger bodies are refused unread
MAX_MODEL_ID_LENGTH = 200
MAX_MODELS_PER_PROVIDER = 5000
CACHE_TTL_SECONDS = 300.0       # in-memory only; must stay <= 600
FAILURE_TTL_SECONDS = 30.0      # a failed fetch is remembered briefly (no retry storms)
_CACHE_MAX_ENTRIES = 64

# A model is dropped only when a type-like field clearly says it is not a chat
# model. Any other value, or no such field, keeps the model (no guessing).
_TYPE_FIELDS = ("type", "kind", "model_type")
_NON_CHAT_TYPES = frozenset({
    "embedding", "embeddings", "text-embedding", "text_embedding",
    "image", "images", "image-generation", "image_generation", "text-to-image",
    "rerank", "reranker", "moderation",
    "audio", "speech", "tts", "text-to-speech", "transcribe", "transcription",
    "automatic-speech-recognition", "video", "text-to-video",
})


@dataclass(frozen=True)
class CatalogResult:
    """Outcome for one provider. ``models`` are engine-ready names."""

    provider: str
    models: tuple[str, ...]
    live: bool
    message: str  # short plain text, never contains the key

    @property
    def label(self) -> str:
        return LABELS.get(self.provider, self.provider)


# ── Parsing ───────────────────────────────────────────────────────────────

def _items_of(payload: Any) -> list:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for name in ("data", "models", "result", "results"):
            value = payload.get(name)
            if isinstance(value, list):
                return value
    return []


def _is_clearly_not_chat(item: dict) -> bool:
    for field_name in _TYPE_FIELDS:
        value = item.get(field_name)
        if isinstance(value, str) and value.strip().lower() in _NON_CHAT_TYPES:
            return True
    return False


def _valid_id(value: Any) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= MAX_MODEL_ID_LENGTH
            and value == value.strip() and value.isprintable() and " " not in value)


def parse_model_ids(provider: str, payload: Any) -> list[str]:
    """Turn a decoded list-endpoint payload into sorted, de-duplicated engine names."""
    prefix = provider + ":"
    found: set[str] = set()
    for item in _items_of(payload)[:MAX_MODELS_PER_PROVIDER * 2]:
        if isinstance(item, str):
            raw = item
        elif isinstance(item, dict):
            if _is_clearly_not_chat(item):
                continue
            raw = item.get("id")
            if not _valid_id(raw):
                raw = item.get("name")
        else:
            continue
        if not _valid_id(raw):
            continue
        found.add(raw if raw.startswith(prefix) else prefix + raw)
    return sorted(found)[:MAX_MODELS_PER_PROVIDER]


# ── Fetching ──────────────────────────────────────────────────────────────

class _Failure(Exception):
    """Carries a fixed, key-free message."""


def _read_capped(response: httpx.Response) -> bytes:
    declared = response.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES:
        raise _Failure("response too large")
    body = bytearray()
    for chunk in response.iter_bytes():
        body.extend(chunk)
        if len(body) > MAX_RESPONSE_BYTES:
            raise _Failure("response too large")
    return bytes(body)


def _fetch_live(provider: str, key: str, transport: Optional[httpx.BaseTransport]) -> list[str]:
    url = BASE_URLS[provider].rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    try:
        with httpx.Client(timeout=httpx.Timeout(TIMEOUT_SECONDS), follow_redirects=False,
                          transport=transport) as client:
            with client.stream("GET", url, headers=headers) as response:
                if response.status_code in (401, 403):
                    raise _Failure("key rejected")
                if response.status_code != 200:
                    raise _Failure(f"provider answered HTTP {response.status_code}")
                body = _read_capped(response)
    except _Failure:
        raise
    except httpx.TimeoutException:
        raise _Failure("timed out") from None
    except httpx.HTTPError:
        raise _Failure("could not connect") from None
    except Exception:  # never let an unexpected error text (which could echo a URL) out
        raise _Failure("request failed") from None
    try:
        payload = json.loads(body)
    except ValueError:
        raise _Failure("unexpected response format") from None
    models = parse_model_ids(provider, payload)
    if not models:
        raise _Failure("no models found in the response")
    return models


# ── Cache (in memory, keyed by SHA-256 of the key, short-lived) ───────────

_cache_lock = threading.Lock()
_CACHE: dict[tuple[str, str], tuple[float, CatalogResult]] = {}


def _now() -> float:
    return time.monotonic()


def _cache_key(provider: str, key: str) -> tuple[str, str]:
    return provider, hashlib.sha256(key.encode("utf-8")).hexdigest()


def _cache_get(provider: str, key: str) -> Optional[CatalogResult]:
    with _cache_lock:
        hit = _CACHE.get(_cache_key(provider, key))
        if hit and hit[0] > _now():
            return hit[1]
        return None


def _cache_put(provider: str, key: str, result: CatalogResult,
               ttl: float = CACHE_TTL_SECONDS) -> None:
    with _cache_lock:
        now = _now()
        for stale in [k for k, (exp, _) in _CACHE.items() if exp <= now]:
            del _CACHE[stale]
        while len(_CACHE) >= _CACHE_MAX_ENTRIES:
            del _CACHE[min(_CACHE, key=lambda k: _CACHE[k][0])]
        _CACHE[_cache_key(provider, key)] = (now + min(ttl, 600.0), result)


def clear_cache() -> None:
    with _cache_lock:
        _CACHE.clear()


# ── Public API ────────────────────────────────────────────────────────────

def fallback_result(provider: str, reason: str) -> CatalogResult:
    return CatalogResult(provider, tuple(FALLBACK_MODELS.get(provider, ())), False, reason)


def fetch_models(provider: str, key: Optional[str], *,
                 transport: Optional[httpx.BaseTransport] = None) -> CatalogResult:
    """Model list for one provider; never raises and never contains ``key``."""
    if provider not in BASE_URLS:
        return fallback_result(provider, "unknown provider")
    key = (key or "").strip()
    if not key:
        return fallback_result(provider, "no key")
    cached = _cache_get(provider, key)
    if cached is not None:
        return cached
    try:
        models = _fetch_live(provider, key, transport)
    except _Failure as failure:
        log.info("model list for %s unavailable: %s", provider, failure)
        result = fallback_result(provider, str(failure))
        _cache_put(provider, key, result, FAILURE_TTL_SECONDS)
        return result
    result = CatalogResult(provider, tuple(models), True, f"{len(models)} models")
    _cache_put(provider, key, result)
    return result


def fetch_all(keys: dict[str, Optional[str]], *,
              transport: Optional[httpx.BaseTransport] = None) -> dict[str, CatalogResult]:
    """Fetch every provider in parallel. ``keys`` maps provider -> typed key."""
    providers = list(PROVIDERS)
    with ThreadPoolExecutor(max_workers=len(providers)) as pool:
        futures = {p: pool.submit(fetch_models, p, keys.get(p), transport=transport)
                   for p in providers}
        return {p: f.result() for p, f in futures.items()}


def fallback_results() -> dict[str, CatalogResult]:
    return {p: fallback_result(p, "no key") for p in PROVIDERS}


def merged_choices(base: Iterable[str], results: dict[str, CatalogResult]) -> list[str]:
    """Existing choices plus each provider's live (or fallback) models, de-duplicated."""
    out = list(dict.fromkeys(base))
    seen = set(out)
    for provider in PROVIDERS:
        result = results.get(provider) or fallback_result(provider, "no key")
        for name in result.models:
            if name not in seen:
                seen.add(name)
                out.append(name)
    return out


def status_line(results: dict[str, CatalogResult]) -> str:
    """One short line: live list vs fallback per provider. Contains no key."""
    parts = []
    for provider in PROVIDERS:
        result = results.get(provider) or fallback_result(provider, "no key")
        if result.live:
            parts.append(f"{result.label}: live list ({len(result.models)} models)")
        else:
            parts.append(f"{result.label}: fallback list ({result.message})")
    return " · ".join(parts)
