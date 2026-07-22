"""LangGraph agent that orchestrates the scraping workflow per (university, department).

Graph:
    START ─► (dept missing? → discover_departments) ─► discover_url
             ─► fetch_page ─► run_scraper ─► validate_and_finalize ─► END

Each node is wrapped with tenacity retry logic.  Results are cached via
CacheManager and state is persisted for resume via LangGraph checkpointing.
Playwright is used as a fallback when a page requires JavaScript rendering.
"""

from __future__ import annotations

import asyncio
import contextlib
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
from .scraper.html_cleaner import clean_html

log = get_logger("graph")
_llm_log = get_llm_logger()

# Module-level progress callback (not serialized, set per-invoke)
_progress_callback: Any = None

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


class AgentState(TypedDict, total=False):
    university: str
    department: str | None
    listing_url: str | None
    page_url: str
    page_html: str | None
    extracted_records: list[dict[str, Any]]
    error: str | None
    discovered_departments: list[str]
    need_discovery: bool
    skip_unchanged: bool
    skipped: bool


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------


def build_agent_graph(
    config: AppConfig,
    schema: Schema,
    llm: BaseChatModel,
    cache: CacheManager,
    checkpointer: BaseCheckpointSaver | None = None,
    skip_unchanged: bool = False,
):
    """Create the compiled LangGraph state graph for scraping."""

    graph = StateGraph(AgentState)

    graph.add_node("discover_departments", _discover_departments_node(config, llm, cache))
    graph.add_node("discover_url", _discover_url_node(config, llm, cache))
    graph.add_node("fetch_page", _fetch_page_node(config, cache, skip_unchanged=skip_unchanged))
    graph.add_node("run_scraper", _run_scraper_node(config, schema, llm, cache))
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
    graph.add_edge("fetch_page", "run_scraper")
    graph.add_edge("run_scraper", "validate_and_finalize")
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
    cache: CacheManager | None,
) -> AgentState:
    uni = state["university"]
    all_depts: list[str] = []
    try:
        page_url = state.get("page_url", "").strip()
        page_html = ""

        if page_url:
            # Use provided link directly, skip search
            log.info("discover_dept using provided link=%s", page_url)
            html = await _http_fetch(page_url, config)
            if not html:
                html = await _playwright_fetch(page_url, config)
            if html and len(html) > 1000:
                page_html = html
            else:
                page_url = ""

        if not page_url:
            # Step 1: Search for a department listing page
            query = f"{uni} academic departments list"
            search_results = await web_search(
                query,
                provider=config.search.provider,
                api_key=config.search.bing_api_key,
                max_results=5,
            )

            # Step 2: Find and fetch a department listing page
            for r in search_results:
                url = r.get("href", "")
                if not url or any(b in url for b in ("wikipedia", "linkedin", "facebook")):
                    continue
                html = await _http_fetch(url, config)
                if not html:
                    html = await _playwright_fetch(url, config)
                if html and len(html) > 1000:
                    page_url = url
                    page_html = html
                    break

        # Step 3: LLM extracts departments from the page (with pagination)
        current_url = page_url
        current_html = page_html
        page_num = 0
        max_pages = 30
        visited_pages: list[str] = []

        while current_url and page_num < max_pages:
            page_num += 1
            html_len = len(current_html) if current_html else 0
            log.info("discover_dept page %d url=%s len=%d", page_num, current_url, html_len)

            # Build context of previously visited pages
            prev_pages = ""
            if visited_pages:
                prev_pages = "Previously visited pages:\n" + "\n".join(
                    f"  {i}. {u}" for i, u in enumerate(visited_pages, 1)
                ) + "\n"

            prompt = f"""From this university page, extract only ACADEMIC departments and schools.

University: {uni}
Current page URL: {current_url}
{prev_pages}
Rules:
- Include ONLY academic teaching/research departments (e.g., Computer Science, Physics).
- Exclude administrative offices (HR, Finance, IT, Library, Registrar).
- Exclude research centres/labs unless they are full academic departments.
- Exclude graduate schools, continuing education, professional studies.

IMPORTANT — Pagination detection:
The page likely uses alphabetical (A, B, C...) or numbered pagination.
- Look at the PREVIOUSLY VISITED PAGES to detect the pattern.
- If previous pages are: .../A.html, .../B.html, .../C.html,
  the next page is .../D.html (next letter in alphabet).
- If previous pages are: .../page/1, .../page/2,
  the next page is .../page/3.
- Look for ALL navigation links on the page (letters A-Z, numbers, "Next").
- If the next page link exists in the HTML, use it.
- If the link is NOT visible but can be INFERRED from the pattern,
  construct it and set as "next_page_url".
- If the current letter/number is the LAST one on this page,
  set "next_page_url" to "".

Return ONLY a JSON object:
{{
  "departments": ["Computer Science", "Physics", "Mathematics"],
  "next_page_url": "https://..." or ""
}}

HTML:
{clean_html(current_html) if current_html else "No page available."}"""

            response = await llm.ainvoke(prompt)
            text = _llm_response_text(response)
            _llm_log.info("===== dept_discovery[%d] PROMPT =====\n%s\n===== END PROMPT =====", page_num, prompt)
            _llm_log.info("===== dept_discovery[%d] RESPONSE =====\n%s\n===== END RESPONSE =====", page_num, text)
            log.debug("dept discovery page %d response:\n%s", page_num, text[:500])

            m = re.search(r"\{[\s\S]*\}", text.strip())
            if m:
                try:
                    data = json.loads(m.group())
                    depts = data.get("departments", [])
                    if isinstance(depts, list):
                        all_depts.extend(depts)
                    next_url = (data.get("next_page_url") or "").strip()
                except json.JSONDecodeError:
                    break
            else:
                break

            # Record current page as visited before moving to next
            visited_pages.append(current_url)
            current_url = next_url

            if current_url and current_url != page_url:
                current_html = await _http_fetch(current_url, config)
                if not current_html:
                    current_html = await _playwright_fetch(current_url, config)
                if not current_html:
                    break
            else:
                break

        # LLM deduplication: remove semantic duplicates
        unique_deduped = await _llm_dedup_departments(all_depts, uni, llm)

        state["discovered_departments"] = unique_deduped
        log.info("dept discovery done: %d departments from %d pages", len(unique_deduped), page_num)

    except Exception as e:
        log.warning("department discovery failed: %s", e)
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
    # If listing_url already provided, validate it, else fall through to discovery
    provided = state.get("listing_url")
    if provided and str(provided).startswith("http"):
        log.info("discover_url using provided listing_url=%s", provided)
        return state

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
    skip_unchanged: bool = False,
):
    """Fetch page HTML, using cache and Playwright fallback for JS pages.

    When *skip_unchanged* is True, always re-fetches the page and compares
    with the cached version.  If the content is unchanged the node sets
    ``state["skipped"] = True`` so downstream nodes can short-circuit.
    """

    async def _node(state: AgentState) -> AgentState:
        url = state.get("listing_url")
        if not url:
            log.warning("fetch_page: no URL")
            if not state.get("error"):
                state["error"] = "No URL to fetch."
            return state

        state_skip = state.get("skip_unchanged", False)
        cached = cache.get_url_content(url)

        # In skip mode the cache must survive between monthly runs →
        # disable TTL expiry so the comparison always has a baseline.
        cache_ttl = 0 if (skip_unchanged or state_skip) else config.files.cache_ttl_url

        # ---- normal (non-skip) path: use cache if available ---------------
        if not (skip_unchanged or state_skip) and cached:
            log.debug("fetch_page cache HIT url=%s len=%d", url, len(cached))
            state["page_html"] = cached
            return state

        # ---- always re-fetch when skip_unchanged is enabled ---------------
        log.info("fetch_page start url=%s skip_mode=%s", url, skip_unchanged or state_skip)
        html = await _http_fetch(url, config)
        if html and _has_content(html) and not _is_js_template(html) and len(html) >= 5000:
            log.debug("fetch_page http OK len=%d", len(html))
        else:
            if html and len(html) < 5000:
                log.info("fetch_page HTTP response too short (%d bytes), trying Playwright...", len(html))
            else:
                log.info("fetch_page trying Playwright fallback...")
            html = await _playwright_fetch(url, config)

        if not html:
            log.warning("fetch_page FAILED url=%s", url)
            # Fall back to cached version if available
            if cached:
                state["page_html"] = cached
            else:
                state["page_html"] = None
            return state

        # ---- comparison logic for skip_unchanged --------------------------
        if (skip_unchanged or state_skip) and cached and cached == html:
            log.info("fetch_page SKIPPED url=%s (content unchanged, len=%d)", url, len(html))
            cache.set_url_content(url, html, ttl_sec=cache_ttl)
            state["skipped"] = True
            state["page_html"] = html
            return state

        # ---- new content, or normal mode: cache and proceed ----------------
        log.debug("fetch_page OK len=%d", len(html))
        cache.set_url_content(url, html, ttl_sec=cache_ttl)
        state["page_html"] = html
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
    """Fetch page with Playwright. Skips headful retry for 404/410/5xx status codes."""
    for attempt in range(2):
        headless = config.scraping.headless and attempt == 0
        try:
            from playwright.async_api import async_playwright

            async with async_playwright() as pw:
                browser = await pw.chromium.launch(
                    headless=headless,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                    ] if headless else [],
                )
                context = await browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/128.0.0.0 Safari/537.36"
                    ),
                    viewport={"width": 1280, "height": 900},
                    locale="en-US",
                )
                page = await context.new_page()
                try:
                    response = await page.goto(url, timeout=config.scraping.browser_timeout * 1000,
                                               wait_until="domcontentloaded")
                    http_status = response.status if response else 0
                    if http_status in (404, 410):
                        log.debug("_playwright_fetch HTTP %d, skipping: %s", http_status, url)
                        return None
                    if http_status >= 500:
                        log.debug("_playwright_fetch HTTP %d server error, skipping: %s", http_status, url)
                        return None

                    # Wait for JS-rendered content to appear
                    content_selectors = [
                        "tr[class]", "div[class*='people']", "div[class*='staff']",
                        "div[class*='person']", "div[class*='profile']", ".member",
                        "li[class]", "table[class]", "*[data-member]", "a[href*='mailto']",
                    ]
                    for sel in content_selectors:
                        try:
                            await page.wait_for_selector(sel, timeout=5_000)
                            break
                        except Exception:
                            continue
                    else:
                        with contextlib.suppress(Exception):
                            await page.wait_for_load_state("networkidle", timeout=8_000)
                        await asyncio.sleep(3)
                    await asyncio.sleep(2)
                    html = await page.content()
                    title = await page.title()
                    is_blocked = (
                        len(html) < 1000
                        or "403" in title
                        or "Forbidden" in title
                        or "Just a moment" in title
                        or "cf-browser-verification" in html[:2000]
                    )
                    if is_blocked and attempt == 0:
                        continue
                    return html
                finally:
                    await browser.close()
        except Exception:
            if attempt == 0 and config.scraping.headless:
                continue
    return None


