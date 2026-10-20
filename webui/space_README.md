---
title: Deep Dog 2
sdk: docker
app_port: 7860
pinned: false
---

# Deep Dog 2

A web front end for the Deep Dog 2 research engine. Ask a question, choose the
models for each role, pick the agents and search engine, set a time and
iteration budget, and read a cited Markdown report.

Provider keys are typed into the page and used for that run only. Nothing is
stored on the server, and the Space keeps no files between restarts.

Operator settings (Space variables and secrets):

| Name | Kind | Effect |
|---|---|---|
| `ACCESS_PASSWORD` | secret | Adds a shared-password sign-in. Leave unset on a private Space. |
| `UI_MAX_MINUTES` | variable | Upper limit for the research time slider. Default 30. |
| `UI_MAX_CONCURRENT_RUNS` | variable | Research runs allowed at once. Default 2. |

Do not add provider keys such as `DEEPSEEK_API_KEY` as Space secrets unless you
want every visitor to spend them: the engine falls back to the server's
environment for any key a visitor leaves blank.

Engine: [beneadie/deep_dog_2](https://github.com/beneadie/deep_dog_2), MIT
licensed, itself built on ThinkDepth Deep Research.
