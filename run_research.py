#!/usr/bin/env python3

"""

Deep Research Agent - Server Runner

A standalone script for running the Deep Research agent on a server.

Loads environment variables, runs research, and saves outputs with citations.

Usage (with uv):

    uv run python run_research.py --prompt "Your research question here"

    uv run python run_research.py --prompt-file input.txt

    uv run python run_research.py --prompt-file input.txt --output-dir my_outputs

Usage (without uv, if dependencies are installed):

    python run_research.py --prompt "Your research question here"

The CLI now executes through the same stable engine entry point the API uses
(``deep_research.integration.run_research``) but with file output enabled, so
CLI behaviour (Markdown/JSON/trace files) is preserved while the underlying
engine is concurrency-safe and per-run configurable.
"""

import asyncio
import argparse
import json
import logging
import os
import random
import string
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

# Load environment variables before importing anything else
from dotenv import load_dotenv

# Find and load .env file from project root
env_path = Path(__file__).resolve().parent / '.env'
load_dotenv(dotenv_path=env_path)

from langchain_core.messages import HumanMessage  # noqa: E402

from deep_research.config import ENABLE_RESEARCH_TRACE, OUTPUT_MODE, get_supervisor_model  # noqa: E402
from deep_research.integration import (  # noqa: E402
    RuntimeOptions,
    RunStatus,
    resume_research as engine_resume_research,
    run_research as engine_run_research,
)
from deep_research.resume import DEFAULT_DIR as _MEMORY_DIR_NAME, FileSnapshotSink  # noqa: E402
from deep_research.run_config import RunConfig  # noqa: E402
from deep_research.runtime import PromptLibrary  # noqa: E402
from deep_research.utils import extract_text_from_response  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


# Resume state of runs that a usage cap stopped early (git-ignored). See deep_research/resume.py.
# DEEPDOG_MEMORY_DIR overrides the folder (the tests point it at a temp dir so they never write here).
DEFAULT_MEMORY_DIR = Path(os.environ.get("DEEPDOG_MEMORY_DIR") or Path(__file__).resolve().parent / _MEMORY_DIR_NAME)


def _save_output_to_db(output_data: dict) -> None:
    """
    Placeholder for database insert logic for research outputs.

    Implement your DB insert here when ready. The output_data dict contains:
        - thread_id: Unique identifier for this research session
        - timestamp: When the research was run
        - prompt: Original user query
        - research_brief: The generated research plan
        - final_report: The main report content
        - notes: List of raw research notes
    """
    # TODO: Implement database insert logic
    pass


