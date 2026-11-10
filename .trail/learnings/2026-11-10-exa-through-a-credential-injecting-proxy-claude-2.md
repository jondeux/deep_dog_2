---
title: "Exa through a credential-injecting proxy: the engine's HTTP client must use HTTPS_PROXY, and a key header is optional"
author: Claude
date: 2026-11-10
branch: deep-dog-2-REA-claude-p
cost: { minutes: 2, tokens: 23469 }
id: l-93de74bd
scope: repo
status: active
replaces: l-22c1872e
src: agent
touches:
  - tests/fake_claude.py
  - deep_research/secrets.py
  - deep_research/utils.py
  - validation/p5/RESULTS.md
---

## Decided
In a cloud session the egress proxy adds the Exa key itself. Two things must be true for the engine: its HTTP client has to go through HTTPS_PROXY (aiohttp does not unless trust_env=True), and the request must not rely on the engine's own key. The engine has an opt-in, EXA_AUTH_PROXY=1: no Authorization header, trust_env on. A real EXA_API_KEY still wins. The web UI (env_fallback=False) never honours it.

## Tried and ruled out
- EXA_API_KEY=dummy with no other change (live4, $2.69): aiohttp bypassed the proxy, so all 80 Exa calls went direct and returned 401, and the run still ended "completed" with its report written from no retrieved sources.
- My first diagnosis, "the proxy does not overwrite a dummy key", was WRONG. I had tested only an x-api-key header. Through the proxy, Authorization: Bearer <anything> and no header both give 200; only x-api-key passes through unchanged. So a placeholder key plus trust_env=True would also have worked.
- Sending no header without trust_env: direct keyless request, Exa answers with an x402 payment-required header over aiohttp's 8,190-byte limit ("400 Got more than 8190 bytes").

## Watch out
- A run can finish "completed" with every search failed: check the log for "Exa search error" and for 0 selected sources before trusting a report.
- Test the exact header form and HTTP client the engine uses. A curl with a different header, or through the proxy while the engine goes direct, proves nothing.
- Tavily is different: its SDK uses requests (honours HTTPS_PROXY) and has a keyless mode, but the engine only offers the Tavily tool when a TAVILY_API_KEY exists, so a placeholder key is enough.
- tests/fake_claude.py had an unlocked call counter; concurrent calls lost counts and made a page-reader test fail only under CPU load. It now takes a file lock.

In the code: deep_research/secrets.py exa_proxy_auth; deep_research/utils.py _exa_search_multiple (headers, trust_env); integration.py validate_credentials; agents/base.py _search_tools_for; tests/test_exa_proxy_auth.py; validation/p5/RESULTS.md.
