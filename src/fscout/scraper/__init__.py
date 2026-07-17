"""Custom web scraper module for scout.

LLM directly extracts all faculty records from the listing page HTML.
Detail pages are visited for any missing fields (null values).
"""

from __future__ import annotations

import asyncio
import re
from collections import deque
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel

from ..config import AppConfig
from ..logging_config import get_logger
from ..schema import Schema
from .detail_visitor import visit_detail_pages
from .pattern_analyzer import analyze_listing_page

log = get_logger("scraper")

MAX_PAGES = 30  # safety limit (can be overridden by config)

# Max concurrent child page fetches
MAX_CHILD_FETCH_CONCURRENT = 3


async def _fetch_child_page(url: str, config: AppConfig) -> str | None:
    """Fetch a child listing page. aiohttp first, Playwright fallback for JS pages."""
    aiohttp_html: str | None = None
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session, session.get(
            url,
            timeout=aiohttp.ClientTimeout(total=min(config.scraping.browser_timeout, 15)),
        ) as resp:
            if resp.status == 200:
                text = await resp.text()
                if text and len(text) > 200:
                    aiohttp_html = text
    except Exception:
        pass

    if aiohttp_html and not _is_js_page(aiohttp_html):
        return aiohttp_html

    if aiohttp_html is not None:
        log.debug("_fetch_child_page JS template detected, trying Playwright: %s", url)
    else:
        log.debug("_fetch_child_page aiohttp failed, trying Playwright: %s", url)

    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
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
                if http_status in (404, 410, 500, 502, 503):
                    return None
                await page.wait_for_load_state("networkidle", timeout=10_000)
                await asyncio.sleep(2)
                html = await page.content()
                if html and len(html) > 200:
                    return html
            finally:
                await browser.close()
    except Exception:
        pass
    return None