async def run_research(prompt: str, output_dir: Path, thread_id: str = None,
                       clean_output: bool = False, config: RunConfig = None,
                       resume: dict = None, memory_dir: Path = None) -> Dict[str, Any]:
    """
    Run the Deep Research agent and save the output to a file.

    Args:
        prompt: The research prompt/question
        output_dir: Directory to save the output file
        thread_id: Optional thread ID for the research session
        clean_output: If True, write only the final report (no header/brief)
        config: Optional RunConfig (e.g. from the model picker); default is RunConfig().finalize()
        resume: A resume state loaded from ``memory_dir`` (``deep_research.resume.load_snapshot``): continue
            that run instead of starting one. ``prompt`` is then taken from it, and ``config=None`` reuses
            the saved configuration.
        memory_dir: Where a run stopped early by a usage cap saves its resume state (default: deep-research-memory/)

    Returns:
        Dictionary with:
            - output_file: Path to the saved output file
            - final_report: The raw final report content
            - sources: List of source dictionaries
            - trace_content: The research trace document (if enabled)
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    memory_dir = Path(memory_dir) if memory_dir else DEFAULT_MEMORY_DIR
    if resume is not None:
        prompt = resume["prompt"]

    # Generate timestamp for filename and thread (random letters avoid
    # collisions when multiple agents start in the same second).
    random_suffix = ''.join(random.choices(string.ascii_lowercase, k=3))
    timestamp = f"{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}_{random_suffix}"
    if thread_id is None:
        thread_id = (f"{resume['root_id']}-r{int(resume.get('resumes') or 0) + 1}"
                     if resume is not None else timestamp)
    output_file = output_dir / f"research_{timestamp}.md"

    logger.info(f"Starting research with thread ID: {thread_id}")
    logger.info(f"Output will be saved to: {output_file}")

    cfg = (config or RunConfig()).finalize() if resume is None or config is not None else None
    memory = FileSnapshotSink(memory_dir)        # keeps the resume state if a usage cap stops the run early
    options = RuntimeOptions(
        run_id=thread_id,
        thread_id=thread_id,
        output_dir=output_dir,
        console_enabled=True,
        artifact_sink=memory,
    )
    if resume is not None:
        logger.info(f"Resuming run {resume['thread_id']} (stopped by {resume.get('stopped_early')}, "
                    f"{resume.get('elapsed_minutes')} research minutes in)")
        result = await engine_resume_research(resume, config=cfg, runtime=options)
    else:
        result = await engine_run_research(prompt, config=cfg, runtime=options)

    # Runs on the Claude plan: show and save what it cost (list-price estimate), and whether a cap stopped it.
    usage = (result.usage or {}).get("claude_code")
    stopped_early = (result.run_metadata or {}).get("stopped_early")
    usage_file = None
    if usage:
        from deep_research.providers.claude_code import format_usage
        print("\n" + format_usage(usage))
        if stopped_early:
            mode = {"synthesized": "the report was still written from the findings gathered so far",
                    "digest": "no budget was left to write a report, so the findings are returned unsynthesised"
                    }.get(stopped_early.get("report_mode"), "")
            print(f"  STOPPED EARLY by {stopped_early['reason']}" + (f": {mode}" if mode else ""))
        if memory.path is not None:
            root = result.run_metadata["resume_state"]["root_id"]
            print(f"  RESUME STATE saved: {memory.path}\n  After the plan window resets, continue with: "
                  f"python run_research.py --resume {root}")
        usage_file = output_dir / f"usage_{timestamp}.json"
        usage_file.write_text(json.dumps({
            "thread_id": thread_id, "status": result.status, "stopped_early": stopped_early, "usage": usage,
            "elapsed_seconds": (result.run_metadata or {}).get("elapsed_seconds"),
            "report_file": str(output_file),
        }, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info(f"Usage saved to: {usage_file}")

    stopped_without_report = bool(stopped_early) and not result.final_report
    if (result.status in (RunStatus.FAILED.value, RunStatus.CANCELLED.value) and not result.final_report) \
            or stopped_without_report:
        error = result.failure or (
            f"stopped by {stopped_early['reason']} before a report could be written (draft only)"
            if stopped_without_report else result.status)
        error_file = output_dir / f"error_{timestamp}.txt"
        error_file.write_text(
            f"Error occurred at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            f"Prompt:\n{prompt}\n\n"
            f"Status: {result.status}\nError:\n{error}\n",
            encoding="utf-8",
        )
        logger.error(f"Research failed ({result.status}): {error}")
        raise RuntimeError(f"Research failed ({result.status}): {error}")

    final_report = result.final_report or result.draft_report or ""
    research_brief = result.research_brief or ""
    sources = result.sources if result.sources else {}

    output_data = {
        "thread_id": thread_id,
        "timestamp": datetime.now().isoformat(),
        "prompt": prompt,
        "research_brief": research_brief,
        "final_report": final_report,
        "status": result.status,
    }

    # Write to file if enabled (CLI default output_mode is "file")
    if OUTPUT_MODE in ("file", "both"):
        output_file.parent.mkdir(parents=True, exist_ok=True)
        with open(output_file, "w", encoding="utf-8") as f:
            if not clean_output:
                f.write("# Deep Research Report\n\n")
                f.write(f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
                f.write("---\n\n")
                f.write("## Research Prompt\n\n")
                f.write(f"{prompt}\n\n")
                f.write("---\n\n")
                if research_brief:
                    f.write("## Research Brief\n\n")
                    f.write(f"{research_brief}\n\n")
                    f.write("---\n\n")
            f.write("## Final Report\n\n")
            f.write(f"{final_report}\n\n")
        logger.info(f"Report saved to file: {output_file}")

    if OUTPUT_MODE in ("file", "both") and sources:
        sources_json_file = output_dir / f"research_data_{timestamp}.json"
        research_data = {
            "thread_id": thread_id,
            "timestamp": datetime.now().isoformat(),
            "prompt": prompt,
            "sources": sources,
            "final_report": final_report,
        }
        sources_json_file.parent.mkdir(parents=True, exist_ok=True)
        with open(sources_json_file, "w", encoding="utf-8") as f:
            json.dump(research_data, f, indent=2, ensure_ascii=False)
        logger.info(f"Sources saved to: {sources_json_file} ({len(sources)} sources)")

    if OUTPUT_MODE in ("db", "both"):
        _save_output_to_db(output_data)
        logger.info("Report saved to database")

    logger.info("Research complete!")

    # Generate research trace content (only if enabled in config/env).
    trace_content = None
    trace_file = None
    trace_data = result.trace if ENABLE_RESEARCH_TRACE else []

    if trace_data:
        interaction_log = ""
        for loop in trace_data:
            interaction_log += f"""

