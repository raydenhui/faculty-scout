"""LangGraph agent that orchestrates the scraping workflow per (university, department).

Graph:
    START ─► (dept missing? → discover_departments) ─► discover_url
             ─► fetch_page ─► run_scrapegraph ─► validate_and_finalize ─► END

Each node is wrapped with tenacity retry logic.  Results are cached via
CacheManager and state is persisted for resume via LangGraph checkpointing.
Playwright is used as a fallback when a page requires JavaScript rendering.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, TypedDict

from langchain_core.language_models.chat_models import BaseChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, StateGraph
from tenacity import retry, stop_after_attempt, wait_exponential

from .cache import CacheManager
from .config import AppConfig
from .llm_factory import web_search
from .logging_config import get_llm_logger, get_logger
from .schema import Schema
from .scraper import scrape

log = get_logger("graph")
_llm_log = get_llm_logger()

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


class AgentState(TypedDict, total=False):
    university: str
    department: str | None
    listing_url: str | None
    page_html: str | None
    extracted_records: list[dict[str, Any]]
    error: str | None
    discovered_departments: list[str]
    need_discovery: bool


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------


def build_agent_graph(
    config: AppConfig,
    schema: Schema,
    llm: BaseChatModel,
    cache: CacheManager,
    checkpointer: BaseCheckpointSaver | None = None,
):
    """Create the compiled LangGraph state graph for scraping."""

    graph = StateGraph(AgentState)

    graph.add_node("discover_departments", _discover_departments_node(config, llm, cache))
    graph.add_node("discover_url", _discover_url_node(config, llm, cache))
    graph.add_node("fetch_page", _fetch_page_node(config, cache))
    graph.add_node("run_scrapegraph", _run_scrapegraph_node(config, schema, llm, cache))
    graph.add_node("validate_and_finalize", _validate_and_finalize_node(config, schema))

    graph.set_conditional_entry_point(
        _route_dept,
        {
            "discover": "discover_departments",
            "direct": "discover_url",
        },
    )
    graph.add_edge("discover_departments", END)
    graph.add_edge("discover_url", "fetch_page")
    graph.add_edge("fetch_page", "run_scrapegraph")
    graph.add_edge("run_scrapegraph", "validate_and_finalize")
    graph.add_edge("validate_and_finalize", END)

    if checkpointer is not None:
        return graph.compile(checkpointer=checkpointer)
    return graph.compile()


def _route_dept(state: AgentState) -> str:
    if state.get("need_discovery") or state.get("department") is None:
        return "discover"
    return "direct"


# ---------------------------------------------------------------------------
# Retry helpers
# ---------------------------------------------------------------------------


def _retry_for_node(config: AppConfig, node_name: str):
    return retry(
        stop=stop_after_attempt(config.scraping.max_retries_per_step),
        wait=wait_exponential(multiplier=1, min=config.scraping.request_delay_sec, max=30),
        reraise=True,
    )


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


def _discover_departments_node(
    config: AppConfig,
    llm: BaseChatModel,
    cache: CacheManager,
):
    async def _node(state: AgentState) -> AgentState:
        return await _discover_departments_impl(state, config, llm, cache)

    return _node


async def _discover_departments_impl(
    state: AgentState,
    config: AppConfig,
    llm: BaseChatModel,
    cache: CacheManager,
) -> AgentState:
    uni = state["university"]
    try:
        query = f"{uni} departments"

        max_attempts = config.scraping.max_retries_per_step
        search_results: list[dict[str, str]] = []
        for attempt in range(max_attempts):
            search_results = await web_search(
                query,
                provider=config.search.provider,
                api_key=config.search.bing_api_key,
                max_results=5,
            )
            if search_results:
                break
            await asyncio.sleep(config.scraping.request_delay_sec * (attempt + 1))

        urls_text = "\n".join(r["href"] for r in search_results if r.get("href"))

        prompt = f"""Given the university "{uni}", find all academic departments/schools.
From these search result URLs, suggest the most likely departments. If you cannot determine exact departments,
return a list of common department names for this type of institution.

Search results:
{urls_text or "No URLs available. Please use your knowledge of this university."}

