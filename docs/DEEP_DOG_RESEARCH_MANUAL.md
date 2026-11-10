# Deep Dog Research: user manual

`/deep-dog-Research` turns a question into a cited Markdown report. A supervisor model plans the research, runs search agents in
parallel, and writes the report from what they found. By default every model call and the web search run through the `claude`
CLI on **your own Claude plan** (no keys, personal use only). Any role can instead use a third-party model on your own API key.

Contents: 1 Quick start · 2 Writing the research question · 3 Choosing models and depth · 4 Every switch · 5 Third-party models ·
6 Cost and the guards · 7 Reading the output · 8 Resume · 9 Troubleshooting · 10 FAQ

---

## 1. Quick start

From the repo root, once `claude` is signed in:

```bash
python run_research.py --prompt "<your question>" --preset economy --usd-cap 2 --output-dir outputs
```

1. **Check the setup without spending anything.** Add `--show-config` to any command: it prints the settings the run would use,
   which keys are set (names only), and any problem a real run would refuse on, then exits.
2. **Run it.** A first run of a session needs a spend cap you approve. `--usd-cap` is a list-price cap for the whole run.
3. **Read the result.** The report, a usage file and (if a cap stopped the run) a resume id are in the output folder (section 7).

In a Claude Code session, type `/deep-dog-Research <question>`: it asks you for the setup with a question dialog, shows you
`--show-config` and the cost estimate, and waits for your spend approval before running.

---

## 2. Writing the research question

The supervisor turns your question into a research brief, so the question sets the scope, the sources and the shape of the
report. A vague question gets a broad, generic report; a specific one gets a focused, critical one. The sweet spot is a few
sentences, not a page and not a phrase.

### Ideal format

Put these in one paragraph, or a short list. Leave out what does not apply.

| Part | Say | Example |
|---|---|---|
| **Question** | The one thing you need answered, as a question | "How strong is the evidence that X improves Y?" |
| **Population / subject** | Who or what it covers | "in adults", "for UK retail banks", "for LLM agents" |
| **Time frame** | Which period counts | "since 2020", "the last 24 months" |
| **Must cover** | 2 to 5 sub-questions or angles | "key randomised trials, meta-analyses, how robust each is" |
| **Sources to favour or avoid** | Evidence quality, source types | "peer-reviewed and regulator documents; ignore vendor blogs" |
| **Output shape** | What you will do with it | "an executive summary, then a table of studies, then gaps" |
| **Stance** | Neutral or critical | "be critical about confounding; say where the evidence is weakest" |

### Template

```text
<Question, one sentence>? Focus on <population/subject> and <time frame>.
Cover: <angle 1>; <angle 2>; <angle 3>.
Prefer <source types>; avoid <source types>.
Give me <shape: summary, table, comparison>, and flag <what to be critical about>.
```

### A good question (this one produced a completed, cited report in the live tests)

> How strong is the evidence that time-restricted eating or intermittent fasting improves long-term cardiometabolic health
> (weight, blood pressure, glycaemic control, cardiovascular events) compared with continuous calorie restriction in adults?
> Summarise the key randomised trials and meta-analyses, how robust they are, and where the evidence is weakest.

It has a comparison, a population, named outcomes, what to summarise, and a request for criticism.

### Weak versus strong

| Weak | Why | Stronger |
|---|---|---|
| "Tell me about intermittent fasting" | No question, no scope, no stance | The question above |
| "Is solar good?" | Good for whom, where, by what measure? | "What did utility-scale solar cost per MWh in the US and EU in 2020 to 2025, how did that compare with new gas, and what drove the change? Prefer government and independent analyst data." |
| "Compare AI coding agents" | Unbounded, dates fast | "Compare Claude Code, Codex and Gemini CLI on agentic coding benchmarks published since January 2026: what was measured, how comparable are the setups, and where do vendor claims outrun independent results?" |

### Tips

- **Ask for the weak spots.** "Where is the evidence weakest?" is what turns a summary into an assessment.
- **Name the time frame** whenever the answer can go stale.
- **One topic per run.** Two unrelated questions cost twice and are answered worse; run them separately.
- **Use a file for long prompts:** `--prompt-file question.txt`.
- **Language:** `--language French` writes the report in another language (default English).
- **Do not paste secrets** into a question: it is sent to the models and saved in the report file.