--- Loop {loop['loop_number']} (at {loop['timestamp']}) ---

SUPERVISOR DELEGATED RESEARCH TOPIC:

{loop['research_topic']}

SUBAGENT RETURNED FINDINGS:

{loop['findings'][:] if loop.get('findings') else 'No findings returned'}

SUPERVISOR REACTION:

{loop.get('supervisor_reaction', 'No explicit reaction captured')}

"""
        try:
            logger.info("Generating research trace document...")
            prompts = PromptLibrary(cfg.prompt_version)
            trace_prompt_tmpl = prompts.get("research_trace_compression_prompt", "")
            if not trace_prompt_tmpl:
                raise ValueError("research_trace_compression_prompt unavailable")
            trace_prompt = trace_prompt_tmpl.format(
                research_brief=research_brief,
                interaction_log=interaction_log,
            )
            trace_model = get_supervisor_model(max_tokens=16000)
            trace_response = await trace_model.ainvoke([HumanMessage(content=trace_prompt)])
            trace_content = extract_text_from_response(trace_response.content)
        except Exception as e:
            logger.warning(f"Failed to generate research trace: {e}")
            trace_content = "# Research Process Trace (Raw)\n\n"
            trace_content += f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            trace_content += f"**Research Brief:** {research_brief}\n\n"
            trace_content += "---\n\n"
            trace_content += interaction_log

        if OUTPUT_MODE in ("file", "both") and trace_content:
            trace_file = output_dir / f"trace_{timestamp}.md"
            trace_file.parent.mkdir(parents=True, exist_ok=True)
            with open(trace_file, "w", encoding="utf-8") as f:
                f.write(trace_content)
            logger.info(f"Research trace saved to: {trace_file}")

    output_data["trace_content"] = trace_content
    if OUTPUT_MODE in ("db", "both"):
        _save_output_to_db(output_data)
        logger.info("Report saved to database")

    return {
        "output_file": str(output_file),
        "final_report": final_report,
        "sources": sources,
        "trace_content": trace_content,
        "status": result.status,
        "usage": usage,
        "usage_file": str(usage_file) if usage_file else None,
        "stopped_early": stopped_early,
        "resume_file": str(memory.path) if memory.path else None,
    }


def main():
    """Main entry point for the script."""
    parser = argparse.ArgumentParser(
        description="Run the Deep Research Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python run_research.py --prompt "What are the latest developments in AI safety?"

    python run_research.py --prompt-file research_question.txt

    python run_research.py --prompt-file input.txt --output-dir results

        """,
    )

    input_group = parser.add_mutually_exclusive_group(required=False)
    input_group.add_argument(
        "--prompt", "-p",
        type=str,
        help="Research prompt/question to investigate",
    )
    input_group.add_argument(
        "--prompt-file", "-f",
        type=str,
        help="Path to a file containing the research prompt",
    )

    parser.add_argument(
        "--resume", metavar="ID",
        help="continue a run that a usage cap stopped early: its id (the thread id printed when it stopped)",
    )
    parser.add_argument(
        "--memory-dir", default=None,
        help=f"folder for resume state (default: {_MEMORY_DIR_NAME}/ next to this script)",
    )
    parser.add_argument(
        "--output-dir", "-o",
        type=str,
        default="outputs",
        help="Directory to save output files (default: outputs)",
    )
    parser.add_argument(
        "--thread-id", "-t",
        type=str,
        default=None,
        help="Thread ID for the research session (default: auto-generated timestamp)",
    )

    picker = parser.add_argument_group(
        "model picker (Claude subscription)",
        "Run every LLM call through the unmodified `claude` CLI on YOUR OWN plan. Personal use only. "
        "Models: haiku-5.5, sonnet-5.5, opus-5.5. Efforts: low, medium, high, xhigh, max.")
    picker.add_argument("--preset", help="economy (default when any picker flag is used), benchmark, custom, "
                                         "testing-framework")
    for role in ("supervisor", "draft", "subagent"):
        picker.add_argument(f"--{role}-model", help=f"model for the {role} role")
        picker.add_argument(f"--{role}-effort", help=f"reasoning effort for the {role} role")
    picker.add_argument("--search", default="claude", help="web search backend: claude (default, no key), exa, tavily, both")
    picker.add_argument("--list-presets", action="store_true", help="show the presets and exit")
    picker.add_argument("--usd-cap", type=float, default=None,
                        help="list-price cap in USD for the whole run (default 5.0). Research stops at 75%% of it, "
                             "the rest is kept for writing the final report")
    picker.add_argument("--plan-window-max", type=float, default=None,
                        help="never exceed this share of the plan window, 0 to 1 (default 0.8). Research stops "
                             "0.1 below it, the rest is kept for the final report")

    args = parser.parse_args()

    if args.list_presets:
        from deep_research import model_picker
        for name in model_picker.PRESETS:
            print(model_picker.describe(name) + ("   (default)" if name == model_picker.DEFAULT_PRESET else ""))
        return

    if args.resume and (args.prompt or args.prompt_file):
        parser.error("--resume continues a saved run; it takes no --prompt or --prompt-file")
    if not args.prompt and not args.prompt_file and not args.resume:
        parser.error("one of the arguments --prompt/-p --prompt-file/-f --resume is required")

    snapshot = None
    if args.resume:
        from deep_research.resume import load_snapshot
        try:
            snapshot = load_snapshot(args.memory_dir or DEFAULT_MEMORY_DIR, args.resume)
        except ValueError as e:
            parser.error(str(e))
        if args.preset or any(getattr(args, f"{r}_{k}") for r in ("supervisor", "draft", "subagent")
                              for k in ("model", "effort")) or args.search != "claude":
            parser.error("--resume keeps the saved models and search; only --usd-cap and --plan-window-max may change")

    picked = None
    wants_picker = args.preset or any(getattr(args, f"{r}_{k}") for r in ("supervisor", "draft", "subagent")
                                      for k in ("model", "effort"))
    if wants_picker:
        from deep_research import model_picker
        overrides = {}
        try:
            base = model_picker.choices(args.preset or model_picker.DEFAULT_PRESET)
            for role in ("supervisor", "draft", "subagent"):
                m, e = getattr(args, f"{role}_model"), getattr(args, f"{role}_effort")
                if m or e:
                    cur = base.get(role) or base["supervisor"]
                    overrides[role] = (m or cur[0], e or cur[1])
            picked = model_picker.apply_preset(RunConfig(), args.preset or model_picker.DEFAULT_PRESET, search=args.search, **overrides)
        except ValueError as e:
            parser.error(str(e))
        print("Models: " + model_picker.describe(args.preset or model_picker.DEFAULT_PRESET, **overrides))
        caps = {k: v for k, v in (("claude_code_usd_cap", args.usd_cap),
                                  ("claude_code_plan_window_max", args.plan_window_max)) if v is not None}
        if caps:
            if (args.usd_cap is not None and args.usd_cap <= 0) or (
                    args.plan_window_max is not None and not 0.1 <= args.plan_window_max <= 1):
                parser.error("--usd-cap must be above 0 and --plan-window-max between 0.1 and 1")
            from dataclasses import replace
            picked = replace(picked, **caps)
    elif args.resume and (args.usd_cap is not None or args.plan_window_max is not None):
        # A resumed segment gets a fresh cap; the window guard still reads the real plan window.
        if (args.usd_cap is not None and args.usd_cap <= 0) or (
                args.plan_window_max is not None and not 0.1 <= args.plan_window_max <= 1):
            parser.error("--usd-cap must be above 0 and --plan-window-max between 0.1 and 1")
        from dataclasses import replace
        from deep_research.resume import config_from_snapshot
        picked = replace(config_from_snapshot(snapshot), **{k: v for k, v in (
            ("claude_code_usd_cap", args.usd_cap), ("claude_code_plan_window_max", args.plan_window_max))
            if v is not None})
    elif args.usd_cap is not None or args.plan_window_max is not None:
        parser.error("--usd-cap and --plan-window-max apply to Claude-plan runs: add --preset or a model flag")

    if snapshot is not None:
        prompt = None
    elif args.prompt:
        prompt = args.prompt
    else:
        prompt_path = Path(args.prompt_file)
        if not prompt_path.exists():
            logger.error(f"Prompt file not found: {prompt_path}")
            sys.exit(1)
        with open(prompt_path, "r", encoding="utf-8") as f:
            prompt = f.read().strip()

    if snapshot is None and not prompt:
        logger.error("Empty prompt provided")
        sys.exit(1)

    logger.info(f"Prompt length: {len(prompt or snapshot['prompt'])} characters")

    output_dir = Path(args.output_dir)

    try:
        extra = {}
        if snapshot is not None:
            extra["resume"] = snapshot
        if args.memory_dir:
            extra["memory_dir"] = args.memory_dir
        result = asyncio.run(run_research(prompt, output_dir, args.thread_id, config=picked, **extra))
        status = result.get("status")
        if result.get("stopped_early"):
            print(f"\n⚠️  Stopped early by a usage limit ({status}). Output saved to: {result['output_file']}")
        else:
            print(f"\n✅ Research complete! Output saved to: {result['output_file']}")
    except KeyboardInterrupt:
        logger.info("Research interrupted by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Research failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
