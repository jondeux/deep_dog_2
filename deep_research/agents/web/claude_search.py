"""Web search through Claude's own WebSearch tool, on the owner's Claude plan (no search API key).

[V] validation/p2: headless `claude -p --tools WebSearch --allowedTools WebSearch` ran a real search on a
plan sign-in (haiku-5.5, one call about $0.013 list price, about 10x a plain call). The model returns
JSON {title, url, snippet}; the engine registers the items like any other search tool's results."""
import logging
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool

from deep_research.providers.claude_code.transcript import ProtocolError, extract_json
from deep_research.agents.web.claude_fetch import page_reader_enabled
from deep_research.runtime import get_runtime

logger = logging.getLogger(__name__)

_SYSTEM = (
    "You are a web search backend. Use the WebSearch tool for the user's query, then reply with JSON only: "
    'a list of at most {n} objects {{"title": str, "url": str, "snippet": str}}. '
    "Each snippet is 1-3 sentences of what that page says that is relevant to the query. "
    "Only include pages you actually found; never invent a URL. No commentary.")


def _items_from(text: str, n: int) -> list:
    obj = extract_json(text)
    if isinstance(obj, dict):
        obj = obj.get("results") or obj.get("items") or []
    if not isinstance(obj, list):
        raise ProtocolError("search reply is not a list")
    seen, items = set(), []
    for r in obj:
        if not isinstance(r, dict):
            continue
        url = str(r.get("url") or "").strip()
        if not url.startswith(("http://", "https://")) or url in seen:
            continue
        seen.add(url)
        items.append({"id": url, "title": str(r.get("title") or url), "url": url,
                      "full_text": str(r.get("snippet") or "")})
        if len(items) >= n:
            break
    return items


def _display(query: str, items: list, reads: int = -1) -> str:
    if not items:
        return f"No results for '{query}'."
    lines = [f"## Search results for '{query}'", ""]
    for it in items:
        lines += [f"- **{it['title']}**", f"  URL: {it['url']}", f"  {it['full_text']}", ""]
    if reads >= 0:
        # Page reader on: tell the agent how deep it may go, so the reads go to the sources that matter.
        limit = f"up to {reads} pages" if reads else "pages"
        lines.append(f"You may read {limit} in full with fetch_urls(urls=[...]) (it extracts what is relevant to your "
                     "topic). Spend the reads on primary sources (trials, reviews, official documents); the other results "
                     "stay available as the summaries above. Then save with "
                     'batch_save_selected(items=[{"ref": "S#", "index": N, "reason": "..."}]).')
    else:
        lines.append("Read a source with fetch_urls(urls=[...]), then save it with "
                     'batch_save_selected(items=[{"ref": "S#", "index": N, "reason": "..."}]).')
    return "\n".join(lines)


@tool(parse_docstring=True)
async def claude_web_search(query: str, max_results: int = 5) -> Any:
    """Search the web with Claude's built-in search.

    Returns results with snippets so you can judge relevance. Read a source in full with
    fetch_urls(urls=[...]) when you want depth, then save it with
    batch_save_selected(items=[{"ref": "S#", "index": N, "reason": "..."}]).

    - Each call counts toward your SEARCH budget; don't re-search once capped.
    - Search once per distinct angle, then read the most promising hits before searching again.

    Args:
        query: A single search query to execute
        max_results: Number of results to return (1-10). 5 is enough for most searches.
    """
    n = min(max(int(max_results), 1), 10)
    rt = get_runtime()
    base = rt.models.get(rt.config.claude_search_model, max_tokens=4000)
    model = base.model_copy(update={"native_tools": ["WebSearch"]})
    messages = [SystemMessage(_SYSTEM.format(n=n)), HumanMessage(query)]
    last = ""
    for _ in range(2):                                  # one repair retry, then an empty result (never a crash)
        reply = await model.ainvoke(messages)
        last = reply.content if isinstance(reply.content, str) else str(reply.content)
        try:
            items = _items_from(last, n)
            break
        except ProtocolError as e:
            messages = messages + [HumanMessage(f"Your reply was rejected ({e}). Reply with the JSON list only.")]
    else:
        logger.warning("claude_web_search: unparseable reply for %r", query)
        return {"display": f"Search for '{query}' returned no parseable results. Try a different query.",
                "items": []}
    reader = page_reader_enabled(rt.config)
    if reader:
        for it in items:                       # a snippet is not the page: fetch_urls must read the page itself
            it["snippet_only"] = True
    return {"display": _display(query, items, int(rt.config.claude_page_reads_per_agent) if reader else -1),
            "items": items}