def _has_content(html: str) -> bool:
    """Crude check: does the HTML contain enough text to be a listing page?"""
    text = re.sub(r"<[^>]+>", " ", html)
    return len(text.strip()) > 200


def _is_js_template(html: str) -> bool:
    """Detect JS framework template HTML (Angular/Vue/React) that needs rendering."""
    indicators = [
        "{{",                       # Angular/Handlebars/Mustache
        "v-bind", "v-if", "v-for", "v-model",  # Vue.js directives
        "ng-app", "ng-controller", "ng-repeat", # AngularJS
        '__vue__', '__vue_app__',               # Vue runtime
        '_reactRootContainer',                  # React
        'data-reactroot', 'data-reactid',       # React (legacy)
        "<div id=\"root\">", "<div id=\"app\">" # Common SPAs
    ]
    snippet = html[:50_000]
    for ind in indicators:
        if ind in snippet:
            return True

    # Dynamic data loading: scripts contain AJAX/API/fetch calls for data
    scripts = re.findall(r"<script[^>]*>.*?</script>", snippet, re.DOTALL | re.IGNORECASE)
    api_patterns = ["/api/", "fetch(", "ajax(", "xmlhttp", "staff_data", "faculty_data",
                    "person_data", "load_people", "get_people", "member_data"]
    for s in scripts:
        for pat in api_patterns:
            if pat.lower() in s.lower():
                return True

    # Content check: pages with faculty words but no structured person data
    text = re.sub(r"<[^>]+>", " ", snippet)
    has_faculty_words = any(t in text.lower() for t in ["professor", "associate", "lecturer",
                                                         "faculty", "academic staff", "teaching staff",
                                                         "staff directory"])

    # Look for structured person data in HTML (tables, lists of people)
    has_person_table = bool(re.search(
        r'<table[^>]*>.*?'                                      # table
        r'<t[rd][^>]*>\s*(?:Prof\.|Dr\.|Professor)\s+[A-Z]'
        r'.*?</table>',
        snippet, re.DOTALL | re.IGNORECASE
    ))
    has_person_list = bool(re.search(
        r'<li[^>]*>\s*(?:Prof\.|Dr\.|Professor)\s*[A-Z]',
        snippet, re.IGNORECASE
    ))
    email_count = len(set(re.findall(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", snippet)))

    return bool(
        has_faculty_words
        and not has_person_table
        and not has_person_list
        and email_count < 3
        and len(snippet) > 15000
    )


def _run_scraper_node(
    config: AppConfig,
    schema: Schema,
    llm: BaseChatModel,
    cache: CacheManager,
):
    async def _node(state: AgentState) -> AgentState:
        return await _run_scraper_impl(state, config, schema, llm, cache, progress_callback=_progress_callback)

    return _node


async def _run_scraper_impl(
    state: AgentState,
    config: AppConfig,
    schema: Schema,
    llm: BaseChatModel,
    cache: CacheManager,
    progress_callback: Any = None,
) -> AgentState:
    if state.get("skipped"):
        log.info("run_scraper: skipped due to unchanged content")
        return state

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
        records = await scrape(url, html, schema, llm, config, progress_callback=progress_callback)
        state["page_html"] = ""

        if records and len(records) == 1 and isinstance(records[0], dict):
            page_err = records[0].get("_page_error")
            if page_err:
                state["error"] = f"Listing page issue: {page_err}"
                state["extracted_records"] = []
                return state

        log.info("run_scraper done  records=%d", len(records))
        if records:
            log.debug("sample keys: %s", list(records[0].keys()) if records else "[]")

        state["extracted_records"] = records

    except Exception as e:
        log.error("run_scraper FAILED  %s: %s", type(e).__name__, e)
        state["error"] = f"Scraper error: {e}"
        state["extracted_records"] = []

    return state


async def _llm_dedup_departments(
    departments: list[str],
    university: str,
    llm: BaseChatModel,
) -> list[str]:
    """Use LLM to remove semantically duplicate department names."""
    if not departments:
        return []

    # First pass: simple string dedup
    seen: set[str] = set()
    unique: list[str] = []
    for d in departments:
        key = d.strip().lower()
        if key and key not in seen:
            seen.add(key)
            unique.append(d.strip())

    if len(unique) <= 1:
        return unique

    dept_list = "\n".join(f"  - {d}" for d in unique)

    prompt = f"""Review this list of academic departments at {university} and remove duplicates.

The list may contain the same department expressed in different formats:
- "Computer Science, Department of" and "Department of Computer Science" → same
- "Computing, School of" and "School of Computing" → same
- "Physics" and "Department of Physics" → same (keep the shorter version)
- "Mathematics" and "Applied Mathematics" → DIFFERENT (keep both)
- "Biology" and "Biological Sciences" → same (keep the shorter version)
- "Electrical and Computer Engineering" and "Computer Engineering" → DIFFERENT (keep both)

Department list:
{dept_list}

Return ONLY a JSON array of the deduplicated department names:
["Computer Science", "Physics", "Mathematics"]"""

    try:
        response = await llm.ainvoke(prompt)
        text = _llm_response_text(response)
        _llm_log.info("===== dept_dedup PROMPT =====\n%s\n===== END PROMPT =====", prompt)
        _llm_log.info("===== dept_dedup RESPONSE =====\n%s\n===== END RESPONSE =====", text)
    except Exception as e:
        log.warning("dept dedup LLM failed: %s", e)
        return unique

    m = re.search(r"\[.*?\]", text, re.DOTALL)
    if m:
        try:
            deduped = json.loads(m.group())
            if isinstance(deduped, list):
                log.info("dept dedup: %d → %d departments", len(unique), len(deduped))
                return deduped
        except json.JSONDecodeError:
            pass

    return unique


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

    if v.regex and val:
        flags = re.IGNORECASE if getattr(v, "case_insensitive", False) else 0
        if not re.match(v.regex, val, flags):
            return ""
    if v.max_length is not None and len(val) > v.max_length:
        return ""
    if v.min_length is not None and val and len(val) < v.min_length:
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