---

## 3. Choosing models and depth

### Presets (Claude plan)

| Preset | Supervisor / sub-agents | Use it for |
|---|---|---|
| `testing-framework` | sonnet-5.5 medium / haiku-5.5 medium | The cheapest of the four: tests, quick looks |
| `economy` (default) | sonnet-5.5 high / haiku-5.5 high | Everyday research |
| `custom` | sonnet-5.5 xhigh / haiku-5.5 xhigh | When depth of reasoning matters more than cost |
| `benchmark` | opus-5.5 medium / sonnet-5.5 high | The strongest and most expensive |

`--list-presets` prints them. Relative cost per token, measured: haiku 1x, sonnet about 6x, opus about 11x. The effort level's own
cost multiplier has not been measured: do not infer cost from the effort level alone.

### Roles

- **Supervisor** plans, delegates and judges. It also writes the final report.
- **Sub-agents** do the searching and reading. They make most of the calls, so a cheaper model here saves the most.
- **Draft** writes the research brief and the first draft. It follows the supervisor unless you set `--draft-model`.

Set a role with `--supervisor-model`, `--draft-model`, `--subagent-model`. A model is a Claude name (`haiku-5.5`, `sonnet-5.5`,
`opus-5.5`, optionally `@low|medium|high|xhigh|max`, or `--<role>-effort`) or a third-party id (section 5). Roles can mix backends.
A role you do not set follows the preset. `--list-models` shows what is available and which keys are set.

Other model switches:
- `--agent-model ResearchReddit=haiku-5.5@low` gives one platform agent its own model (repeatable).
- `--supervisor-fallback sonnet-5.5@low,deepinfra:<id>` adds fallbacks, tried in order if the first model fails. Without it a model
  never changes silently.

### Agents and search

- **Agents:** `--agents ResearchWeb,ResearchReddit,...` (default: `ResearchWeb` only). `--list-agents` shows them all: web, general,
  reddit, substack, pubmed, arxiv, sec. `--general-platforms` limits what the general agent may use.
