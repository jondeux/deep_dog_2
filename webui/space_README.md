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

Provider and search keys set as Space secrets (`DEEPSEEK_API_KEY`, `EXA_API_KEY`,
`TAVILY_API_KEY` and so on) are never used by the page: a web run uses only the
keys the visitor types, and a key left blank counts as missing even if the server
has one. Visitors cannot spend your keys. There is no need to add provider keys
to the Space.

The page remembers non-secret settings (question, models, budget, search and
tracing choices) and the last finished report in the visitor's own browser. Key
boxes, "Other keys" and the LangSmith key are never remembered.

Do not set any `LANGSMITH_*` or `LANGCHAIN_*` variable on the Space, as a secret or
a variable. That includes `LANGSMITH_API_KEY`, `LANGSMITH_ENDPOINT`, `LANGSMITH_TRACING`,
`LANGSMITH_WORKSPACE_ID` and `LANGSMITH_RUNS_ENDPOINTS`. The page ignores the key and
endpoint, and a visitor must type their own LangSmith key to trace a run, but the tracing
library reads the other settings itself: they can route visitors' runs to your account
or make tracing fail with a warning. They are only meant for the command-line tool.

Engine: [beneadie/deep_dog_2](https://github.com/beneadie/deep_dog_2), MIT
licensed, itself built on ThinkDepth Deep Research.
