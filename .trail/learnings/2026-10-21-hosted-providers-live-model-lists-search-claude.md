---
title: "Hosted providers, live model lists, search toggles, LangSmith tracing"
author: Claude
date: 2026-10-21
branch: main
cost: { minutes: 1, tokens: 1485 }
touches:
  - deep_research/config.py
  - deep_research/langsmith_tracing.py
  - deep_research/models.py
  - webui/app.py
  - webui/model_catalog.py
---

## Decided
- Hosted providers are selected only by an explicit prefix (deepinfra:, together:, fireworks:) checked before all substring routing and before the OpenRouter switch; everything after the first colon is sent verbatim. One ordered _PROVIDERS table feeds get_model and model_required_keys.
- LangSmith tracing is opt-in per run (RunConfig.langsmith_tracing). In web-UI mode the key and endpoint come only from typed credentials (Credentials.langsmith_from_env=False) and the endpoint only from a fixed US/EU map, never free text (SSRF).
- Failure text is redacted before it reaches the page: longest secret first, redact BEFORE slicing.
- The Other keys box is an allow-list with 4096 char / 20 entry caps; LANGSMITH_* names are dropped.
## Tried and ruled out
- A free-text LangSmith endpoint: would let a visitor aim the server at internal hosts.
- Slicing text before redacting it: a key straddling the cut leaves a readable prefix.
- In-place source mutation by one agent while another runs pytest: causes transient failures; use in-memory mutants.
## Watch out
- Operator LANGSMITH_TRACING / WORKSPACE_ID / RUNS_ENDPOINTS set on the Space still affect visitors (documented, do not set them).
- The OpenAI and Gemini key boxes are unused by gpt-* / gemini-* (init_chat_model path takes no api_key).
- trail check shows 5 keyword false positives (README.md:483, config.py:777, tests/test_webui.py:28, webui/Dockerfile:4, webui/app.py:78).
- Full suite is about 112 s, almost all waiting: e2e app launches, a 0.7 s poll sleep per run_ui test, one 10 s flush test.
