# Secrets in a Claude Code cloud environment

Checked 2026-11-01 against code.claude.com/docs/en/cloud-environments ("Add network secrets") and tested live in
this project's cloud environment the same day. Updated 2026-11-10: Exa and Tavily were tested in a session where
their secrets were attached, and the engine (Deep Dog 2) got an option, `EXA_AUTH_PROXY=1`, so it works with them
(sections 1c, 1d, 3 and 6). Covers Gemini and OpenRouter (this project), LangSmith, Exa and Tavily.

**Verified 2026-11-01:** the agent proxy **replaces** a header the client already sends (`x-goog-api-key` for Gemini,
`Authorization` for OpenRouter), so a placeholder key in an environment variable is safe. A placeholder in a URL
query (`?key=...`) is **not** replaced and fails (HTTP 400): send keys in headers.

**Verified 2026-11-10 (Exa, Tavily):** the same holds. Through the proxy, `Authorization: Bearer <dummy value>` and a
request with no key header both succeed; a key sent in a header the proxy does not own (Exa's `x-api-key`) passes
through unchanged and is rejected (401). **The client must also go through the proxy**: a client that ignores
`HTTPS_PROXY` sends its placeholder straight to the provider and fails (section 6).

## Which mechanism

| Mechanism | Who can read the value | Use it for |
|---|---|---|
| **Network secret** | Nobody in the session. Anthropic's agent proxy adds it to matching requests after they leave the VM | API keys: Gemini, OpenRouter, LangSmith, Exa, Tavily |
| Environment variable | Anyone using the environment, and every command in the session | Non-secret settings (project names, flags such as `EXA_AUTH_PROXY=1`), and **placeholders** for SDKs that refuse to start without a key |
| Setup script | Runs before the session; network secrets are **not** attached to its requests | Installing tools |
| `.env` in the repo | Anyone with the repo if it's committed | Local machines only, git-ignored. Never commit it |

A network secret is never attached to: GitHub (the GitHub proxy handles it), `api.anthropic.com`, the public package
registries (pypi.org, npm, crates, Go), setup-script requests, or Claude Code's own telemetry export.

## 1. Gemini (Nano Banana 2.1, Omni Flash)

Open the environment (session title bar › cloud environment menu › **Edit**) › **Network secrets** › **Add secret**:

| Field | Value |
|---|---|
| Add to | The environment this project's sessions use (yours shows **Environment: Default**) |
| Name | `Gemini API` (a label only) |
| Secret type | **Bearer** (keep the default; it means "a key sent in a request header") |
| Allowed websites | `generativelanguage.googleapis.com` |
| Path prefixes | Leave empty. The project calls `/v1beta/models/...` and `/v1beta/interactions` (Omni Flash), and file uploads use `/upload/v1beta/...` |
| Custom headers › Header name | `x-goog-api-key` (header names aren't case-sensitive; `X-goog-api-key` also works) |
| Custom headers › Prefix | **Empty.** Google takes the bare key, not `Bearer <key>` |
| Custom headers › Value | Your new (rotated) key |

Select **Add**. You can't view the value again; to change it, delete the secret and add it again.

Then add one **environment variable** (Edit › Environment variables) so the SDK starts:

```text
GEMINI_API_KEY=set-by-network-secret
```

The google-genai SDK refuses to build a client without a key, so it needs a placeholder; the proxy replaces it with
the real key (verified). Image models need billing on the key's Google Cloud project: the free tier answered
429 RESOURCE_EXHAUSTED for Nano Banana 2.1 on 2026-11-01.

## 1b. OpenRouter (optional: `--provider openrouter` for images and video)

| Field | Value |
|---|---|
| Name | `OpenRouter` |
| Secret type | **Bearer** |
| Allowed websites | `openrouter.ai` |
| Path prefixes | `/api/v1/` (optional; every call the project makes is under it) |
| Header name / Prefix / Value | `Authorization` / `Bearer` / your OpenRouter key (`sk-or-...`) |

Environment variable placeholder: `OPENROUTER_API_KEY=set-by-network-secret`. The project then runs with
`--provider openrouter` (images: google/gemini-nano-banana-2.1; video: google/veo-3.1-lite). Planning and judging
still use the Gemini secret. Verified 2026-11-01: `/api/v1/key` answered 200 with no header and with the placeholder.

## 1c. Exa (web search; `EXA_API_KEY`)

**Status 2026-11-10:** verified live, in a session where the Exa secret was attached. Through the proxy, `POST /search`
answered 200 with no key header and with `Authorization: Bearer <placeholder>`, and 401 with the placeholder in
`x-api-key`. The Deep Dog 2 engine returned real results (see "The engine" below).

| Field | Value |
|---|---|
| Name | `Exa` |
| Secret type | **Bearer** |
| Allowed websites | `api.exa.ai` |
| Path prefixes | Leave empty (the API uses `/search`, `/contents`, `/websets/v0/...` and more) |
| Header name / Prefix / Value | `Authorization` / **Bearer** / "$EXA_API_KEY" |

Equivalent alternative: `Authorization` / `Bearer` / your key. Exa accepts both. Use the header your client sends, so
the proxy replaces it instead of adding a second one (rule 4 in section 5). **Do not use `x-api-key`** for the
secret's header: the proxy would add it next to the `Authorization` header the engine sends.

Environment variables (both are plain variables, not secrets):

```text
EXA_AUTH_PROXY=1
EXA_API_KEY=set-by-network-secret
```

`EXA_API_KEY` is optional with `EXA_AUTH_PROXY=1` (the engine works with or without the placeholder); set it if
another SDK in the session refuses to start without it. Keys are created at dashboard.exa.ai/api-keys.

**The engine (Deep Dog 2).** Without `EXA_AUTH_PROXY=1` the engine refuses to enable Exa when no key is set, and its
HTTP client (aiohttp) ignores `HTTPS_PROXY`, so even with a placeholder the request goes straight to Exa and fails.
With `EXA_AUTH_PROXY=1` the engine sends Exa requests through `HTTPS_PROXY` and lets the proxy supply the key. Details:

- Use it for command-line runs (`python run_research.py ... --search exa`). The web UI never honours it: a visitor's
  run uses only the keys that visitor typed, so a credential added by the operator's network can't be spent by a visitor.
- A real `EXA_API_KEY` is still sent as `Authorization: Bearer <key>`; the switch only adds the proxy routing.
- It does nothing until the code that reads it is on the branch your sessions start from. Commit and push first, then
  set the variable. Setting it earlier only matters for sessions that already have the code.
- Verified: a one-query call through the engine's own Exa function returned results with no key set, and with the
  placeholder above set. A full run (`live4b`, cap $3) made 30 searches with 0 errors.

Verified 2026-11-10, direct (no proxy) and through the proxy:

| Call | Result |
|---|---|
| `POST /search`, no key, **direct to Exa** (no proxy) | **402**, an x402 "payment required" JSON body (`X402_PAYMENT_REQUIRED`, about US$0.007 quoted per search); the payment header can exceed 8 KB, which aiohttp reports as "400 Got more than 8190 bytes". Not a billing problem: it means no key arrived |
| `GET /websets/v0/websets`, no key | 401 "Authentication required" |
| Placeholder in `x-api-key` (direct, or through the proxy) | 401 `INVALID_API_KEY`. The proxy does not own this header |
| `POST /search`, **through the proxy**, no key header | 200 |
| `POST /search`, through the proxy, `Authorization: Bearer <placeholder>` | 200 (the proxy replaced it) |

## 1d. Tavily (web search; `TAVILY_API_KEY`)

**Status 2026-11-10:** verified live, in a session where the Tavily secret was attached. `POST /search` answered 200
with no key header, with the SDK's keyless header and with `Authorization: Bearer <placeholder>`.

| Field | Value |
|---|---|
| Name | `Tavily` |
| Secret type | **Bearer** |
| Allowed websites | `api.tavily.com` |
| Path prefixes | Leave empty |
| Header name / Prefix / Value | `Authorization` / `Bearer` / "$TAVILY_API_KEY" (`tvly-...`) |

Environment variable placeholder: `TAVILY_API_KEY=set-by-network-secret`.

**The engine.** No engine option is needed, but the placeholder variable is:
- The Tavily SDK (`tavily-python`, built on `requests`) honours `HTTPS_PROXY` and the session's CA settings, so there
  is no routing problem like Exa's.
- The SDK also has a keyless mode, so a call with no key returns results. **Unverified: whether such a call used your
  injected key or Tavily's free keyless tier.** Check the credit count on the Tavily dashboard after a run.
- The engine only offers the Tavily tool to a run when a `TAVILY_API_KEY` exists, so with no variable `--search tavily`
  runs with no search tool. With the placeholder set the tool is offered, and the engine's own Tavily function
  returned a result (verified).

Verified 2026-11-10:

| Call | Result |
|---|---|
| `GET /usage`, no key, no secret attached | 401 "Unauthorized: missing API key." |
| Same, placeholder as `Authorization: Bearer ...`, no secret attached | 401 "Unauthorized: missing or invalid API key." (the wording changed, so Tavily reads that header) |
| Same, placeholder in the query (`?api_key=...`) | 401 "missing API key": Tavily ignores a key in the URL, so send the header |
| `POST /search`, secret attached, no key / keyless header / Bearer placeholder | 200 / 200 / 200 |

Tavily's docs describe only Bearer authentication and `tvly-` keys. They don't say whether `GET /usage` ("Get API key
and account usage details") uses credits; it is not a search call, but check the dashboard after the first run.

## 2. LangSmith (tracing and gateway plugins)

| Field | Value |
|---|---|
| Name | `LangSmith` |
| Secret type | **Bearer** |
| Allowed websites | `api.smith.langchain.com` (EU: `eu.api.smith.langchain.com`) |
| Header name / Prefix / Value | `x-api-key` / **empty** / your key (current keys start with `lsv2_`) |

Environment variables. The tracing plugin reads the `CC_LANGSMITH_*` names; its bundled SDK reads `LANGSMITH_*`.
Set one block, not both:

```text
TRACE_TO_LANGSMITH=true
CC_LANGSMITH_PROJECT=media-agent
CC_LANGSMITH_API_KEY=set-by-network-secret
CC_LANGSMITH_DEBUG=false
NODE_USE_ENV_PROXY=1
```

- `CC_LANGSMITH_API_KEY` is a placeholder, like the Gemini one. The old guide's `X-Api-Key` value worked the same way
  but read like a header name; any non-secret string does.
- `NODE_USE_ENV_PROXY=1` makes Node's built-in `fetch` use the session's `HTTPS_PROXY`, which is the agent proxy that
  attaches secrets. Without it, Node hooks bypass the proxy and get no key.
- Turn `CC_LANGSMITH_DEBUG` on only while debugging; debug logs are verbose.
- Plugin install (unchanged): add the marketplace `https://github.com/jondeux/langsmith-claude-code-plugins.git`,
  then enable **langsmith-tracing** and **langsmith-gateway**.

## 3. Verify (in a NEW session; secrets and variables load at session start)

Ask Claude to run these. The Gemini and OpenRouter calls read metadata only, so nothing is billed. Exa and Tavily
checks are covered below and do cost a search each (about a cent).

Gemini:

```bash
curl -s -o /dev/null -w "%{http_code}\n" "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1"
```

```bash
curl -s -o /dev/null -w "%{http_code}\n" -H "x-goog-api-key: $GEMINI_API_KEY" "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1"
```

| First | Second | Meaning | Action |
|---|---|---|---|
| 200 | 200 | Secret attached; the proxy replaces the placeholder | Done: the SDK works as is |
| 200 | 400 | Secret attached; the proxy adds a second header, so Google sees the placeholder | Code change: send no key header (ask Claude) |
| 403 | 403 | Secret not attached | Check the host spelling, the environment the session uses, and the **Not sent** note in the list |

The same three readings apply to the other providers, with their own "not attached" status (OpenRouter: expect `200`
twice; Exa and Tavily: 401 twice for the free read calls, or 402 for a keyless Exa `/search`).

OpenRouter (expect `200` twice):

```bash
curl -s -o /dev/null -w "%{http_code}\n" https://openrouter.ai/api/v1/key
```

```bash
curl -s -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $OPENROUTER_API_KEY" https://openrouter.ai/api/v1/key
```

Exa (expect `200` for each; `401` means the secret is not attached):

```bash
curl -s -o /dev/null -w "%{http_code}\n" https://api.exa.ai/monitors
curl -s -o /dev/null -w "%{http_code}\n" https://api.exa.ai/agent/runs
curl -s -o /dev/null -w "%{http_code}\n" -X POST https://api.exa.ai/search -H "Content-Type: application/json" -d '{"query":"test","numResults":1}'
curl -s -o /dev/null -w "%{http_code}\n" -X POST https://api.exa.ai/contents -H "Content-Type: application/json" -d '{"urls":["https://exa.ai"]}'
curl -s -o /dev/null -w "%{http_code}\n" -X POST https://api.exa.ai/answer -H "Content-Type: application/json" -d '{"query":"What is Exa?"}'
```

```bash
curl -s -o /dev/null -w "%{http_code}\n" -X POST -H "Authorization: Bearer $EXA_API_KEY" -H "Content-Type: application/json" -d '{"query":"test","numResults":1}' https://api.exa.ai/search
curl -s -o /dev/null -w "%{http_code}\n" -X POST -H "Authorization: Bearer $EXA_API_KEY" -H "Content-Type: application/json" -d '{"urls":["https://exa.ai"]}' https://api.exa.ai/contents
curl -s -o /dev/null -w "%{http_code}\n" -X POST -H "Authorization: Bearer $EXA_API_KEY" -H "Content-Type: application/json" -d '{"query":"What is Exa?"}' https://api.exa.ai/answer
curl -s -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $EXA_API_KEY" https://api.exa.ai/monitors
curl -s -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $EXA_API_KEY" https://api.exa.ai/agent/runs
```

Verified 2026-11-10: only the `POST /search` form was run (200 with no header, 200 with `Bearer dummy`). The other
Exa endpoints above have not been run in a session with the secret attached. Exa has no documented free "whoami"
call. If your plan answers 403, a 403 still means Exa accepted the key (401 is the rejection).

The engine itself (this is the check that matters for Deep Dog 2; one search):

```bash
EXA_AUTH_PROXY=1 python3 - <<'EOF'
import asyncio
from deep_research.utils import _exa_search_multiple
r = asyncio.run(_exa_search_multiple(["time-restricted eating randomized trial"], max_results=1))[0]["results"]
print("results:", len(r))      # expect 1; 0 plus a warning in the log means the search failed
EOF
```

Tavily (expect `200` for a search; `401` means the secret is not attached):

```bash
curl -s -o /dev/null -w "%{http_code}\n" https://api.tavily.com/usage
```

```bash
curl -s -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $TAVILY_API_KEY" https://api.tavily.com/usage
```

```bash
curl -s -o /dev/null -w "%{http_code}\n" -X POST https://api.tavily.com/search -H "Content-Type: application/json" -d '{"query":"test","max_results":1}'
```

The first two `/usage` calls have not been run with the secret attached; the `POST /search` form was (200).

Result on 2026-11-01: Gemini 200/200, OpenRouter 200/200 (the proxy replaces placeholders).

Result on 2026-11-10 (a session without the secrets, literal placeholder): Exa 401/401 and Tavily 401/401, the
"not attached" signature. Result later the same day (secrets attached): Exa `POST /search` 200 with no header and
200 with `Bearer dummy`, 401 with `x-api-key: dummy`; Tavily `POST /search` 200 / 200 / 200. Replacement is verified
for the `Authorization: Bearer` form on both.

Then: `env | grep -c <first 6 characters of the key>` should print `0`; the key never enters the VM.

## 4. Order of operations

1. Rotate any key that was ever committed or pasted in a chat (done for Gemini on 2026-11-01).
2. Add the network secret(s), then the placeholder variables.
3. For Exa in Deep Dog 2: make sure the commit that adds `EXA_AUTH_PROXY` is on the branch your sessions start from,
   then add `EXA_AUTH_PROXY=1`.
4. Paste the setup script under **Setup script** (it installs tools; it can't use the secrets).
5. Start a new session and run step 3 above.
6. If a key was ever committed: rotate it first, then purge it from the branch history (a force push; ask first).

Never paste a key into a chat, a commit, a setup script, or an environment variable.

## 5. Rule of thumb: the entry for a key not listed here

Every entry above is the same five fields. Work them out in this order, using the provider's docs and its error
messages.

1. **Allowed websites = the API host.** Take it from the provider's base URL (`api.exa.ai`, `api.tavily.com`). Host
   only: no `https://`, no path. Add regional or upload hosts if the project calls them (LangSmith EU).
2. **Header = where the provider reads the key.** The "Authentication" docs page says, and so does the error body of
   a call with no key (Exa's lists both accepted headers). It has to be a request **header**: a key in the URL query or
   the JSON body is not replaced (Gemini `?key=` fails with 400; Tavily ignores `?api_key=`). If the provider offers no
   header form, a network secret can't carry that key.
3. **Prefix = the word before the key in that header.** `Authorization: Bearer <key>` gives header `Authorization`,
   prefix `Bearer`. A header carrying the bare key (`x-api-key`, `x-goog-api-key`) gives an empty prefix. Secret type
   stays **Bearer** in both cases. Key shapes (`tvly-`, `sk-or-`, `lsv2_`) are for recognizing a key; they go nowhere in
   the config.
4. **Use the header name the client sends.** The proxy replaces a header of the same name. If the SDK sends
   `Authorization` and the secret sets `x-api-key`, the request carries both (the Gemini 200/400 row). Observed for
   Exa: the proxy replaced an `Authorization: Bearer` header carrying a dummy value, and left a key in `x-api-key` untouched.
5. **Placeholder = the SDK's variable, any non-secret string.** The convention is `<PROVIDER>_API_KEY=set-by-network-secret`.
   It exists only because SDKs refuse to build a client without a key.
6. **Path prefixes = empty**, unless you want to restrict. If you set one, it must cover every path the project
   calls, uploads included (Gemini `/upload/v1beta/`).

Probe before you add the secret (free):

1. Pick an auth-gated read call: a list, usage or key-info endpoint. Avoid billed endpoints: a keyless call to one
   can answer something odd (Exa `/search` returns 402, not 401).
2. Call it with no key and note the status and message.
3. Call it again with the placeholder in the header from rule 2. The message should change from "missing" to
   "invalid" (Tavily: "missing API key" became "missing or invalid API key"; Exa: "Authentication required" became
   "Invalid API key"). **Changed** = the provider reads that header. **Unchanged** = wrong header or format; fix it
   before you add the secret. (Observed for Exa and Tavily on 2026-11-10; not tested for the other providers.)
4. After adding the secret and variable, start a new session and run the same two calls: no key and placeholder should
   both return 200. Read the pair with the table in section 3.

Reading statuses: 401 or 403 = key missing or invalid; 402 or 429 = payment or quota (Gemini's free tier gave 429;
Exa's keyless `/search` gives 402 with a payment body); 400 = the request was rejected after auth, or a placeholder in
the wrong place.

Record each new provider in this file with the same shape as 1c: status line with the date and what was actually run,
the five fields, the placeholder variable, and the keyless and placeholder responses. Say "not yet verified" for
anything you did not run in a session where the secret was attached.

## 6. Lessons from the Exa and Tavily setup (2026-11-10)

1. **A secret in the proxy is not enough: the client must use the proxy.** The proxy only touches traffic that reaches
   it. A client that ignores `HTTPS_PROXY` sends its request straight out. aiohttp does this unless the session is
   created with `trust_env=True`; Node's built-in `fetch` needs `NODE_USE_ENV_PROXY=1` (section 2); `requests` and
   `curl` use the proxy by default. A direct, keyless Exa `/search` answers 402 with an oversized payment header.
2. **Test the exact header form and client the project uses.** A `curl` with `x-api-key: dummy` failed with 401, and
   that was wrongly read as "a placeholder key can't work". The engine sends `Authorization: Bearer`, which the proxy
   replaces. A `curl` that succeeds through the proxy proves nothing about a client that bypasses it.
3. **A run can finish with every search failed.** The first Exa run (cost an estimated $2.69) made 80 searches that
   all returned 401, and still ended "completed" with a report written from the model's memory. The engine now has a
   guard: after `search_failure_limit` failed searches in a row (default 6; 0 turns it off) the run stops searching
   and either writes a report from the findings it already has (stop reason `search_unavailable`, resumable with
   `--resume`) or fails with a message that names the backend. It covers Exa and Tavily, not Claude search. Still
   check the log for `Exa search error` and for zero selected sources after the first run on a new backend.
4. **The final-report call has its own budget.** A run with a $3.00 cap ended at an estimated $3.25 because the last
   call, which writes the report, cost $1.33 after 353 sources were saved. The engine now projects that call's cost
   first and trims the source text to fit the room left under `--usd-cap`, or `--report-budget USD` if you set less. If
   even the bare call is too dear it returns the findings digest instead of overspending (stop reason `report_budget`;
   `--resume <id> --report-budget <higher>` finishes it). The projection is an estimate from the model's own measured
   price with a 25 % margin; it is tested on a fake binary, not yet on a live run.
5. **Search credits and run cost are separate.** A run's cost figure is the list-price estimate of the model calls
   (the `claude` binary's figure, not a bill). Exa and Tavily bill on their own dashboards.
6. **Tavily needs the placeholder variable, Exa needs the switch.** Tavily: `TAVILY_API_KEY=set-by-network-secret`.
   Exa: `EXA_AUTH_PROXY=1`. Neither variable reaches the web UI (it never reads the process environment).