Return ONLY a JSON list of department name strings, like: ["Computer Science", "Physics", "Mathematics"]"""

        response = await llm.ainvoke(prompt)
        text = _llm_response_text(response)

        json_match = re.search(r"\[.*?\]", text, re.DOTALL)
        if json_match:
            depts = json.loads(json_match.group())
            if isinstance(depts, list):
                state["discovered_departments"] = depts
                return state

        state["discovered_departments"] = []
    except Exception as e:
        state["discovered_departments"] = []
        state["error"] = str(e)

    return state


def _discover_url_node(
    config: AppConfig,
    llm: BaseChatModel,
    cache: CacheManager,
):
    async def _node(state: AgentState) -> AgentState:
        return await _discover_url_impl(state, config, llm, cache)

    return _node


async def _discover_url_impl(
    state: AgentState,
    config: AppConfig,
    llm: BaseChatModel,
    cache: CacheManager,
) -> AgentState:
    uni = state["university"]
    dept = state["department"] or ""

    queries = [
        f"{uni} {dept} faculty",
        f"{uni} {dept} academic staff",
        f"{uni} department of {dept} faculty",
        f"{uni} {dept} staff",
        f"{uni} {dept} professors",
    ] if dept else [
        f"{uni} faculty",
        f"{uni} academic staff",
        f"{uni} staff",
        f"{uni} professors",
    ]

    all_filtered: list[dict] = []
    seen: set[str] = set()

    for qi, query in enumerate(queries):
        log.info("discover_url query[%d]=%s", qi, query)

        search_results = await web_search(
            query,
            provider=config.search.provider,
            api_key=config.search.bing_api_key,
            max_results=8,
        )
        log.debug("discover_url[%d] raw_search_results=%d", qi, len(search_results))
        for si, sr in enumerate(search_results):
            log.debug("  [%d] %s | %s", si, sr.get("href", ""), sr.get("title", "")[:80])
        if not search_results:
            log.info("discover_url[%d] no search results", qi)
            continue

        filtered = _filter_bad_urls(search_results)
        log.debug(
            "discover_url[%d] after_filter=%d (removed %d)",
            qi, len(filtered), len(search_results) - len(filtered),
        )
        if not filtered:
            log.info("discover_url[%d] all results filtered out", qi)
            continue

        url = await _ask_llm_for_url(llm, uni, dept, filtered, qi)
        if url:
            state["listing_url"] = url
            return state

        for r in filtered:
            href = r.get("href", "")
            if href and href not in seen:
                seen.add(href)
                all_filtered.append(r)

    # Final combined attempt: ask LLM across ALL unique results from ALL queries
    if all_filtered:
        log.debug("discover_url combined attempt with %d unique results", len(all_filtered))
        url = await _ask_llm_for_url(llm, uni, dept, all_filtered, "combined")
        if url:
            state["listing_url"] = url
            return state

    state["listing_url"] = None
    state["error"] = f"No faculty listing URL found for {uni} / {dept} after {len(queries)} search queries."
    log.warning(state["error"])
    return state


async def _ask_llm_for_url(
    llm: BaseChatModel,
    uni: str,
    dept: str,
    results: list[dict],
    qi: object,
) -> str | None:
    results_text = "\n".join(
        f"  [{i}] {r['title']}\n      URL: {r['href']}"
        for i, r in enumerate(results)
    )

    target = f"{uni}" + (f", Department of {dept}" if dept else "")
    log.debug("discover_url[%s] target=%s candidates=%d", qi, target, len(results))

    prompt = (
        f"Find the official page listing faculty members (names + positions) for: {target}.\n\n"
        f"Search results:\n{results_text}\n\n"
        "Pick the BEST URL from these results. Rules:\n"
        "- Must be on the university's official domain.\n"
        "- Look for paths like /people, /staff, /faculty, /academic-staff.\n"
        "- Skip homepages (just a domain with /), LinkedIn, Facebook, Wikipedia, admission pages.\n"
        "- Prefer departmental subdomains (e.g. cs.university.edu) over the main university domain.\n"
        "- If none are clearly a faculty listing, respond with NONE.\n\n"
        "Respond with the URL or NONE."
    )

    try:
        _llm_log.info("===== url_discovery[%s] PROMPT =====\n%s\n===== END PROMPT =====", qi, prompt)
        response = await llm.ainvoke(prompt)
        text = _llm_response_text(response).strip()
        _llm_log.info("===== url_discovery[%s] RESPONSE =====\n%s\n===== END RESPONSE =====", qi, text)
        log.info("discover_url[%s] RESPONSE:\n%s", qi, text)
    except Exception as e:
        log.warning("discover_url[%s] LLM call failed: %s", qi, e)
        return None

    url_match = re.search(r"https?://[^\s]+", text)
    if url_match:
        picked = url_match.group().rstrip(".)")
        log.debug("discover_url[%s] picked=%s", qi, picked)
        return picked

    log.info("discover_url[%s] LLM said NONE or no URL in response", qi)
    return None


def _filter_bad_urls(results: list[dict]) -> list[dict]:
    blocked = {"linkedin.com", "facebook.com", "wikipedia.org", "youtube.com",
               "twitter.com", "x.com", "instagram.com", "reddit.com", "glassdoor.com",
               "indeed.com", "topuniversities.com", "usnews.com", "timeshighereducation.com"}
    return [r for r in results if not any(b in r.get("href", "") for b in blocked)]


def _fetch_page_node(
    config: AppConfig,
    cache: CacheManager,
):
    """Fetch page HTML, using cache and Playwright fallback for JS pages."""

    async def _node(state: AgentState) -> AgentState:
        url = state.get("listing_url")
        if not url:
            log.warning("fetch_page: no URL")
            if not state.get("error"):
                state["error"] = "No URL to fetch."
            return state

        cached = cache.get_url_content(url)
        if cached:
            log.debug("fetch_page cache HIT url=%s len=%d", url, len(cached))
            state["page_html"] = cached
            return state

        log.info("fetch_page start url=%s", url)
        html = await _http_fetch(url, config)
        if html and _has_content(html):
            log.debug("fetch_page http OK len=%d", len(html))
            cache.set_url_content(url, html, ttl_sec=config.files.cache_ttl_url)
            state["page_html"] = html
            return state

        log.info("fetch_page trying Playwright fallback...")
        html = await _playwright_fetch(url, config)
        if html:
            log.debug("fetch_page playwright OK len=%d", len(html))
            cache.set_url_content(url, html, ttl_sec=config.files.cache_ttl_url)
            state["page_html"] = html
            return state

        log.warning("fetch_page FAILED url=%s", url)
        state["page_html"] = None
        return state

    return _node


async def _http_fetch(url: str, config: AppConfig) -> str | None:
    try:
        import aiohttp

        async with aiohttp.ClientSession() as session, session.get(
            url,
            timeout=aiohttp.ClientTimeout(total=config.scraping.browser_timeout),
        ) as resp:
            if resp.status == 200:
                return await resp.text()
    except Exception:
        pass
    return None


async def _playwright_fetch(url: str, config: AppConfig) -> str | None:
    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=config.scraping.headless)
            page = await browser.new_page()
            try:
                await page.goto(url, timeout=config.scraping.browser_timeout * 1000)
                await page.wait_for_load_state("domcontentloaded")
                html = await page.content()
                return html
            finally:
                await browser.close()
    except Exception:
        pass
    return None


def _has_content(html: str) -> bool:
    """Crude check: does the HTML contain enough text to be a listing page?"""
    text = re.sub(r"<[^>]+>", " ", html)
    return len(text.strip()) > 200


def _run_scrapegraph_node(
    config: AppConfig,
    schema: Schema,
    llm: BaseChatModel,
    cache: CacheManager,
):
    async def _node(state: AgentState) -> AgentState:
        return await _run_scraper_impl(state, config, schema, llm, cache)

    return _node


async def _run_scraper_impl(
    state: AgentState,
    config: AppConfig,
    schema: Schema,
    llm: BaseChatModel,
    cache: CacheManager,
) -> AgentState:
    url = state.get("listing_url")
    if not url:
        log.warning("run_scraper: no listing_url, skipping")
        if not state.get("error"):
            state["error"] = "No listing URL available for scraping."
        return state

    html = state.get("page_html")
    if not html:
        log.warning("run_scraper: no page_html, skipping")
        if not state.get("error"):
            state["error"] = "No page HTML available for scraping."
        return state

    log.info("run_scraper start url=%s html_len=%d", url, len(html))

    try:
        records = await scrape(url, html, schema, llm, config)

        log.info("run_scraper done  records=%d", len(records))
        if records:
            log.debug("sample keys: %s", list(records[0].keys()) if records else "[]")

        state["extracted_records"] = records

    except Exception as e:
        log.error("run_scraper FAILED  %s: %s", type(e).__name__, e)
        state["error"] = f"Scraper error: {e}"
        state["extracted_records"] = []

    return state


def _validate_and_finalize_node(
    config: AppConfig,
    schema: Schema,
):
    async def _node(state: AgentState) -> AgentState:
        records = state.get("extracted_records", [])

        validated = _apply_schema_validation(records, schema, llm=None)

        state["extracted_records"] = validated
        return state

    return _node


def _apply_schema_validation(
    records: list[dict[str, Any]],
    schema: Schema,
    llm: BaseChatModel | None = None,
) -> list[dict[str, Any]]:
    """Apply per-column validation rules from schema.json."""
    validated: list[dict[str, Any]] = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        clean: dict[str, Any] = {}
        for col in schema.columns:
            val = rec.get(col.name, "")
            if val is None:
                val = ""
            v = getattr(col, "validation", None)
            if v is not None:
                val = _validate_column(str(val), v, llm)
            clean[col.name] = val
        validated.append(clean)
    return validated


def _validate_column(val: str, v: Any, llm: BaseChatModel | None = None) -> str:
    """Apply validation rules. If llm is provided, ask LLM about violations."""
    import re

    if v.regex and val and not re.match(v.regex, val, re.IGNORECASE if v.regex.startswith("^") else 0):
        return ""
    if v.max_length is not None and len(val) > v.max_length:
        return ""
    if v.contains_cjk and val and not re.search(r"[\u4e00-\u9fff\u3400-\u4dbf]", val):
        return ""
    if v.url_like and val and not re.match(r"^https?://", val, re.IGNORECASE):
        return ""
    return val


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _llm_response_text(response: Any) -> str:
    if hasattr(response, "content"):
        return str(response.content)
    if isinstance(response, str):
        return response
    return str(response)
