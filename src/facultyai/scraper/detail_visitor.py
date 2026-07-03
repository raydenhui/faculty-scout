"""Detail page visitor — LLM-only extraction for missing fields.

For each record with missing fields (empty string is valid data; None/null
means truly not found), fetches the profile page and asks the LLM to extract
only the missing fields. Tracks failures in a "Remark" column.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from langchain_core.language_models.chat_models import BaseChatModel

from ..config import AppConfig
from ..logging_config import get_llm_logger, get_logger
from ..schema import Schema
from .html_cleaner import clean_html

log = get_logger("scraper.detail")
_llm_log = get_llm_logger()

MAX_CONCURRENT = 3

_JSON_OBJ_RE = re.compile(r"\{[\s\S]*\}", re.MULTILINE)


async def visit_detail_pages(
    listing_html: str,
    listing_url: str,
    records: list[dict[str, str]],
    analysis: dict[str, Any],
    field_names: list[str],
    schema: Schema,
    llm: BaseChatModel,
    config: AppConfig,
) -> list[dict[str, str]]:
    """LLM-only detail page extraction. None for truly unfound fields."""
    follow_selector = analysis.get("follow_link_selector", "")

    # Collect detail URLs: from CSS selector OR from records' profile_url field
    detail_urls: list[tuple[int, str]] = []

    if follow_selector:
        soup = BeautifulSoup(listing_html, "html.parser")
        items = soup.select(analysis.get("item_selector", ""))
        for idx, item in enumerate(items):
            if idx >= len(records):
                break
            link_el = item.select_one(follow_selector)
            if link_el and link_el.get("href"):
                href = link_el["href"].strip()
                full_url = urljoin(listing_url, href)
                detail_urls.append((idx, full_url))
    else:
        # Direct mode: use profile_url from records
        for idx, rec in enumerate(records):
            pu = rec.get("profile_url") or rec.get("Profile URL") or ""
            if pu and pu.startswith("http"):
                detail_urls.append((idx, pu))

    if not detail_urls:
        log.info("visit_detail_pages no detail links found")
        return records

    log.info("visit_detail_pages found %d detail links", len(detail_urls))

    missing_fields = _find_missing_fields(records, field_names)
    if not missing_fields:
        log.info("visit_detail_pages no null fields (empty is valid), skipping")
        return records

    # Also include empty-string fields so LLM can fill them if data is found
    all_empty = _all_empty_fields(records, field_names)
    log.info("visit_detail_pages null fields: %s, all empty: %s", missing_fields, all_empty)
    extract_fields = list(set(missing_fields) | set(all_empty))

    log.info("visit_detail_pages missing fields: %s", missing_fields)

    # Process detail pages (capped at 50 for performance)
    to_process = detail_urls[:50]
    log.info("visit_detail_pages processing %d detail pages", len(to_process))

    sem = asyncio.Semaphore(MAX_CONCURRENT)

    async def _process(idx: int, url: str) -> None:
        async with sem:
            html = await _fetch_one_url(url, config)
            if not html or len(html) < 500:
                _add_remark(records[idx], f"Detail page unreachable: {url}")
                return

            result = await _llm_extract_detail(clean_html(html), extract_fields, schema, url, llm)
            if result is None:
                _add_remark(records[idx], f"LLM extraction failed for: {url}")
                return

            for field_name, value in result.items():
                if value is None:
                    _add_remark(records[idx], f"{field_name}: not found on profile page")
                elif field_name in records[idx] and records[idx].get(field_name) in (None, "") and value:
                    records[idx][field_name] = str(value)

    tasks = [_process(idx, url) for idx, url in to_process]
    await asyncio.gather(*tasks)

    return records


async def _llm_extract_detail(
    html: str,
    missing_fields: list[str],
    schema: Schema,
    url: str,
    llm: BaseChatModel,
) -> dict[str, str | None] | None:
    """LLM extracts missing fields from a detail page. Returns None for unfound."""
    fields_desc = "\n".join(
        f'  "{c.name}": {c.hint or "no hint"}'
        for c in schema.extracted_columns()
        if c.name in missing_fields
    )

    prompt = f"""Extract these fields from a faculty profile page:

{fields_desc}

Profile URL: {url}

Rules:
- If a field exists on the page, return its value as a string.
- If the field genuinely does not exist (e.g., no Chinese name, no email listed),
  return null for that field (not empty string).
- The page may use 1990s-style HTML with tables and font tags.
- Find email in text, mailto: links, or [@] / [at] obfuscation patterns.
- Look for title prefixes (Prof., Dr., Mr., Ms.) near the person's name.

HTML:
{html}

Return ONLY a JSON object with field names as keys. Use null for missing values.
Example: {{"Email": "prof@cuhk.edu.hk", "Chinese Full Name": null}}"""

    try:
        _llm_log.info("===== detail_extract PROMPT =====\n%s\n===== END PROMPT =====", prompt)
        response = await llm.ainvoke(prompt)
        text = _response_text(response)
        _llm_log.info("===== detail_extract RESPONSE =====\n%s\n===== END RESPONSE =====", text)
        log.debug("_llm_extract_detail response:\n%s", text)
    except Exception as e:
        log.warning("_llm_extract_detail LLM call failed for %s: %s", url, e)
        return None

    m = _JSON_OBJ_RE.search(text.strip())
    if not m:
        log.warning("_llm_extract_detail no JSON found for %s", url)
        return None

    try:
        data = json.loads(m.group())
        if isinstance(data, dict):
            return {k: (None if v is None else str(v)) for k, v in data.items()}
    except json.JSONDecodeError:
        log.warning("_llm_extract_detail invalid JSON for %s", url)

    return None


def _find_missing_fields(records: list[dict[str, str]], field_names: list[str]) -> list[str]:
    """Find fields that need detail page visits (None/null)."""
    log.debug("_find_missing_fields records=%d fields=%d", len(records), len(field_names))
    null_counts: dict[str, int] = dict.fromkeys(field_names, 0)
    for r in records:
        for f in field_names:
            if r.get(f) is None:
                null_counts[f] += 1
    missing = [f for f, c in null_counts.items() if c > 0]
    log.debug("_find_missing_fields done missing=%s", missing)
    return missing


def _all_empty_fields(records: list[dict[str, str]], field_names: list[str]) -> list[str]:
    """Find fields that are empty (None or '') across any record."""
    empty: set[str] = set()
    for r in records:
        for f in field_names:
            val = r.get(f)
            if val is None or val == "":
                empty.add(f)
    return list(empty)


def _add_remark(record: dict[str, str], message: str) -> None:
    existing = record.get("Remark", "")
    if existing:
        record["Remark"] = existing + "; " + message
    else:
        record["Remark"] = message


async def _fetch_one_url(url: str, config: AppConfig) -> str | None:
    html = await _playwright_fetch(url, config)
    if html:
        return html
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session, session.get(
            url, timeout=aiohttp.ClientTimeout(total=config.scraping.browser_timeout),
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
                await page.goto(url, timeout=config.scraping.browser_timeout * 1000,
                                wait_until="domcontentloaded")
                await asyncio.sleep(2)
                html = await page.content()
                return html
            finally:
                await browser.close()
    except Exception:
        pass
    return None


def _response_text(response: Any) -> str:
    if hasattr(response, "content"):
        return str(response.content)
    if isinstance(response, str):
        return response
    return str(response)
