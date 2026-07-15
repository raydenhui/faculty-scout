"""Custom web scraper module for FacultyAI.

LLM directly extracts all faculty records from the listing page HTML.
Detail pages are visited for any missing fields (null values).
"""

from __future__ import annotations

from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel

from ..config import AppConfig
from ..logging_config import get_logger
from ..schema import Schema
from .detail_visitor import _fetch_one_url, visit_detail_pages
from .pattern_analyzer import analyze_listing_page

log = get_logger("scraper")

MAX_PAGES = 30  # safety limit (can be overridden by config)


async def scrape(
    url: str,
    html: str,
    schema: Schema,
    llm: BaseChatModel,
    config: AppConfig,
    progress_callback: Any = None,
) -> list[dict[str, Any]]:
    """Extract faculty records from all listing pages using DFS traversal.

    1. LLM analyzes listing page → records + child_page_urls
    2. DFS stack-based traversal of child pages up to MAX_PAGES
    3. Combine all records, visit profile pages for missing fields
    """
    field_names = [c.name for c in schema.extracted_columns()]
    all_records: list[dict[str, Any]] = []
    visited_urls: set[str] = set()
    visited_list: list[str] = []
    page_num = 0

    # DFS stack: each entry is (url, html) to process
    stack: list[tuple[str, str]] = [(url, html)]

    while stack and page_num < MAX_PAGES:
        current_url, current_html = stack.pop()
        if current_url in visited_urls:
            continue

        page_num += 1
        visited_urls.add(current_url)
        visited_list.append(current_url)

        log.info("scrape page %d url=%s html_len=%d stack=%d",
                 page_num, current_url, len(current_html), len(stack))
        _update_progress(progress_callback, min(5 + page_num * 5, 30),
                         f"Analyzing page {page_num}...")

        analysis = await analyze_listing_page(llm, current_html, schema, current_url,
                                              visited_pages=visited_list if page_num > 1 else None)
        if analysis is None:
            log.error("scrape listing analysis failed on page %d", page_num)
            continue

        if analysis.page_error:
            log.warning("scrape page error: %s", analysis.page_error)
            return [{"_page_error": analysis.page_error}]

        records = analysis_dict_to_records(analysis, field_names)
        all_records.extend(records)
        log.info("scrape page %d: %d records (total: %d)", page_num, len(records), len(all_records))

        # Collect child pages for DFS traversal
        child_urls: list[str] = list(analysis.child_page_urls)
        raw_next = (analysis.next_page_url or "").strip()
        if raw_next and raw_next.startswith("http") and raw_next not in visited_urls:
            child_urls.append(raw_next)

        # Deduplicate and filter already-visited URLs
        new_children: list[str] = []
        for child_url in child_urls:
            child_url = child_url.strip()
            if child_url and child_url.startswith("http") and child_url not in visited_urls:
                new_children.append(child_url)

        # DFS: push children in reverse order so the first child is processed next
        for child_url in reversed(new_children):
            child_html = await _fetch_one_url(child_url, config)
            if child_html:
                stack.append((child_url, child_html))
            else:
                log.info("scrape child page fetch failed: %s", child_url)

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