- **Search:** `--search claude` (default: Claude's own search, no key), `tavily`, `exa`, or `both`. Tavily and Exa need your own
  keys, set outside chat (section 5). With `--search claude`, each agent reads 3 pages through Claude's WebFetch, which returns an
  extract focused on the agent's topic, not the whole page. `--page-reads N` changes the 3 (0 = no limit, slower and deeper) and
  `--no-page-reader` is the thinner, cheaper arm.

### Depth

Depth comes from the effort budget: `--time-max`, `--supervisor-iterations`, `--parallel-research`, `--searches-per-agent`,
`--reads-per-agent`. Defaults: research runs 5 to 15 minutes, at most 30 supervisor turns, 3 sub-agents at once, 3 searches each.
More of any of them means more sources and a higher cost; the cap (section 6) is the real limit.

---

## 4. Every switch

Any engine setting not listed can be set with `--set FIELD=VALUE` (repeatable; numbers, `true`/`false`, `none`, comma lists or JSON;
unknown names are refused with a suggestion), or in a JSON file with `--config run.json`. Order of precedence, weakest first:
`--config`, the named switches below, `--set`. `--show-config` prints the result.

Input and output: `--prompt` / `--prompt-file`, `--output-dir`, `--thread-id`, `--resume ID`, `--memory-dir`.
Information (no run): `--help`, `--list-presets`, `--list-models`, `--list-agents`, `--show-config`.

Models: `--preset`, `--supervisor-model` / `-effort` / `-fallback`, the same for `--draft-` and `--subagent-`, `--agent-model`,
`--price` (section 5).

<!-- switches:begin (generated by deep_research/cli_options.py; do not edit) -->

**Effort budget**

| Switch | Sets | Default | What it does |
|---|---|---|---|
| `--time-min MIN` | `research_time_min_minutes` | 5 | shortest research window, minutes |
| `--time-max MIN` | `research_time_max_minutes` | 15 | longest research window, minutes (research then goes to the report) |
| `--salvage-fraction F` | `findings_salvage_time_fraction` | 0.6 | share of the time limit after which findings are salvaged into a report |
| `--supervisor-iterations N` | `supervisor_max_iterations` | 30 | most supervisor turns |
| `--supervisor-timeout SEC` | `supervisor_timeout_seconds` | 420 | seconds one supervisor turn may take |
| `--parallel-research N` | `supervisor_max_concurrent_research` | 3 | sub-agents the supervisor may run at once |
| `--parallel-discovery N` | `supervisor_max_concurrent_discovery` | 2 | discovery sub-agents at once |
| `--subagent-iterations N` | `subagent_max_iterations` | 5 | most turns per sub-agent |
| `--searches-per-agent N` | `subagent_max_searches` | 3 | most searches per sub-agent |
| `--reads-per-agent N` | `subagent_max_reads` | 10 | most page reads per sub-agent turn |
| `--total-reads N` | `subagent_max_total_reads` | 25 | most page reads per sub-agent in all |
| `--saves-per-agent N` | `subagent_max_saves` | 10 | most sources a sub-agent may save |
| `--subagent-concurrency N` | `subagent_max_concurrency` | 3 | tool calls a sub-agent runs at once |
| `--subagent-timeout SEC` | `subagent_timeout_seconds` | 600 | seconds one sub-agent may take |
| `--llm-timeout SEC` | `llm_timeout` | 180 | seconds one model call may take |
| `--tool-timeout SEC` | `tool_timeout` | 120 | seconds one tool call may take |

**Agents**

| Switch | Sets | Default | What it does |
|---|---|---|---|
| `--agents A,B` | `enabled_agents` | ResearchWeb | agents the supervisor may call, comma separated (see --list-agents) |
| `--general-platforms P,Q` | `general_agent_platforms` |  | platforms ResearchGeneral may use, comma separated (empty = all) |

**Search**

| Switch | Sets | Default | What it does |
|---|---|---|---|
| `--search` | `web_search_engine` | exa | web search backend: claude (Claude's own search, no key), exa, tavily, both |
| `--exa-max-chars N` | `exa_search_max_chars` | 6000 | characters of page text Exa returns per result |
| `--fetch-max-chars N` | `fetch_url_max_chars` | 10000 | characters a page read may return |
| `--search-failure-limit N` | `search_failure_limit` | 6 | stop the run after N failed Exa or Tavily searches in a row (0 = off) |
| `--no-page-reader` | `claude_page_reader` | on | with --search claude, do not read pages through WebFetch: agents get the search summary only (the cheaper, thinner arm) |
| `--page-reads N` | `claude_page_reads_per_agent` | 3 | with --search claude: pages each agent reads through WebFetch (default 3; its other reads stay search summaries; 0 = no limit, the slower, deeper arm) |

**Output**

| Switch | Sets | Default | What it does |
|---|---|---|---|
| `--output-mode` | `output_mode` | file | where outputs go |
| `--log-mode` | `log_mode` | file | where logs go |
| `--language LANG` | `target_language_fallback` | English | language the report is written in |
| `--subtopics` / `--no-subtopics` | `enable_subtopic_generation` | off | generate sub-topics before research |
| `--trace` / `--no-trace` | `enable_research_trace` | off | write the research trace document |
| `--source-log` / `--no-source-log` | `enable_source_log` | on | log every source |
| `--save-subagent-reports` / `--no-save-subagent-reports` | `save_subagent_reports_to_file` | off | save each sub-agent's report to a file |
| `--subagent-output-mode MODE` | `subagent_output_mode` | sources | what sub-agents hand back |
| `--discovery-output-mode MODE` | `discovery_output_mode` | report_inline | what discovery agents hand back |

**Behaviour**

| Switch | Sets | Default | What it does |
|---|---|---|---|
| `--thinking` | `thinking_mode` | auto | model thinking mode |
| `--route-openrouter` / `--no-route-openrouter` | `route_via_openrouter` | off | route supported (non-Claude-plan) models through OpenRouter |

**LangSmith**

| Switch | Sets | Default | What it does |
|---|---|---|---|
| `--langsmith` / `--no-langsmith` | `langsmith_tracing` | off | trace the run in LangSmith (the key comes from LANGSMITH_API_KEY in the environment) |
| `--langsmith-project NAME` | `langsmith_project` |  | LangSmith project name (default deep-dog-2) |

**Cost and plan limits**

| Switch | Sets | Default | What it does |
|---|---|---|---|
| `--usd-cap USD` | `claude_code_usd_cap` | 5.0 | list-price cap in USD for the whole run (default 5.0), Claude-plan and third-party spend together. Research stops when spend plus the projected final report reaches it (--static-reserve: at 75% of it) |
| `--report-budget USD` | `claude_code_report_budget_usd` |  | most the final-report call may cost, in USD (default: whatever is left under --usd-cap). The engine projects the call's cost first: over budget it shortens the per-source text, and if even that is too dear it returns the findings digest, which --resume can finish with a higher budget |
| `--static-reserve` | `claude_code_dynamic_reserve` | on | keep a fixed share of --usd-cap (25%, at least $0.50) for the final report instead of stopping research when spend plus the projected report reaches the cap |
| `--plan-window-max F` | `claude_code_plan_window_max` | 0.8 | never exceed this share of the Claude plan window, 0.1 to 1 (default 0.8). Research stops 0.1 below it, the rest is kept for the final report |

<!-- switches:end -->

---

## 5. Third-party models

Instead of the Claude plan, a role can use a model billed per token on **your own** key:

| Prefix | Example | Key (environment variable) |
|---|---|---|
| `deepinfra:` | `deepinfra:deepseek-ai/DeepSeek-V3` | `DEEPINFRA_API_KEY` |
| `openrouter:` | `openrouter:deepseek/deepseek-chat` | `OPENROUTER_API_KEY` |
| `together:` | `together:openai/gpt-oss-120b` | `TOGETHER_API_KEY` |
| `fireworks:` | `fireworks:accounts/fireworks/models/llama-v3p3-70b-instruct` | `FIREWORKS_API_KEY` |

```bash
python run_research.py --prompt "<question>" --preset economy --usd-cap 2 \
  --subagent-model deepinfra:deepseek-ai/DeepSeek-V3
```

That runs the supervisor and draft writer on the Claude plan and the sub-agents on DeepInfra. For an all-third-party run set all
three roles (`--supervisor-model`, `--draft-model`, `--subagent-model`); a role you leave out follows the preset (the Claude plan).

### Keys

- Put keys in the **environment settings** of your cloud environment (or your shell), never in chat and never on the command line.
  The CLI has no switch that takes a key. `--list-models` shows whether each is set (names only).
- **OpenRouter through the network proxy:** if your environment injects the OpenRouter key at the proxy, the engine still needs the
  variable to exist. Set `OPENROUTER_API_KEY` to a placeholder such as `set-by-network-secret`; the proxy replaces it on the wire.
  (The same is true of Tavily; Exa needs `EXA_AUTH_PROXY=1`. See `docs/SETUP_SECRETS_IN_CC_CLOUD_ENV.md`.)
- The Claude plan never needs a provider key and never receives one.

### What a model costs

The meter counts every third-party call's tokens and adds the cost to the same `--usd-cap` as your Claude-plan calls. A cost is:

1. **reported**: the provider's own figure (OpenRouter returns each call's cost); else
2. **estimated**: tokens times a price per million, from `--price`, else from the provider's public model list (looked up once at
   start-up for DeepInfra and OpenRouter; Together's list needs your key and its price format is unverified, so give Together models a `--price`).

A model with no price from any source is **refused before the run starts** (nothing is guessed). Give it one:

```bash
--price 'fireworks:accounts/fireworks/models/llama-v3p3-70b-instruct=0.9/0.9'     # USD per million tokens, input/output
```

`--show-config` shows the prices it found and any problem. The usage printout labels each row `reported` or `estimated`; compare it
with your provider dashboard on the first run, since the figures are estimates, not a bill.

Limits: effort levels apply to Claude-plan models only (a third-party model ignores them, so the CLI refuses the combination). The
plan-window guard applies only to Claude-plan calls. The web UI never uses this: it refuses `claudecode:` models, and its
third-party runs are not metered.

---

## 6. Cost and the guards

Every limit below is set per run. **No run starts without a spend cap you approved.**

| Guard | What it does | Switch |
|---|---|---|
| **USD cap** | The most the run may spend, Claude plan and third-party together (list-price estimate). Default 5.0. | `--usd-cap` |
| **Dynamic reserve** | Research stops early when spend + the next round + the cheapest useful report would reach the cap, so a report can still be written. Stop reason `report_reserve`. | `--static-reserve` (fixed 25 %, at least $0.50, instead) |
| **Report-cost guard** | The final-report call is projected first. Over budget, the per-source text is shortened; if even that is too dear, you get the findings digest, not a model-written report (`report_budget`). | `--report-budget` |
| **Plan-window guard** | Claude plan only: never uses more than this share of your five-hour window (default 0.8; research stops 0.1 below it). The window is shared with all your Claude use. | `--plan-window-max` |
| **Search guard** | After 6 failed Exa or Tavily searches in a row, stops searching and writes from what it has (`search_unavailable`). Not for `--search claude`. | `--search-failure-limit` |

Honest limits:
- Caps are ceilings, not hard bounds: calls already running finish. One live run ended $0.097 over a $1 cap.
- A small cap is a real limit: at $1 the cheapest report projects near $0.57, leaving little for research. Measured: a $2 run
  (22 calls, Tavily) completed at $1.83. Use a cap you can afford to lose.
- If the plan window is already above its research limit when you start, the run stops at its first call (about $0.01). Do not
  raise `--plan-window-max` unless you mean to spend that much of your plan.
- Costs are list-price estimates, not what you are billed.

---

## 7. Reading the output

In `--output-dir` (default `outputs/`):

| File | What it is |
|---|---|
| `research_<timestamp>.md` | The report: prompt, research brief, then the final report with numbered citations |
| `research_data_<timestamp>.json` | The sources behind the citations |
| `usage_<timestamp>.json` | Cost, calls, plan-window use, and cost per model (with `reported` / `estimated` for third-party) |
| `trace_<timestamp>.md` | The research trace, if `--trace` is on |
| `error_<timestamp>.txt` | Only when a run fails before a report could be written |

How a run ended is printed at the end:

- **completed**: no cap was hit.
- **STOPPED EARLY** with a report: a cap cut research short; the report covers what was gathered.
- **STOPPED EARLY**, status `partial`: no budget was left to write a report, so the file is a labelled digest of the findings. No
  model wrote it.
- Stop reasons: `usd_cap`, `plan_window_guard`, `report_reserve` (kept for the report), `report_budget` (the report did not fit its
  budget), `search_unavailable` (search kept failing).

Treat the report as a research draft: check the claims you will act on against their cited sources.

---

## 8. Resume

A run a cap stopped prints `RESUME STATE saved: ...` and a command. After the plan window resets, with a new cap you approve:

```bash
python run_research.py --resume <id> --usd-cap 2 --output-dir outputs
```

The saved models, search and limits are reused; only `--usd-cap`, `--report-budget` and `--plan-window-max` may change. The state
lives in `deep-research-memory/` (git-ignored): do not delete or edit it.

---

## 9. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Stops at its first call, `plan_window_guard` | The five-hour window is already above the research limit | Wait for the reset, or raise `--plan-window-max` if you accept the spend |
| `<model> needs <KEY> in the environment` | The key is not set in the process | Set it in the environment settings and start a new session; check with `--list-models` |
| `No price for <model>` | The provider's list has no price | Add `--price 'MODEL=IN/OUT'` |
| `unknown setting 'x'` | A `--set` or `--config` name is wrong | The message suggests the closest field; `--show-config` lists them all |
| Exa returns 401 or an oversized-header error | The key is not being injected | Set `EXA_AUTH_PROXY=1` (see the setup guide) |
| Tavily searches do nothing | No `TAVILY_API_KEY` variable | Set a placeholder value; the proxy injects the real key |
| A report is a digest, not prose | The report did not fit its budget, or no budget was left | `--resume` with a higher `--usd-cap` or `--report-budget` |
| `--resume` refuses a switch | Only the three cost flags may change on a resume | Start a new run for a new setup |

---

## 10. FAQ

**Does this use my API keys?** Not by default: Claude-plan runs use only your own `claude` sign-in and no provider key. Third-party
models use the key you put in the environment, and nothing else.

**Can others use it through the web app?** No. The web UI refuses `claudecode:` models by design: it would spend your plan.

**Can an AI agent change the settings?** Yes. Everything above is a switch, so the agent can build the command, show you
`--show-config`, and run it once you approve the cap. It cannot approve spend for you.

**How do I keep a setup?** Put the fields in a JSON file and pass `--config my-setup.json`; switches still override it.

**Is the cost shown a bill?** No. It is a list-price estimate (Claude plan) or the provider's reported or priced figure
(third-party). Your plan or provider dashboard is the source of truth.
