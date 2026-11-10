---
title: Exa through a credential-injecting proxy needs no key header and a client that honours HTTPS_PROXY
author: Claude
date: 2026-11-10
branch: deep-dog-2-REA-claude-p
cost: { minutes: 58, tokens: 161368 }
id: l-22c1872e
scope: repo
status: active
src: agent
touches:
  - tests/fake_claude.py
  - tests/test_exa_proxy_auth.py
  - deep_research/secrets.py
  - deep_research/utils.py
  - deep_research/integration.py
  - deep_research/agents/base.py
  - validation/p5/RESULTS.md
  - ".ai-context/handoff.md"
  - claude/memory/deep-research-memory.md
---

## Decided
In a cloud session the egress proxy adds the Exa key itself, but only to requests that carry no key header, and only for clients that go through HTTPS_PROXY. The engine gets an opt-in, EXA_AUTH_PROXY=1, that sends Exa requests with no Authorization header and turns on trust_env for the HTTP session. It is read from the process environment only, so the web UI (env_fallback=False) can never use it.

## Tried and ruled out
- EXA_API_KEY=dummy so the engine would enable Exa and the proxy would swap in the real key. The proxy does not overwrite a key header that is present (curl with x-api-key: dummy through the proxy gave 401). A full live run (live4, $2.69) made 80 Exa calls that all returned 401 and wrote its report without any retrieved sources.
- Sending no header alone. The engine's aiohttp session ignores HTTPS_PROXY unless trust_env=True, so the request went straight to Exa with no key. Exa answered with an x402 payment-required response whose header exceeds aiohttp's 8,190-byte limit, which shows up as a confusing "400 Got more than 8190 bytes when reading".

## Watch out
- A run can finish "completed" with every search failed: check the log for "Exa search error" and for 0 selected sources before trusting a run or its report.
- curl through the proxy succeeding does not prove the engine's HTTP client does; test the engine's own function with one query.
- tests/fake_claude.py had an unlocked call counter; concurrent calls lost counts and made test_zero_means_every_read_goes_through_the_reader fail only under CPU load. It now takes a file lock.

In the code: deep_research/secrets.py exa_proxy_auth; deep_research/utils.py _exa_search_multiple (headers, trust_env); deep_research/integration.py validate_credentials; deep_research/agents/base.py _search_tools_for; tests/test_exa_proxy_auth.py; validation/p5/RESULTS.md (live3, live4).
