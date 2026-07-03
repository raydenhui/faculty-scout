"""Field extraction engine.

Applies extraction methods (regex, CSS selector, LLM) determined by the
pattern analyzer to extract structured data from HTML.
"""

from __future__ import annotations

import re
from typing import Any

from bs4 import BeautifulSoup, Tag
from langchain_core.language_models.chat_models import BaseChatModel

from ..logging_config import get_logger

log = get_logger("scraper.extractor")


async def extract_fields_from_listing(
    html: str,
    analysis: dict[str, Any],
    llm: BaseChatModel,
    field_names: list[str],
) -> list[dict[str, str]]:
    """Extract fields from a listing page using the analysis plan."""
    soup = BeautifulSoup(html, "html.parser")
    extraction_methods: dict[str, dict[str, Any]] = analysis.get("extraction_methods", {})

    # Split HTML into individual items
    items = _split_items(soup, html, analysis)
    if not items:
        log.warning("extract_fields no items found")
        return []

    log.info("extract_fields found %d items", len(items))
    records: list[dict[str, str]] = []

    for idx, item in enumerate(items):
        record: dict[str, str] = {}
        for field_name in field_names:
            method_info = extraction_methods.get(field_name)
            if not method_info:
                record[field_name] = ""
                continue

            value = _extract_one_field(item, field_name, method_info, llm, idx)
            record[field_name] = value if value else ""
        records.append(record)

    return records


async def extract_fields_from_detail(
    html: str,
    methods: dict[str, dict[str, Any]],
    llm: BaseChatModel,
) -> dict[str, str]:
    """Extract specific fields from a detail page using the given methods."""
    soup = BeautifulSoup(html, "html.parser")
    result: dict[str, str] = {}

    for field_name, method_info in methods.items():
        value = _extract_one_field(soup, field_name, method_info, llm, 0)
        result[field_name] = value if value else ""

    return result


def _split_items(soup: BeautifulSoup, html: str, analysis: dict[str, Any]) -> list[Any]:
    """Split the HTML into individual faculty entries."""
    item_selector = analysis.get("item_selector", "")
    item_regex = analysis.get("item_regex", "")

    items: list[Any] = []

    if item_selector:
        log.debug("_split_items using selector: %s", item_selector)
        try:
            elements = soup.select(item_selector)
            if elements:
                items = list(elements)
                log.debug("_split_items selector found %d elements", len(items))
        except Exception as e:
            log.debug("_split_items selector failed: %s", e)

    if not items and item_regex:
        log.debug("_split_items using regex: %s", item_regex)
        try:
            pattern = re.compile(item_regex, re.DOTALL)
            matches = pattern.findall(html)
            # Wrap each match in a simple tag for extraction
            for m in matches:
                if isinstance(m, str):
                    items.append(m)
                else:
                    items.append(m)
            log.debug("_split_items regex found %d matches", len(items))
        except Exception as e:
            log.debug("_split_items regex failed: %s", e)

    # Fallback: try to find repeating table rows or list items
    if not items:
        for tag_name in ("tr", "li", "article", "div.profile"),:
            if isinstance(tag_name, tuple):
                for t in tag_name:
                    elements = soup.find_all(t)
                    if len(elements) > 3:
                        items = list(elements)
                        break
            else:
                elements = soup.find_all(tag_name)
                if len(elements) > 3:
                    items = list(elements)
                    break
            if items:
                break

        log.debug("_split_items fallback found %d items", len(items))

    return items


def _extract_one_field(
    item: Any,
    field_name: str,
    method_info: dict[str, Any],
    llm: BaseChatModel,
    item_index: int,
) -> str:
    """Extract a single field value from an item using the specified method."""
    method = method_info.get("method", "llm")
    pattern = method_info.get("pattern", "")

    if method == "static":
        return str(pattern) if pattern else ""

    if method == "regex" and pattern:
        return _extract_regex(item, pattern)

    if method == "selector" and pattern:
        return _extract_selector(item, pattern)

    return ""  # LLM extraction is handled separately for groups


def _extract_regex(item: Any, pattern: str) -> str:
    """Extract a value using regex."""
    text = _item_text(item)
    try:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            value = m.group(0) if m.lastindex is None else m.group(1)
            value = re.sub(r"^mailto:", "", value, flags=re.IGNORECASE).strip()
            if not _looks_like_url(value):
                return value
    except re.error as e:
        log.debug("_extract_regex pattern error: %s pattern=%s", e, pattern)
    return ""


def _extract_selector(item: Any, pattern: str) -> str:
    """Extract a value using CSS selector on the item element."""
    if isinstance(item, Tag):
        try:
            el = item.select_one(pattern)
            if el:
                return _best_selector_value(el)
        except Exception:
            pass
    return ""


def _best_selector_value(el: Tag) -> str:
    """Pick the best text value from a BeautifulSoup element."""
    href = el.get("href", "")

    # mailto: links — extract email from href
    if "mailto:" in href.lower():
        email = re.sub(r"^mailto:", "", href, flags=re.IGNORECASE).strip()
        if email:
            return email

    # Always prefer visible text content
    text = el.get_text(" ", strip=True)
    log.debug("_best_selector_value tag=%s text=%s href=%s",
              el.name,
              repr(text[:80]) if text else "(empty)",
              href[:80] if href else "(empty)")

    if text and not _looks_like_url(text):
        return text

    # For links with no visible text: try title attribute, then deeper children
    if not text and href:
        title = el.get("title", "").strip()
        if title and not _looks_like_url(title):
            return title
        img = el.find("img")
        if img and img.get("alt"):
            alt = img.get("alt", "").strip()
            if alt and not _looks_like_url(alt):
                return alt
        deep_text = " ".join(
            c.get_text(strip=True) for c in el.descendants
            if hasattr(c, "get_text") and getattr(c, "name", None) is None
            if c.get_text(strip=True)
        ).strip()
        if deep_text and not _looks_like_url(deep_text):
            return deep_text

    if text and _looks_like_url(text):
        title = el.get("title", "").strip()
        if title and not _looks_like_url(title):
            return title
        children_text = " ".join(
            c.get_text(strip=True) for c in el.children
            if hasattr(c, "get_text")
        ).strip()
        if children_text and not _looks_like_url(children_text):
            return children_text

    return ""


def _looks_like_url(val: str) -> bool:
    return bool(re.match(r"^(https?://|/|#|javascript:)", val.strip()))


def _item_text(item: Any) -> str:
    """Get the text representation of an item."""
    if isinstance(item, Tag):
        return item.get_text(" ", strip=True)
    if isinstance(item, str):
        return item
    return str(item)