def _is_js_page(html: str) -> bool:
    """Detect if HTML is a JS-rendered page that lacks structured content."""
    snippet = html[:50_000]
    indicators = [
        "{{", "v-bind", "v-if", "v-for", "v-model",
        "ng-app", "ng-controller", "ng-repeat",
        "__vue__", "__vue_app__", "_reactRootContainer",
        'data-reactroot', 'data-reactid',
        '<div id="root">', '<div id="app">',
    ]
    for ind in indicators:
        if ind in snippet:
            return True

    scripts = re.findall(r"<script[^>]*>.*?</script>", snippet, re.DOTALL | re.IGNORECASE)
    api_patterns = ["/api/", "fetch(", "ajax(", "xmlhttp", "staff_data", "faculty_data",
                    "person_data", "load_people", "get_people", "member_data"]
    for s in scripts:
        for pat in api_patterns:
            if pat.lower() in s.lower():
                return True

    text = re.sub(r"<[^>]+>", " ", snippet)
    has_faculty_words = any(t in text.lower() for t in [
        "professor", "associate", "lecturer", "faculty",
        "academic staff", "teaching staff", "staff directory",
    ])
    has_person_data = bool(re.search(
        r'<table[^>]*>.*?<t[rd][^>]*>\s*(?:Prof\.|Dr\.|Professor)\s+[A-Z].*?</table>',
        snippet, re.DOTALL | re.IGNORECASE,
    )) or bool(re.search(
        r'<li[^>]*>\s*(?:Prof\.|Dr\.|Professor)\s*[A-Z]',
        snippet, re.IGNORECASE,
    ))
    email_count = len(set(re.findall(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", snippet)))

    if len(snippet) < 5000 and has_faculty_words:
        return True

    return bool(
        has_faculty_words
        and not has_person_data
        and email_count < 3
        and len(snippet) > 15000
    )


async def scrape(
    url: str,
    html: str,
    schema: Schema,
    llm: BaseChatModel,
    config: AppConfig,
    progress_callback: Any = None,
) -> list[dict[str, Any]]:
    """Extract faculty records from all listing pages using BFS+DFS traversal.

    1. LLM analyzes listing page → records + next_page_url + child_page_urls
    2. BFS for child_page_urls (sibling categories), DFS for next_page_url (pagination)
    3. Combine all records, visit profile pages for missing fields
    """
    field_names = [c.name for c in schema.extracted_columns()]
    all_records: list[dict[str, Any]] = []
    visited_urls: set[str] = set()
    pending_urls: set[str] = set()
    page_num = 0

    # deque: pop from left. next_page_url → appendleft (left, DFS within chain).
    # child_page_urls → append (right, BFS across siblings).
    # Each entry is (url, html, ancestor_chain)
    queue: deque[tuple[str, str, list[str]]] = deque()
    queue.append((url, html, []))
    pending_urls.add(url)

    while queue and page_num < MAX_PAGES:
        current_url, current_html, ancestor_chain = queue.popleft()
        pending_urls.discard(current_url)

        if current_url in visited_urls:
            continue

        page_num += 1
        visited_urls.add(current_url)

        log.info("scrape page %d url=%s html_len=%d queue=%d ancestors=%d",
                 page_num, current_url, len(current_html), len(queue), len(ancestor_chain))
        _update_progress(progress_callback, min(5 + page_num * 5, 30),
                         f"Analyzing page {page_num}...")

        # Build the set of all known URLs (visited + pending) to tell the LLM what to avoid
        all_known = sorted(visited_urls | pending_urls)

        analysis = await analyze_listing_page(
            llm, current_html, schema, current_url,
            ancestor_urls=ancestor_chain,
            known_urls=all_known,
        )
        if analysis is None:
            log.error("scrape listing analysis failed on page %d", page_num)
            continue

        if analysis.page_error:
            log.warning("scrape page %d error: %s", page_num, analysis.page_error)
            if page_num == 1:
                return [{"_page_error": analysis.page_error}]
            continue

        records = analysis_dict_to_records(analysis, field_names)
        all_records.extend(records)
        log.info("scrape page %d: %d records (total: %d)", page_num, len(records), len(all_records))

        # ---- next_page_url: DFS — push to LEFT, processed immediately after current ----
        next_url = (analysis.next_page_url or "").strip()
        if next_url and next_url.startswith("http") and next_url not in visited_urls and next_url not in pending_urls:
            next_html = await _fetch_child_page(next_url, config)
            if next_html:
                next_chain = ancestor_chain + [current_url]
                queue.appendleft((next_url, next_html, next_chain))
                pending_urls.add(next_url)
                log.info("scrape queued next page: %s", next_url)
            else:
                log.info("scrape next page fetch failed, skipping: %s", next_url)

        # ---- child_page_urls: BFS — push to RIGHT, processed after all current-level siblings ----
        child_urls: list[str] = []
        for cu in analysis.child_page_urls:
            cu = cu.strip()
            if cu and cu.startswith("http") and cu not in visited_urls and cu not in pending_urls:
                child_urls.append(cu)

        if child_urls:
            log.info("scrape fetching %d child pages concurrently...", len(child_urls))
            sem = asyncio.Semaphore(MAX_CHILD_FETCH_CONCURRENT)

            async def _fetch_child(cu: str) -> tuple[str, str | None]:
                async with sem:
                    log.debug("scrape child fetch start: %s", cu)
                    html = await _fetch_child_page(cu, config)
                    log.debug("scrape child fetch done: %s (len=%d)", cu, len(html) if html else 0)
                    return cu, html

            results = await asyncio.gather(*[_fetch_child(cu) for cu in child_urls])

            for child_url, child_html in results:
                if child_html:
                    child_chain = ancestor_chain + [current_url]
                    queue.append((child_url, child_html, child_chain))
                    pending_urls.add(child_url)
                    log.info("scrape queued child page: %s", child_url)
                else:
                    log.info("scrape child page fetch failed, skipping: %s", child_url)

    if not all_records:
        return []

    # 50% — listing extraction done
    _update_progress(progress_callback, 50,
                     f"Extracted {len(all_records)} entries, visiting profile pages...")

    # Detail page extraction for missing fields
    analysis_dict = {"has_detail_pages": True, "static_values": {}}
    all_records = await visit_detail_pages(
        html, url, all_records, analysis_dict, field_names, schema, llm, config,
        progress_callback=progress_callback,
    )
    log.info("scrape after detail pages: %d records", len(all_records))
    _update_progress(progress_callback, 100, f"Done — {len(all_records)} entries")

    for rec in all_records:
        rec.pop("profile_url", None)

    log.info("scrape done url=%s records=%d pages=%d", url, len(all_records), page_num)
    return all_records


def analysis_dict_to_records(analysis: Any, field_names: list[str]) -> list[dict[str, Any]]:
    """Extract and normalize records from a ListingAnalysis."""
    d = analysis.to_dict()
    records = d.get("_direct_records", [])
    return _normalize_fields(records, field_names)


def _update_progress(callback: Any, pct: int, msg: str) -> None:
    if callback:
        callback(pct, msg)


def _normalize_fields(records: list[dict[str, Any]], field_names: list[str]) -> list[dict[str, Any]]:
    """Normalize LLM-returned field names: field_N → real name, fill missing."""
    alias_map: dict[str, str] = {}
    for i, fn in enumerate(field_names, 1):
        alias_map[f"field_{i}"] = fn
    for fn in field_names:
        key = fn.lower().replace(" ", "_")
        alias_map[key] = fn

    for rec in records:
        for key in list(rec.keys()):
            mapped = alias_map.get(key) or alias_map.get(key.lower().replace(" ", "_"))
            if mapped and mapped != key:
                rec[mapped] = rec.pop(key)
        existing = set(rec.keys())
        for fn in field_names:
            if fn not in existing:
                rec[fn] = None
    return records
