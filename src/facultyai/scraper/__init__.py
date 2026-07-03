"""Custom web scraper module for FacultyAI.

Cell 2: LLM analyzes listing page (2a: item split + statics, 2b: extraction methods + follow link)
Cell 3: Detail page extraction (LLM only, None for unfound, remark tracking)
"""

from __future__ import annotations

from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel

from ..config import AppConfig
from ..logging_config import get_logger
from ..schema import Schema
from .detail_visitor import visit_detail_pages
from .field_extractor import extract_fields_from_listing
from .pattern_analyzer import analyze_listing_page

log = get_logger("scraper")


async def scrape(
    url: str,
    html: str,
    schema: Schema,
    llm: BaseChatModel,
    config: AppConfig,
) -> list[dict[str, Any]]:
    """Extract faculty records from a listing page.

    Cell 2a+2b: LLM analyzes listing page → item pattern, static values,
                extraction methods per field, follow link selector.
    Cell 3: Detail page extraction for missing fields (LLM only).
    """
    field_names = [c.name for c in schema.extracted_columns()]
    mode = config.scraping.item_extract_mode
    log.info("scrape start url=%s html_len=%d fields=%d mode=%s",
             url, len(html), len(field_names), mode)

    # Cell 2a + 2b (or direct mode): Analyze listing page
    analysis = await analyze_listing_page(llm, html, schema, url, mode=mode)
    if analysis is None:
        log.error("scrape listing analysis failed")
        return []

    analysis_dict = analysis.to_dict()
    log.info(
        "scrape analysis: item_selector=%s follow_link=%s methods=%d statics=%d",
        analysis.item_selector or "(regex)",
        analysis.follow_link_selector or "(none)",
        len(analysis.extraction_methods),
        len(analysis.static_values),
    )

    # Direct mode: use LLM-extracted records directly, skip split/extract
    if mode == "direct":
        records = analysis_dict.get("_direct_records", [])
        # Normalize field names from LLM output (e.g., profile_url → Profile URL)
        records = _normalize_fields(records, field_names)
        log.info("scrape direct mode: %d records from LLM", len(records))
    else:
        # Split mode: apply static values and extract from listing
        for field_name, value in analysis.static_values.items():
            if field_name in field_names:
                analysis_dict.setdefault("extraction_methods", {})[field_name] = {
                    "method": "static", "pattern": str(value),
                }

        records = await extract_fields_from_listing(html, analysis_dict, llm, field_names)
        log.info("scrape listing extraction: %d records", len(records))

    # Cell 3: Detail page extraction for missing fields
    if analysis.has_detail_pages and records:
        records = await visit_detail_pages(
            html, url, records, analysis_dict, field_names, schema, llm, config
        )
        log.info("scrape after detail pages: %d records", len(records))

    # Strip internal profile_url from records (no longer needed)
    for rec in records:
        rec.pop("profile_url", None)

    log.info("scrape done url=%s records=%d", url, len(records))
    return records


def _normalize_fields(records: list[dict[str, Any]], field_names: list[str]) -> list[dict[str, Any]]:
    """Normalize LLM-returned field names: field_N → real name, fill missing."""
    # Build alias → real name mapping from schema
    alias_map: dict[str, str] = {}
    for i, fn in enumerate(field_names, 1):
        alias_map[f"field_{i}"] = fn

    # Also map common lowercase variants
    for fn in field_names:
        key = fn.lower().replace(" ", "_")
        alias_map[key] = fn

    for rec in records:
        # Detect None/null values from LLM (should trigger detail page visits)
        for key in list(rec.keys()):
            if rec[key] is None:
                # Keep None — signals "needs detail page visit"
                pass
            elif rec[key] == "":
                pass  # Empty = "not applicable"
        # Map field_N → real name
        for key in list(rec.keys()):
            mapped = alias_map.get(key) or alias_map.get(key.lower().replace(" ", "_"))
            if mapped and mapped != key:
                rec[mapped] = rec.pop(key)
        # Ensure all schema fields exist (preserving None vs "")
        existing = set(rec.keys())
        for fn in field_names:
            if fn not in existing:
                rec[fn] = None  # Not provided by LLM = needs detail page
        # Keep profile_url for detail page visits — stripped later
    return records
