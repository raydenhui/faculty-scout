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
    """Extract faculty records from all listing pages (with pagination).

    1. LLM analyzes listing page → records + next_page_url
    2. Follow next page links up to MAX_PAGES
    3. Combine all records, visit profile pages for missing fields
    """
    field_names = [c.name for c in schema.extracted_columns()]
    all_records: list[dict[str, Any]] = []
    current_url = url
    current_html = html
    page_num = 0
    visited_pages: list[str] = []

    while current_url and page_num < MAX_PAGES:
        page_num += 1
        log.info("scrape page %d url=%s html_len=%d", page_num, current_url, len(current_html))
        _update_progress(progress_callback, min(5 + page_num * 5, 30),
                         f"Analyzing page {page_num}...")

        analysis = await analyze_listing_page(llm, current_html, schema, current_url,
                                              visited_pages=visited_pages if page_num > 1 else None)
        if analysis is None:
            log.error("scrape listing analysis failed on page %d", page_num)
            break

        if analysis.page_error:
            log.warning("scrape page error: %s", analysis.page_error)
            return [{"_page_error": analysis.page_error}]

        records = analysis_dict_to_records(analysis, field_names)
        all_records.extend(records)
        log.info("scrape page %d: %d records (total: %d)", page_num, len(records), len(all_records))

        # Follow next page
        visited_pages.append(current_url)
        current_url = analysis.next_page_url.strip() if analysis.next_page_url else ""
        if current_url:
            _update_progress(progress_callback, 30, f"Fetching page {page_num + 1}...")
            current_html = await _fetch_one_url(current_url, config)
            if not current_html:
                log.info("scrape next page fetch failed: %s", current_url)
                break

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
